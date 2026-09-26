"""One complete check of a single page: render, find contracts, detect changes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from scraper_bot.chains.address_extractor import AddressExtractor
from scraper_bot.chains.contract_validators import ContractValidator
from scraper_bot.models import (
    ConfirmedContract,
    Observation,
    PageCheckOutcome,
    PageStatus,
    Sighting,
    SightingSource,
    Signature,
    Snapshot,
    ValidationResult,
)
from scraper_bot.pages.blocking import BlockDetector
from scraper_bot.pages.change_detector import ChangeDetector
from scraper_bot.pages.fetcher import FetchResult, PageFetcher
from scraper_bot.pages.html_normalizer import HtmlNormalizer
from scraper_bot.pages.noise_filter import NoiseFilter
from scraper_bot.pages.sightings import find_sightings
from scraper_bot.preferences import Tuning
from scraper_bot.storage import SnapshotStore

log = logging.getLogger(__name__)

T = TypeVar("T")
Observer = Callable[[Observation], Awaitable[None]]


def _contracts(results: tuple[ValidationResult, ...]) -> tuple[ConfirmedContract, ...]:
    return tuple(result.contract for result in results if result.contract is not None)


def _without_old_deep(
    sightings: tuple[Sighting, ...], deep_seen: frozenset[str] | None, *, recording: bool
) -> tuple[tuple[Sighting, ...], frozenset[str] | None]:
    """Leave out the addresses a page's scripts and JSON carried when deep scan first saw it.

    Wallet kits and price widgets ship dozens of token addresses in their bundles; none
    of them is a launch. While ``recording`` (the page's first round with deep scan) every
    address found only there is remembered and left out; afterwards only new ones get
    through. An address that also shows in the page itself is never left out.
    """
    deep = frozenset(s.address.value for s in sightings if s.source is SightingSource.SCRIPT)
    if recording:
        kept = tuple(s for s in sightings if s.source is not SightingSource.SCRIPT)
        return kept, (deep_seen or frozenset()) | deep
    if not deep or not deep_seen:
        return sightings, deep_seen
    kept = tuple(
        s
        for s in sightings
        if s.source is not SightingSource.SCRIPT or s.address.value not in deep_seen
    )
    return kept, deep_seen


class PageChecker:
    """Runs the per-page pipeline; keeps no per-page state.

    A change counts only when it survives a second render taken ``confirm_delay``
    seconds later: whatever differs between the two renders is volatile, elements
    that keep changing across rounds are silenced by :class:`NoiseFilter`, and the
    snapshot always advances to the second render so noise never accumulates.
    """

    def __init__(
        self,
        *,
        fetcher: PageFetcher,
        normalizer: HtmlNormalizer,
        detector: ChangeDetector,
        extractor: AddressExtractor,
        validator: ContractValidator,
        snapshots: SnapshotStore,
        tuning: Tuning,
        block_detector: BlockDetector | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetcher = fetcher
        self._normalizer = normalizer
        self._detector = detector
        self._extractor = extractor
        self._validator = validator
        self._snapshots = snapshots
        self._tuning = tuning
        self._blocks = block_detector or BlockDetector()
        self._sleep = sleep
        self._clock = clock

    async def check(
        self,
        page_url: str,
        *,
        load_timeout: float | None = None,
        on_observation: Observer | None = None,
    ) -> PageCheckOutcome:
        """Check one page; ``load_timeout`` replaces the configured load timeout (seconds).

        ``on_observation`` gets the first render's addresses and verdicts as soon as they are
        known, before the comparison and any second render, so a token alert never waits for
        change detection. Later calls carry only the addresses the confirming render adds.
        """
        started = self._clock()
        first = await self._fetcher.fetch(page_url, timeout=load_timeout)
        seen_at = self._clock()
        if first.started_at is not None:
            started = first.started_at  # after the wait for a free tab
        block = self._blocks.inspect(first)
        if block is not None:
            # A challenge or ban page must never be diffed against the real snapshot.
            return PageCheckOutcome(
                page_url, PageStatus.UNAVAILABLE, error=block.reason, block=block
            )
        if not first.ok:
            return PageCheckOutcome(
                page_url, PageStatus.UNAVAILABLE, error=first.reason, timed_out=first.timed_out
            )

        snapshot = self._snapshots.load(page_url)
        current = await asyncio.to_thread(self._normalizer.signature, first.html)
        deep_seen = snapshot.deep_seen if snapshot is not None else None
        recording = deep_seen is None and bool(first.extra_texts)
        sightings, deep_seen = _without_old_deep(
            await self._sightings(current, first), deep_seen, recording=recording
        )
        if recording and snapshot is not None:
            # A page stored before deep scan was on: its deep sources are known from now on.
            snapshot.deep_seen = deep_seen
            self._snapshots.save(page_url, snapshot)
        results = await self._validate(sightings)
        await self._observe(
            on_observation, Observation(page_url, seen_at, sightings, results, started)
        )
        contracts = _contracts(results)
        candidates = tuple(sighting.address for sighting in sightings)
        if not current:
            # A blank 200 (deploy in progress, empty shell) is not a page to compare.
            return PageCheckOutcome(
                page_url,
                PageStatus.UNAVAILABLE,
                contracts,
                error="empty page",
                candidates=candidates,
            )
        if snapshot is None:
            self._snapshots.save(page_url, Snapshot(current, deep_seen=deep_seen))
            return PageCheckOutcome(page_url, PageStatus.BASELINE, contracts, candidates=candidates)
        if not self._detector.differs(snapshot.signature, current):
            return PageCheckOutcome(
                page_url, PageStatus.UNCHANGED, contracts, candidates=candidates
            )

        # Something differs: render again to separate real edits from volatile parts.
        await self._sleep(self._tuning.confirm_delay)
        started_again = self._clock()
        second = await self._fetcher.fetch(page_url, timeout=load_timeout)
        seen_again = self._clock()
        if second.started_at is not None:
            started_again = second.started_at
        block = self._blocks.inspect(second)
        if block is not None or not second.ok:
            reason = block.reason if block is not None else second.reason
            return PageCheckOutcome(
                page_url,
                PageStatus.UNAVAILABLE,
                contracts,
                error=reason,
                block=block,
                candidates=candidates,
                timed_out=second.timed_out,
            )
        confirmed = await asyncio.to_thread(self._normalizer.signature, second.html)
        seen = set(candidates)
        again, deep_seen = _without_old_deep(
            await self._sightings(confirmed, second), deep_seen, recording=recording
        )
        new = tuple(s for s in again if s.address not in seen)
        if new:
            more = await self._validate(new)
            await self._observe(
                on_observation, Observation(page_url, seen_again, new, more, started_again)
            )
            candidates += tuple(sighting.address for sighting in new)
            contracts = self._unique(contracts, _contracts(more), key=lambda c: c.address)
        if not confirmed:
            return PageCheckOutcome(
                page_url,
                PageStatus.UNAVAILABLE,
                contracts,
                error="empty page",
                candidates=candidates,
            )
        if self._detector.is_unstable(current, confirmed):
            reason = f"renders differ in size ({len(current)} vs {len(confirmed)} nodes)"
            return PageCheckOutcome(
                page_url, PageStatus.UNSTABLE, contracts, error=reason, candidates=candidates
            )
        # The diffs are pure CPU work that can take seconds on pages with thousands of
        # look-alike nodes; a worker thread keeps the other monitors and Telegram alive.
        volatile = await asyncio.to_thread(self._detector.volatile_nodes, current, confirmed)
        report = await asyncio.to_thread(
            self._detector.compare, snapshot.signature, confirmed, page_url, ignore=volatile
        )
        noise = NoiseFilter(self._tuning.flap_window, self._tuning.flap_threshold)
        report, memory = noise.apply(
            report, snapshot.noise, self._clock(), before=snapshot.signature, after=confirmed
        )
        self._snapshots.save(page_url, Snapshot(confirmed, memory, deep_seen))
        if report.is_empty:
            return PageCheckOutcome(
                page_url, PageStatus.UNCHANGED, contracts, candidates=candidates
            )
        return PageCheckOutcome(
            page_url, PageStatus.CHANGED, contracts, report=report, candidates=candidates
        )

    async def _sightings(self, signature: Signature, result: FetchResult) -> tuple[Sighting, ...]:
        found = await asyncio.to_thread(
            find_sightings,
            self._extractor,
            signature,
            result.html,
            result.extra_texts,
            result.dom_texts,
        )
        return tuple(found)

    async def _validate(self, sightings: tuple[Sighting, ...]) -> tuple[ValidationResult, ...]:
        if not sightings:
            return ()
        return tuple(await self._validator.validate_many(s.address for s in sightings))

    @staticmethod
    async def _observe(observer: Observer | None, observation: Observation) -> None:
        """Tell the observer; its failure must not cost the change detection."""
        if observer is None:
            return
        try:
            await observer(observation)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("handling the addresses seen on %s failed", observation.page_url)

    @staticmethod
    def _unique(
        first: tuple[T, ...], second: tuple[T, ...], *, key: Callable[[T], object]
    ) -> tuple[T, ...]:
        """``first`` followed by the items of ``second`` whose key is new."""
        seen = {key(item) for item in first}
        return first + tuple(item for item in second if key(item) not in seen)
