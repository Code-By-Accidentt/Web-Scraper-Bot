import asyncio
import dataclasses
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

from scraper_bot.models import (
    Address,
    AddressKind,
    BlockSignal,
    Chain,
    ChangeCounts,
    ChangeReport,
    ConfirmedContract,
    Observation,
    PageCheckOutcome,
    PageStatus,
    Sighting,
    SightingSource,
    SiteState,
    Timeline,
    ValidationResult,
    ValidationStatus,
)
from scraper_bot.monitoring.site_monitor import _MAX_BLOCK_LEVEL, SiteMonitor
from scraper_bot.pages.urls import SubUrlDeriver
from tests.conftest import FakeChecker, FakeNotifier, FakeProber, FakeTuning, node

ROOT = "https://example.com"
APP = "https://app.example.com"
DOCS = "https://example.com/docs"


def address(index: int) -> Address:
    return Address(Chain.ETHEREUM, f"0x{index:040x}")


def contract(index: int) -> ConfirmedContract:
    return ConfirmedContract(address(index), "Ethereum")


class RecordingWatcher:
    """Stand-in for UnverifiedWatcher that only records what the monitor feeds it."""

    enabled = True

    def __init__(self) -> None:
        self.observed: list[tuple[tuple[Address, ...], str]] = []
        self.rechecks = 0

    async def observe(self, candidates: Sequence[Address], page_url: str) -> None:
        self.observed.append((tuple(candidates), page_url))

    async def recheck(self) -> None:
        self.rechecks += 1


class Harness:
    def __init__(
        self,
        *,
        statuses: dict[str, int | None] | None = None,
        threshold: int = 5,
        watcher: RecordingWatcher | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.site = SiteState(ROOT)
        self.checker = FakeChecker()
        self.prober = FakeProber(statuses)
        self.notifier = FakeNotifier()
        self.persist_calls = 0
        self.auto_pauses: list[tuple[str, str]] = []
        self.tuning = FakeTuning(contract_pause_threshold=threshold)

        def persist() -> None:
            self.persist_calls += 1

        async def on_auto_pause(url: str, page_url: str) -> None:
            self.auto_pauses.append((url, page_url))

        self.monitor = SiteMonitor(
            self.site,
            checker=self.checker,  # type: ignore[arg-type]
            prober=self.prober,  # type: ignore[arg-type]
            deriver=SubUrlDeriver(("https://app.{domain}", "https://{domain}/docs")),
            notifier=self.notifier,  # type: ignore[arg-type]
            persist=persist,
            on_auto_pause=on_auto_pause,
            tuning=self.tuning,
            watcher=watcher,  # type: ignore[arg-type]
            rng=random.Random(1),
            sleep=sleep,
        )

    def outcome(self, url: str, status: PageStatus, **kwargs: Any) -> None:
        self.checker.outcomes[url] = PageCheckOutcome(url, status, **kwargs)


async def test_round_checks_root_and_only_live_sub_pages() -> None:
    h = Harness(statuses={APP: None, DOCS: 200})
    outcomes = await h.monitor.run_once()
    assert sorted(h.prober.calls) == [APP, DOCS]
    assert h.checker.calls == [ROOT, DOCS]
    assert [o.url for o in outcomes] == [ROOT, DOCS]
    stats = h.monitor.stats
    assert (stats.rounds, stats.last_pages) == (1, 2)
    assert stats.last_round_at is not None and stats.last_round_seconds >= 0


async def test_discovered_sub_page_is_announced_once_and_persisted() -> None:
    h = Harness(statuses={APP: 404})
    await h.monitor.run_once()
    await h.monitor.run_once()
    assert h.notifier.discovered == [DOCS]
    assert h.site.announced_pages == {DOCS}
    assert h.persist_calls == 1


async def test_unavailable_page_is_not_announced() -> None:
    h = Harness(statuses={APP: 404})
    h.outcome(DOCS, PageStatus.UNAVAILABLE, error="HTTP 500")
    await h.monitor.run_once()
    assert h.notifier.discovered == []
    assert h.site.announced_pages == set()


async def test_change_is_reported_only_when_alerts_are_enabled() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404})
    report = ChangeReport(ROOT, removed=(node("p", "old"),), added=(node("p", "new"),))
    h.outcome(ROOT, PageStatus.CHANGED, report=report)
    await h.monitor.run_once()
    assert h.notifier.changes == [report]
    assert h.site.last_change is not None
    assert h.site.last_change.counts == ChangeCounts(removed=1, added=1)
    assert h.persist_calls == 1

    h.site.notifications_enabled = False
    await h.monitor.run_once()
    assert len(h.notifier.changes) == 1
    assert h.persist_calls == 2


