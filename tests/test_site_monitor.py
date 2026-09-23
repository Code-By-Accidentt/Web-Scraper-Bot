import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

from scraper_bot.models import (
    Address,
    BlockSignal,
    Chain,
    ChangeCounts,
    ChangeReport,
    ConfirmedContract,
    PageCheckOutcome,
    PageStatus,
    SiteState,
)
from scraper_bot.site_monitor import _MAX_BLOCK_LEVEL, SiteMonitor
from scraper_bot.urls import SubUrlDeriver
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

    async def broken(page_url: str, *, load_timeout: float | None = None) -> PageCheckOutcome:
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

    async def cancelled(page_url: str, *, load_timeout: float | None = None) -> PageCheckOutcome:
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

    async def stop_while_checking(
        page_url: str, *, load_timeout: float | None = None
    ) -> PageCheckOutcome:
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
