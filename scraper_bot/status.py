"""Human readable health report behind the /status command."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from scraper_bot.monitor_manager import MonitorManager
from scraper_bot.storage import AddressCache
from scraper_bot.turbo import TurboManager


class BrowserHealth(Protocol):
    @property
    def is_connected(self) -> bool: ...


class StatusReporter:
    """Collects uptime, browser state, per-site round timing, turbo sessions and caches."""

    def __init__(
        self,
        *,
        version: str,
        manager: MonitorManager,
        cache: AddressCache,
        browser: BrowserHealth,
        turbo: TurboManager | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._version = version
        self._manager = manager
        self._cache = cache
        self._browser = browser
        self._turbo = turbo
        self._clock = clock
        self._started_at = clock()

    def render(self) -> str:
        now = self._clock()
        sites = self._manager.sites()
        running = sum(1 for site in sites if self._manager.is_running(site.url))
        paused = sum(1 for site in sites if site.paused)
        lines = [
            f"🤖 scraper-bot {self._version}, up {format_duration(now - self._started_at)}",
            f"Browser: {'connected' if self._browser.is_connected else 'DISCONNECTED'}",
            f"Sites: {len(sites)} total, {running} running, {paused} paused",
        ]
        for site in sites:
            lines.append(f"• {site.url}: {self._describe(site.url, site.paused, now)}")
        if self._turbo is not None:
            for session in self._turbo.active():
                stats = session.stats
                lines.append(
                    f"🚀 Turbo {session.url}: {stats.cycles} cycles, {len(stats.found)} confirmed, "
                    f"{stats.unverified} unverified, {format_duration(session.seconds_left)} left"
                )
            for plan in self._turbo.scheduled():
                when = datetime.fromtimestamp(plan.start_at).strftime("%H:%M")
                lines.append(f"⏰ Turbo {plan.url}: scheduled at {when} for {plan.minutes:.0f} min")
        lines.append(
            f"Address cache: {self._cache.known_count} known contract(s), "
            f"{self._cache.rejected_count} rejected"
        )
        return "\n".join(lines)

    def _describe(self, url: str, paused: bool, now: float) -> str:
        if paused:
            return "paused"
        stats = self._manager.stats(url)
        if stats is None or stats.last_round_at is None:
            return "starting"
        text = (
            f"last round {format_duration(now - stats.last_round_at)} ago "
            f"({stats.last_round_seconds:.1f}s, {stats.last_pages} page(s), "
            f"{stats.rounds} rounds)"
        )
        site = next((s for s in self._manager.sites() if s.url == url), None)
        if site is not None and site.last_change is not None:
            when = datetime.fromtimestamp(site.last_change.at).strftime("%Y-%m-%d %H:%M")
            text += f"; last change {when}"
        if stats.blocked:
            since = datetime.fromtimestamp(stats.blocked_since or now).strftime("%H:%M")
            text += (
                f"; ⚠️ BLOCKED? {stats.block_reason} since {since}, interval x{2**stats.block_level}"
            )
        return text


def format_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
