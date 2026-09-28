"""The monitoring loop of one root site and its sub-pages."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from itertools import islice

from scraper_bot.models.addresses import Address, ConfirmedContract, ValidationResult
from scraper_bot.models.pages import BlockSignal, PageCheckOutcome, PageStatus
from scraper_bot.models.sightings import Observation, Sighting, Timeline
from scraper_bot.models.sites import LastChange, SiteState
from scraper_bot.monitoring.alerts import AlertRegistry
from scraper_bot.monitoring.pending.watch import PendingWatch
from scraper_bot.monitoring.rounds import live_pages, wait_or_stop
from scraper_bot.monitoring.unverified import UnverifiedWatcher
from scraper_bot.pages.fetching.blocking import BlockDetector
from scraper_bot.pages.fetching.prober import HttpProber
from scraper_bot.pages.page_checker import PageChecker
from scraper_bot.pages.urls import SubUrlDeriver
from scraper_bot.settings.live import Tuning
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)

PersistCallback = Callable[[], None]
AutoPauseCallback = Callable[[str, str], Awaitable[None]]


@dataclass(slots=True)
class MonitorStats:
    """Live counters of one monitor, shown by /status and never saved."""

    rounds: int = 0
    last_round_at: float | None = None
    last_round_seconds: float = 0.0
    last_pages: int = 0
    block_level: int = 0  # each level doubles the pause between rounds (up to block_max_backoff)
    block_reason: str | None = None
    blocked_since: float | None = None
    retry_after: float | None = None
    timing_out: int = 0  # pages the user was warned about for repeated load timeouts

    @property
    def blocked(self) -> bool:
        """True while the site seems to block the bot."""
        return self.block_level > 0


@dataclass(slots=True)
class _PageHealth:
    """Load timeouts in a row of one page, and how far its load timeout was stretched."""

    timeouts: int = 0
    factor: int = 1  # page_load_timeout is multiplied by this, up to _MAX_TIMEOUT_FACTOR
    warned: bool = False


_MAX_BLOCK_LEVEL = 8
# Blocked rounds in a row that a refusal to one light request spares from rendering.
_LIGHT_SKIPS = 3
_MAX_TIMEOUT_FACTOR = 3
_FIRST_SEEN_MEMORY = 20_000  # (page, address) pairs remembered for "on page since"


class SiteMonitor:
    """Runs rounds of checks for one site until asked to stop.

    Each round checks the live pages concurrently and announces tokens as soon as a render
    shows them. Tuning is re-read every round, so /set changes apply at once.
    """

    def __init__(
        self,
        site: SiteState,
        *,
        checker: PageChecker,
        prober: HttpProber,
        deriver: SubUrlDeriver,
        notifier: Notifier,
        persist: PersistCallback,
        on_auto_pause: AutoPauseCallback,
        tuning: Tuning,
        watcher: UnverifiedWatcher | None = None,
        registry: AlertRegistry | None = None,
        pending: PendingWatch | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        page_load_timeout: float = 30.0,
        block_detector: BlockDetector | None = None,
    ) -> None:
        """Set up the monitor of ``site``; nothing runs until :meth:`run`.

        ``sleep`` replaces the stop-aware wait between rounds (tests pass an instant one).
        """
        self._site = site
        self._checker = checker
        self._prober = prober
        self._deriver = deriver
        self._notifier = notifier
        self._persist = persist
        self._on_auto_pause = on_auto_pause
        self._tuning = tuning
        self._watcher = watcher
        self._registry = registry or AlertRegistry()
        self._pending = pending
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._page_load_timeout = page_load_timeout
        self._blocks = block_detector or BlockDetector()
        self._light_skips = 0  # blocked rounds in a row whose renders a light request spared
        self._stop = asyncio.Event()
        self._stats = MonitorStats()
        self._planned_delay: float | None = None
        self._health: dict[str, _PageHealth] = {}
        self._last_read: dict[str, float] = {}  # page -> when it was last read successfully
        self._first_seen: dict[tuple[str, Address], float] = {}  # (page, address) -> first seen
        self._round_tokens: dict[str, int] = {}  # page -> tokens announced this round
        self._first_round: list[ValidationResult] = []  # what the first round did not confirm
        self._paused_first = False  # the first round paused the site

    @property
    def stats(self) -> MonitorStats:
        """The live counters of this monitor."""
        return self._stats

    @property
    def stopped(self) -> bool:
        """True once a stop was requested."""
        return self._stop.is_set()

    def request_stop(self) -> None:
        """Ask the monitor to stop; a round in progress still finishes."""
        self._stop.set()

    async def run(self) -> None:
        """Run rounds, with a pause between them, until a stop is requested."""
        log.info("monitor started for %s", self._site.url)
        try:
            while not self._stop.is_set():
                await self.run_once()
                if self._stop.is_set():
                    break
                await self._sleep_between_rounds()
        finally:
            log.info("monitor stopped for %s", self._site.url)

    async def run_once(self) -> list[PageCheckOutcome]:
        """Check every live page once and return the outcomes.

        Each outcome is handled as soon as its check ends. A failed check is reported to the
        user and left out of the result. While the site blocks, a light request may show it
        still refuses; then nothing is rendered and the refusal is the round's only outcome.
        """
        started = time.perf_counter()
        self._round_tokens = {}
        refusal = await self._light_check()
        if refusal is not None:
            if self._watcher is not None:
                await self._watcher.recheck()
            self._stats.rounds += 1
            self._stats.last_round_at = time.time()
            self._stats.last_round_seconds = time.perf_counter() - started
            self._stats.last_pages = 0
            outcome = PageCheckOutcome(
                self._site.url, PageStatus.UNAVAILABLE, error=refusal.reason, block=refusal
            )
            await self._update_block_state([outcome])
            return [outcome]
        pages = await live_pages(self._prober, self._deriver, self._site.url)
        results = await asyncio.gather(
            *(self._check_and_handle(url) for url in pages), return_exceptions=True
        )
        outcomes: list[PageCheckOutcome] = []
        for url, result in zip(pages, results, strict=True):
            if isinstance(result, PageCheckOutcome):
                outcomes.append(result)
            elif isinstance(result, asyncio.CancelledError):
                raise result
            elif isinstance(result, BaseException):
                log.error(
                    "[%s] check of %s failed: %r", self._site.url, url, result, exc_info=result
                )
                await self._notifier.problem(
                    f"check:{self._site.url}",
                    f"Page check failed on {url}",
                    f"{type(result).__name__}: {result}",
                    hint="The page is retried on the next round; if this keeps coming, "
                    "look at the bot log.",
                )
        if self._watcher is not None:
            await self._watcher.recheck()
        self._first_round = []
        self._stats.rounds += 1
        self._stats.last_round_at = time.time()
        self._stats.last_round_seconds = time.perf_counter() - started
        self._stats.last_pages = len(pages)
        for url in [url for url in self._health if url not in pages]:
            del self._health[url]
        self._count_timing_out()
        await self._update_block_state(outcomes)
        return outcomes

    async def _light_check(self) -> BlockSignal | None:
        """While the site blocks, ask its root page once over HTTP before rendering anything.

        Return the block signal of a refusal: the round then renders nothing, so the bot does
        not prolong the ban. After ``_LIGHT_SKIPS`` spared rounds in a row one renders anyway,
        as the browser may pass where the HTTP client is refused. Anything but a refusal
        (the page, a 404, a network error) lets the round render as usual.
        """
        if not self._stats.blocked or self._light_skips >= _LIGHT_SKIPS:
            self._light_skips = 0
            return None
        signal = self._blocks.inspect(await self._prober.peek(self._site.url))
        if signal is None:
            self._light_skips = 0
            return None
        self._light_skips += 1
        log.info(
            "[%s] still refused (%s); nothing rendered this round",
            self._site.url,
            signal.describe(),
        )
        return signal

    def next_delay(self) -> float:
        """Return the seconds until the next round.

        That is a random interval doubled per block level (up to ``block_max_backoff``), and
        never shorter than a Retry-After the site asked for.
        """
        base = self._rng.uniform(self._tuning.poll_interval_min, self._tuning.poll_interval_max)
        cap = max(self._tuning.block_max_backoff, base)
        delay = min(base * (2**self._stats.block_level), cap)
        if self._stats.retry_after:
            delay = max(delay, self._stats.retry_after)
        return delay

    async def _check_and_handle(self, page_url: str) -> PageCheckOutcome:
        """Check one page, handle the outcome and track its load timeouts.

        A page with a timeout streak gets a longer load timeout.
        """
        health = self._health.get(page_url)
        stretched = None
        if health is not None and health.factor > 1:
            stretched = self._page_load_timeout * health.factor
        outcome = await self._checker.check(
            page_url, load_timeout=stretched, on_observation=self._observed
        )
        await self._handle(outcome)
        await self._track_timeouts(outcome)
        return outcome

    async def _observed(self, observation: Observation) -> None:
        """Announce the tokens a render showed, before the page is even compared.

        Addresses that may still become a token go to the pending watch. A token the first
        check that paused the site showed unconfirmed is old news: the watch sends it in a
        document with the other late ones, and it counts for no pause.
        """
        page, seen_at = observation.page_url, observation.seen_at
        missing_at = self._last_read.get(page)
        # A sub-page first seen after the first round went live meanwhile: what it shows is news.
        site = self._site
        new_page = page != site.url and page not in site.announced_pages and self._stats.rounds > 0
        # A render shows the page as of some moment after its start: the next window opens there.
        started = observation.started_at
        if observation.read:  # a challenge or a blank render did not show the page
            self._last_read[page] = started if started is not None else seen_at
        for sighting in observation.sightings:
            key = (page, sighting.address)
            # Seen again: moved to the end, so the memory forgets the pairs gone longest.
            self._first_seen[key] = self._first_seen.pop(key, seen_at)
        excess = max(len(self._first_seen) - _FIRST_SEEN_MEMORY, 0)
        for key in list(islice(self._first_seen, excess)):
            del self._first_seen[key]
        sightings = {sighting.address: sighting for sighting in observation.sightings}
        found: list[tuple[ConfirmedContract, Sighting | None, Timeline]] = []
        unconfirmed: list[ValidationResult] = []
        for result in observation.results:
            # A token behind an LP pair borrows the pair's place on the page.
            spot = result.via or result.address
            sighting = sightings.get(spot)
            contract = result.contract
            if contract is None:
                if self._pending is not None:
                    self._pending.offer(site.url, page, sighting, result)
                unconfirmed.append(result)
                continue
            if self._pending is not None and self._pending.take_old_news(site.url, contract, spot):
                continue
            first = self._first_seen.get((page, spot), seen_at)
            if first < seen_at:
                timeline = Timeline(seen_at, on_page_since=first)
            else:
                timeline = Timeline(seen_at, missing_at=missing_at)
            found.append((contract, sighting, timeline))
        if unconfirmed and self._stats.rounds == 0:
            self._first_round.extend(unconfirmed)
            if self._paused_first and self._pending is not None:
                self._pending.first_check_paused(site.url, unconfirmed)  # rendered after the pause
        if found:
            await self._announce(page, found, new_page=new_page)

    async def _announce(
        self,
        page_url: str,
        found: Sequence[tuple[ConfirmedContract, Sighting | None, Timeline]],
        *,
        new_page: bool = False,
    ) -> None:
        """Announce the tokens no other page or turbo announced yet, one message each.

        When a page reaches ``contract_pause_threshold`` tokens in one round, they go out as
        a document instead and the site is paused. When that is the first round, the pending
        watch hears what the round could not confirm: it may turn out a token later.
        """
        new = [item for item in found if self._registry.claim(item[0])]
        if not new:
            return
        if self._pending is not None:
            for contract, _, timeline in new:
                # Turbo counts a token only if it appeared since the last read or on a new page.
                if timeline.missing_at is not None or new_page:
                    self._pending.announced(self._site.url, contract.address)
        count = self._round_tokens.get(page_url, 0) + len(new)
        self._round_tokens[page_url] = count
        if count < self._tuning.contract_pause_threshold:
            for contract, sighting, timeline in new:
                await self._notifier.contract_found(
                    contract, page_url, sighting=sighting, timeline=timeline
                )
            return
        site = self._site
        await self._notifier.contracts_batch(site.url, page_url, [c for c, _, _ in new])
        if self._stop.is_set():
            return  # another page of this round already paused the site
        self.request_stop()
        first_check = self._stats.rounds == 0
        if first_check and self._pending is not None:
            self._paused_first = True
            self._pending.first_check_paused(site.url, self._first_round)
        # Paused (and saved) first: the notice waits for the document and flood limits.
        await self._on_auto_pause(site.url, page_url)
        await self._notifier.site_auto_paused(
            site.url,
            page_url,
            count,
            self._deriver.pages(site.url),
            first_check=first_check,
        )

    async def _track_timeouts(self, outcome: PageCheckOutcome) -> None:
        """Count a page's load timeouts in a row and stretch its load timeout as they grow.

        The timeout grows every ``render_failure_threshold`` timeouts. The user is told once
        per streak, and again when the page loads normally.
        """
        threshold = self._tuning.render_failure_threshold
        health = self._health.get(outcome.url)
        if outcome.timed_out:
            if not threshold:
                return
            health = self._health.setdefault(outcome.url, _PageHealth())
            health.timeouts += 1
            if health.timeouts % threshold:
                return
            health.factor = min(health.factor + 1, _MAX_TIMEOUT_FACTOR)
            stretched = self._page_load_timeout * health.factor
            log.warning(
                "[%s] %s timed out %d times in a row; load timeout now %.0fs",
                self._site.url,
                outcome.url,
                health.timeouts,
                stretched,
            )
            if not health.warned:
                health.warned = True
                self._count_timing_out()  # before the alert, so /status agrees with it
                await self._notifier.page_timing_out(
                    outcome.url, health.timeouts, outcome.error or "timeout", stretched
                )
            return
        if health is None or outcome.status is PageStatus.UNAVAILABLE:
            return  # another kind of failure neither extends nor ends a timeout streak
        del self._health[outcome.url]
        if health.warned:
            self._count_timing_out()
            await self._notifier.page_renders_again(outcome.url, self._page_load_timeout)

    def _count_timing_out(self) -> None:
        """Update the /status count of pages the user was warned about for load timeouts."""
        self._stats.timing_out = sum(1 for health in self._health.values() if health.warned)

    async def _handle(self, outcome: PageCheckOutcome) -> None:
        """Log a page's outcome and announce a new page, a captcha or a change.

        It also feeds the unverified watcher. Tokens were already announced by
        :meth:`_observed` as soon as the renders showed them.
        """
        site = self._site
        if outcome.status is PageStatus.UNAVAILABLE:
            log.info("[%s] %s unavailable: %s", site.url, outcome.url, outcome.error)
        else:
            log.info("[%s] %s: %s", site.url, outcome.url, outcome.status.value)
            await self._announce_if_new(outcome.url)
            if outcome.captcha is not None:
                await self._announce_captcha(outcome.url, outcome.captcha)

        if self._watcher is not None and outcome.candidates:
            await self._watcher.observe(outcome.candidates, outcome.url)

        if outcome.status is PageStatus.CHANGED and outcome.report is not None:
            site.last_change = LastChange(time.time(), outcome.url, outcome.report.counts)
            self._persist()
            if site.notifications_enabled:
                await self._notifier.page_changed(outcome.report)
            else:
                log.info(
                    "[%s] change on %s not announced (notifications off)", site.url, outcome.url
                )
        elif outcome.status is PageStatus.UNSTABLE:
            log.warning("[%s] %s skipped: %s", site.url, outcome.url, outcome.error)

    async def _announce_if_new(self, page_url: str) -> None:
        """Tell the user about a sub-page the first time it answers."""
        site = self._site
        if page_url == site.url or page_url in site.announced_pages:
            return
        site.announced_pages.add(page_url)
        self._persist()
        await self._notifier.page_discovered(page_url)

    async def _announce_captcha(self, page_url: str, widget: str) -> None:
        """Tell the user once per page (and widget) that a captcha stands on the page."""
        site = self._site
        told = site.captcha_pages.setdefault(page_url, [])
        if widget in told:
            return
        told.append(widget)
        self._persist()
        log.info("[%s] %s shows a %s widget", site.url, page_url, widget)
        await self._notifier.captcha_shown(page_url, widget)

    async def _update_block_state(self, outcomes: Sequence[PageCheckOutcome]) -> None:
        """Raise the block level on a block signal, or reset it once the root page answers.

        The user is told when a block starts and when the site recovers.
        """
        site, stats = self._site, self._stats
        signals = [(o.url, o.block) for o in outcomes if o.block is not None]
        if signals:
            page_url, signal = signals[0]
            first_time = stats.blocked_since is None
            stats.block_level = min(stats.block_level + 1, _MAX_BLOCK_LEVEL)
            stats.block_reason = signal.describe()
            stats.retry_after = signal.retry_after
            if first_time:
                stats.blocked_since = time.time()
            self._planned_delay = self.next_delay()
            log.warning(
                "[%s] possible block on %s (%s); next round in %.0fs",
                site.url,
                page_url,
                stats.block_reason,
                self._planned_delay,
            )
            if first_time:
                await self._notifier.site_blocked(site.url, page_url, signal, self._planned_delay)
            return
        root_answered = any(
            o.url == site.url and o.status is not PageStatus.UNAVAILABLE for o in outcomes
        )
        if stats.blocked and root_answered:
            since = stats.blocked_since
            stats.block_level = 0
            stats.block_reason = None
            stats.blocked_since = None
            stats.retry_after = None
            log.info("[%s] answers normally again; normal interval restored", site.url)
            await self._notifier.site_recovered(
                site.url, time.time() - since if since is not None else None
            )

    async def _sleep_between_rounds(self) -> None:
        """Wait the planned delay (or a new one) before the next round, less if stopped."""
        delay = self._planned_delay if self._planned_delay is not None else self.next_delay()
        self._planned_delay = None
        if self._sleep is not None:
            await self._sleep(delay)
            return
        await wait_or_stop(self._stop, delay)
