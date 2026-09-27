"""Starting, scheduling and stopping turbo sessions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from scraper_bot.monitoring.rounds import live_pages
from scraper_bot.monitoring.turbo.session import LimitsSource, TurboLimits, TurboSession, TurboStats
from scraper_bot.pages.fetching.prober import HttpProber
from scraper_bot.pages.urls import SubUrlDeriver, normalize_url
from scraper_bot.settings.live import Tuning
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)


class TurboError(Exception):
    """Base of the turbo errors that a command turns into a reply."""

    def __init__(self, url: str) -> None:
        """Keep the URL of the site the error is about."""
        super().__init__(url)
        self.url = url


class TurboAlreadyRunning(TurboError):
    """Turbo mode is already on, launching or scheduled for this site."""


class TurboNotRunning(TurboError):
    """No turbo session runs for this site, or its launch was called off."""


class TurboLimitReached(TurboError):
    """As many turbo sessions as ``max_sites`` allows are already running or scheduled."""

    def __init__(self, url: str, limit: int) -> None:
        """Keep the site URL and the session limit that was reached."""
        super().__init__(url)
        self.limit = limit


@dataclass(frozen=True, slots=True)
class ScheduledTurbo:
    """A turbo session that starts at ``start_at`` (epoch seconds) and lasts ``minutes``."""

    url: str
    start_at: float
    minutes: float


SessionFactory = Callable[[str, list[str], float, LimitsSource], TurboSession]


class TurboManager:
    """Starts, schedules, lists and stops turbo sessions within the configured limits."""

    def __init__(
        self,
        *,
        tuning: Tuning,
        deriver: SubUrlDeriver,
        prober: HttpProber,
        session_factory: SessionFactory,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        notifier: Notifier | None = None,
    ) -> None:
        """Set up the manager; ``session_factory`` builds each session when it starts."""
        self._tuning = tuning
        self._deriver = deriver
        self._prober = prober
        self._factory = session_factory
        self._clock = clock
        self._sleep = sleep
        self._notifier = notifier
        self._sessions: dict[str, TurboSession] = {}
        self._tasks: dict[str, asyncio.Task[TurboStats]] = {}
        self._scheduled: dict[str, tuple[ScheduledTurbo, asyncio.Task[None]]] = {}
        self._launching: set[str] = set()
        self._closing = False
        self._called_off: set[str] = set()  # launches stopped while probing their pages

    @property
    def limits(self) -> TurboLimits:
        """Current limits, read from the live tuning so /set changes apply at once."""
        tuning = self._tuning
        return TurboLimits(
            interval=tuning.turbo_interval,
            default_minutes=tuning.turbo_default_minutes,
            max_minutes=tuning.turbo_max_minutes,
            stop_after=tuning.turbo_stop_after,
            max_sites=tuning.turbo_max_sites,
            max_pages=tuning.turbo_max_pages,
        )

    def active(self) -> list[TurboSession]:
        """Return the sessions that are running now."""
        return [
            session
            for url, session in self._sessions.items()
            if url in self._tasks and not self._tasks[url].done()
        ]

    def scheduled(self) -> list[ScheduledTurbo]:
        """Return the sessions planned for later."""
        return [plan for plan, _ in self._scheduled.values()]

    async def start(
        self, raw_url: str, minutes: float | None = None, start_at: float | None = None
    ) -> TurboSession | ScheduledTurbo:
        """Start turbo for a site now, or schedule it when ``start_at`` is in the future.

        Return the running session or the plan. The length is kept between one minute and
        ``max_minutes``; raises TurboAlreadyRunning or TurboLimitReached when it cannot start.
        """
        url = normalize_url(raw_url)
        if self._is_busy(url):
            raise TurboAlreadyRunning(url)
        limits = self.limits
        if self._reserved_count() >= limits.max_sites:
            raise TurboLimitReached(url, limits.max_sites)
        if minutes is None:
            minutes = limits.default_minutes
        minutes = min(max(minutes, 1.0), limits.max_minutes)
        now = self._clock()
        if start_at is not None and start_at > now + 1:
            plan = ScheduledTurbo(url, start_at, minutes)
            task = asyncio.create_task(self._start_later(plan), name=f"turbo-timer:{url}")
            self._scheduled[url] = (plan, task)
            return plan
        return await self._launch(url, minutes * 60)

    async def stop(self, raw_url: str) -> str:
        """Stop the site's session, plan or launch and return the site key.

        A running session gets up to 15 seconds to finish; raises TurboNotRunning when there
        is nothing to stop.
        """
        url = normalize_url(raw_url)
        scheduled = self._scheduled.pop(url, None)
        if scheduled is not None:
            scheduled[1].cancel()
            return url
        if url in self._launching:
            self._called_off.add(url)  # still probing its sub-pages: it will not start
            return url
        session, task = self._sessions.get(url), self._tasks.get(url)
        if session is None or task is None or task.done():
            raise TurboNotRunning(url)
        session.request_stop()
        # asyncio.wait ignores the session's own outcome but lets our cancellation through.
        await asyncio.wait({task}, timeout=15)
        return url

    async def shutdown(self) -> None:
        """Cancel every session and plan, wait for them to end, and start nothing more."""
        self._closing = True
        tasks = [task for _, task in self._scheduled.values()] + list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduled.clear()

    def _is_busy(self, url: str) -> bool:
        """Return True when the site has a plan, a launch or a running session."""
        task = self._tasks.get(url)
        return (
            url in self._scheduled
            or url in self._launching
            or (task is not None and not task.done())
        )

    def _reserved_count(self) -> int:
        """Return how many sites hold a ``max_sites`` slot: running, scheduled or launching.

        A scheduled session that is launching counts once.
        """
        running = {session.url for session in self.active()}
        return len(running | set(self._scheduled) | self._launching)

    async def _start_later(self, plan: ScheduledTurbo) -> None:
        """Wait until the plan's start time, then launch its session.

        The plan stays listed until the session runs, so /turbo stop can still cancel it
        while the sub-pages are probed.
        """
        try:
            await self._sleep(max(plan.start_at - self._clock(), 0.0))
            if not self._closing:
                await self._launch(plan.url, plan.minutes * 60)
        finally:
            entry = self._scheduled.get(plan.url)
            if entry is not None and entry[0] is plan:
                del self._scheduled[plan.url]

    async def _launch(self, url: str, duration: float) -> TurboSession:
        """Probe the site's pages, then create and start its session.

        The URL stays reserved while probing, so a concurrent /turbo can neither start it
        twice nor take its slot. Raises TurboNotRunning if a stop or shutdown came meanwhile.
        """
        self._launching.add(url)
        try:
            limits = self.limits
            pages = await self._pick_pages(url, limits.max_pages)
            if self._closing or url in self._called_off:
                self._called_off.discard(url)
                raise TurboNotRunning(url)
            session = self._factory(url, pages, duration, lambda: self.limits)
            task = asyncio.create_task(session.run(), name=f"turbo:{url}")
            task.add_done_callback(lambda done, u=url: self._on_done(u, done))
            self._sessions[url] = session
            self._tasks[url] = task
            return session
        finally:
            self._launching.discard(url)

    async def _pick_pages(self, url: str, max_pages: int) -> list[str]:
        """Return the live pages of the site, at most ``max_pages`` of them."""
        return (await live_pages(self._prober, self._deriver, url))[:max_pages]

    def _on_done(self, url: str, task: asyncio.Task[TurboStats]) -> None:
        """Forget a finished session, and tell the user if it crashed."""
        if self._tasks.get(url) is task:
            del self._tasks[url]
            self._sessions.pop(url, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        log.error("turbo session for %s crashed: %r", url, error, exc_info=error)
        if self._notifier is not None:
            self._notifier.report_later(
                f"turbo:{url}",
                f"Turbo session for {url} crashed",
                f"{type(error).__name__}: {error}",
                hint="The session is over; start it again with /turbo when the cause is fixed.",
            )
