"""Turbo mode: render one site every few seconds to catch a launch as it happens.

Sessions only hunt tokens (change alerts stay with the regular monitor) and announce
only confirmed ones. An address a page shows on its first good render that already
exists is old news. One that does not exist yet, and any later address that is not a
token yet, goes to the pending watch, which checks it at the turbo pace and announces
it once deployed. A session ends on ``stop_after`` tokens, timeout, /turbo stop or a
block.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from scraper_bot.chains.address_extractor import AddressExtractor
from scraper_bot.chains.contract_validators import ContractValidator
from scraper_bot.models import (
    Address,
    BlockSignal,
    ConfirmedContract,
    Sighting,
    Timeline,
    ValidationStatus,
)
from scraper_bot.monitoring.alerts import AlertRegistry
from scraper_bot.monitoring.pending import PendingWatch
from scraper_bot.pages.blocking import BlockDetector
from scraper_bot.pages.fetcher import FetchResult, HttpProber, PageFetcher
from scraper_bot.pages.html_normalizer import HtmlNormalizer
from scraper_bot.pages.sightings import find_sightings
from scraper_bot.pages.urls import SubUrlDeriver, normalize_url
from scraper_bot.preferences import Tuning
from scraper_bot.storage import AddressCache
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)


class TurboError(Exception):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


class TurboAlreadyRunning(TurboError):
    pass


class TurboNotRunning(TurboError):
    pass


class TurboLimitReached(TurboError):
    def __init__(self, url: str, limit: int) -> None:
        super().__init__(url)
        self.limit = limit


@dataclass(frozen=True, slots=True)
class TurboLimits:
    interval: float  # seconds between render cycles
    default_minutes: float
    max_minutes: float
    stop_after: int  # confirmed contracts that end the session; 0 = never
    max_sites: int  # concurrent (running or scheduled) sessions
    max_pages: int  # pages rendered per cycle, the root page included


# A session takes either fixed limits or a callable returning the current ones, so that
# /set changes reach sessions that are already running.
LimitsSource = TurboLimits | Callable[[], TurboLimits]

_MANUAL_STOP = "stopped manually"


@dataclass(slots=True)
class TurboStats:
    started_at: float | None = None
    deadline: float | None = None
    cycles: int = 0
    renders: int = 0
    pending: int = 0  # addresses found after a page's first cycle and newly watched
    found: list[ConfirmedContract] = field(default_factory=list)
    stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduledTurbo:
    url: str
    start_at: float
    minutes: float


class TurboSession:
    """One site rendered in a tight loop until its token appears or the time is up."""

    def __init__(
        self,
        url: str,
        pages: list[str],
        *,
        fetcher: PageFetcher,
        extractor: AddressExtractor,
        validator: ContractValidator,
        cache: AddressCache,
        notifier: Notifier,
        block_detector: BlockDetector,
        limits: LimitsSource,
        duration: float,
        pending: PendingWatch,
        registry: AlertRegistry,
        normalizer: HtmlNormalizer | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._url = url
        self._pages = list(pages)
        self._fetcher = fetcher
        self._extractor = extractor
        self._normalizer = normalizer or HtmlNormalizer()
        self._validator = validator
        self._cache = cache
        self._notifier = notifier
        self._blocks = block_detector
        self._limits_source = limits
        self._duration = duration
        self._pending = pending
        self._registry = registry
        self._clock = clock
        self._sleep = sleep  # tests replace the stop-aware pause with an instant one
        self._stats = TurboStats()
        self._stop = asyncio.Event()
        self._seen: set[Address] = set()
        self._last_read: dict[str, float] = {}  # page -> when the previous cycle began

    @property
    def url(self) -> str:
        return self._url

    @property
    def pages(self) -> list[str]:
        return list(self._pages)

    @property
    def limits(self) -> TurboLimits:
        """Current limits, read live when a callable was given."""
        source = self._limits_source
        return source() if callable(source) else source

    @property
    def stats(self) -> TurboStats:
        return self._stats

    @property
    def pending_count(self) -> int:
        """Addresses of this site the pending watch checks at the turbo pace."""
        return self._pending.count(self._url)

    @property
    def seconds_left(self) -> float:
        if self._stats.deadline is None:
            return self._duration
        return max(self._stats.deadline - self._clock(), 0.0)

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> TurboStats:
        stats, limits = self._stats, self.limits
        stats.started_at = self._clock()
        stats.deadline = stats.started_at + self._duration
        log.info("turbo started for %s (%d pages)", self._url, len(self._pages))
        await self._notifier.turbo_started(
            self._url, len(self._pages), limits.interval, self._duration / 60, limits.stop_after
        )
        reason: str | None = None
        self._pending.start_turbo(self._url, self._deployed)
        try:
            while True:
                if self._stop.is_set():
                    reason = _MANUAL_STOP
                    break
                if self._clock() >= stats.deadline:
                    reason = "time limit reached"
                    break
                cycle_started = time.perf_counter()
                signal = await self._cycle()
                if signal is not None:
                    reason = f"possible IP block ({signal.reason})"
                    break
                limits = self.limits  # /set may have changed the pace or the stop rule
                if limits.stop_after and len(stats.found) >= limits.stop_after:
                    reason = f"found {len(stats.found)} confirmed contract(s)"
                    break
                await self._pause(limits.interval - (time.perf_counter() - cycle_started))
        except asyncio.CancelledError:
            reason = "bot is shutting down"
            raise
        finally:
            self._pending.stop_turbo(self._url, self._deployed)
            stats.stop_reason = reason or _MANUAL_STOP
            log.info("turbo finished for %s: %s", self._url, stats.stop_reason)
            with contextlib.suppress(Exception):
                await self._notifier.turbo_finished(
                    self._url,
                    stats.stop_reason,
                    tuple(stats.found),
                    self._clock() - stats.started_at,
                    stats.renders,
                )
        return stats

    async def _cycle(self) -> BlockSignal | None:
        started = self._clock()
        results = await asyncio.gather(*(self._fetcher.fetch(page) for page in self._pages))
        seen_at = self._clock()
        self._stats.cycles += 1
        self._stats.renders += len(results)
        for result in results:
            signal = self._blocks.inspect(result)
            if signal is not None:
                return signal
        # Pages read before go first: an address a late page shows on its baseline render is
        # then already known from them as news, not taken for old news.
        for result in sorted(results, key=lambda r: r.url not in self._last_read):
            if not result.ok:
                continue
            # A page's first good render is its baseline, even when an earlier cycle failed.
            baseline = result.url not in self._last_read
            fresh = [s for s in await self._sightings(result) if s.address not in self._seen]
            self._seen.update(sighting.address for sighting in fresh)
            # Known tokens (a trending list, an old token, one the regular monitor just
            # announced and the pending watch passed on) are not news for this session.
            fresh = [s for s in fresh if not self._cache.is_known(s.address)]
            if baseline:
                # Already on the page when it was first read: news only if it does not exist.
                for sighting in fresh:
                    self._pending.offer_baseline(self._url, result.url, sighting)
            elif fresh:
                await self._check(result.url, fresh, seen_at)
            # What this cycle read may predate its end, so the next window opens at its start.
            self._last_read[result.url] = result.started_at or started
        return None

    async def _sightings(self, result: FetchResult) -> list[Sighting]:
        signature = await asyncio.to_thread(self._normalizer.signature, result.html)
        return await asyncio.to_thread(
            find_sightings,
            self._extractor,
            signature,
            result.html,
            result.extra_texts,
            result.dom_texts,
        )

    async def _check(self, page_url: str, sightings: Sequence[Sighting], seen_at: float) -> None:
        """A new address: announce it if it is a token, else let the pending watch wait."""
        results = await self._validator.validate_many(
            [s.address for s in sightings], negative_cache=False
        )
        by_address = {sighting.address: sighting for sighting in sightings}
        for result in results:
            sighting = by_address.get(result.via or result.address)
            if result.contract is not None:
                self._deployed(result.contract.address)
                if self._registry.claim(result.contract.address):
                    timeline = Timeline(seen_at, missing_at=self._last_read.get(page_url))
                    await self._notifier.contract_found(
                        result.contract, page_url, sighting=sighting, timeline=timeline
                    )
            elif result.status is ValidationStatus.ALREADY_KNOWN and result.via is None:
                # announced by the regular monitor meanwhile (an old token behind a new LP
                # pair is not this launch)
                self._deployed(result.address)
            elif self._pending.offer(self._url, page_url, sighting, result, fast=True):
                self._stats.pending += 1

    def _deployed(self, address: Address) -> None:
        """A token of this site: found by the session or by the pending watch."""
        if any(contract.address == address for contract in self._stats.found):
            return
        network = self._cache.known_network(address) or "unknown network"
        self._stats.found.append(ConfirmedContract(address, network))

    async def _pause(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(max(seconds, 0.0))
            return
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)


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
        return [
            session
            for url, session in self._sessions.items()
            if url in self._tasks and not self._tasks[url].done()
        ]

    def scheduled(self) -> list[ScheduledTurbo]:
        return [plan for plan, _ in self._scheduled.values()]

    async def start(
        self, raw_url: str, minutes: float | None = None, start_at: float | None = None
    ) -> TurboSession | ScheduledTurbo:
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
        url = normalize_url(raw_url)
        scheduled = self._scheduled.pop(url, None)
        if scheduled is not None:
            scheduled[1].cancel()
            return url
        session, task = self._sessions.get(url), self._tasks.get(url)
        if session is None or task is None or task.done():
            raise TurboNotRunning(url)
        session.request_stop()
        # asyncio.wait never raises for the session's own outcome, and it lets a
        # cancellation of the caller (Ctrl-C during /turbo stop) through.
        await asyncio.wait({task}, timeout=15)
        return url

    async def shutdown(self) -> None:
        tasks = [task for _, task in self._scheduled.values()] + list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduled.clear()

    def _is_busy(self, url: str) -> bool:
        task = self._tasks.get(url)
        return (
            url in self._scheduled
            or url in self._launching
            or (task is not None and not task.done())
        )

    def _reserved_count(self) -> int:
        """Everything that holds a ``max_sites`` slot."""
        return len(self.active()) + len(self._scheduled) + len(self._launching)

    async def _start_later(self, plan: ScheduledTurbo) -> None:
        try:
            await self._sleep(max(plan.start_at - self._clock(), 0.0))
        except asyncio.CancelledError:
            self._scheduled.pop(plan.url, None)
            raise
        # No await between dropping the timer entry and _launch reserving the slot, so a
        # concurrent /turbo cannot slip in.
        self._scheduled.pop(plan.url, None)
        await self._launch(plan.url, plan.minutes * 60)

    async def _launch(self, url: str, duration: float) -> TurboSession:
        # Probing the sub-pages takes a moment; keep the URL reserved meanwhile so a
        # concurrent /turbo can neither duplicate it nor take its slot.
        self._launching.add(url)
        try:
            limits = self.limits
            pages = await self._pick_pages(url, limits.max_pages)
            session = self._factory(url, pages, duration, lambda: self.limits)
            task = asyncio.create_task(session.run(), name=f"turbo:{url}")
            task.add_done_callback(lambda done, u=url: self._on_done(u, done))
            self._sessions[url] = session
            self._tasks[url] = task
            return session
        finally:
            self._launching.discard(url)

    async def _pick_pages(self, url: str, max_pages: int) -> list[str]:
        probes = await asyncio.gather(*(self._prober.probe(p) for p in self._deriver.derive(url)))
        live = [url, *(probe.url for probe in probes if probe.worth_rendering)]
        return live[:max_pages]

    def _on_done(self, url: str, task: asyncio.Task[TurboStats]) -> None:
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
