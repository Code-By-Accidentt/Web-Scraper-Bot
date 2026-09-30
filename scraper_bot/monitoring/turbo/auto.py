"""Auto-turbo: a site checked faster around the launch time one of its pages shows."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from scraper_bot.models.addresses import ConfirmedContract
from scraper_bot.models.launch import LaunchClues
from scraper_bot.monitoring.pace import AUTO, SitePace
from scraper_bot.monitoring.rounds import wait_or_stop
from scraper_bot.monitoring.tokens import SiteTokens, Subscriber
from scraper_bot.monitoring.turbo.clues import LaunchCandidate, LaunchClueTracker
from scraper_bot.monitoring.turbo.session import MANUAL_STOP
from scraper_bot.settings.live import Tuning
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)

MOVED = 60.0  # how far a launch time must move before its window follows it (s)
SPENT_FOR = 24 * 3600.0  # a launch time whose window ended is not planned again for this long
DAY = 24 * 3600.0
STOP_WAIT = 5.0  # how long /turbo stop waits for a window's summary (s)
_FORGOTTEN = "the site was paused or removed"


@dataclass(frozen=True, slots=True)
class AutoWindowView:
    """What /turbo and /status show of one auto-turbo window."""

    site_url: str
    page_url: str
    what: str  # what the page showed, in words
    target: float  # the launch time (epoch seconds), or for a phrase, when it appeared
    timed: bool  # False for a phrase, which names no time
    start_at: float
    end_at: float
    active: bool  # the site is checked faster right now


@dataclass(frozen=True, slots=True)
class _Spent:
    """A launch time, or a phrase, whose window ended: it plans no other window until ``until``."""

    source: str
    at: float
    timed: bool
    until: float


@dataclass(eq=False, slots=True)
class _Window:
    """The faster checks of one site around one launch time, while they wait or run."""

    site_url: str
    page_url: str
    source: str  # the page and the countdown, time or phrase on it that planned the window
    what: str
    target: float
    timed: bool
    start_at: float
    end_at: float
    started_at: float | None = None  # when the faster checks first began
    active_since: float | None = None  # when the current stretch of faster checks began
    fast_seconds: float = 0.0  # time spent in earlier stretches
    found: list[ConfirmedContract] = field(default_factory=list)
    reason: str | None = None  # why it ends, once it does
    quiet: bool = False  # it ends without a summary (cancelled before it began, or at shutdown)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    heard: Subscriber | None = None  # hears the site's tokens
    earlier: list[_Spent] = field(default_factory=list)  # the launch times it had before

    @property
    def active(self) -> bool:
        """True while the site is checked faster."""
        return self.active_since is not None

    def view(self) -> AutoWindowView:
        """Return a copy of what /turbo and /status show."""
        return AutoWindowView(
            self.site_url,
            self.page_url,
            self.what,
            self.target,
            self.timed,
            self.start_at,
            self.end_at,
            self.active,
        )


class AutoTurbo:
    """Plans and runs the auto-turbo windows of every site.

    The monitors hand it what their renders say about a coming launch, and a
    :class:`LaunchClueTracker` picks out the launch times. A window runs from
    ``auto_turbo_before`` before the launch time to ``auto_turbo_after`` after it: the site
    is sped up in :class:`SitePace` meanwhile (its rounds, light polls and pending checks).
    The site's ``turbo_stop_after``-th token, as :class:`SiteTokens` brings it, or
    /turbo stop ends a window early. One window per site; an earlier launch time takes over
    a window that has not begun yet.
    """

    def __init__(
        self,
        *,
        tuning: Tuning,
        notifier: Notifier,
        pace: SitePace | None = None,
        tokens: SiteTokens | None = None,
        tracker: LaunchClueTracker | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Set up without any window; ``sleep`` replaces the waits (tests pass a fake one)."""
        self._tuning = tuning
        self._notifier = notifier
        self._pace = pace or SitePace()
        self._tokens = tokens or SiteTokens()
        self._tracker = tracker or LaunchClueTracker()
        self._clock = clock
        self._sleep = sleep
        self._windows: dict[str, _Window] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._spent: dict[str, list[_Spent]] = {}
        self._told_full: set[tuple[str, str]] = set()  # (site, source) told the limit is reached
        self._closing = False

    def windows(self) -> list[AutoWindowView]:
        """Return the windows waiting or running, the earliest first."""
        ordered = sorted(self._windows.values(), key=lambda window: window.start_at)
        return [window.view() for window in ordered]

    async def observe(self, site_url: str, page_url: str, clues: LaunchClues) -> None:
        """Take in what a render of ``page_url`` says about a coming launch."""
        if self._closing:
            return
        candidates = self._tracker.read(site_url, page_url, clues)
        if not self._tuning.auto_turbo:
            return
        for candidate in candidates:
            await self._consider(site_url, candidate)

    async def stop(self, site_url: str) -> bool:
        """End the window of ``site_url`` (/turbo stop); return False when it has none.

        A window that had not begun yet ends without a summary.
        """
        window = self._windows.get(site_url)
        if window is None:
            return False
        self._end(window, MANUAL_STOP, quiet=window.started_at is None)
        task = self._tasks.get(site_url)
        if task is not None:
            await asyncio.wait({task}, timeout=STOP_WAIT)
        return True

    def forget(self, site_url: str) -> None:
        """End the window of a paused or removed site quietly and drop what was read on it."""
        window = self._windows.get(site_url)
        if window is not None:
            self._end(window, _FORGOTTEN, quiet=True)
        self._tracker.forget(site_url)
        self._spent.pop(site_url, None)
        self._told_full = {told for told in self._told_full if told[0] != site_url}

    async def shutdown(self) -> None:
        """Cancel every window without a summary and plan nothing more."""
        self._closing = True
        tasks = list(self._tasks.values())
        for window in self._windows.values():
            window.quiet = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _consider(self, site_url: str, candidate: LaunchCandidate) -> None:
        """Plan a window for a launch time, follow a moved one, or say the limit is reached."""
        now = self._clock()
        window = self._windows.get(site_url)
        if window is not None:
            if window.source == candidate.source:
                moved = abs(candidate.target - window.target) > MOVED
                if moved and candidate.timed and window.target > now:
                    await self._move(window, candidate)
            elif (
                candidate.timed
                and window.started_at is None
                and candidate.target < window.target - MOVED
            ):
                await self._move(window, candidate)  # an earlier launch takes the window over
            return
        tuning = self._tuning
        too_late = candidate.target + tuning.auto_turbo_after <= now
        if too_late or self._spent_on(site_url, candidate, now):
            return
        if len(self._windows) >= tuning.auto_turbo_max_sites:
            if (site_url, candidate.source) not in self._told_full:
                self._told_full.add((site_url, candidate.source))
                log.info(
                    "[%s] %s on %s, but auto-turbo is full",
                    site_url,
                    candidate.what,
                    candidate.page_url,
                )
                await self._notifier.auto_turbo_full(
                    site_url,
                    candidate.page_url,
                    candidate.what,
                    candidate.target if candidate.timed else None,
                    len(self._windows),
                )
            return
        window = _Window(
            site_url,
            candidate.page_url,
            candidate.source,
            candidate.what,
            candidate.target,
            candidate.timed,
            start_at=max(candidate.target - tuning.auto_turbo_before, now),
            end_at=candidate.target + tuning.auto_turbo_after,
        )
        window.heard = lambda contract: self._heard(window, contract)
        self._tokens.subscribe(site_url, window.heard)
        self._windows[site_url] = window
        task = asyncio.create_task(self._run(window), name=f"auto:{site_url}")
        task.add_done_callback(lambda done: self._on_done(site_url, done))
        self._tasks[site_url] = task
        log.info(
            "[%s] auto-turbo planned from %s on %s: faster checks %.0f s from now for %.0f s",
            site_url,
            candidate.what,
            candidate.page_url,
            window.start_at - now,
            window.end_at - window.start_at,
        )
        await self._notifier.auto_turbo_planned(
            site_url,
            candidate.page_url,
            candidate.what,
            candidate.target if candidate.timed else None,
            window.start_at,
            window.end_at,
        )

    async def _move(self, window: _Window, candidate: LaunchCandidate) -> None:
        """Follow a launch time that moved, or an earlier one that takes the window over."""
        tuning, before = self._tuning, window.target
        window.earlier.append(_Spent(window.source, window.target, window.timed, 0.0))
        window.page_url, window.source = candidate.page_url, candidate.source
        window.what, window.target = candidate.what, candidate.target
        window.start_at = max(candidate.target - tuning.auto_turbo_before, self._clock())
        window.end_at = candidate.target + tuning.auto_turbo_after
        window.changed.set()
        log.info("[%s] the launch time moved (%s)", window.site_url, candidate.what)
        await self._notifier.auto_turbo_planned(
            window.site_url,
            window.page_url,
            window.what,
            window.target,
            window.start_at,
            window.end_at,
            moved_from=before,
        )

    async def _run(self, window: _Window) -> None:
        """Wait for the window to begin, keep the site fast until it ends, then sum it up."""
        try:
            while window.reason is None:
                now = self._clock()
                if now >= window.end_at:
                    window.reason = "the window is over"
                    break
                if now >= window.start_at:
                    if not window.active:
                        self._activate(window)
                    deadline = window.end_at
                else:
                    if window.active:
                        self._deactivate(window)  # the launch time moved later
                    deadline = window.start_at
                await self._wait(window, deadline - now)
        except asyncio.CancelledError:
            window.reason = window.reason or "the bot is shutting down"
            raise
        except Exception as exc:
            window.reason = window.reason or f"crashed ({type(exc).__name__})"
            raise
        finally:
            await self._finish(window)

    def _on_done(self, site_url: str, task: asyncio.Task[None]) -> None:
        """Tell the user when a window crashed; a cancelled one (at shutdown) did not."""
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        log.error("auto-turbo window for %s crashed: %r", site_url, error, exc_info=error)
        self._notifier.report_later(
            f"auto:{site_url}",
            f"Auto-turbo window for {site_url} crashed",
            f"{type(error).__name__}: {error}",
            hint="The site is back at its usual pace; start /turbo by hand if the launch is "
            "still ahead.",
        )

    def _activate(self, window: _Window) -> None:
        """Speed the site up; its monitor starts its next round at once."""
        now = self._clock()
        window.active_since = now
        if window.started_at is None:
            window.started_at = now
        log.info("[%s] auto-turbo on (%s)", window.site_url, window.what)
        self._pace.boost(window.site_url, AUTO, rounds=True)

    def _deactivate(self, window: _Window) -> None:
        """Return the site to its usual pace."""
        if window.active_since is not None:
            window.fast_seconds += self._clock() - window.active_since
            window.active_since = None
        self._pace.release(window.site_url, AUTO)

    def _heard(self, window: _Window, contract: ConfirmedContract) -> None:
        """Count a token of the window's site; ``turbo_stop_after`` of them end the window."""
        if window.reason is not None or any(k.address == contract.address for k in window.found):
            return
        window.found.append(contract)
        stop_after = self._tuning.turbo_stop_after
        if stop_after and len(window.found) >= stop_after:
            self._end(window, f"found {len(window.found)} confirmed contract(s)")

    def _end(self, window: _Window, reason: str, *, quiet: bool = False) -> None:
        """Ask a window to end with ``reason``; its task sums it up and forgets it."""
        if window.reason is None:
            window.reason = reason
            window.quiet = window.quiet or quiet
        window.changed.set()

    async def _finish(self, window: _Window) -> None:
        """Return the site to its pace, forget the window and send its summary."""
        if window.active:
            self._deactivate(window)
        if window.heard is not None:
            self._tokens.unsubscribe(window.site_url, window.heard)
        if self._windows.get(window.site_url) is window:
            del self._windows[window.site_url]
            self._tasks.pop(window.site_url, None)
        if window.reason != _FORGOTTEN:
            self._spend(window)
        log.info("[%s] auto-turbo finished: %s", window.site_url, window.reason)
        if window.quiet or (window.started_at is None and not window.found):
            return
        seconds = window.fast_seconds if window.started_at is not None else None
        with contextlib.suppress(Exception):
            await self._notifier.auto_turbo_finished(
                window.site_url, window.reason or MANUAL_STOP, tuple(window.found), seconds
            )

    def _spend(self, window: _Window) -> None:
        """Keep the window's launch times from planning another window for a day.

        That covers the ones an earlier launch time took the window over from, too.
        """
        spent = self._spent.setdefault(window.site_url, [])
        until = self._clock() + SPENT_FOR
        for old in [*window.earlier, _Spent(window.source, window.target, window.timed, 0.0)]:
            spent.append(_Spent(old.source, old.at, old.timed, until))

    def _spent_on(self, site_url: str, candidate: LaunchCandidate, now: float) -> bool:
        """True when a window of ``site_url`` for this launch ended less than a day ago.

        A launch time counts as the same within ``MOVED`` seconds, so a postponed launch or a
        countdown to the next sale plans again; so does the time a text showed with the same
        clock a day later, which is a stale text, not a new launch (and stays quiet after).
        A phrase counts as the same when the same page shows it again.
        """
        spent = [entry for entry in self._spent.get(site_url, ()) if entry.until > now]
        if not spent:
            self._spent.pop(site_url, None)
            return False
        self._spent[site_url] = spent
        if not candidate.timed:
            return any(entry.source == candidate.source for entry in spent)
        for entry in spent:
            if not entry.timed:
                continue
            if abs(candidate.target - entry.at) <= MOVED:
                return True
            if entry.source == candidate.source and abs(candidate.target - entry.at - DAY) <= MOVED:
                spent.append(_Spent(entry.source, candidate.target, True, now + SPENT_FOR))
                return True
        return False

    async def _wait(self, window: _Window, seconds: float) -> None:
        """Wait ``seconds``, or less when the window changes (moved, stopped) meanwhile."""
        if self._sleep is None:
            await wait_or_stop(window.changed, seconds)
        else:
            sleeper = asyncio.ensure_future(self._sleep(max(seconds, 0.0)))
            waiter = asyncio.ensure_future(window.changed.wait())
            try:
                await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                sleeper.cancel()
                waiter.cancel()
        window.changed.clear()