async def test_few_contracts_are_announced_one_by_one() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404})
    h.outcome(ROOT, PageStatus.UNCHANGED, contracts=(contract(1), contract(2)))
    await h.monitor.run_once()
    assert [c.address.value for c, _ in h.notifier.contracts] == [
        contract(1).address.value,
        contract(2).address.value,
    ]
    assert h.notifier.batches == []
    assert h.auto_pauses == []
    assert not h.monitor.stopped


async def test_many_contracts_send_a_document_and_pause_the_site() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404}, threshold=3)
    found = tuple(contract(i) for i in range(3))
    h.outcome(ROOT, PageStatus.CHANGED, contracts=found, report=ChangeReport(ROOT))
    await h.monitor.run_once()
    assert h.notifier.contracts == []
    assert h.notifier.batches == [(ROOT, ROOT, found)]
    assert h.notifier.paused == [(ROOT, ROOT, 3)]
    assert h.auto_pauses == [(ROOT, ROOT)]
    assert h.monitor.stopped


async def test_failing_check_does_not_break_the_round() -> None:
    h = Harness(statuses={APP: 404, DOCS: 200})

    async def broken(page_url: str, **_: Any) -> PageCheckOutcome:
        if page_url == DOCS:
            raise RuntimeError("boom")
        return PageCheckOutcome(page_url, PageStatus.UNCHANGED)

    h.checker.check = broken  # type: ignore[method-assign]
    outcomes = await h.monitor.run_once()
    assert [o.url for o in outcomes] == [ROOT]
    assert len(h.notifier.problems) == 1
    key, title, detail, hint = h.notifier.problems[0]
    assert key == f"check:{ROOT}" and title == f"Page check failed on {DOCS}"
    assert detail == "RuntimeError: boom" and hint


async def test_run_loops_until_stopped() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404})
    task = asyncio.create_task(h.monitor.run())
    for _ in range(100):
        if len(h.checker.calls) >= 2:
            break
        await asyncio.sleep(0.01)
    h.monitor.request_stop()
    await asyncio.wait_for(task, timeout=2)
    assert len(h.checker.calls) >= 2


async def test_block_signal_doubles_the_interval_and_warns_once() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404})
    h.outcome(ROOT, PageStatus.UNAVAILABLE, error="HTTP 403", block=BlockSignal("HTTP 403"))
    await h.monitor.run_once()
    assert h.monitor.stats.block_level == 1
    assert h.monitor.stats.blocked and h.monitor.stats.block_reason == "HTTP 403"
    assert len(h.notifier.blocked) == 1
    site_url, page_url, reason, delay = h.notifier.blocked[0]
    assert (site_url, page_url, reason) == (ROOT, ROOT, "HTTP 403")
    assert 0.02 <= delay <= 0.04  # base 0.01-0.02 doubled once

    await h.monitor.run_once()
    assert h.monitor.stats.block_level == 2
    assert len(h.notifier.blocked) == 1  # warned only once per block episode
    assert 0.04 <= h.monitor.next_delay() <= 0.08

    h.outcome(ROOT, PageStatus.UNCHANGED)
    await h.monitor.run_once()
    assert not h.monitor.stats.blocked
    assert h.notifier.recovered == [ROOT]


async def test_retry_after_and_max_backoff_bound_the_delay() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404})
    h.outcome(ROOT, PageStatus.UNAVAILABLE, block=BlockSignal("HTTP 429", retry_after=5.0))
    await h.monitor.run_once()
    assert h.monitor.next_delay() == 5.0

    capped = Harness(statuses={APP: 404, DOCS: 404})
    capped.tuning.block_max_backoff = 0.03
    for _ in range(6):
        capped.outcome(ROOT, PageStatus.UNAVAILABLE, block=BlockSignal("HTTP 403"))
        await capped.monitor.run_once()
    assert capped.monitor.next_delay() <= 0.03


