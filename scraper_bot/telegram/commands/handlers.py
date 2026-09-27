"""What each bot command does and how the bot replies."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from scraper_bot.models.sites import SiteState
from scraper_bot.monitoring.monitor_manager import (
    MonitorError,
    MonitorManager,
    SiteAlreadyPaused,
    SiteAlreadyTracked,
    SiteNotPaused,
    SiteNotTracked,
)
from scraper_bot.monitoring.turbo.manager import (
    ScheduledTurbo,
    TurboAlreadyRunning,
    TurboError,
    TurboLimitReached,
    TurboManager,
    TurboNotRunning,
)
from scraper_bot.pages.urls import InvalidUrlError
from scraper_bot.settings.live import ConfirmationRequired, PreferenceError, Preferences
from scraper_bot.telegram.client import TelegramClient
from scraper_bot.telegram.commands.help import HELP_TEXT, SETTINGS_USAGE, TURBO_USAGE, usage
from scraper_bot.telegram.commands.parsing import ParsedCommand, parse_command, parse_turbo_request
from scraper_bot.telegram.markup import code, escape
from scraper_bot.telegram.timefmt import format_clock, format_duration, format_schedule
from scraper_bot.telegram.views.settings import describe_setting, settings_overview

log = logging.getLogger(__name__)


Handler = Callable[[int, str], Awaitable[str]]

# An unauthorised chat learns its chat id from /start at most this often (seconds).
_STRANGER_REPLY_INTERVAL = 600.0
_STRANGER_MEMORY = 1000


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._manager = manager
        self._telegram = telegram
        self._alerts_configured = bool(alert_chat_ids)
        # Commands come from the allowlist or, when it is empty, from the alert chats;
        # any other chat can only learn its chat id with /start.
        self._allowlist = frozenset(allowed_chat_ids)
        self._commanders = self._allowlist or frozenset(alert_chat_ids)
        self._status_provider = status_provider
        self._turbo = turbo
        self._preferences = preferences
        self._clock = clock
        self._stranger_replies: dict[int, float] = {}
        self._warned_chats: set[int] = set()  # told that TELEGRAM_CHAT_IDS is empty
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
            "unmute": lambda chat_id, argument: self._notify(chat_id, argument, "unmute"),
            "nonotifi": self._nonotifi,
            "mute": lambda chat_id, argument: self._nonotifi(chat_id, argument, "mute"),
        }

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text")
        if not isinstance(chat_id, int) or not isinstance(text, str):
            return
        command = parse_command(text, self._telegram.username)
        if command is None:
            return
        if chat_id not in self._commanders:
            await self._answer_stranger(chat_id, command.name)
            return
        reply = await self.execute(chat_id, command)
        if (
            not self._alerts_configured
            and command.name in {"add", "list", "resume"}
            and chat_id not in self._warned_chats
        ):
            # Once per chat and start: the reminder must not bury every reply.
            self._warned_chats.add(chat_id)
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
            log.exception("command /%s %r failed", command.name, command.argument)
            detail = f"{type(exc).__name__}: {exc}"[:300]
            return (
                f"⚠️ Internal error while running {code('/' + command.name)}: {code(detail)}\n"
                f"The bot keeps running; details are in the log."
            )

    async def _answer_stranger(self, chat_id: int, name: str) -> None:
        """An unauthorised chat gets its chat id for /start (rate limited), nothing else."""
        if name != "start":
            log.warning("ignoring %r from unauthorised chat %s", "/" + name, chat_id)
            return
        now = self._clock()
        last = self._stranger_replies.get(chat_id)
        if last is not None and now - last < _STRANGER_REPLY_INTERVAL:
            return
        if len(self._stranger_replies) >= _STRANGER_MEMORY:
            self._stranger_replies = {
                chat: at
                for chat, at in self._stranger_replies.items()
                if now - at < _STRANGER_REPLY_INTERVAL
            }
        self._stranger_replies[chat_id] = now
        if self._allowlist:
            # With an allowlist only that list commands; TELEGRAM_CHAT_IDS would not help.
            where = "TELEGRAM_ALLOWED_CHAT_IDS"
        else:
            where = (
                "TELEGRAM_CHAT_IDS (alerts and commands) or TELEGRAM_ALLOWED_CHAT_IDS "
                "(commands only)"
            )
        await self._telegram.send_message(
            chat_id,
            f"👋 Your chat id is {code(chat_id)}. This chat cannot control the bot: add the id "
            f"to {where} in .env and restart the bot.",
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
            return (
                f"⏰ Turbo for {escape(started.url)} scheduled at "
                f"{format_schedule(started.start_at, time.time())} for "
                f"{started.minutes:.0f} min. Cancel with {code(f'/turbo stop {started.url}')}"
            )
        limits = self._turbo.limits
        return (
            f"🚀 Turbo started for {escape(started.url)}: {len(started.pages)} page(s) every "
            f"{limits.interval:.0f}s for {started.seconds_left / 60:.0f} min."
        )

    # ── settings ────────────────────────────────────────────────────────────
    async def _settings(self, _chat_id: int, argument: str) -> str:
        if self._preferences is None:
            return "Settings are not available."
        tokens = argument.split()
        if not tokens:
            return settings_overview(self._preferences)
        if tokens[0].lower() == "reset":
            target = tokens[1] if len(tokens) > 1 else "all"
            changes = self._preferences.reset(None if target.lower() == "all" else target)
            if not changes:
                return "Nothing to reset: every setting already has its .env value."
            lines = ["♻️ Restored .env values:"]
            lines.extend(
                f"• {code(c.spec.key)}: {escape(c.spec.format(c.old))} → "
                f"{escape(c.spec.format(c.new))}"
                for c in changes
            )
            return "\n".join(lines)
        return describe_setting(self._preferences, tokens[0])

    async def _set(self, _chat_id: int, argument: str) -> str:
        if self._preferences is None:
            return "Settings are not available."
        tokens = argument.split()
        if len(tokens) < 2:
            return SETTINGS_USAGE
        confirmed = len(tokens) > 2 and tokens[2].lower() == "confirm"
        change = self._preferences.set(tokens[0], tokens[1], confirmed=confirmed)
        spec = change.spec
        return (
            f"✅ {code(spec.key)}: {escape(spec.format(change.old))} → "
            f"{escape(spec.format(change.new))} (applies immediately)"
        )

    def _describe_confirmation(self, exc: ConfirmationRequired) -> str:
        value = "on" if exc.value is True else "off" if exc.value is False else f"{exc.value:g}"
        return (
            f"⚠️ {escape(exc.warning)}\n"
            f"To apply anyway, repeat: {code(f'/set {exc.spec.key} {value} confirm')}"
        )

    async def _unverified(self, _chat_id: int, argument: str) -> str:
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
                f"{len(stats.found)} confirmed, {session.pending_count} pending, "
                f"{format_duration(session.seconds_left)} left; "
                f"stop with {code(f'/turbo stop {session.url}')}"
            )
        for plan in self._turbo.scheduled():
            when = format_schedule(plan.start_at, time.time())
            lines.append(
                f"⏰ {escape(plan.url)}: starts at {when} for "
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
        reply = f"✅ {escape(site.url)} added to monitoring."
        shared = self._manager.sharing_pages(site.url)
        if shared:
            reply += (
                f"\n⚠️ It shares pages with {', '.join(escape(url) for url in shared)}. "
                "Both monitors render them and keep one snapshot of each, so a change "
                "there is reported only by the monitor that sees it first (by none, if "
                "that site is muted)."
            )
        return reply

    async def _remove(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return usage("/remove <url>")
        url = self._manager.resolve(argument)
        if self._turbo is not None:
            # Its session would keep handing the site's addresses to the pending watch.
            with contextlib.suppress(TurboError):
                await self._turbo.stop(url)
        site = await self._manager.remove_site(url)
        return f"✅ {escape(site.url)} removed from monitoring."

    async def _pause(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return usage("/pause <url>")
        site = await self._manager.pause_site(argument)
        return f"⏸️ {escape(site.url)} paused. Resume with {code(f'/resume {site.url}')}"

    async def _resume(self, _chat_id: int, argument: str) -> str:
        if not argument:
            return usage("/resume <url>")
        site, restarted = await self._manager.resume_site(argument)
        if restarted:
            return f"▶️ {escape(site.url)} was stopped by an error; its monitor is running again."
        return f"▶️ {escape(site.url)} resumed."

    async def _notify(self, _chat_id: int, argument: str, name: str = "notify") -> str:
        if not argument:
            return usage(f"/{name} <url>")
        site = self._manager.set_notifications(argument, True)
        return f"🔔 Change alerts enabled for {escape(site.url)}."

    async def _nonotifi(self, _chat_id: int, argument: str, name: str = "nonotifi") -> str:
        if not argument:
            return usage(f"/{name} <url>")
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
            state = "STOPPED"  # the monitor crashed and did not come back; /resume restarts it
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
