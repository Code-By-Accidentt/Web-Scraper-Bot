"""The monitoring loop of one root site and its sub-pages."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence

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
from scraper_bot.urls import SubUrlDeriver

log = logging.getLogger(__name__)

PersistCallback = Callable[[], None]
AutoPauseCallback = Callable[[str, str], Awaitable[None]]


class SiteMonitor:
    """Runs rounds of checks for a site until asked to stop.

    Every round probes the derived sub-pages, checks each live page concurrently
    (bounded by the shared browser), announces newly discovered pages, reports
    confirmed changes and contracts, and auto-pauses the site when one page
    yields ``contract_threshold`` or more new contracts at once.
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
        poll_interval: tuple[float, float],
        contract_threshold: int,
        rng: random.Random | None = None,
    ) -> None:
        self._site = site
        self._checker = checker
        self._prober = prober
        self._deriver = deriver
        self._notifier = notifier
        self._persist = persist
        self._on_auto_pause = on_auto_pause
        self._poll_interval = poll_interval
        self._threshold = contract_threshold
        self._rng = rng or random.Random()
        self._stop = asyncio.Event()

    @property
    def site(self) -> SiteState:
        return self._site

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
        """One round over every live page; outcomes are acted upon as they arrive."""
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
        return outcomes

    # ── internals ───────────────────────────────────────────────────────────
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
        outcome = await self._checker.check(page_url)
        await self._handle(outcome)
        return outcome

    async def _handle(self, outcome: PageCheckOutcome) -> None:
        site = self._site
        if outcome.status is PageStatus.UNAVAILABLE:
            log.info("[%s] %s unavailable: %s", site.url, outcome.url, outcome.error)
        else:
            log.info("[%s] %s: %s", site.url, outcome.url, outcome.status.value)
            await self._announce_if_new(outcome.url)

        if outcome.contracts:
            await self._handle_contracts(outcome.url, outcome.contracts)

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
        if len(contracts) < self._threshold:
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

    async def _sleep_between_rounds(self) -> None:
        delay = self._rng.uniform(*self._poll_interval)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
