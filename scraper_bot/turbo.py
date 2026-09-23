"""Turbo mode: render one site every few seconds to catch a launch the moment it happens.

A :class:`TurboSession` renders the root page plus a few live sub-pages every
``interval`` seconds and only looks for addresses (change alerts stay with the
regular monitor). Every address that shows up after the first cycle is announced
immediately as *unverified* and re-checked on-chain until it is confirmed; the
session ends after ``stop_after`` confirmed contracts, when the time is up, on a
manual stop, or when the site starts refusing us.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from scraper_bot.address_extractor import AddressExtractor
from scraper_bot.blocking import BlockDetector
from scraper_bot.contract_validators import ContractValidator
from scraper_bot.fetcher import HttpProber, PageFetcher
from scraper_bot.models import Address, BlockSignal, ConfirmedContract, ValidationStatus
from scraper_bot.notifier import Notifier
from scraper_bot.preferences import Tuning
from scraper_bot.storage import AddressCache
from scraper_bot.urls import SubUrlDeriver, normalize_url

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
    recheck_interval: float  # seconds between on-chain re-checks of unverified addresses
    max_watch: int = 50  # unverified addresses tracked per session


# A session takes either fixed limits or a callable returning the current ones, so that
# /set changes reach sessions that are already running.
LimitsSource = TurboLimits | Callable[[], TurboLimits]


@dataclass(slots=True)
class TurboStats:
    started_at: float | None = None
    deadline: float | None = None
    cycles: int = 0
    renders: int = 0
    unverified: int = 0
    found: list[ConfirmedContract] = field(default_factory=list)
    stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduledTurbo:
    url: str
    start_at: float
    minutes: float


class TurboSession:
    """One site rendered in a tight loop until a contract appears or the time is up."""

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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._url = url
        self._pages = list(pages)
        self._fetcher = fetcher
        self._extractor = extractor
        self._validator = validator
        self._cache = cache
        self._notifier = notifier
        self._blocks = block_detector
        self._limits_source = limits
        self._duration = duration
        self._clock = clock
        self._stats = TurboStats()
        self._stop = asyncio.Event()
        self._stop_reason = "stopped manually"
        self._seen: set[Address] = set()
        self._watch: dict[Address, float] = {}  # address -> last on-chain check (0 = never)
        self._page_of: dict[Address, str] = {}

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
    def seconds_left(self) -> float:
        if self._stats.deadline is None:
            return self._duration
        return max(self._stats.deadline - self._clock(), 0.0)

    def request_stop(self, reason: str = "stopped manually") -> None:
        self._stop_reason = reason
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
        try:
            while True:
                if self._stop.is_set():
                    reason = self._stop_reason
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
            stats.stop_reason = reason or self._stop_reason
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

    # ── internals ───────────────────────────────────────────────────────────
    async def _cycle(self) -> BlockSignal | None:
        results = await asyncio.gather(*(self._fetcher.fetch(page) for page in self._pages))
        self._stats.cycles += 1
        self._stats.renders += len(results)
        for result in results:
            signal = self._blocks.inspect(result)
            if signal is not None:
                return signal
        baseline = self._stats.cycles == 1
        for result in results:
            if not result.ok:
                continue
            for address in self._extractor.extract_many(result.scan_texts):
                await self._consider(address, result.url, baseline)
        await self._recheck()
        return None

    async def _consider(self, address: Address, page_url: str, baseline: bool) -> None:
        """Track an address seen on a page; announce it unless it was there from the start."""
        if address in self._seen or self._cache.is_known(address):
            return
        self._seen.add(address)
        if len(self._watch) >= self.limits.max_watch:
            log.warning("turbo %s: watch list is full, ignoring %s", self._url, address)
            return
        self._watch[address] = 0.0
        self._page_of[address] = page_url
        if not baseline:
            self._stats.unverified += 1
            await self._notifier.unverified_address(address, page_url, self.limits.recheck_interval)

    async def _recheck(self) -> None:
        now = self._clock()
        due = [
            address
            for address, last in self._watch.items()
            if last == 0.0 or now - last >= self.limits.recheck_interval
        ]
        if not due:
            return
        for result in await self._validator.validate_many(due, negative_cache=False):
            address = result.address
            if result.status is ValidationStatus.CONFIRMED and result.contract is not None:
                self._watch.pop(address, None)
                self._stats.found.append(result.contract)
                await self._notifier.address_confirmed(
                    result.contract, self._page_of.get(address, self._url)
                )
            elif result.status is ValidationStatus.ALREADY_KNOWN:
                # Confirmed (and announced) by the regular monitor in the meantime.
                self._watch.pop(address, None)
                network = self._cache.known_network(address) or "unknown network"
                self._stats.found.append(ConfirmedContract(address, network))
            else:
                self._watch[address] = now

    async def _pause(self, seconds: float) -> None:
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
        self._background: set[asyncio.Task[object]] = set()
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
            recheck_interval=tuning.turbo_recheck_interval,
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

    async def stop(self, raw_url: str, reason: str = "stopped manually") -> str:
        url = normalize_url(raw_url)
        scheduled = self._scheduled.pop(url, None)
        if scheduled is not None:
            scheduled[1].cancel()
            return url
        session, task = self._sessions.get(url), self._tasks.get(url)
        if session is None or task is None or task.done():
            raise TurboNotRunning(url)
        session.request_stop(reason)
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=15)
        return url

    async def shutdown(self) -> None:
        tasks = [task for _, task in self._scheduled.values()] + list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduled.clear()

    # ── internals ───────────────────────────────────────────────────────────
    def _is_busy(self, url: str) -> bool:
        task = self._tasks.get(url)
        return (
            url in self._scheduled
            or url in self._launching
            or (task is not None and not task.done())
        )

    def _reserved_count(self) -> int:
        """Sessions that occupy a slot: running, waiting for their start time, or launching."""
        return len(self.active()) + len(self._scheduled) + len(self._launching)

    async def _start_later(self, plan: ScheduledTurbo) -> None:
        try:
            await self._sleep(max(plan.start_at - self._clock(), 0.0))
        except asyncio.CancelledError:
            self._scheduled.pop(plan.url, None)
            raise
        # No await between dropping the timer entry and reserving the launch slot.
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
        if task.cancelled() or task.exception() is None:
            return
        error = task.exception()
        log.error("turbo session for %s crashed: %r", url, error, exc_info=error)
        if self._notifier is not None:
            report = asyncio.ensure_future(
                self._notifier.problem(
                    f"turbo:{url}",
                    f"Turbo session for {url} crashed",
                    f"{type(error).__name__}: {error}",
                    hint="The session is over; start it again with /turbo when the cause is fixed.",
                )
            )
            self._background.add(report)
            report.add_done_callback(self._background.discard)
