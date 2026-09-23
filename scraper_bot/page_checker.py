"""One complete check of a single page: render, find contracts, detect changes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from scraper_bot.address_extractor import AddressExtractor
from scraper_bot.blocking import BlockDetector
from scraper_bot.change_detector import ChangeDetector
from scraper_bot.contract_validators import ContractValidator
from scraper_bot.fetcher import FetchResult, PageFetcher
from scraper_bot.html_normalizer import HtmlNormalizer
from scraper_bot.models import (
    Address,
    ConfirmedContract,
    PageCheckOutcome,
    PageStatus,
    Snapshot,
)
from scraper_bot.noise_filter import NoiseFilter
from scraper_bot.preferences import Tuning
from scraper_bot.storage import SnapshotStore

log = logging.getLogger(__name__)


class PageChecker:
    """Stateless orchestration of the per-page pipeline.

    A change is reported only when it survives a second render taken
    ``confirm_delay`` seconds later; whatever differs between the two renders is
    volatile and ignored, and elements that keep changing across rounds are
    silenced by the :class:`NoiseFilter`. The stored snapshot is always advanced
    to the second render so that noise never accumulates. Every outcome also
    carries the syntactically valid addresses seen on the page (``candidates``)
    for the unverified mode, next to the on-chain confirmed ``contracts``.
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

    async def check(self, page_url: str) -> PageCheckOutcome:
        first = await self._fetcher.fetch(page_url)
        block = self._blocks.inspect(first)
        if block is not None:
            # A challenge or ban page must never be diffed against the real snapshot.
            return PageCheckOutcome(
                page_url, PageStatus.UNAVAILABLE, error=block.reason, block=block
            )
        if not first.ok:
            return PageCheckOutcome(page_url, PageStatus.UNAVAILABLE, error=first.reason)

        candidates = self._candidates(first)
        contracts = await self._confirm(candidates)
        current = self._normalizer.signature(first.html)
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
        second = await self._fetcher.fetch(page_url)
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
            )
        more = self._candidates(second)
        candidates = self._merge_addresses(candidates, more)
        contracts = self._merge(contracts, await self._confirm(more))
        confirmed = self._normalizer.signature(second.html)
        if self._detector.is_unstable(current, confirmed):
            reason = f"renders differ in size ({len(current)} vs {len(confirmed)} nodes)"
            return PageCheckOutcome(
                page_url, PageStatus.UNSTABLE, contracts, error=reason, candidates=candidates
            )
        volatile = self._detector.volatile_nodes(current, confirmed)
        report = self._detector.compare(snapshot.signature, confirmed, page_url, ignore=volatile)
        noise = NoiseFilter(self._tuning.flap_window, self._tuning.flap_threshold)
        report, memory = noise.apply(report, snapshot.noise, self._clock())
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
    def _merge(
        first: tuple[ConfirmedContract, ...], second: tuple[ConfirmedContract, ...]
    ) -> tuple[ConfirmedContract, ...]:
        seen = {contract.address for contract in first}
        return first + tuple(c for c in second if c.address not in seen)

    @staticmethod
    def _merge_addresses(
        first: tuple[Address, ...], second: tuple[Address, ...]
    ) -> tuple[Address, ...]:
        seen = set(first)
        return first + tuple(a for a in second if a not in seen)
