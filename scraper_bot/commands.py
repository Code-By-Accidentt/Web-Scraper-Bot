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

from scraper_bot.markup import code, escape
from scraper_bot.models import SiteState
from scraper_bot.monitor_manager import (
    MonitorError,
    MonitorManager,
    SiteAlreadyPaused,
    SiteAlreadyTracked,
    SiteNotPaused,
    SiteNotTracked,
)
from scraper_bot.preferences import ConfirmationRequired, PreferenceError, Preferences
from scraper_bot.telegram_client import TelegramClient
from scraper_bot.timefmt import format_clock, format_duration
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

COMMAND_HELP: tuple[tuple[str, str], ...] = (
    ("/add <url>", "start monitoring a site"),
    ("/remove <url>", "stop monitoring a site"),
    ("/pause <url>", "pause monitoring"),
    ("/resume <url>", "resume monitoring"),
    ("/list", "monitored sites with their status"),
    ("/nonotifi <url>", "mute change alerts for a site"),
    ("/notify <url>", "un-mute change alerts for a site"),
    ("/turbo <url> [minutes] [at HH:MM | in 10m]", "render every 2s until a contract appears"),
    ("/turbo stop <url>", "end turbo mode; /turbo alone shows turbo status"),
    ("/unverified <url> on|off", "announce every new address at once, confirm later"),
    ("/settings [key]", "live settings with explanations; /set <key> <value> changes one"),
    ("/status", "bot health, last round per site, cache sizes"),
    ("/help", "this message"),
)
HELP_TEXT = "Available commands:\n" + "\n".join(
    f"{code(usage)} — {escape(description)}" for usage, description in COMMAND_HELP
)
TURBO_USAGE = "Usage:\n" + "\n".join(
    code(line)
    for line in ("/turbo <url> [minutes] [at HH:MM | in 10m]", "/turbo stop <url>", "/turbo")
)
SETTINGS_USAGE = "Usage:\n" + "\n".join(
    f"{code(usage)} — {escape(description)}"
    for usage, description in (
        ("/settings", "all settings with live values"),
        ("/settings <key>", "what a setting does, its range and default"),
        ("/set <key> <value>", "change it (add 'confirm' for risky values)"),
        ("/settings reset <key|all>", "back to the .env value"),
    )
)

