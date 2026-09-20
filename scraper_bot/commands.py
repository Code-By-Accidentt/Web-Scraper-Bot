"""Telegram command parsing, dispatch and the long-polling listener."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from scraper_bot.models import SiteState
from scraper_bot.monitor_manager import (
    MonitorError,
    MonitorManager,
    SiteAlreadyPaused,
    SiteAlreadyTracked,
    SiteNotPaused,
    SiteNotTracked,
)
from scraper_bot.telegram_client import TelegramClient
from scraper_bot.urls import InvalidUrlError

log = logging.getLogger(__name__)

Handler = Callable[[int, str], Awaitable[str]]

HELP_TEXT = (
    "Available commands:\n"
    "/add <url> — start monitoring a site\n"
    "/remove <url> — stop monitoring a site\n"
    "/pause <url> — pause monitoring\n"
    "/resume <url> — resume monitoring\n"
    "/list — monitored sites with their status\n"
    "/nonotifi <url> — mute change alerts for a site\n"
    "/notify <url> — un-mute change alerts for a site\n"
    "/help — this message"
)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    argument: str


def parse_command(text: str) -> ParsedCommand | None:
    """``"/add@MyBot https://x.io"`` -> ``ParsedCommand("add", "https://x.io")``."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped[1:].partition(" ")
    name = head.split("@", 1)[0].lower()
    if not name:
        return None
    return ParsedCommand(name, rest.strip())


class BotCommands:
    """Maps commands to :class:`MonitorManager` calls and phrases the replies."""

    def __init__(
        self,
        manager: MonitorManager,
        telegram: TelegramClient,
        *,
        allowed_chat_ids: Sequence[int] = (),
        alert_chat_ids: Sequence[int] = (),
    ) -> None:
        self._manager = manager
        self._telegram = telegram
        self._allowed = frozenset(allowed_chat_ids)
        self._alerts_configured = bool(alert_chat_ids)
        self._handlers: dict[str, Handler] = {
            "start": self._start,
            "help": self._help,
            "add": self._add,
            "remove": self._remove,
            "pause": self._pause,
            "resume": self._resume,
            "list": self._list,
            "notify": self._notify,
            "unmute": self._notify,
            "nonotifi": self._nonotifi,
            "mute": self._nonotifi,
        }

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text")
        if not isinstance(chat_id, int) or not isinstance(text, str):
            return
        command = parse_command(text)
        if command is None:
            return
        if self._allowed and chat_id not in self._allowed:
            log.warning("ignoring /%s from unauthorised chat %s", command.name, chat_id)
            return
        reply = await self.execute(chat_id, command)
        if not self._alerts_configured and command.name in {"add", "list", "resume"}:
            reply += (
                f"\n\n⚠️ TELEGRAM_CHAT_IDS is empty, so alerts are dropped. "
                f"Put your chat id ({chat_id}) into .env and restart the bot."
            )
        await self._telegram.send_message(chat_id, reply)

    async def execute(self, chat_id: int, command: ParsedCommand) -> str:
        handler = self._handlers.get(command.name)
        if handler is None:
            return f"Unknown command /{command.name}. Send /help for the list."
        try:
            return await handler(chat_id, command.argument)
        except MonitorError as exc:
            return self._describe(exc)
        except InvalidUrlError as exc:
            return f"❌ {exc}"
        except Exception:
            log.exception("command /%s %s failed", command.name, command.argument)
            return "⚠️ Internal error while running the command; see the bot log."

    # ── handlers ────────────────────────────────────────────────────────────
    async def _start(self, chat_id: int, _argument: str) -> str:
        return f"👋 Your chat id is {chat_id}.\n\n{HELP_TEXT}"

    async def _help(self, _chat_id: int, _argument: str) -> str:
        return HELP_TEXT

    async def _add(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return "Usage: /add <url>"
        site = await self._manager.add_site(argument)
        return f"✅ {site.url} added to monitoring."

    async def _remove(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return "Usage: /remove <url>"
        site = await self._manager.remove_site(argument)
        return f"✅ {site.url} removed from monitoring."

    async def _pause(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return "Usage: /pause <url>"
        site = await self._manager.pause_site(argument)
        return f"⏸️ {site.url} paused."

    async def _resume(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return "Usage: /resume <url>"
        site = await self._manager.resume_site(argument)
        return f"▶️ {site.url} resumed."

    async def _notify(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return "Usage: /notify <url>"
        site = self._manager.set_notifications(argument, True)
        return f"🔔 Change alerts enabled for {site.url}."

    async def _nonotifi(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return "Usage: /nonotifi <url>"
        site = self._manager.set_notifications(argument, False)
        return f"🔇 Change alerts disabled for {site.url}."

    async def _list(self, _chat_id: int, _argument: str) -> str:
        sites = self._manager.sites()
        if not sites:
            return "‼️ No site is monitored. Add one with /add <url>."
        lines = [f"➡️ Monitored sites ({len(sites)}):"]
        lines.extend(self._describe_site(site) for site in sites)
        return "\n".join(lines)

    # ── formatting ──────────────────────────────────────────────────────────
    def _describe_site(self, site: SiteState) -> str:
        notifications = "ON" if site.notifications_enabled else "OFF"
        if site.paused:
            state = "PAUSED"
        elif self._manager.is_running(site.url):
            state = "RUNNING"
        else:
            state = "STOPPED"
        line = f"• {site.url} [alerts: {notifications}] [{state}]"
        if site.last_change is not None:
            when = datetime.fromtimestamp(site.last_change.at).strftime("%Y-%m-%d %H:%M")
            line += f" last change: {when}"
        return line

    @staticmethod
    def _describe(error: MonitorError) -> str:
        if isinstance(error, SiteAlreadyTracked):
            return f"‼️ {error.url} is already monitored."
        if isinstance(error, SiteNotTracked):
            return f"❌ {error.url} is not monitored."
        if isinstance(error, SiteAlreadyPaused):
            return f"⏸️ {error.url} is already paused."
        if isinstance(error, SiteNotPaused):
            return f"▶️ {error.url} is not paused."
        return f"❌ {error}"


class TelegramListener:
    """Long-polls ``getUpdates`` forever and feeds each update to :class:`BotCommands`."""

    def __init__(
        self,
        telegram: TelegramClient,
        commands: BotCommands,
        *,
        poll_timeout: int = 30,
        error_backoff: float = 5.0,
    ) -> None:
        self._telegram = telegram
        self._commands = commands
        self._poll_timeout = poll_timeout
        self._error_backoff = error_backoff

    async def run(self) -> None:
        log.info("Telegram listener started")
        offset: int | None = None
        while True:
            updates = await self._telegram.get_updates(offset, self._poll_timeout)
            if updates is None:
                await asyncio.sleep(self._error_backoff)
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                try:
                    await self._commands.handle_update(update)
                except Exception:
                    log.exception("handling update %s failed", update_id)
