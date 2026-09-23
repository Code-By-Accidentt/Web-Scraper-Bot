"""The monitoring loop of one root site and its sub-pages."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from scraper_bot.fetcher import HttpProber
from scraper_bot.models import (
    ConfirmedContract,
    LastChange,
    PageCheckOutcome,
    PageStatus,
    SiteState,
)
from scraper_bot.notifier import Notifier
from scraper_bot.page_checker import PageChecker
from scraper_bot.preferences import Tuning
from scraper_bot.unverified import UnverifiedWatcher
from scraper_bot.urls import SubUrlDeriver

log = logging.getLogger(__name__)

PersistCallback = Callable[[], None]
AutoPauseCallback = Callable[[str, str], Awaitable[None]]


@dataclass(slots=True)
class MonitorStats:
    """Runtime counters of one monitor, shown by /status (not persisted)."""

    rounds: int = 0
    last_round_at: float | None = None
    last_round_seconds: float = 0.0
    last_pages: int = 0
    # Suspected block: every round with a block signal doubles the pause (cap: block_max_backoff).
    block_level: int = 0
    block_reason: str | None = None
    blocked_since: float | None = None
    retry_after: float | None = None
    timing_out: int = 0  # pages flagged for repeated load timeouts (see render_failure_threshold)

    @property
    def blocked(self) -> bool:
        return self.block_level > 0


@dataclass(slots=True)
class _PageHealth:
    """Consecutive load timeouts of one page and the timeout stretch they earned."""

    timeouts: int = 0
    factor: int = 1  # multiplier on page_load_timeout, grows to _MAX_TIMEOUT_FACTOR
    warned: bool = False  # the user heard about this streak


_MAX_BLOCK_LEVEL = 8
_MAX_TIMEOUT_FACTOR = 3


class SiteMonitor:
    """Runs rounds of checks for one site until asked to stop.

    A round probes the sub-pages, checks the live ones concurrently, announces new
    pages, changes and contracts, feeds the unverified watcher and auto-pauses the
    site when one page yields ``contract_pause_threshold`` contracts. Tuning is
    re-read every round, so /set changes apply at once.
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
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        page_load_timeout: float = 30.0,
    ) -> None:
        self._site = site
        self._checker = checker
        self._prober = prober
        self._deriver = deriver
        self._notifier = notifier
        self._persist = persist
        self._on_auto_pause = on_auto_pause
        self._tuning = tuning
        self._watcher = watcher
        self._rng = rng or random.Random()
        self._sleep = sleep  # tests replace the stop-aware wait with an instant one
        self._page_load_timeout = page_load_timeout
        self._stop = asyncio.Event()
        self._stats = MonitorStats()
        self._planned_delay: float | None = None
        self._health: dict[str, _PageHealth] = {}

    @property
    def stats(self) -> MonitorStats:
        return self._stats

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
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
        """One round over every live page; each outcome is handled as soon as it arrives."""
        started = time.perf_counter()
        pages = await self._live_pages()
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
        self._stats.rounds += 1
        self._stats.last_round_at = time.time()
        self._stats.last_round_seconds = time.perf_counter() - started
        self._stats.last_pages = len(pages)
        for url in [url for url in self._health if url not in pages]:
            del self._health[url]  # the page is gone (probe says so); its streak with it
        self._stats.timing_out = sum(1 for health in self._health.values() if health.warned)
        await self._update_block_state(outcomes)
        return outcomes

    def next_delay(self) -> float:
        """Seconds until the next round: the random interval, doubled per block level."""
        base = self._rng.uniform(self._tuning.poll_interval_min, self._tuning.poll_interval_max)
        cap = max(self._tuning.block_max_backoff, base)
        delay = min(base * (2**self._stats.block_level), cap)
        if self._stats.retry_after:
            delay = max(delay, self._stats.retry_after)
        return delay

    async def _live_pages(self) -> list[str]:
        root = self._site.url
        sub_pages = self._deriver.derive(root)
        probes = await asyncio.gather(*(self._prober.probe(url) for url in sub_pages))
        live = [root, *(probe.url for probe in probes if probe.worth_rendering)]
        skipped = [probe.url for probe in probes if not probe.worth_rendering]
        if skipped:
            log.debug("[%s] not rendering absent pages: %s", root, ", ".join(skipped))
        return live

    async def _check_and_handle(self, page_url: str) -> PageCheckOutcome:
        health = self._health.get(page_url)
        stretched = None
        if health is not None and health.factor > 1:
            stretched = self._page_load_timeout * health.factor
        outcome = await self._checker.check(page_url, load_timeout=stretched)
        await self._handle(outcome)
        await self._track_timeouts(outcome)
        return outcome

    async def _track_timeouts(self, outcome: PageCheckOutcome) -> None:
        """Count load timeouts in a row per page; stretch its timeout and tell the user once."""
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
                await self._notifier.page_timing_out(
                    outcome.url, health.timeouts, outcome.error or "timeout", stretched
                )
            return
        if health is None or outcome.status is PageStatus.UNAVAILABLE:
            return  # another kind of failure neither extends nor ends a timeout streak
        del self._health[outcome.url]
        if health.warned:
            await self._notifier.page_renders_again(outcome.url, self._page_load_timeout)

    async def _handle(self, outcome: PageCheckOutcome) -> None:
        site = self._site
        if outcome.status is PageStatus.UNAVAILABLE:
            log.info("[%s] %s unavailable: %s", site.url, outcome.url, outcome.error)
        else:
            log.info("[%s] %s: %s", site.url, outcome.url, outcome.status.value)
            await self._announce_if_new(outcome.url)

        if outcome.contracts:
            await self._handle_contracts(outcome.url, outcome.contracts)
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
        site = self._site
        if page_url == site.url or page_url in site.announced_pages:
            return
        site.announced_pages.add(page_url)
        self._persist()
        await self._notifier.page_discovered(page_url)

    async def _handle_contracts(
        self, page_url: str, contracts: Sequence[ConfirmedContract]
    ) -> None:
        site = self._site
        if len(contracts) < self._tuning.contract_pause_threshold:
            for contract in contracts:
                await self._notifier.contract_found(contract, page_url)
            return
        await self._notifier.contracts_batch(site.url, page_url, contracts)
        if self._stop.is_set():
            return  # another page of this round already paused the site
        self.request_stop()
        await self._notifier.site_auto_paused(
            site.url, page_url, len(contracts), self._deriver.pages(site.url)
        )
        await self._on_auto_pause(site.url, page_url)

    async def _update_block_state(self, outcomes: Sequence[PageCheckOutcome]) -> None:
        """Slow down on block signals; restore the pace (and tell the user) on recovery."""
        site, stats = self._site, self._stats
        signals = [(o.url, o.block) for o in outcomes if o.block is not None]
        if signals:
            page_url, signal = signals[0]
            first_time = stats.blocked_since is None
            stats.block_level = min(stats.block_level + 1, _MAX_BLOCK_LEVEL)
            stats.block_reason = signal.reason
            stats.retry_after = signal.retry_after
            if first_time:
                stats.blocked_since = time.time()
            self._planned_delay = self.next_delay()
            log.warning(
                "[%s] possible block on %s (%s); next round in %.0fs",
                site.url,
                page_url,
                signal.reason,
                self._planned_delay,
            )
            if first_time:
                await self._notifier.site_blocked(
                    site.url, page_url, signal.reason, self._planned_delay
                )
            return
        root_answered = any(
            o.url == site.url and o.status is not PageStatus.UNAVAILABLE for o in outcomes
        )
        if stats.blocked and root_answered:
            stats.block_level = 0
            stats.block_reason = None
            stats.blocked_since = None
            stats.retry_after = None
            log.info("[%s] answers normally again; normal interval restored", site.url)
            await self._notifier.site_recovered(site.url)

    async def _sleep_between_rounds(self) -> None:
        delay = self._planned_delay if self._planned_delay is not None else self.next_delay()
        self._planned_delay = None
        if self._sleep is not None:
            await self._sleep(delay)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
