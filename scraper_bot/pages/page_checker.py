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
    Address,
    ConfirmedContract,
    PageCheckOutcome,
    PageStatus,
    Snapshot,
)
from scraper_bot.pages.blocking import BlockDetector
from scraper_bot.pages.change_detector import ChangeDetector
from scraper_bot.pages.fetcher import FetchResult, PageFetcher
from scraper_bot.pages.html_normalizer import HtmlNormalizer
from scraper_bot.pages.noise_filter import NoiseFilter
from scraper_bot.preferences import Tuning
from scraper_bot.storage import SnapshotStore

log = logging.getLogger(__name__)

T = TypeVar("T")


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

    async def check(self, page_url: str, *, load_timeout: float | None = None) -> PageCheckOutcome:
        """Check one page; ``load_timeout`` replaces the configured load timeout (seconds)."""
        first = await self._fetcher.fetch(page_url, timeout=load_timeout)
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

        candidates = self._candidates(first)
        contracts = await self._confirm(candidates)
        current = self._normalizer.signature(first.html)
        if not current:
            # A blank 200 (deploy in progress, empty shell) is not a page to compare.
            return PageCheckOutcome(
                page_url,
                PageStatus.UNAVAILABLE,
                contracts,
                error="empty page",
                candidates=candidates,
            )
        snapshot = self._snapshots.load(page_url)
        if snapshot is None:
            self._snapshots.save(page_url, Snapshot(current))
            return PageCheckOutcome(page_url, PageStatus.BASELINE, contracts, candidates=candidates)
        if not self._detector.differs(snapshot.signature, current):
            return PageCheckOutcome(
                page_url, PageStatus.UNCHANGED, contracts, candidates=candidates
            )

        # Something differs: render again to separate real edits from volatile parts.
        await self._sleep(self._tuning.confirm_delay)
        second = await self._fetcher.fetch(page_url, timeout=load_timeout)
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
        more = self._candidates(second)
        candidates = self._unique(candidates, more, key=lambda address: address)
        contracts = self._unique(contracts, await self._confirm(more), key=lambda c: c.address)
        confirmed = self._normalizer.signature(second.html)
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
        self._snapshots.save(page_url, Snapshot(confirmed, memory))
        if report.is_empty:
            return PageCheckOutcome(
                page_url, PageStatus.UNCHANGED, contracts, candidates=candidates
            )
        return PageCheckOutcome(
            page_url, PageStatus.CHANGED, contracts, report=report, candidates=candidates
        )

    def _candidates(self, result: FetchResult) -> tuple[Address, ...]:
        return tuple(self._extractor.extract_many(result.scan_texts))

    async def _confirm(self, candidates: tuple[Address, ...]) -> tuple[ConfirmedContract, ...]:
        if not candidates:
            return ()
        results = await self._validator.validate_many(candidates)
        return tuple(r.contract for r in results if r.contract is not None)

    @staticmethod
    def _unique(
        first: tuple[T, ...], second: tuple[T, ...], *, key: Callable[[T], object]
    ) -> tuple[T, ...]:
        """``first`` followed by the items of ``second`` whose key is new."""
        seen = {key(item) for item in first}
        return first + tuple(item for item in second if key(item) not in seen)
