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
from scraper_bot.monitoring.alerts import AlertRegistry, Announcer
from scraper_bot.monitoring.pace import TURBO, SitePace
from scraper_bot.monitoring.pending.watch import PendingWatch
from scraper_bot.monitoring.rounds import wait_or_stop
from scraper_bot.monitoring.tokens import SiteTokens
from scraper_bot.pages.changes.normalizer import HtmlNormalizer
from scraper_bot.pages.fetching.blocking import BlockDetector
from scraper_bot.pages.fetching.result import FetchResult, PageFetcher
from scraper_bot.pages.sightings import find_sightings, shown_on_a_challenge
from scraper_bot.pages.verdicts import VerdictBatch, verdicts
from scraper_bot.storage.address_cache import AddressCache
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurboLimits:
    """The turbo settings in force: the pace, how long sessions last, how many sites and pages."""

    interval: float  # seconds between render cycles
    default_minutes: float
    max_minutes: float
    stop_after: int  # confirmed contracts that end the session (0 = never)
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
        pace: SitePace | None = None,
        tokens: SiteTokens | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Set up a session over ``pages`` of ``url`` that lasts ``duration`` seconds once run.

        While it runs, ``pace`` has the site's light polls and pending checks sped up, and
        ``tokens`` brings the site's tokens that others announced.
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
        self._pace = pace or SitePace()
        self._tokens = tokens or SiteTokens()
        self._announcer = Announcer(registry, notifier, self._tokens, pending)
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
        The user hears when it starts and ends (a crash is named in the summary too);
        meanwhile the pending watch checks the site's addresses at the turbo pace.
        """
        stats, limits = self._stats, self.limits
        stats.started_at = self._clock()
        stats.deadline = stats.started_at + self._duration
        log.info("turbo started for %s (%d pages)", self._url, len(self._pages))
        await self._notifier.turbo_started(
            self._url, len(self._pages), limits.interval, self._duration / 60, limits.stop_after
        )
        reason: str | None = None
        self._pace.boost(self._url, TURBO)
        self._tokens.subscribe(self._url, self._found)
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
        except Exception as exc:
            reason = f"crashed ({type(exc).__name__})"
            raise
        finally:
            self._pace.release(self._url, TURBO)
            self._tokens.unsubscribe(self._url, self._found)
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
        the same cycle, since the CA may be on it. Once every page has its baseline, each is
        read as soon as its render ends, not after the slowest one.
        """
        started = self._clock()
        fetches = [asyncio.create_task(self._fetch(page)) for page in self._pages]
        signals: dict[str, BlockSignal | None] = {}
        try:
            if all(page in self._last_read for page in self._pages):
                for fetch in asyncio.as_completed(fetches):
                    result = await fetch
                    signals[result.url] = await self._read(result, started, self._clock())
            else:
                results = await asyncio.gather(*fetches)
                seen_at = self._clock()
                # Pages read before go first, so an address a new page also shows is news,
                # not a baseline.
                for result in sorted(results, key=lambda r: r.url not in self._last_read):
                    signals[result.url] = await self._read(result, started, seen_at)
        finally:
            for fetch in fetches:
                fetch.cancel()
        self._stats.cycles += 1
        # A server that is down (a deploy at launch time) isn't a block: the session goes on.
        return next(
            (s for page in self._pages if (s := signals.get(page)) is not None and not s.outage),
            None,
        )

    async def _fetch(self, page_url: str) -> FetchResult:
        """Render one page; a render that raises (say, the browser is gone) counts as failed."""
        try:
            return await self._fetcher.fetch(page_url)
        except Exception as exc:
            log.warning("[%s] turbo render of %s failed: %r", self._url, page_url, exc)
            return FetchResult(page_url, None, error=f"{type(exc).__name__}: {exc}")

    async def _read(
        self, result: FetchResult, started: float, seen_at: float
    ) -> BlockSignal | None:
        """Check what one render shows and return its block signal, if any.

        ``started`` is when the cycle began, and ``seen_at`` when the render was read.
        """
        self._stats.renders += 1
        signal = self._blocks.inspect(result)
        if not result.ok:
            return signal
        signature = await asyncio.to_thread(self._normalizer.signature, result.html)
        if not signature:
            return signal  # a blank render (hydrating, a deploy in progress) shows nothing yet
        baseline = result.url not in self._last_read
        found = await asyncio.to_thread(
            find_sightings,
            self._extractor,
            signature,
            result.html,
            result.extra_texts,
            result.dom_texts,
        )
        if signal is not None:
            # A 2xx that looks like a challenge may be the real page, so read only its text.
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
        if signal is None:
            self._last_read[result.url] = result.started_at or started
        return signal

    async def _check(self, page_url: str, sightings: Sequence[Sighting], seen_at: float) -> None:
        """Validate new addresses and act on their verdicts as they come, a token's at once."""
        found = verdicts(self._validator, [s.address for s in sightings], negative_cache=False)
        async with contextlib.aclosing(found):
            async for batch in found:
                await self._take(page_url, sightings, batch, seen_at)

    async def _take(
        self, page_url: str, sightings: Sequence[Sighting], batch: VerdictBatch, seen_at: float
    ) -> None:
        """Announce the tokens among a batch of verdicts; hand the rest to the pending watch."""
        by_address = {sighting.address: sighting for sighting in sightings}
        for result in batch.results:
            sighting = by_address.get(result.via or result.address)
            contract = result.contract
            if contract is not None:
                await self._announcer.announce(
                    self._url,
                    [contract],
                    lambda fresh, seen=sighting: self._notifier.contract_found(
                        fresh[0],
                        page_url,
                        sighting=seen,
                        timeline=Timeline(seen_at, missing_at=self._last_read.get(page_url)),
                    ),
                )
            elif result.status is ValidationStatus.ALREADY_KNOWN and result.via is None:
                # the monitor announced it in the meantime
                # (an old token behind a new pair isn't news)
                network = self._cache.known_network(result.address)
                self._tokens.publish({self._url}, result.address, network)
            else:
                self._pending.offer(self._url, page_url, sighting, result, fast=True)

    def _found(self, contract: ConfirmedContract) -> None:
        """Add a token of this site to the session's finds, once.

        It comes from the session itself, the regular monitor or the pending watch.
        """
        if all(known.address != contract.address for known in self._stats.found):
            self._stats.found.append(contract)

    async def _pause(self, seconds: float) -> None:
        """Wait ``seconds`` before the next cycle, or less if a stop is requested."""
        if self._sleep is not None:
            await self._sleep(max(seconds, 0.0))
            return
        await wait_or_stop(self._stop, seconds)