async def test_watcher_receives_candidates_and_is_rechecked_each_round() -> None:
    watcher = RecordingWatcher()
    h = Harness(statuses={APP: 404, DOCS: 200}, watcher=watcher)
    h.outcome(ROOT, PageStatus.UNCHANGED, candidates=(address(1),))
    h.outcome(DOCS, PageStatus.BASELINE, candidates=(address(2), address(3)))
    await h.monitor.run_once()
    assert sorted(watcher.observed, key=lambda seen: seen[1]) == [
        ((address(1),), ROOT),
        ((address(2), address(3)), DOCS),
    ]
    assert watcher.rechecks == 1

    h.outcome(DOCS, PageStatus.UNCHANGED)  # no candidates on the page: observe is skipped
    await h.monitor.run_once()
    assert watcher.observed[2:] == [((address(1),), ROOT)]
    assert watcher.rechecks == 2


async def test_contracts_are_still_reported_while_change_alerts_are_muted() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404})
    h.site.notifications_enabled = False
    report = ChangeReport(ROOT, added=(node("p", "token live"),))
    h.outcome(ROOT, PageStatus.CHANGED, report=report, contracts=(contract(1),))
    await h.monitor.run_once()
    assert h.notifier.changes == []
    assert h.notifier.contracts == [(contract(1), ROOT)]
    assert h.site.last_change is not None and h.persist_calls == 1


async def test_unstable_outcome_announces_and_persists_nothing() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404})
    h.outcome(ROOT, PageStatus.UNSTABLE, error="two renders disagreed")
    outcomes = await h.monitor.run_once()
    assert [o.status for o in outcomes] == [PageStatus.UNSTABLE]
    assert h.notifier.changes == [] and h.notifier.contracts == []
    assert h.notifier.discovered == [] and h.notifier.problems == []
    assert h.site.last_change is None and h.persist_calls == 0
    assert h.monitor.stats.rounds == 1 and not h.monitor.stopped


async def test_two_pages_over_threshold_in_one_round_pause_once() -> None:
    h = Harness(statuses={APP: 404, DOCS: 200}, threshold=2)
    h.outcome(ROOT, PageStatus.CHANGED, contracts=(contract(1), contract(2)))
    h.outcome(DOCS, PageStatus.CHANGED, contracts=(contract(3), contract(4)))
    await h.monitor.run_once()
    assert sorted(page for _, page, _ in h.notifier.batches) == [ROOT, DOCS]
    assert h.notifier.contracts == []
    assert len(h.notifier.paused) == 1
    assert len(h.auto_pauses) == 1
    assert h.monitor.stopped


async def test_block_level_is_capped() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404})
    h.tuning.block_max_backoff = 1.0
    h.outcome(ROOT, PageStatus.UNAVAILABLE, error="HTTP 403", block=BlockSignal("HTTP 403"))
    for _ in range(_MAX_BLOCK_LEVEL + 3):
        await h.monitor.run_once()
        assert h.monitor.stats.block_level <= _MAX_BLOCK_LEVEL
    assert h.monitor.stats.block_level == _MAX_BLOCK_LEVEL
    assert h.monitor.next_delay() <= max(h.tuning.block_max_backoff, h.tuning.poll_interval_max)
    assert len(h.notifier.blocked) == 1  # still one warning for the whole episode


async def test_cancellation_propagates_out_of_a_round() -> None:
    h = Harness(statuses={APP: 404, DOCS: 200})

    async def cancelled(page_url: str, **_: Any) -> PageCheckOutcome:
        if page_url == DOCS:
            raise asyncio.CancelledError
        return PageCheckOutcome(page_url, PageStatus.UNCHANGED)

    h.checker.check = cancelled  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await h.monitor.run_once()
    assert h.notifier.problems == []
    assert h.monitor.stats.rounds == 0


