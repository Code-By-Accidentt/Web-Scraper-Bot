"""One turbo session: a site rendered every few seconds until its token appears."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from scraper_bot.chains.addresses.extractor import AddressExtractor
from scraper_bot.chains.validator import ContractValidator
from scraper_bot.models.addresses import Address, ConfirmedContract, ValidationStatus
from scraper_bot.models.pages import BlockSignal
from scraper_bot.models.sightings import Sighting, Timeline
from scraper_bot.monitoring.alerts import AlertRegistry
from scraper_bot.monitoring.pending.watch import PendingWatch
from scraper_bot.monitoring.rounds import wait_or_stop
from scraper_bot.pages.changes.normalizer import HtmlNormalizer
from scraper_bot.pages.fetching.blocking import BlockDetector
from scraper_bot.pages.fetching.result import PageFetcher
from scraper_bot.pages.sightings import find_sightings, shown_on_a_challenge
from scraper_bot.storage.address_cache import AddressCache
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurboLimits:
    """The turbo settings in force: the pace, how long sessions last, how many sites and pages."""

    interval: float  # seconds between render cycles
    default_minutes: float
    max_minutes: float
    stop_after: int  # confirmed contracts that end the session; 0 = never
    max_sites: int  # concurrent (running or scheduled) sessions
    max_pages: int  # pages rendered per cycle, the root page included


# Fixed limits, or a callable returning the current ones so /set reaches running sessions.
LimitsSource = TurboLimits | Callable[[], TurboLimits]

MANUAL_STOP = "stopped manually"


@dataclass(slots=True)
class TurboStats:
    """Counters and results of one turbo session."""

    started_at: float | None = None
    deadline: float | None = None
    cycles: int = 0
    renders: int = 0
    pending: int = 0  # addresses found after a page's first cycle and newly watched
    found: list[ConfirmedContract] = field(default_factory=list)
    stop_reason: str | None = None


class TurboSession:
    """One site rendered in a tight loop until its token appears or the time is up.

    Addresses already on a page at its first read go to the pending watch as baselines;
    addresses that show up later are validated at once.
    """

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
        """Set up a session over ``pages`` of ``url`` that lasts ``duration`` seconds once run.

        ``sleep`` replaces the stop-aware pause between cycles (tests pass an instant one).
        """
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
        self._sleep = sleep
        self._stats = TurboStats()
        self._stop = asyncio.Event()
        self._seen: set[Address] = set()
        self._last_read: dict[str, float] = {}  # page -> when its last good render began

    @property
    def url(self) -> str:
        """The site this session renders."""
        return self._url

    @property
    def pages(self) -> list[str]:
        """The pages rendered in every cycle."""
        return list(self._pages)

    @property
    def limits(self) -> TurboLimits:
        """Current limits, read live when a callable was given."""
        source = self._limits_source
        return source() if callable(source) else source

    @property
    def stats(self) -> TurboStats:
        """The counters and results so far."""
        return self._stats

    @property
    def pending_count(self) -> int:
        """Addresses of this site the pending watch checks at the turbo pace."""
        return self._pending.count(self._url)

    @property
    def seconds_left(self) -> float:
        """Seconds until the time limit; the full duration before the session starts."""
        if self._stats.deadline is None:
            return self._duration
        return max(self._stats.deadline - self._clock(), 0.0)

    def request_stop(self) -> None:
        """Ask the session to stop after the current cycle."""
        self._stop.set()

    async def run(self) -> TurboStats:
        """Render the pages in cycles until the session ends, then return its stats.

        It ends on the time limit, a stop request, ``stop_after`` tokens or a block signal.
        The user hears when it starts and ends; meanwhile the pending watch checks the site's
        addresses at the turbo pace.
        """
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
                    reason = MANUAL_STOP
                    break
                if self._clock() >= stats.deadline:
                    reason = "time limit reached"
                    break
                cycle_started = time.perf_counter()
                signal = await self._cycle()
                limits = self.limits  # /set may have changed the pace or the stop rule
                if limits.stop_after and len(stats.found) >= limits.stop_after:
                    reason = f"found {len(stats.found)} confirmed contract(s)"
                    break
                if signal is not None:
                    reason = f"possible IP block ({signal.describe()})"
                    break
                await self._pause(limits.interval - (time.perf_counter() - cycle_started))
        except asyncio.CancelledError:
            reason = "bot is shutting down"
            raise
        finally:
            self._pending.stop_turbo(self._url, self._deployed)
            stats.stop_reason = reason or MANUAL_STOP
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
        """Render every page once, check what they show, and return the first block signal.

        A page's first good render is its baseline: its addresses go to the pending watch as
        baselines. A page that answers normally is read even when another one is blocked in
        the same cycle, since the CA may be on it.
        """
        started = self._clock()
        results = await asyncio.gather(*(self._fetcher.fetch(page) for page in self._pages))
        seen_at = self._clock()
        self._stats.cycles += 1
        self._stats.renders += len(results)
        signals = {result.url: self._blocks.inspect(result) for result in results}
        # Pages read before go first: an address a new page also shows is news, not baseline.
        for result in sorted(results, key=lambda r: r.url not in self._last_read):
            if not result.ok:
                continue
            signature = await asyncio.to_thread(self._normalizer.signature, result.html)
            if not signature:
                continue  # a blank render (hydrating, a deploy in progress) shows nothing yet
            baseline = result.url not in self._last_read
            found = await asyncio.to_thread(
                find_sightings,
                self._extractor,
                signature,
                result.html,
                result.extra_texts,
                result.dom_texts,
            )
            blocked = signals[result.url] is not None
            if blocked:
                # A 2xx that looks like a challenge may be the real page: read its text only.
                found = shown_on_a_challenge(found)
            fresh = [s for s in found if s.address not in self._seen]
            self._seen.update(sighting.address for sighting in fresh)
            # Known tokens (a trending list, one the monitor just announced) are not news here.
            fresh = [s for s in fresh if not self._cache.is_known(s.address)]
            if baseline:
                for sighting in fresh:
                    self._pending.offer_baseline(self._url, result.url, sighting)
            elif fresh:
                await self._check(result.url, fresh, seen_at)
            # A challenge did not show the page, so the next good render is still the baseline.
            if not blocked:
                self._last_read[result.url] = result.started_at or started
        return next((signal for signal in signals.values() if signal is not None), None)

    async def _check(self, page_url: str, sightings: Sequence[Sighting], seen_at: float) -> None:
        """Validate new addresses; announce the tokens and hand the rest to the pending watch."""
        results = await self._validator.validate_many(
            [s.address for s in sightings], negative_cache=False
        )
        by_address = {sighting.address: sighting for sighting in sightings}
        for result in results:
            sighting = by_address.get(result.via or result.address)
            if result.contract is not None:
                claimed = self._registry.claim(result.contract)  # records it as known
                self._deployed(result.contract.address)
                if claimed:
                    timeline = Timeline(seen_at, missing_at=self._last_read.get(page_url))
                    await self._notifier.contract_found(
                        result.contract, page_url, sighting=sighting, timeline=timeline
                    )
            elif result.status is ValidationStatus.ALREADY_KNOWN and result.via is None:
                # the monitor announced it meanwhile (an old token behind a new pair is not news)
                self._deployed(result.address)
            elif self._pending.offer(self._url, page_url, sighting, result, fast=True):
                self._stats.pending += 1

    def _deployed(self, address: Address) -> None:
        """Add a token of this site to the session's finds, once.

        Both the session and the pending watch call this.
        """
        if any(contract.address == address for contract in self._stats.found):
            return
        network = self._cache.known_network(address) or "unknown network"
        self._stats.found.append(ConfirmedContract(address, network))

    async def _pause(self, seconds: float) -> None:
        """Wait ``seconds`` before the next cycle, or less if a stop is requested."""
        if self._sleep is not None:
            await self._sleep(max(seconds, 0.0))
            return
        await wait_or_stop(self._stop, seconds)
