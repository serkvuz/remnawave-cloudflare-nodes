import asyncio
import contextlib
import signal
import sys

import uvicorn

from .cloudflare_dns import CloudflareClient, DNSManager
from .config import Config
from .discovery import NodeDiscovery
from .hosts_config import HostsConfig
from .i18n import get_translator
from .monitoring_service import MonitoringService
from .panel import HostManager, NodeMonitor, RemnawaveClient
from .probe import TcpProber
from .state import StateStore
from .summary import SummaryTracker
from .telegram import ServiceStarted, TelegramNotifier
from .utils import setup_logger, short_error


async def run_api_server(app, host: str, port: int) -> None:
    server_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(server_config)
    server.install_signal_handlers = lambda: None  # our signal handlers manage shutdown
    await server.serve()


async def run_monitoring_loop(
        service: MonitoringService, config: Config, logger, shutdown: asyncio.Event
) -> None:
    logger.info(f"Starting monitoring loop with {config.check_interval}s interval")

    while not shutdown.is_set():
        try:
            await service.perform_health_check()
            interval = config.check_interval
            logger.info(f"Waiting {interval} seconds until next check...")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            interval = config.check_interval
            logger.info(f"Retrying in {interval} seconds after error: {short_error(e)}")

        # Wake immediately on shutdown, or when a command asks for a check now
        # (/resume, /reload, maintenance changes), instead of sleeping it out.
        wake = service.wake_event
        waiters = [asyncio.create_task(shutdown.wait()), asyncio.create_task(wake.wait())]
        try:
            await asyncio.wait(waiters, timeout=interval, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiters:
                task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        wake.clear()


def install_signal_handlers(loop, shutdown: asyncio.Event, logger) -> None:
    """Route termination signals through the event loop.

    Raising SystemExit from a C-level signal handler surfaces the exception in
    the event loop rather than inside the running coroutine, so `except` blocks
    around the monitoring loop never fired and shutdown logging was dead code.
    Setting an asyncio.Event keeps shutdown inside the coroutine where it can be
    observed and awaited.
    """

    def request_shutdown(signame: str) -> None:
        if not shutdown.is_set():
            logger.info(f"Received {signame}, shutting down...")
            shutdown.set()

    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame)
        try:
            loop.add_signal_handler(sig, request_shutdown, signame)
        except NotImplementedError:  # pragma: no cover - non-POSIX platforms
            signal.signal(sig, lambda *_: shutdown.set())


async def main():
    config = Config()
    config.validate()

    logger = setup_logger(
        name="remnawave-cloudflare-monitor",
        level=config.log_level,
        log_file="logs/app.log",
    )

    for warning in config.validate_zones():
        logger.warning(warning)

    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    install_signal_handlers(loop, shutdown, logger)

    host_manager = None
    monitoring_service = None

    async def reload_config():
        """Reload config.yml and hosts.yml. Returns None on success, else the error."""
        lock = monitoring_service.lock if monitoring_service else contextlib.nullcontext()
        async with lock:  # never swap the config under a running cycle
            try:
                config.reload()  # rolls back by itself if the new file is invalid
                config.validate()
                for warning in config.validate_zones():
                    logger.warning(warning)
                if host_manager:
                    host_manager.reload()
            except Exception as e:
                logger.error(f"Config reload failed, keeping current config: {e}")
                return str(e)
        logger.info(
            "Reloaded config.yml and hosts.yml from disk "
            "(environment variables are read at startup and need a restart)"
        )
        return None

    reload_tasks = set()

    def handle_sighup():
        task = asyncio.create_task(reload_config())
        reload_tasks.add(task)
        task.add_done_callback(reload_tasks.discard)

    loop.add_signal_handler(signal.SIGHUP, handle_sighup)

    get_translator(config.language)

    logger.info("Starting Remnawave-Cloudflare DNS Monitor")
    logger.info(f"Check interval: {config.check_interval}s")

    state = StateStore(config.state_file)
    state.load()

    remnawave_client = RemnawaveClient(
        api_url=config.remnawave_url,
        api_key=config.remnawave_api_key,
        cf_access_client_id=config.cf_access_client_id,
        cf_access_client_secret=config.cf_access_client_secret,
    )

    node_monitor = NodeMonitor(remnawave_client)

    notifier = TelegramNotifier(
        bot_token=config.telegram_bot_token,
        chat_id=config.telegram_chat_id,
        topic_id=config.telegram_topic_id,
        enabled=config.telegram_enabled,
        notify_api_changes=config.telegram_notify_api_changes,
    )

    host_manager = HostManager(
        client=remnawave_client,
        notifier=notifier,
        enabled=config.disable_unreachable_hosts,
        notify_changes=config.telegram_notify_host_changes,
        hosts_config=HostsConfig(),
        state=state,
    )

    cloudflare_client = CloudflareClient(api_token=config.cloudflare_token)
    dns_manager = DNSManager(
        client=cloudflare_client,
        notifier=notifier,
        notify_dns_changes=config.telegram_notify_dns_changes,
        notify_errors=config.telegram_notify_errors,
    )

    summary = SummaryTracker(config=config, state=state, notifier=notifier)
    dns_manager.on_change = summary.record_dns_change

    monitoring_service = MonitoringService(
        config=config,
        node_monitor=node_monitor,
        cloudflare_client=cloudflare_client,
        dns_manager=dns_manager,
        host_manager=host_manager,
        notifier=notifier,
        state=state,
        discovery=NodeDiscovery(config=config, client=remnawave_client, notifier=notifier),
        prober=TcpProber(),
        summary=summary,
    )

    command_bot = None
    if config.telegram_commands_enabled:
        if notifier.bot is None:
            logger.warning("TELEGRAM_COMMANDS_ENABLED is set but Telegram is disabled or not configured")
        else:
            from .telegram.commands import CommandBot

            command_bot = CommandBot(
                bot=notifier.bot,
                formatter=notifier.formatter,
                config=config,
                service=monitoring_service,
                summary=summary,
                reload=reload_config,
            )

    api_task = None
    exit_code = 0

    try:
        await notifier.start()
        notifier.notify_service_started(
            ServiceStarted(
                domains=config.domains,
                api_enabled=config.api_enabled,
                api_host=config.api_host,
                api_port=config.api_port,
            )
        )

        await monitoring_service.initialize_and_print_zones()

        if command_bot:
            await command_bot.start()

        if config.api_enabled:
            from .api import create_app

            api_app = create_app(config, notifier, monitoring_service, summary)
            api_task = asyncio.create_task(
                run_api_server(api_app, config.api_host, config.api_port)
            )
            logger.info(f"API server listening on {config.api_host}:{config.api_port}")

        await run_monitoring_loop(
            service=monitoring_service, config=config, logger=logger, shutdown=shutdown
        )
    except asyncio.CancelledError:
        logger.info("Shutting down gracefully")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        exit_code = 1
    finally:
        if command_bot:
            await command_bot.stop()
        if api_task:
            api_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await api_task
        state.save(force=True)
        notifier.notify_service_stopped()
        await notifier.stop()
        await remnawave_client.close()

    logger.info("Remnawave-Cloudflare DNS Monitor stopped")
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