async def test_stop_requested_during_a_round_skips_the_sleep() -> None:
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    h = Harness(statuses={APP: 404, DOCS: 404}, sleep=record)

    async def stop_while_checking(page_url: str, **_: Any) -> PageCheckOutcome:
        h.monitor.request_stop()  # e.g. /pause arrives while the page renders
        return PageCheckOutcome(page_url, PageStatus.UNCHANGED)

    h.checker.check = stop_while_checking  # type: ignore[method-assign]
    await asyncio.wait_for(h.monitor.run(), timeout=2)
    assert h.monitor.stats.rounds == 1
    assert delays == []


async def test_injected_sleep_replaces_the_wait() -> None:
    delays: list[float] = []

    async def instant(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 3:
            h.monitor.request_stop()

    h = Harness(statuses={APP: 404, DOCS: 404}, sleep=instant)
    await asyncio.wait_for(h.monitor.run(), timeout=2)
    assert h.monitor.stats.rounds == 3
    assert len(delays) == 3
    assert all(0.01 <= delay <= 0.02 for delay in delays)  # the base interval, nothing waited


TIMEOUT = "timeout after 30s"


def timed_out(url: str) -> PageCheckOutcome:
    return PageCheckOutcome(url, PageStatus.UNAVAILABLE, error=TIMEOUT, timed_out=True)


async def test_load_timeouts_in_a_row_stretch_the_timeout_and_alert_once() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404})
    h.checker.outcomes[ROOT] = timed_out(ROOT)
    for _ in range(4):
        await h.monitor.run_once()
    assert h.notifier.timing_out == [] and h.monitor.stats.timing_out == 0
    await h.monitor.run_once()  # the fifth timeout in a row
    assert h.notifier.timing_out == [(ROOT, 5, TIMEOUT, 60.0)]
    assert h.monitor.stats.timing_out == 1
    assert h.checker.timeouts == [None] * 5
    for _ in range(5):
        await h.monitor.run_once()  # rounds 6-10 render with the doubled timeout
    assert h.checker.timeouts[5:] == [60.0] * 5
    await h.monitor.run_once()  # the 10th timeout raised it again, capped at three times
    assert h.checker.timeouts[-1] == 90.0
    assert len(h.notifier.timing_out) == 1  # one alert per streak
    h.outcome(ROOT, PageStatus.UNCHANGED)
    await h.monitor.run_once()
    assert h.notifier.renders_again == [(ROOT, 30.0)]
    assert h.monitor.stats.timing_out == 0
    await h.monitor.run_once()
    assert h.checker.timeouts[-1] is None  # back to the configured timeout


async def test_other_failures_neither_count_nor_break_a_timeout_streak() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404})
    h.outcome(ROOT, PageStatus.UNAVAILABLE, error="HTTP 500")
    for _ in range(6):
        await h.monitor.run_once()
    assert h.notifier.timing_out == []
    h.checker.outcomes[ROOT] = timed_out(ROOT)
    for _ in range(3):
        await h.monitor.run_once()
    h.outcome(ROOT, PageStatus.UNAVAILABLE, error="HTTP 500")
    await h.monitor.run_once()  # a 500 in between is not a render: the streak survives
    h.checker.outcomes[ROOT] = timed_out(ROOT)
    await h.monitor.run_once()
    assert h.notifier.timing_out == []
    await h.monitor.run_once()  # the fifth timeout
    assert h.notifier.timing_out == [(ROOT, 5, TIMEOUT, 60.0)]


async def test_render_failure_threshold_zero_disables_the_counter() -> None:
    h = Harness(statuses={APP: 404, DOCS: 404})
    h.tuning.render_failure_threshold = 0
    h.checker.outcomes[ROOT] = timed_out(ROOT)
    for _ in range(12):
        await h.monitor.run_once()
    assert h.notifier.timing_out == [] and set(h.checker.timeouts) == {None}
    assert h.monitor.stats.timing_out == 0


async def test_timeouts_are_counted_per_page() -> None:
    h = Harness(statuses={APP: 404, DOCS: 200})
    h.checker.outcomes[DOCS] = timed_out(DOCS)
    for _ in range(5):
        await h.monitor.run_once()
    assert h.notifier.timing_out == [(DOCS, 5, TIMEOUT, 60.0)]
    await h.monitor.run_once()
    last_timeout_per_page = dict(zip(h.checker.calls, h.checker.timeouts, strict=True))
    assert last_timeout_per_page == {ROOT: None, DOCS: 60.0}


