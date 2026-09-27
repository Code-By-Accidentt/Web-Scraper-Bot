import asyncio
from pathlib import Path
from typing import Any

from scraper_bot.chains.rpc.health import FailingEndpoint
from scraper_bot.models.addresses import Address, Chain, ConfirmedContract
from scraper_bot.models.pages import ChangeCounts
from scraper_bot.models.sites import LastChange
from scraper_bot.monitoring.monitor_manager import MonitorManager
from scraper_bot.monitoring.turbo.manager import ScheduledTurbo, TurboManager
from scraper_bot.pages.urls import SubUrlDeriver
from scraper_bot.storage.address_cache import AddressCache
from scraper_bot.storage.snapshots import SnapshotStore
from scraper_bot.storage.state import StateStore
from scraper_bot.telegram.timefmt import format_duration, format_schedule
from scraper_bot.telegram.views.status import StatusReporter
from tests.conftest import FakeProber, FakeTuning, StubMonitor, StubTurboSession


class Browser:
    def __init__(self, connected: bool) -> None:
        self.is_connected = connected


def test_format_duration() -> None:
    assert format_duration(5) == "5s"
    assert format_duration(65) == "1m 5s"
    assert format_duration(3_720) == "1h 2m"
    assert format_duration(90_000) == "1d 1h"
    assert format_duration(-3) == "0s"


async def test_status_report_lists_sites_and_caches(tmp_path: Path) -> None:
    manager = MonitorManager(
        state_store=StateStore(tmp_path / "state.json"),
        snapshot_store=SnapshotStore(tmp_path / "snaps"),
        deriver=SubUrlDeriver(("https://app.{domain}",)),
        monitor_factory=StubMonitor,
        restart_delay=0,
    )
    cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
    cache.load()
    clock_now = 10_000.0
    reporter = StatusReporter(
        version="9.9", manager=manager, cache=cache, browser=Browser(True), clock=lambda: clock_now
    )
    await manager.start()
    try:
        running = await manager.add_site("https://a.io")
        running.last_change = LastChange(9_000.0, "https://a.io", ChangeCounts(1, 0, 0))
        stats = manager.stats("https://a.io")
        assert stats is not None
        stats.rounds, stats.last_round_at, stats.last_round_seconds, stats.last_pages = (
            7,
            9_970.0,
            2.4,
            3,
        )
        await manager.add_site("https://b.io")
        await manager.pause_site("https://b.io")
        clock_now = 10_125.0

        text = reporter.render()
    finally:
        await manager.shutdown()
    lines = text.splitlines()
    assert lines[0] == "🤖 scraper-bot 9.9, up 2m 5s"
    assert lines[1] == "Browser: connected"
    assert lines[2] == "Sites: 2 total, 1 running, 1 paused"
    assert lines[3].startswith(
        "• https://a.io: last round 2m 35s ago (2.4s, 3 page(s), 7 rounds); last change "
    )
    assert lines[4] == "• https://b.io: paused"
    assert lines[5] == "Address cache: 0 known contract(s), 0 rejected"


async def test_status_marks_disconnected_browser_and_starting_sites(tmp_path: Path) -> None:
    manager = MonitorManager(
        state_store=StateStore(tmp_path / "state.json"),
        snapshot_store=SnapshotStore(tmp_path / "snaps"),
        deriver=SubUrlDeriver(()),
        monitor_factory=StubMonitor,
        restart_delay=0,
    )
    cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
    reporter = StatusReporter(version="1", manager=manager, cache=cache, browser=Browser(False))
    await manager.start()
    try:
        await manager.add_site("https://a.io")
        text = reporter.render()
    finally:
        await manager.shutdown()
    assert "Browser: DISCONNECTED" in text
    assert "• https://a.io: starting" in text


async def test_status_shows_block_state(tmp_path: Path) -> None:
    manager = MonitorManager(
        state_store=StateStore(tmp_path / "state.json"),
        snapshot_store=SnapshotStore(tmp_path / "snaps"),
        deriver=SubUrlDeriver(()),
        monitor_factory=StubMonitor,
        restart_delay=0,
    )
    cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
    reporter = StatusReporter(version="1", manager=manager, cache=cache, browser=Browser(True))
    await manager.start()
    try:
        await manager.add_site("https://a.io")
        stats = manager.stats("https://a.io")
        assert stats is not None
        stats.last_round_at = 1.0
        stats.block_level, stats.block_reason, stats.blocked_since = 2, "HTTP 403", 1.0
        text = reporter.render()
    finally:
        await manager.shutdown()
    assert "⚠️ BLOCKED? HTTP 403 since" in text
    assert "interval x4" in text


