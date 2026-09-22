from pathlib import Path

from scraper_bot.models import ChangeCounts, LastChange
from scraper_bot.monitor_manager import MonitorManager
from scraper_bot.status import StatusReporter, format_duration
from scraper_bot.storage import AddressCache, SnapshotStore, StateStore
from scraper_bot.urls import SubUrlDeriver
from tests.conftest import StubMonitor


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
