import asyncio
import random
from typing import Any

from scraper_bot.models import (
    Address,
    Chain,
    ChangeReport,
    ConfirmedContract,
    PageCheckOutcome,
    PageStatus,
    SiteState,
)
from scraper_bot.site_monitor import SiteMonitor
from scraper_bot.urls import SubUrlDeriver
from tests.conftest import FakeChecker, FakeNotifier, FakeProber, node

ROOT = "https://example.com"
APP = "https://app.example.com"
DOCS = "https://example.com/docs"


def contract(index: int) -> ConfirmedContract:
    return ConfirmedContract(Address(Chain.ETHEREUM, f"0x{index:040x}"), "Ethereum")


class Harness:
    def __init__(
        self, *, statuses: dict[str, int | None] | None = None, threshold: int = 5
    ) -> None:
        self.site = SiteState(ROOT)
        self.checker = FakeChecker()
        self.prober = FakeProber(statuses)
        self.notifier = FakeNotifier()
        self.persist_calls = 0
        self.auto_pauses: list[tuple[str, str]] = []

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
            poll_interval=(0.01, 0.02),
            contract_threshold=threshold,
            rng=random.Random(1),
        )

    def outcome(self, url: str, status: PageStatus, **kwargs: Any) -> None:
        self.checker.outcomes[url] = PageCheckOutcome(url, status, **kwargs)


async def test_round_checks_root_and_only_live_sub_pages() -> None:
    h = Harness(statuses={APP: None, DOCS: 200})
    outcomes = await h.monitor.run_once()
    assert sorted(h.prober.calls) == [APP, DOCS]
    assert h.checker.calls == [ROOT, DOCS]
    assert [o.url for o in outcomes] == [ROOT, DOCS]


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
    assert h.site.last_change.counts.total == 2
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

    async def broken(page_url: str) -> PageCheckOutcome:
        if page_url == DOCS:
            raise RuntimeError("boom")
        return PageCheckOutcome(page_url, PageStatus.UNCHANGED)

    h.checker.check = broken  # type: ignore[method-assign]
    outcomes = await h.monitor.run_once()
    assert [o.url for o in outcomes] == [ROOT]


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
