import asyncio
import contextlib
import signal
import sys

import uvicorn

from .cloudflare_dns import CloudflareClient, DNSManager
from .config import Config
from .hosts_config import HostsConfig
from .i18n import get_translator
from .monitoring_service import MonitoringService
from .panel import HostManager, NodeMonitor, RemnawaveClient
from .state import StateStore
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

        # Wake immediately on shutdown instead of sleeping out the full interval.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=interval)


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

    def handle_sighup():
        try:
            config.reload()
            config.validate()
            if host_manager:
                host_manager.reload()
            logger.info(
                "Reloaded config.yml and hosts.yml from disk "
                "(environment variables are read at startup and need a restart)"
            )
        except Exception as e:
            logger.error(f"Config reload failed, keeping current config: {e}")

    loop.add_signal_handler(signal.SIGHUP, handle_sighup)

    get_translator(config.language)

    logger.info("Starting Remnawave-Cloudflare DNS Monitor")
    logger.info(f"Check interval: {config.check_interval}s")

    state = StateStore(config.state_file)
    state.load()

    remnawave_client = RemnawaveClient(
        api_url=config.remnawave_url, api_key=config.remnawave_api_key
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

    monitoring_service = MonitoringService(
        config=config,
        node_monitor=node_monitor,
        cloudflare_client=cloudflare_client,
        dns_manager=dns_manager,
        host_manager=host_manager,
        notifier=notifier,
        state=state,
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

        if config.api_enabled:
            from .api import create_app

            api_app = create_app(config, notifier, monitoring_service)
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