async def test_a_vanished_page_drops_its_streak_silently() -> None:
    h = Harness(statuses={APP: 404, DOCS: 200})
    h.checker.outcomes[DOCS] = timed_out(DOCS)
    for _ in range(5):
        await h.monitor.run_once()
    assert h.monitor.stats.timing_out == 1
    h.prober.statuses[DOCS] = 404  # the sub-page disappeared for good
    await h.monitor.run_once()
    assert h.monitor.stats.timing_out == 0 and h.notifier.renders_again == []


# ── token alerts from observations ──────────────────────────────────────────
class ObservingChecker:
    """Reports scripted observations per page, then an UNCHANGED outcome."""

    def __init__(self) -> None:
        self.observations: dict[str, list[Observation]] = {}

    async def check(self, page_url: str, **kwargs: Any) -> PageCheckOutcome:
        observer = kwargs.get("on_observation")
        for observation in self.observations.pop(page_url, []):
            await observer(observation)
        return PageCheckOutcome(page_url, PageStatus.UNCHANGED)


def seen(page: str, at: float, *addresses: Address, via: dict[Address, Address] | None = None):
    """An observation of ``addresses`` in page text; tokens behind pairs through ``via``."""
    via = via or {}
    sightings = tuple(
        Sighting(a, SightingSource.TEXT, f"CA {i}", True) for i, a in enumerate(addresses)
    )
    results = tuple(ValidationResult(a, ValidationStatus.CONFIRMED, "Base") for a in addresses)
    results += tuple(
        ValidationResult(token, ValidationStatus.CONFIRMED, "Base", via=pair)
        for token, pair in via.items()
    )
    return Observation(page, at, sightings, results)


def observing(
    statuses: dict[str, int | None], threshold: int = 5
) -> tuple[Harness, ObservingChecker]:
    h = Harness(statuses=statuses, threshold=threshold)
    checker = ObservingChecker()
    h.monitor._checker = checker  # type: ignore[assignment]
    return h, checker


async def test_the_appearance_window_starts_when_the_previous_render_began() -> None:
    h, checker = observing({APP: 404, DOCS: 404})
    first = seen(ROOT, 100.0)
    checker.observations = {ROOT: [dataclasses.replace(first, started_at=98.5)]}
    await h.monitor.run_once()  # that render fetched the page at 98.5 and read it at 100
    checker.observations = {ROOT: [seen(ROOT, 105.0, address(1))]}
    await h.monitor.run_once()
    [(_, timeline)] = h.notifier.contract_details
    assert timeline == Timeline(105.0, missing_at=98.5)


async def test_one_token_on_two_pages_is_announced_once() -> None:
    h, checker = observing({APP: 404, DOCS: 200})
    checker.observations = {
        ROOT: [seen(ROOT, 10.0, address(7))],
        DOCS: [seen(DOCS, 10.0, address(7))],
    }
    await h.monitor.run_once()
    assert [c.address for c, _ in h.notifier.contracts] == [address(7)]


async def test_the_timeline_says_when_the_address_appeared() -> None:
    h, checker = observing({APP: 404, DOCS: 404})
    checker.observations = {ROOT: [seen(ROOT, 100.0, address(1))]}
    await h.monitor.run_once()  # the first read of the page
    checker.observations = {ROOT: [seen(ROOT, 105.0, address(1), address(2))]}
    await h.monitor.run_once()  # address(2) is new; address(1) was announced already
    first, second = h.notifier.contract_details
    assert first[1] == Timeline(100.0)  # nothing earlier to compare with
    assert second[1] == Timeline(105.0, missing_at=100.0)
    assert [c.address for c, _ in h.notifier.contracts] == [address(1), address(2)]
    assert second[0] is not None and second[0].context == "CA 1"


async def test_an_address_seen_earlier_that_confirms_now_says_since_when() -> None:
    h, checker = observing({APP: 404, DOCS: 404})
    wallet_then_token = address(3)
    rejected = Observation(
        ROOT,
        50.0,
        (Sighting(wallet_then_token, SightingSource.TEXT),),
        (ValidationResult(wallet_then_token, ValidationStatus.REJECTED),),
    )
    checker.observations = {ROOT: [rejected]}
    await h.monitor.run_once()
    checker.observations = {ROOT: [seen(ROOT, 80.0, wallet_then_token)]}
    await h.monitor.run_once()
    [(_, timeline)] = h.notifier.contract_details
    assert timeline == Timeline(80.0, on_page_since=50.0)