async def test_status_lists_turbo_sessions_and_schedules(tmp_path: Path) -> None:
    manager = MonitorManager(
        state_store=StateStore(tmp_path / "state.json"),
        snapshot_store=SnapshotStore(tmp_path / "snaps"),
        deriver=SubUrlDeriver(()),
        monitor_factory=StubMonitor,
        restart_delay=0,
    )
    cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
    clock_now = 10_000.0
    turbo = TurboManager(
        tuning=FakeTuning(turbo_max_sites=2),
        deriver=SubUrlDeriver(()),
        prober=FakeProber(),
        session_factory=StubTurboSession,
        clock=lambda: clock_now,
    )
    reporter = StatusReporter(
        version="1",
        manager=manager,
        cache=cache,
        browser=Browser(True),
        turbo=turbo,
        clock=lambda: clock_now,
    )
    starts_at = clock_now + 3_600
    try:
        session = await turbo.start("https://a.io", 10)
        assert isinstance(session, StubTurboSession)
        session.stats.cycles, session.pending_count = 3, 2
        session.stats.found.append(
            ConfirmedContract(Address(Chain.ETHEREUM, f"0x{1:040x}"), "Ethereum")
        )
        plan = await turbo.start("https://b.io", 5, start_at=starts_at)
        assert isinstance(plan, ScheduledTurbo)
        text = reporter.render()
    finally:
        await turbo.shutdown()
    lines = text.splitlines()
    assert lines[2] == "Sites: 0 total, 0 running, 0 paused"
    assert lines[3] == "🚀 Turbo https://a.io: 3 cycles, 1 confirmed, 2 pending, 10m 0s left"
    assert lines[4] == (
        f"⏰ Turbo https://b.io: scheduled at {format_schedule(starts_at, clock_now)} for 5 min"
    )
    assert lines[5].startswith("Address cache:")


async def test_status_counts_pages_timing_out(tmp_path: Path) -> None:
    manager = MonitorManager(
        state_store=StateStore(tmp_path / "state.json"),
        snapshot_store=SnapshotStore(tmp_path / "snaps"),
        deriver=SubUrlDeriver(()),
        monitor_factory=StubMonitor,
        restart_delay=0,
    )
    cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
    reporter = StatusReporter(version="1", manager=manager, cache=cache, browser=Browser(True))
    await manager.start()
    try:
        await manager.add_site("https://a.io")
        stats = manager.stats("https://a.io")
        assert stats is not None
        stats.last_round_at = 1.0
        stats.timing_out = 2
        text = reporter.render()
    finally:
        await manager.shutdown()
    assert "; 🐢 2 page(s) timing out" in text


class CountingPending:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self, site_url: str | None = None) -> int:
        return self._count


async def test_status_says_how_many_addresses_wait_for_deployment(tmp_path: Path) -> None:
    manager = MonitorManager(
        state_store=StateStore(tmp_path / "state.json"),
        snapshot_store=SnapshotStore(tmp_path / "snaps"),
        deriver=SubUrlDeriver(()),
        monitor_factory=StubMonitor,
        restart_delay=0,
    )
    cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
    for count, shown in ((3, True), (0, False)):
        reporter = StatusReporter(
            version="1",
            manager=manager,
            cache=cache,
            browser=Browser(True),
            pending=CountingPending(count),  # type: ignore[arg-type]
        )
        text = reporter.render()
        assert ("⏳ 3 address(es) watched until they become a token" in text) is shown


async def test_status_names_rpc_endpoints_that_do_not_answer(tmp_path: Path) -> None:
    manager = MonitorManager(
        state_store=StateStore(tmp_path / "state.json"),
        snapshot_store=SnapshotStore(tmp_path / "snaps"),
        deriver=SubUrlDeriver(()),
        monitor_factory=StubMonitor,
        restart_delay=0,
    )
    cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
    failing = [FailingEndpoint("Sui", 425.0, "sui_getObject via sui.rpc: HTTP 502")]
    reporter = StatusReporter(
        version="1", manager=manager, cache=cache, browser=Browser(True), rpc_health=lambda: failing
    )
    assert "⚠️ RPC not answering: Sui for 7m 5s" in reporter.render()
    failing.clear()
    assert "RPC not answering" not in reporter.render()


async def test_a_crashed_monitor_is_stopped_not_starting(tmp_path: Path) -> None:
    def crashing(site: Any, persist: Any, on_auto_pause: Any) -> StubMonitor:
        return StubMonitor(site, persist, on_auto_pause, crash=True)

    manager = MonitorManager(
        state_store=StateStore(tmp_path / "state.json"),
        snapshot_store=SnapshotStore(tmp_path / "snaps"),
        deriver=SubUrlDeriver(()),
        monitor_factory=crashing,
        restart_delay=0,
    )
    cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
    cache.load()
    reporter = StatusReporter(version="9.9", manager=manager, cache=cache, browser=Browser(True))
    await manager.start()
    try:
        await manager.add_site("https://a.io")
        await asyncio.sleep(0)  # the monitor runs and crashes
        text = reporter.render()
    finally:
        await manager.shutdown()
    assert "Sites: 1 total, 0 running, 0 paused, 1 stopped" in text
    # restart_delay=0: nothing restarts it by itself (production: 30 s after each crash)
    assert "• https://a.io: ⛔ stopped by an error: /resume restarts it" in text
