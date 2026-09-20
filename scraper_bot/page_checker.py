"""One complete check of a single page: render, find contracts, detect changes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from scraper_bot.address_extractor import AddressExtractor
from scraper_bot.change_detector import ChangeDetector
from scraper_bot.contract_validators import ContractValidator
from scraper_bot.fetcher import PageFetcher
from scraper_bot.html_normalizer import HtmlNormalizer
from scraper_bot.models import ConfirmedContract, PageCheckOutcome, PageStatus
from scraper_bot.storage import SnapshotStore

log = logging.getLogger(__name__)


class PageChecker:
    """Stateless orchestration of the per-page pipeline.

    A change is reported only when it survives a second render taken
    ``confirm_delay`` seconds later; whatever differs between the two renders is
    volatile and ignored. The stored snapshot is always advanced to the second
    render so that noise never accumulates.
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
        confirm_delay: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._fetcher = fetcher
        self._normalizer = normalizer
        self._detector = detector
        self._extractor = extractor
        self._validator = validator
        self._snapshots = snapshots
        self._confirm_delay = confirm_delay
        self._sleep = sleep

    async def check(self, page_url: str) -> PageCheckOutcome:
        first = await self._fetcher.fetch(page_url)
        if not first.ok:
            return PageCheckOutcome(page_url, PageStatus.UNAVAILABLE, error=first.reason)

        contracts = await self._confirm_contracts(first.html)
        current = self._normalizer.signature(first.html)
        previous = self._snapshots.load(page_url)
        if previous is None:
            self._snapshots.save(page_url, current)
            return PageCheckOutcome(page_url, PageStatus.BASELINE, contracts)
        if not self._detector.differs(previous, current):
            return PageCheckOutcome(page_url, PageStatus.UNCHANGED, contracts)

        # Something differs: render again to separate real edits from volatile parts.
        await self._sleep(self._confirm_delay)
        second = await self._fetcher.fetch(page_url)
        if not second.ok:
            return PageCheckOutcome(
                page_url, PageStatus.UNAVAILABLE, contracts, error=second.reason
            )
        contracts = self._merge(contracts, await self._confirm_contracts(second.html))
        confirmed = self._normalizer.signature(second.html)
        if self._detector.is_unstable(current, confirmed):
            reason = f"renders differ in size ({len(current)} vs {len(confirmed)} nodes)"
            return PageCheckOutcome(page_url, PageStatus.UNSTABLE, contracts, error=reason)
        volatile = self._detector.volatile_nodes(current, confirmed)
        report = self._detector.compare(previous, confirmed, page_url, ignore=volatile)
        self._snapshots.save(page_url, confirmed)
        if report.is_empty:
            return PageCheckOutcome(page_url, PageStatus.UNCHANGED, contracts)
        return PageCheckOutcome(page_url, PageStatus.CHANGED, contracts, report=report)

    async def _confirm_contracts(self, html: str) -> tuple[ConfirmedContract, ...]:
        candidates = self._extractor.extract(html)
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
