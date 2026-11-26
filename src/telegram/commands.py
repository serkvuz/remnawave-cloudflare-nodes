import asyncio
import contextlib
import html
from typing import TYPE_CHECKING, Awaitable, Callable, List, Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BotCommandScopeChat, Message

from .formatter import MessageFormatter
from .notifier import split_message
from ..utils import format_epoch, get_logger, parse_duration, short_error

if TYPE_CHECKING:
    from ..config import Config
    from ..monitoring_service import MonitoringService
    from ..summary import SummaryTracker

# Returns None on success, or the error message.
ReloadCallback = Callable[[], Awaitable[Optional[str]]]

_COMMANDS = [
    ("status", "Nodes and current DNS records"),
    ("reload", "Reload config.yml and hosts.yml"),
    ("pause", "Stop DNS changes: /pause [30m|2h|1d]"),
    ("resume", "Resume DNS changes"),
    ("maintenance", "List, or /maintenance on|off <node>"),
    ("summary", "Uptime statistics so far"),
    ("help", "List commands"),
]


class CommandBot:
    """Telegram commands for the configured chat.

    Only messages from TELEGRAM_CHAT_ID are handled; everything else is ignored
    without a reply, so the bot does not reveal itself to strangers. In a group,
    TELEGRAM_COMMAND_USERS can narrow this further to specific user IDs.

    Uses long polling on the notifier's Bot. Pending updates are dropped on
    start, so a command sent while the service was down (say, /pause) is not
    executed later by surprise.
    """

    def __init__(
        self,
        bot: Bot,
        formatter: MessageFormatter,
        config: "Config",
        service: "MonitoringService",
        summary: Optional["SummaryTracker"],
        reload: ReloadCallback,
    ):
        self.bot = bot
        self.fmt = formatter
        self.config = config
        self.service = service
        self.summary = summary
        self.reload = reload
        self.logger = get_logger(__name__)
        self.chat_id = str(config.telegram_chat_id).strip()
        self.allowed_users = set(config.telegram_command_users)
        self._dp = Dispatcher()
        self._dp.include_router(self._build_router())
        self._task: Optional[asyncio.Task] = None
        self._ignored_chats: set = set()

    # --- lifecycle ---

    async def start(self) -> None:
        try:
            # Polling and a webhook cannot coexist on one bot token.
            await self.bot.delete_webhook(drop_pending_updates=True)
            commands = [BotCommand(command=c, description=d) for c, d in _COMMANDS]
            scope_chat = int(self.chat_id) if self.chat_id.lstrip("-").isdigit() else self.chat_id
            await self.bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=scope_chat))
        except Exception as e:
            self.logger.warning(f"Could not register bot commands with Telegram: {short_error(e)}")

        self._task = asyncio.create_task(self._poll())
        self.logger.info("Telegram commands enabled")

    async def _poll(self) -> None:
        try:
            await self._dp.start_polling(
                self.bot,
                handle_signals=False,       # our own handlers own SIGTERM/SIGINT
                close_bot_session=False,    # the notifier closes the shared session
                allowed_updates=["message"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.error(f"Telegram command polling stopped: {short_error(e)}")

    async def stop(self) -> None:
        if not self._task:
            return
        with contextlib.suppress(Exception):
            await self._dp.stop_polling()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        except Exception:
            pass
        self._task = None

    # --- authorisation ---

    def _authorized(self, message: Message) -> bool:
        chat = message.chat
        if self.chat_id.startswith("@"):
            chat_ok = bool(chat.username) and f"@{chat.username}".lower() == self.chat_id.lower()
        else:
            chat_ok = str(chat.id) == self.chat_id
        user_ok = not self.allowed_users or bool(message.from_user and message.from_user.id in self.allowed_users)
        if chat_ok and user_ok:
            return True
        key = (chat.id, message.from_user.id if message.from_user else None)
        if key not in self._ignored_chats:
            self._ignored_chats.add(key)
            self.logger.warning(f"Ignoring bot command from unauthorised chat {chat.id} / user {key[1]}")
        return False

    @staticmethod
    def _actor(message: Message) -> str:
        user = message.from_user
        if not user:
            return "Telegram"
        return f"@{user.username}" if user.username else (user.full_name or str(user.id))

    async def _reply(self, message: Message, text: str) -> None:
        for chunk in split_message(text):
            await message.answer(chunk)

    # --- handlers ---

    def _build_router(self) -> Router:
        router = Router()
        router.message.filter(self._authorized)

        router.message.register(self._help, Command("help", "start"))
        router.message.register(self._status, Command("status"))
        router.message.register(self._reload, Command("reload"))
        router.message.register(self._pause, Command("pause"))
        router.message.register(self._resume, Command("resume"))
        router.message.register(self._maintenance, Command("maintenance"))
        router.message.register(self._summary, Command("summary"))
        return router

    async def _help(self, message: Message) -> None:
        await self._reply(message, self.fmt.text("cmd-help"))

    async def _status(self, message: Message) -> None:
        snapshot = self.service.status_snapshot()
        try:
            dns = await self.service.dns_overview()
        except Exception as e:
            self.logger.error(f"/status could not read DNS records: {short_error(e)}")
            dns = None
        last = snapshot["last_cycle_at"]
        last_text = format_epoch(last, self.config) if last else None
        await self._reply(message, self.fmt.format_status(snapshot, dns, last_text))

    async def _reload(self, message: Message) -> None:
        error = await self.reload()
        if error:
            await self._reply(message, self.fmt.text("cmd-reload-failed", error=html.escape(error)))
            return
        self.service.request_check()
        await self._reply(message, self.fmt.text("cmd-reload-ok"))

    async def _pause(self, message: Message, command: CommandObject) -> None:
        duration = None
        if command.args:
            try:
                duration = parse_duration(command.args)
            except ValueError as e:
                await self._reply(message, self.fmt.text("cmd-error", error=html.escape(str(e))))
                return
        event = self.service.pause(duration, by=self._actor(message), notify=False)
        await self._reply(message, self.fmt.format_dns_paused(event))

    async def _resume(self, message: Message) -> None:
        event = self.service.resume(by=self._actor(message), notify=False)
        if event is None:
            await self._reply(message, self.fmt.text("cmd-resume-not-paused"))
            return
        await self._reply(message, self.fmt.format_dns_resumed(event))

    async def _maintenance(self, message: Message, command: CommandObject) -> None:
        args = (command.args or "").split(maxsplit=1)
        if not args:
            await self._reply(message, self._maintenance_list())
            return
        action = args[0].lower()
        if action not in ("on", "add", "off", "remove") or len(args) < 2:
            await self._reply(message, self.fmt.text("cmd-maintenance-usage"))
            return

        target = args[1].strip()
        node = self.service.find_node(target)
        if node is None:
            if not self.service.last_nodes:
                await self._reply(message, self.fmt.text("cmd-maintenance-no-data"))
                return
            if action in ("off", "remove") and target.lower() in {m.lower() for m in self.config.maintenance}:
                # Not a panel node any more, but the entry exists: allow removing it.
                identifiers = [target]
                label = target
            else:
                names = ", ".join(sorted(html.escape(n.name) for n in self.service.last_nodes))
                await self._reply(message, self.fmt.text("cmd-maintenance-unknown", node=html.escape(target), nodes=names))
                return
        else:
            identifiers = [target, node.name, node.address, str(node.uuid)]
            label = node.name

        name = html.escape(label)
        try:
            if action in ("on", "add"):
                changed = self.config.add_maintenance(node.name if node else target)
                reply = self.fmt.text("cmd-maintenance-on" if changed else "cmd-maintenance-already", name=name)
            else:
                changed = self.config.remove_maintenance(identifiers)
                reply = self.fmt.text("cmd-maintenance-off" if changed else "cmd-maintenance-not-in", name=name)
        except Exception as e:
            self.logger.error(f"/maintenance could not update config.yml: {short_error(e)}")
            await self._reply(message, self.fmt.text("cmd-error", error=html.escape(short_error(e))))
            return

        self.logger.info(f"/maintenance {action} {label} by {self._actor(message)}")
        if changed:
            self.service.request_check()
        await self._reply(message, reply)

    def _maintenance_list(self) -> str:
        entries = self.service.maintenance_entries()
        if not entries:
            return self.fmt.text("cmd-maintenance-empty")
        lines: List[str] = []
        for item in entries:
            if item["name"]:
                lines.append(self.fmt.text(
                    "cmd-maintenance-entry", name=html.escape(item["name"]), address=html.escape(item["address"])
                ))
            else:
                lines.append(self.fmt.text("cmd-maintenance-entry-unmatched", entry=html.escape(item["entry"])))
        return self.fmt.text("cmd-maintenance-list", entries="\n".join(lines))

    async def _summary(self, message: Message) -> None:
        if not self.summary:
            await self._reply(message, self.fmt.text("summary-no-nodes"))
            return
        await self._reply(message, self.fmt.format_summary(self.summary.build_report(scheduled=False)))
