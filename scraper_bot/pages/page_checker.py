"""One complete check of a single page: render, find contracts, detect changes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from scraper_bot.chains.addresses.extractor import AddressExtractor
from scraper_bot.chains.validator import ContractValidator
from scraper_bot.models.addresses import ValidationResult
from scraper_bot.models.pages import PageCheckOutcome, PageStatus, Signature, Snapshot
from scraper_bot.models.sightings import Observation, Sighting, SightingSource
from scraper_bot.pages.changes.detector import ChangeDetector
from scraper_bot.pages.changes.noise import NoiseFilter
from scraper_bot.pages.changes.normalizer import HtmlNormalizer
from scraper_bot.pages.fetching.blocking import BlockDetector
from scraper_bot.pages.fetching.result import FetchResult, PageFetcher
from scraper_bot.pages.sightings import find_sightings, shown_on_a_challenge
from scraper_bot.settings.live import Tuning
from scraper_bot.storage.snapshots import SnapshotStore

log = logging.getLogger(__name__)

Observer = Callable[[Observation], Awaitable[None]]


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
    snapshot advances to the second render without the volatile nodes it did not have,
    so noise never accumulates and a change that landed between the renders is
    reported by the next round.
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
        if not first.ok:
            # A challenge or ban page must never be diffed against the real snapshot.
            return PageCheckOutcome(
                page_url,
                PageStatus.UNAVAILABLE,
                error=block.reason if block is not None else first.reason,
                block=block,
                timed_out=first.timed_out,
            )

        snapshot = self._snapshots.load(page_url)
        current = await asyncio.to_thread(self._normalizer.signature, first.html)
        deep_seen = snapshot.deep_seen if snapshot is not None else None
        found = await self._sightings(current, first)
        recording = False
        if block is None:
            recording = deep_seen is None and bool(first.extra_texts)
            sightings, deep_seen = _without_old_deep(found, deep_seen, recording=recording)
        else:
            # A 2xx answer that looks like a challenge may be the real page with an unlucky
            # title: what it shows is read (a CA must not wait), not its source or scripts.
            sightings = tuple(shown_on_a_challenge(found))
        if recording and snapshot is not None:
            # A page stored before deep scan was on: its deep sources are known from now on.
            snapshot.deep_seen = deep_seen
            self._snapshots.save(page_url, snapshot)
        results = await self._validate(sightings)
        # A challenge or a blank render did not show the page: no appearance window opens.
        read = block is None and bool(current)
        await self._observe(
            on_observation, Observation(page_url, seen_at, sightings, results, started, read)
        )
        candidates = tuple(sighting.address for sighting in sightings)
        if block is not None:
            return PageCheckOutcome(
                page_url,
                PageStatus.UNAVAILABLE,
                error=block.reason,
                block=block,
                candidates=candidates,
            )
        captcha = await asyncio.to_thread(self._blocks.captcha, first.html)
        if not current:
            # A blank 200 (deploy in progress, empty shell) is not a page to compare.
            return PageCheckOutcome(
                page_url,
                PageStatus.UNAVAILABLE,
                error="empty page",
                candidates=candidates,
            )
        if snapshot is None:
            self._snapshots.save(page_url, Snapshot(current, deep_seen=deep_seen))
            return PageCheckOutcome(
                page_url, PageStatus.BASELINE, candidates=candidates, captcha=captcha
            )
        if not self._detector.differs(snapshot.signature, current):
            return PageCheckOutcome(
                page_url, PageStatus.UNCHANGED, candidates=candidates, captcha=captcha
            )

        # Something differs: render again to separate real edits from volatile parts.
        await self._sleep(self._tuning.confirm_delay)
        started_again = self._clock()
        second = await self._fetcher.fetch(page_url, timeout=load_timeout)
        seen_again = self._clock()
        if second.started_at is not None:
            started_again = second.started_at
        block = self._blocks.inspect(second)
        if block is not None and second.ok:
            # Like the first render: what a 2xx challenge-looking page shows is read.
            signature = await asyncio.to_thread(self._normalizer.signature, second.html)
            shown = shown_on_a_challenge(await self._sightings(signature, second))
            new = tuple(s for s in shown if s.address not in set(candidates))
            if new:
                more = await self._validate(new)
                observation = Observation(
                    page_url, seen_again, new, more, started_again, read=False
                )
                await self._observe(on_observation, observation)
                candidates += tuple(sighting.address for sighting in new)
        if block is not None or not second.ok:
            reason = block.reason if block is not None else second.reason
            return PageCheckOutcome(
                page_url,
                PageStatus.UNAVAILABLE,
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
            observation = Observation(
                page_url, seen_again, new, more, started_again, read=bool(confirmed)
            )
            await self._observe(on_observation, observation)
            candidates += tuple(sighting.address for sighting in new)
        if not confirmed:
            return PageCheckOutcome(
                page_url,
                PageStatus.UNAVAILABLE,
                error="empty page",
                candidates=candidates,
            )
        if self._detector.is_unstable(current, confirmed):
            reason = f"renders differ in size ({len(current)} vs {len(confirmed)} nodes)"
            return PageCheckOutcome(
                page_url,
                PageStatus.UNSTABLE,
                error=reason,
                candidates=candidates,
                captcha=captcha,
            )
        # The diffs are pure CPU work that can take seconds on pages with thousands of
        # look-alike nodes; a worker thread keeps the other monitors and Telegram alive.
        volatile = await asyncio.to_thread(self._detector.volatile_nodes, current, confirmed)
        # The snapshot keeps only confirmed content: a change that landed between the two
        # renders (volatile now) is reported by the next round, if it lasts.
        report, accepted = await asyncio.to_thread(
            self._detector.diff, snapshot.signature, confirmed, page_url, ignore=volatile
        )
        noise = NoiseFilter(self._tuning.flap_window, self._tuning.flap_threshold)
        report, memory = await asyncio.to_thread(
            noise.apply,
            report,
            snapshot.noise,
            self._clock(),
            before=snapshot.signature,
            after=accepted,
        )
        self._snapshots.save(page_url, Snapshot(accepted, memory, deep_seen))
        if report.is_empty:
            return PageCheckOutcome(
                page_url, PageStatus.UNCHANGED, candidates=candidates, captcha=captcha
            )
        return PageCheckOutcome(
            page_url,
            PageStatus.CHANGED,
            report=report,
            candidates=candidates,
            captcha=captcha,
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
