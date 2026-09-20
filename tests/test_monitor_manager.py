import asyncio
from pathlib import Path
from typing import Any

import pytest

from scraper_bot.models import SiteState
from scraper_bot.monitor_manager import (
    MonitorManager,
    SiteAlreadyPaused,
    SiteAlreadyTracked,
    SiteNotPaused,
    SiteNotTracked,
)
from scraper_bot.storage import LegacyStateImporter, SnapshotStore, StateStore
from scraper_bot.urls import InvalidUrlError, SubUrlDeriver
from tests.conftest import StubMonitor, node

ROOT = "https://example.com"
APP = "https://app.example.com"
SIGNATURE = (node("p", "x"),)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        restart_delay: float = 0.0,
        crash_first: bool = False,
        legacy: LegacyStateImporter | None = None,
    ) -> None:
        self.state = StateStore(tmp_path / "state.json")
        self.snapshots = SnapshotStore(tmp_path / "snaps")
        self.monitors: list[StubMonitor] = []
        self.crash_first = crash_first
        self.manager = MonitorManager(
            state_store=self.state,
            snapshot_store=self.snapshots,
            deriver=SubUrlDeriver(("https://app.{domain}",)),
            monitor_factory=self.factory,
            legacy_importer=legacy,
            restart_delay=restart_delay,
        )

    def factory(self, site: SiteState, persist: Any, on_auto_pause: Any) -> StubMonitor:
        crash = self.crash_first and not self.monitors
        monitor = StubMonitor(site, persist, on_auto_pause, crash=crash)
        self.monitors.append(monitor)
        return monitor


async def test_add_site_normalises_persists_and_starts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.manager.start()
    try:
        site = await h.manager.add_site("Example.com/")
        assert site.url == ROOT
        assert h.manager.is_running(ROOT)
        await asyncio.sleep(0)
        assert h.monitors[0].started
        assert ROOT in h.state.load()
        with pytest.raises(SiteAlreadyTracked):
            await h.manager.add_site("https://example.com")
        with pytest.raises(InvalidUrlError):
            await h.manager.add_site("nope")
    finally:
        await h.manager.shutdown()
    assert not h.manager.is_running(ROOT)
    assert h.monitors[0].stopped


async def test_pause_and_resume(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.manager.start()
    try:
        await h.manager.add_site(ROOT)
        with pytest.raises(SiteNotPaused):
            await h.manager.resume_site(ROOT)
        paused = await h.manager.pause_site(ROOT)
        assert paused.paused and not h.manager.is_running(ROOT)
        assert h.state.load()[ROOT].paused is True
        with pytest.raises(SiteAlreadyPaused):
            await h.manager.pause_site(ROOT)
        resumed = await h.manager.resume_site(ROOT)
        assert not resumed.paused and h.manager.is_running(ROOT)
        assert len(h.monitors) == 2
        with pytest.raises(SiteNotTracked):
            await h.manager.pause_site("https://unknown.io")
    finally:
        await h.manager.shutdown()


async def test_remove_stops_monitor_and_deletes_only_its_snapshots(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.manager.start()
    try:
        await h.manager.add_site(ROOT)
        await h.manager.add_site("https://other.io")
        for url in (ROOT, APP, "https://other.io"):
            h.snapshots.save(url, SIGNATURE)
        removed = await h.manager.remove_site("example.com")
        assert removed.url == ROOT
        assert not h.manager.is_running(ROOT)
        assert h.snapshots.load(ROOT) is None and h.snapshots.load(APP) is None
        assert h.snapshots.load("https://other.io") == SIGNATURE
        assert ROOT not in h.state.load()
        with pytest.raises(SiteNotTracked):
            await h.manager.remove_site(ROOT)
    finally:
        await h.manager.shutdown()


async def test_start_restores_state_and_prunes_orphan_snapshots(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.state.save({ROOT: SiteState(ROOT, paused=True), "https://b.io": SiteState("https://b.io")})
    for url in (ROOT, APP, "https://gone.io"):
        h.snapshots.save(url, SIGNATURE)
    await h.manager.start()
    try:
        assert not h.manager.is_running(ROOT)
        assert h.manager.is_running("https://b.io")
        assert h.snapshots.load("https://gone.io") is None
        assert h.snapshots.load(ROOT) == SIGNATURE and h.snapshots.load(APP) == SIGNATURE
        assert [site.url for site in h.manager.sites()] == ["https://b.io", ROOT]
    finally:
        await h.manager.shutdown()


async def test_auto_pause_marks_paused_and_stops_monitor(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.manager.start()
    try:
        await h.manager.add_site(ROOT)
        monitor = h.monitors[0]
        await monitor.on_auto_pause(ROOT, APP)
        assert monitor.stopped
        assert h.state.load()[ROOT].paused is True
        await asyncio.sleep(0)
        assert not h.manager.is_running(ROOT)
        await h.manager.auto_pause(ROOT, APP)  # idempotent
        await h.manager.resume_site(ROOT)
        assert h.manager.is_running(ROOT)
    finally:
        await h.manager.shutdown()


async def test_crashed_monitor_is_restarted(tmp_path: Path) -> None:
    h = Harness(tmp_path, restart_delay=0.01, crash_first=True)
    await h.manager.start()
    try:
        await h.manager.add_site(ROOT)
        for _ in range(100):
            if len(h.monitors) == 2 and h.manager.is_running(ROOT):
                break
            await asyncio.sleep(0.01)
        assert len(h.monitors) == 2
        assert h.manager.is_running(ROOT)
    finally:
        await h.manager.shutdown()


async def test_legacy_files_are_imported_when_no_state_exists(tmp_path: Path) -> None:
    (tmp_path / "monitored_sites.txt").write_text("https://a.io\nhttps://b.io\n", encoding="utf-8")
    h = Harness(tmp_path, legacy=LegacyStateImporter(tmp_path))
    await h.manager.start()
    try:
        assert [site.url for site in h.manager.sites()] == ["https://a.io", "https://b.io"]
        assert h.state.exists()
        assert h.manager.is_running("https://a.io")
    finally:
        await h.manager.shutdown()
