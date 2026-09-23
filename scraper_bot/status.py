"""Human readable health report behind the /status command."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from scraper_bot.markup import escape
from scraper_bot.models import SiteState
from scraper_bot.monitor_manager import MonitorManager
from scraper_bot.storage import AddressCache
from scraper_bot.timefmt import format_clock, format_duration
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
            lines.append(f"• {escape(site.url)}: {self._describe(site, now)}")
        if self._turbo is not None:
            for session in self._turbo.active():
                stats = session.stats
                lines.append(
                    f"🚀 Turbo {escape(session.url)}: {stats.cycles} cycles, "
                    f"{len(stats.found)} confirmed, "
                    f"{stats.unverified} unverified, {format_duration(session.seconds_left)} left"
                )
            for plan in self._turbo.scheduled():
                lines.append(
                    f"⏰ Turbo {escape(plan.url)}: scheduled at "
                    f"{format_clock(plan.start_at, date=False)} for {plan.minutes:.0f} min"
                )
        lines.append(
            f"Address cache: {self._cache.known_count} known contract(s), "
            f"{self._cache.rejected_count} rejected"
        )
        return "\n".join(lines)

    def _describe(self, site: SiteState, now: float) -> str:
        if site.paused:
            return "paused"
        stats = self._manager.stats(site.url)
        if stats is None or stats.last_round_at is None:
            return "starting"
        text = (
            f"last round {format_duration(now - stats.last_round_at)} ago "
            f"({stats.last_round_seconds:.1f}s, {stats.last_pages} page(s), "
            f"{stats.rounds} rounds)"
        )
        if site.last_change is not None:
            text += f"; last change {format_clock(site.last_change.at)}"
        if stats.timing_out:
            text += f"; 🐢 {stats.timing_out} page(s) timing out"
        if stats.blocked:
            since = format_clock(stats.blocked_since or now, date=False)
            text += (
                f"; ⚠️ BLOCKED? {escape(stats.block_reason)} since {since}, "
                f"interval x{2**stats.block_level}"
            )
        return text
