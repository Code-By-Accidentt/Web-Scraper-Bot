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

# A round whose renders keep less than this share of the stored snapshot's nodes (a loading
# skeleton, a half-loaded list) is skipped; the third such round in a row is a real change.
_SMALL_RENDER = 0.6
_SMALL_ROUNDS = 3


def _without_old_deep(
    sightings: tuple[Sighting, ...], deep_seen: frozenset[str] | None, *, recording: bool
) -> tuple[tuple[Sighting, ...], frozenset[str] | None]:
    """Drop the script-only addresses a page already carried when deep scan first saw it.

    Wallet kits and price widgets ship dozens of token addresses in their bundles. On the
    page's first deep-scan round (``recording``) all of them are remembered and left out;
    later only new ones get through. Return the kept sightings and the remembered set.
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
    """Runs the full check of one page; per page it remembers only its small-render streak.

    A change counts only if a second render ``confirm_delay`` seconds later still shows it;
    what differs between the two renders is volatile and stays out of the report and the
    snapshot. Elements that keep changing across rounds are silenced by :class:`NoiseFilter`.
    Renders much smaller than the stored snapshot replace it only after a few rounds.
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
        """Keep the parts the check uses; ``sleep`` and ``clock`` can be swapped in tests."""
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
        self._small: dict[str, int] = {}  # page -> rounds in a row that rendered small

    async def check(
        self,
        page_url: str,
        *,
        load_timeout: float | None = None,
        on_observation: Observer | None = None,
    ) -> PageCheckOutcome:
        """Check one page and return the outcome; ``load_timeout`` (seconds) overrides the default.

        ``on_observation`` gets the first render's addresses and verdicts at once, before any
        comparison, so a token alert never waits for change detection. Later calls carry only
        the addresses the second render adds.
        """
        started = self._clock()
        first = await self._fetcher.fetch(page_url, timeout=load_timeout)
        seen_at = self._clock()
        if first.started_at is not None:
            started = first.started_at
        block = self._blocks.inspect(first)
        if not first.ok:
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
            # A 2xx "challenge" may be the real page with an unlucky title: read what it shows.
            sightings = tuple(shown_on_a_challenge(found))
        if recording and snapshot is not None:
            # Stored before deep scan was on: remember its script addresses from now on.
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
        # A blank 200 (deploy in progress, empty shell) is not a page to compare.
        if not current:
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
            self._small.pop(page_url, None)
            return PageCheckOutcome(
                page_url, PageStatus.UNCHANGED, candidates=candidates, captcha=captcha
            )

        await self._sleep(self._tuning.confirm_delay)
        started_again = self._clock()
        second = await self._fetcher.fetch(page_url, timeout=load_timeout)
        seen_again = self._clock()
        if second.started_at is not None:
            started_again = second.started_at
        block = self._blocks.inspect(second)
        if block is not None and second.ok:
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
        if self._small_round(page_url, snapshot.signature, confirmed):
            stored = len(snapshot.signature)
            reason = f"renders much smaller than the snapshot ({len(confirmed)} vs {stored} nodes)"
            return PageCheckOutcome(
                page_url,
                PageStatus.UNSTABLE,
                error=reason,
                candidates=candidates,
                captcha=captcha,
            )
        # The diffs can take seconds of CPU on big pages; a thread keeps the event loop free.
        volatile = await asyncio.to_thread(self._detector.volatile_nodes, current, confirmed)
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

    def _small_round(self, page_url: str, stored: Signature, render: Signature) -> bool:
        """Return True when this round is skipped: its render is much smaller than the snapshot.

        A loading skeleton must not replace the stored page and be reported as a removal. A
        page that stays that small for ``_SMALL_ROUNDS`` rounds in a row really changed, so
        that round is compared as usual.
        """
        if len(render) >= _SMALL_RENDER * len(stored):
            self._small.pop(page_url, None)
            return False
        rounds = self._small.get(page_url, 0) + 1
        if rounds >= _SMALL_ROUNDS:
            self._small.pop(page_url, None)
            return False
        self._small[page_url] = rounds
        return True

    async def _sightings(self, signature: Signature, result: FetchResult) -> tuple[Sighting, ...]:
        """Return one sighting per address found in a render, searched in a worker thread."""
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
        """Check the sighted addresses and return one verdict each, in the same order."""
        if not sightings:
            return ()
        return tuple(await self._validator.validate_many(s.address for s in sightings))

    @staticmethod
    async def _observe(observer: Observer | None, observation: Observation) -> None:
        """Pass the observation to the observer, if there is one.

        A failing observer is only logged, so it never stops the change detection.
        """
        if observer is None:
            return
        try:
            await observer(observation)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("handling the addresses seen on %s failed", observation.page_url)
