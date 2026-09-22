"""Telegram command parsing, dispatch and the long-polling listener."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from scraper_bot.status import format_duration
from scraper_bot.telegram_client import TelegramClient
from scraper_bot.turbo import (
    ScheduledTurbo,
    TurboAlreadyRunning,
    TurboError,
    TurboLimitReached,
    TurboManager,
    TurboNotRunning,
)
from scraper_bot.urls import InvalidUrlError

log = logging.getLogger(__name__)

Handler = Callable[[int, str], Awaitable[str]]

TURBO_USAGE = (
    "Usage: /turbo <url> [minutes] [at HH:MM | in 10m]\n"
    "       /turbo stop <url>\n"
    "       /turbo (status)"
)
HELP_TEXT = (
    "Available commands:\n"
    "/add <url> — start monitoring a site\n"
    "/remove <url> — stop monitoring a site\n"
    "/pause <url> — pause monitoring\n"
    "/resume <url> — resume monitoring\n"
    "/list — monitored sites with their status\n"
    "/nonotifi <url> — mute change alerts for a site\n"
    "/notify <url> — un-mute change alerts for a site\n"
    "/turbo <url> [minutes] [at HH:MM | in 10m] — render every 2s until a contract appears\n"
    "/turbo stop <url> — end turbo mode; /turbo alone shows turbo status\n"
    "/status — bot health, last round per site, cache sizes\n"
    "/help — this message"
)

_DELAY_RE = re.compile(r"^(\d+)([mh])$", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass(frozen=True, slots=True)
class TurboRequest:
    url: str
    minutes: float | None
    start_at: float | None


def parse_turbo_request(tokens: Sequence[str], now: float) -> TurboRequest:
    """``<url> [minutes] [at HH:MM | in 10m]``; raises ValueError with an explanation."""
    url, rest = tokens[0], list(tokens[1:])
    minutes: float | None = None
    start_at: float | None = None
    index = 0
    while index < len(rest):
        token = rest[index].lower()
        if token.isdigit():
            minutes = float(token)
            index += 1
        elif token == "at" and index + 1 < len(rest):
            match = _CLOCK_RE.match(rest[index + 1])
            if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
                raise ValueError("the start time must look like HH:MM (24h clock)")
            start_at = _next_occurrence(int(match.group(1)), int(match.group(2)), now)
            index += 2
        elif token == "in" and index + 1 < len(rest):
            match = _DELAY_RE.match(rest[index + 1])
            if not match:
                raise ValueError("the delay must look like 10m or 2h")
            unit = 60 if match.group(2).lower() == "m" else 3600
            start_at = now + int(match.group(1)) * unit
            index += 2
        else:
            raise ValueError(f"unexpected argument {rest[index]!r}")
    return TurboRequest(url, minutes, start_at)


def _next_occurrence(hour: int, minute: int, now: float) -> float:
    candidate = datetime.fromtimestamp(now).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


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
        status_provider: Callable[[], str] | None = None,
        turbo: TurboManager | None = None,
    ) -> None:
        self._manager = manager
        self._telegram = telegram
        self._allowed = frozenset(allowed_chat_ids)
        self._alerts_configured = bool(alert_chat_ids)
        self._status_provider = status_provider
        self._turbo = turbo
        self._handlers: dict[str, Handler] = {
            "start": self._start,
            "help": self._help,
            "status": self._status,
            "turbo": self._turbo_command,
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
        except TurboError as exc:
            return self._describe_turbo(exc)
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

    async def _status(self, _chat_id: int, _argument: str) -> str:
        if self._status_provider is None:
            return "Status is not available."
        return self._status_provider()

    async def _turbo_command(self, _chat_id: int, argument: str) -> str:
        if self._turbo is None:
            return "Turbo mode is not available."
        tokens = argument.split()
        if not tokens:
            return self._turbo_status()
        if tokens[0].lower() == "stop":
            if len(tokens) < 2:
                return "Usage: /turbo stop <url>"
            url = await self._turbo.stop(tokens[1])
            return f"⏹️ Turbo stopped for {url}."
        try:
            request = parse_turbo_request(tokens, time.time())
        except ValueError as exc:
            return f"❌ {exc}\n{TURBO_USAGE}"
        started = await self._turbo.start(request.url, request.minutes, request.start_at)
        if isinstance(started, ScheduledTurbo):
            when = datetime.fromtimestamp(started.start_at).strftime("%Y-%m-%d %H:%M")
            return f"⏰ Turbo for {started.url} scheduled at {when} for {started.minutes:.0f} min."
        limits = self._turbo.limits
        return (
            f"🚀 Turbo started for {started.url}: {len(started.pages)} page(s) every "
            f"{limits.interval:.0f}s for {started.seconds_left / 60:.0f} min."
        )

    def _turbo_status(self) -> str:
        assert self._turbo is not None
        lines = []
        for session in self._turbo.active():
            stats = session.stats
            lines.append(
                f"🚀 {session.url}: {stats.cycles} cycles, {stats.renders} renders, "
                f"{len(stats.found)} confirmed, {stats.unverified} unverified, "
                f"{format_duration(session.seconds_left)} left"
            )
        for plan in self._turbo.scheduled():
            when = datetime.fromtimestamp(plan.start_at).strftime("%Y-%m-%d %H:%M")
            lines.append(f"⏰ {plan.url}: starts at {when} for {plan.minutes:.0f} min")
        if not lines:
            return f"No turbo session is running.\n{TURBO_USAGE}"
        return "\n".join(lines)

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
        stats = self._manager.stats(site.url)
        if stats is not None and stats.blocked:
            line += f" ⚠️ BLOCKED? ({stats.block_reason})"
        if site.last_change is not None:
            when = datetime.fromtimestamp(site.last_change.at).strftime("%Y-%m-%d %H:%M")
            line += f" last change: {when}"
        return line

    @staticmethod
    def _describe_turbo(error: TurboError) -> str:
        if isinstance(error, TurboAlreadyRunning):
            return f"🚀 Turbo is already running (or scheduled) for {error.url}."
        if isinstance(error, TurboLimitReached):
            return (
                f"❌ Turbo limit reached: at most {error.limit} site(s) at a time. "
                f"Stop one with /turbo stop <url>."
            )
        if isinstance(error, TurboNotRunning):
            return f"❌ No turbo session for {error.url}."
        return f"❌ {error}"

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