async def test_a_token_behind_an_lp_pair_takes_the_pair_s_place() -> None:
    h, checker = observing({APP: 404, DOCS: 404})
    pair, token = address(8), address(9)
    observation = Observation(
        ROOT,
        10.0,
        (Sighting(pair, SightingSource.LINK, "href=https://dexscreener.com/base/"),),
        (
            ValidationResult(pair, ValidationStatus.REJECTED, kind=AddressKind.PAIR),
            ValidationResult(token, ValidationStatus.CONFIRMED, "Base", via=pair),
        ),
    )
    checker.observations = {ROOT: [observation]}
    await h.monitor.run_once()
    assert [c.address for c, _ in h.notifier.contracts] == [token]
    [(sighting, _)] = h.notifier.contract_details
    assert (
        sighting is not None and sighting.address == pair and sighting.source is SightingSource.LINK
    )


async def test_the_threshold_counts_both_renders_of_a_page() -> None:
    h, checker = observing({APP: 404, DOCS: 404}, threshold=3)
    checker.observations = {
        ROOT: [seen(ROOT, 1.0, address(1), address(2)), seen(ROOT, 3.0, address(3))]
    }
    await h.monitor.run_once()
    assert [c.address for c, _ in h.notifier.contracts] == [address(1), address(2)]
    assert [tuple(c.address for c in batch) for _, _, batch in h.notifier.batches] == [
        (address(3),)
    ]
    assert h.auto_pauses == [(ROOT, ROOT)] and h.monitor.stopped


class RecordingPending:
    def __init__(self) -> None:
        self.offers: list[tuple[str, str, Address, ValidationStatus]] = []
        self.announced_tokens: list[tuple[str, Address]] = []

    def offer(self, site_url, page_url, sighting, result, *, fast=False) -> bool:
        self.offers.append((site_url, page_url, result.address, result.status))
        return True

    def announced(self, site_url: str, address: Address) -> None:
        self.announced_tokens.append((site_url, address))


async def test_addresses_that_are_not_a_token_yet_go_to_the_pending_watch() -> None:
    h, checker = observing({APP: 404, DOCS: 404})
    pending = RecordingPending()
    h.monitor._pending = pending  # type: ignore[assignment]
    wallet, token = address(4), address(5)
    checker.observations = {
        ROOT: [
            Observation(
                ROOT,
                1.0,
                (Sighting(wallet, SightingSource.TEXT), Sighting(token, SightingSource.TEXT)),
                (
                    ValidationResult(wallet, ValidationStatus.REJECTED, kind=AddressKind.NO_CODE),
                    ValidationResult(token, ValidationStatus.CONFIRMED, "Base"),
                ),
            )
        ]
    }
    await h.monitor.run_once()
    assert pending.offers == [(ROOT, ROOT, wallet, ValidationStatus.REJECTED)]
    assert [c.address for c, _ in h.notifier.contracts] == [token]
    assert pending.announced_tokens == []  # on the page at its first check: old news to turbo
    checker.observations = {ROOT: [seen(ROOT, 5.0, address(6))]}
    await h.monitor.run_once()
    assert pending.announced_tokens == [(ROOT, address(6))]  # appeared: turbo counts it


async def test_a_token_on_a_sub_page_that_went_live_counts_for_turbo() -> None:
    h, checker = observing({APP: 404, DOCS: 404})
    pending = RecordingPending()
    h.monitor._pending = pending  # type: ignore[assignment]
    checker.observations = {ROOT: [seen(ROOT, 1.0)]}
    await h.monitor.run_once()
    h.prober.statuses[APP] = 200  # app.<domain> goes live with the launch
    checker.observations = {ROOT: [seen(ROOT, 5.0)], APP: [seen(APP, 5.0, address(8))]}
    await h.monitor.run_once()
    assert pending.announced_tokens == [(ROOT, address(8))]