_DELAY_RE = re.compile(r"^(\d+)([mh])$", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def usage(command: str) -> str:
    return f"Usage: {code(command)}"


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
    """Turns commands into manager, turbo and preferences calls and phrases the replies (HTML)."""

    def __init__(
        self,
        manager: MonitorManager,
        telegram: TelegramClient,
        *,
        allowed_chat_ids: Sequence[int] = (),
        alert_chat_ids: Sequence[int] = (),
        status_provider: Callable[[], str] | None = None,
        turbo: TurboManager | None = None,
        preferences: Preferences | None = None,
    ) -> None:
        self._manager = manager
        self._telegram = telegram
        self._allowed = frozenset(allowed_chat_ids)
        self._alerts_configured = bool(alert_chat_ids)
        # Settings may be changed from the command allowlist, or from the alert chats
        # when no allowlist is configured; never from arbitrary chats.
        self._admins = frozenset(allowed_chat_ids) or frozenset(alert_chat_ids)
        self._status_provider = status_provider
        self._turbo = turbo
        self._preferences = preferences
        self._handlers: dict[str, Handler] = {
            "start": self._start,
            "help": self._help,
            "status": self._status,
            "turbo": self._turbo_command,
            "settings": self._settings,
            "set": self._set,
            "unverified": self._unverified,
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
                f"Put your chat id {code(chat_id)} into .env and restart the bot."
            )
        await self._telegram.send_message(chat_id, reply)

    async def execute(self, chat_id: int, command: ParsedCommand) -> str:
        handler = self._handlers.get(command.name)
        if handler is None:
            return f"Unknown command {code('/' + command.name)}. Send {code('/help')} for the list."
        try:
            return await handler(chat_id, command.argument)
        except MonitorError as exc:
            return self._describe(exc)
        except TurboError as exc:
            return self._describe_turbo(exc)
        except ConfirmationRequired as exc:
            return self._describe_confirmation(exc)
        except (PreferenceError, InvalidUrlError) as exc:
            return f"❌ {escape(exc)}"
        except Exception as exc:
            log.exception("command /%s %s failed", command.name, command.argument)
            detail = f"{type(exc).__name__}: {exc}"[:300]
            return (
                f"⚠️ Internal error while running {code('/' + command.name)}: {code(detail)}\n"
                f"The bot keeps running; details are in the log."
            )

    # ── handlers ────────────────────────────────────────────────────────────
    async def _start(self, chat_id: int, _argument: str) -> str:
        return f"👋 Your chat id is {code(chat_id)}.\n\n{HELP_TEXT}"

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
                return usage("/turbo stop <url>")
            url = await self._turbo.stop(tokens[1])
            return f"⏹️ Turbo stopped for {escape(url)}."
        try:
            request = parse_turbo_request(tokens, time.time())
        except ValueError as exc:
            return f"❌ {escape(exc)}\n{TURBO_USAGE}"
        started = await self._turbo.start(request.url, request.minutes, request.start_at)
        if isinstance(started, ScheduledTurbo):
            when = format_clock(started.start_at)
            return (
                f"⏰ Turbo for {escape(started.url)} scheduled at {when} for "
                f"{started.minutes:.0f} min. Cancel with {code(f'/turbo stop {started.url}')}"
            )
        limits = self._turbo.limits
        return (
            f"🚀 Turbo started for {escape(started.url)}: {len(started.pages)} page(s) every "
            f"{limits.interval:.0f}s for {started.seconds_left / 60:.0f} min."
        )

    # ── settings ────────────────────────────────────────────────────────────
    def _admin_only(self, chat_id: int) -> str | None:
        if not self._admins:
            return (
                "🔒 Settings can only be changed from chats listed in "
                "TELEGRAM_ALLOWED_CHAT_IDS or TELEGRAM_CHAT_IDS; both are empty."
            )
        if chat_id not in self._admins:
            return (
                "🔒 Only chats listed in TELEGRAM_ALLOWED_CHAT_IDS (or TELEGRAM_CHAT_IDS) "
                "can change settings."
            )
        return None

    async def _settings(self, chat_id: int, argument: str) -> str:
        if self._preferences is None:
            return "Settings are not available."
        tokens = argument.split()
        if not tokens:
            return self._preferences.overview()
        if tokens[0].lower() == "reset":
            denied = self._admin_only(chat_id)
            if denied:
                return denied
            target = tokens[1] if len(tokens) > 1 else "all"
            changes = self._preferences.reset(None if target.lower() == "all" else target)
            if not changes:
                return "Nothing to reset: every setting already has its .env value."
            prefs = self._preferences
            lines = ["♻️ Restored .env values:"]
            lines.extend(
                f"• {code(c.spec.key)}: {escape(prefs.format_value(c.spec, c.old))} → "
                f"{escape(prefs.format_value(c.spec, c.new))}"
                for c in changes
            )
            return "\n".join(lines)
        return self._preferences.describe(tokens[0])

    async def _set(self, chat_id: int, argument: str) -> str:
        if self._preferences is None:
            return "Settings are not available."
        denied = self._admin_only(chat_id)
        if denied:
            return denied
        tokens = argument.split()
        if len(tokens) < 2:
            return SETTINGS_USAGE
        confirmed = len(tokens) > 2 and tokens[2].lower() == "confirm"
        change = self._preferences.set(tokens[0], tokens[1], confirmed=confirmed)
        prefs = self._preferences
        return (
            f"✅ {code(change.spec.key)}: {escape(prefs.format_value(change.spec, change.old))} → "
            f"{escape(prefs.format_value(change.spec, change.new))} (applies immediately)"
        )

    def _describe_confirmation(self, exc: ConfirmationRequired) -> str:
        value = "on" if exc.value is True else "off" if exc.value is False else f"{exc.value:g}"
        return (
            f"⚠️ {escape(exc.warning)}\n"
            f"To apply anyway, repeat: {code(f'/set {exc.spec.key} {value} confirm')}"
        )

    async def _unverified(self, chat_id: int, argument: str) -> str:
        tokens = argument.split()
        if not tokens:
            sites = [s for s in self._manager.sites() if s.unverified_enabled]
            if not sites:
                return (
                    f"Unverified mode is off for every site.\n{usage('/unverified <url> on|off')}\n"
                    "It announces every new address on the site at once and re-checks it "
                    "on-chain until it is confirmed."
                )
            return "Unverified mode is on for:\n" + "\n".join(f"• {escape(s.url)}" for s in sites)
        denied = self._admin_only(chat_id)
        if denied:
            return denied
        if len(tokens) < 2 or tokens[1].lower() not in {"on", "off"}:
            return usage("/unverified <url> on|off")
        enabled = tokens[1].lower() == "on"
        site = self._manager.set_unverified(tokens[0], enabled)
        if enabled:
            return (
                f"⚠️ Unverified mode ON for {escape(site.url)}: every new address is announced "
                f"at once (wallets and example addresses included) and re-checked on-chain until "
                f"it is confirmed. Alerts are capped per hour. Turn off with "
                f"{code(f'/unverified {site.url} off')}"
            )
        return (
            f"✅ Unverified mode OFF for {escape(site.url)}; "
            "only confirmed contracts are announced."
        )

    def _turbo_status(self) -> str:
        assert self._turbo is not None
        lines = []
        for session in self._turbo.active():
            stats = session.stats
            lines.append(
                f"🚀 {escape(session.url)}: {stats.cycles} cycles, {stats.renders} renders, "
                f"{len(stats.found)} confirmed, {stats.unverified} unverified, "
                f"{format_duration(session.seconds_left)} left; "
                f"stop with {code(f'/turbo stop {session.url}')}"
            )
        for plan in self._turbo.scheduled():
            lines.append(
                f"⏰ {escape(plan.url)}: starts at {format_clock(plan.start_at)} for "
                f"{plan.minutes:.0f} min; "
                f"cancel with {code(f'/turbo stop {plan.url}')}"
            )
        if not lines:
            return f"No turbo session is running.\n{TURBO_USAGE}"
        return "\n".join(lines)

    async def _add(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return usage("/add <url>")
        site = await self._manager.add_site(argument)
        return f"✅ {escape(site.url)} added to monitoring."

    async def _remove(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return usage("/remove <url>")
        site = await self._manager.remove_site(argument)
        return f"✅ {escape(site.url)} removed from monitoring."

    async def _pause(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return usage("/pause <url>")
        site = await self._manager.pause_site(argument)
        return f"⏸️ {escape(site.url)} paused. Resume with {code(f'/resume {site.url}')}"

    async def _resume(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return usage("/resume <url>")
        site = await self._manager.resume_site(argument)
        return f"▶️ {escape(site.url)} resumed."

    async def _notify(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return usage("/notify <url>")
        site = self._manager.set_notifications(argument, True)
        return f"🔔 Change alerts enabled for {escape(site.url)}."

    async def _nonotifi(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return usage("/nonotifi <url>")
        site = self._manager.set_notifications(argument, False)
        return f"🔇 Change alerts disabled for {escape(site.url)}."

    async def _list(self, _chat_id: int, _argument: str) -> str:
        sites = self._manager.sites()
        if not sites:
            return f"‼️ No site is monitored. Add one with {code('/add <url>')}."
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
        line = f"• {escape(site.url)} [alerts: {notifications}] [{state}]"
        if site.unverified_enabled:
            line += " [unverified]"
        stats = self._manager.stats(site.url)
        if stats is not None and stats.blocked:
            line += f" ⚠️ BLOCKED? ({escape(stats.block_reason)})"
        if site.last_change is not None:
            line += f" last change: {format_clock(site.last_change.at)}"
        return line

    @staticmethod
    def _describe_turbo(error: TurboError) -> str:
        if isinstance(error, TurboAlreadyRunning):
            return f"🚀 Turbo is already running (or scheduled) for {escape(error.url)}."
        if isinstance(error, TurboLimitReached):
            return (
                f"❌ Turbo limit reached: at most {error.limit} site(s) at a time. "
                f"Stop one with {code('/turbo stop <url>')}."
            )
        if isinstance(error, TurboNotRunning):
            return f"❌ No turbo session for {escape(error.url)}."
        return f"❌ {escape(error)}"

    @staticmethod
    def _describe(error: MonitorError) -> str:
        if isinstance(error, SiteAlreadyTracked):
            return f"‼️ {escape(error.url)} is already monitored."
        if isinstance(error, SiteNotTracked):
            return f"❌ {escape(error.url)} is not monitored. See {code('/list')}."
        if isinstance(error, SiteAlreadyPaused):
            return f"⏸️ {escape(error.url)} is already paused."
        if isinstance(error, SiteNotPaused):
            return f"▶️ {escape(error.url)} is not paused."
        return f"❌ {escape(error)}"


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
