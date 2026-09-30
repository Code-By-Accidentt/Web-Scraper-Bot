import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from scraper_bot.models.pages import Snapshot
from scraper_bot.models.sites import SiteState
from scraper_bot.monitoring import monitor_manager
from scraper_bot.monitoring.monitor_manager import (
    MonitorManager,
    SiteAlreadyPaused,
    SiteAlreadyTracked,
    SiteNotPaused,
    SiteNotTracked,
)
from scraper_bot.pages.urls import InvalidUrlError, SubUrlDeriver
from scraper_bot.storage.legacy import LegacyStateImporter
from scraper_bot.storage.snapshots import SnapshotStore
from scraper_bot.storage.state import StateStore
from tests.conftest import FakeNotifier, StubMonitor, node

ROOT = "https://example.com"
APP = "https://app.example.com"
SIGNATURE = (node("p", "x"),)


class LingeringMonitor(StubMonitor):
    """Keeps running after request_stop() until released, like a monitor mid-round."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.released = asyncio.Event()

    async def run(self) -> None:
        self.started = True
        await self.released.wait()


class SelfPausingMonitor(StubMonitor):
    """Hits the contract threshold at once: pauses the site from inside its own task and ends."""

    async def run(self) -> None:
        self.started = True
        await self.on_auto_pause(self.site.url, APP)


class CleaningUpMonitor(StubMonitor):
    """Ignores request_stop() (like a monitor mid-render); only cancellation ends it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cleaned_up = False

    async def run(self) -> None:
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)  # closing the browser page takes a moment
            self.cleaned_up = True
            raise


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        restart_delay: float = 0.0,
        crash_first: bool = False,
        lingering: bool = False,
        legacy: LegacyStateImporter | None = None,
        kind: type[StubMonitor] = StubMonitor,
    ) -> None:
        self.state = StateStore(tmp_path / "state.json")
        self.snapshots = SnapshotStore(tmp_path / "snaps")
        self.monitors: list[StubMonitor] = []
        self.crash_first = crash_first
        self.kind = LingeringMonitor if lingering else kind
        self.notifier = FakeNotifier()
        self.manager = MonitorManager(
            state_store=self.state,
            snapshot_store=self.snapshots,
            deriver=SubUrlDeriver(("https://app.{domain}",)),
            monitor_factory=self.factory,
            legacy_importer=legacy,
            restart_delay=restart_delay,
            notifier=self.notifier,  # type: ignore[arg-type]
        )

    def factory(self, site: SiteState, persist: Any, on_auto_pause: Any) -> StubMonitor:
        crash = self.crash_first and not self.monitors
        monitor = self.kind(site, persist, on_auto_pause, crash=crash)
        self.monitors.append(monitor)
        return monitor


async def crash_reported(h: Harness) -> None:
    """Yield until the first monitor's crash is reported, which also arms its restart timer.

    The yields take no time, so the caller's next step still runs before that timer can fire,
    however slow the machine is.
    """
    for _ in range(100):
        if h.notifier.problems:
            return
        await asyncio.sleep(0)
    raise AssertionError("the monitor did not crash")


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
        resumed, restarted = await h.manager.resume_site(ROOT)
        assert not resumed.paused and not restarted and h.manager.is_running(ROOT)
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
            h.snapshots.save(url, Snapshot(SIGNATURE))
        removed = await h.manager.remove_site("example.com")
        assert removed.url == ROOT
        assert not h.manager.is_running(ROOT)
        assert h.snapshots.load(ROOT) is None and h.snapshots.load(APP) is None
        assert h.snapshots.load("https://other.io").signature == SIGNATURE
        assert ROOT not in h.state.load()
        with pytest.raises(SiteNotTracked):
            await h.manager.remove_site(ROOT)
    finally:
        await h.manager.shutdown()


async def test_start_restores_state_and_prunes_orphan_snapshots(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.state.save({ROOT: SiteState(ROOT, paused=True), "https://b.io": SiteState("https://b.io")})
    for url in (ROOT, APP, "https://gone.io"):
        h.snapshots.save(url, Snapshot(SIGNATURE))
    await h.manager.start()
    try:
        assert not h.manager.is_running(ROOT)
        assert h.manager.is_running("https://b.io")
        assert h.snapshots.load("https://gone.io") is None
        assert (
            h.snapshots.load(ROOT).signature == SIGNATURE
            and h.snapshots.load(APP).signature == SIGNATURE
        )
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


async def test_resume_during_the_auto_pause_round_still_gets_a_monitor(tmp_path: Path) -> None:
    h = Harness(tmp_path, lingering=True)
    await h.manager.start()
    try:
        await h.manager.add_site(ROOT)
        first = h.monitors[0]
        assert isinstance(first, LingeringMonitor)
        await first.on_auto_pause(ROOT, APP)  # the monitor is told to stop, its round goes on
        assert h.manager.is_running(ROOT)
        await h.manager.resume_site(ROOT)  # the user answers the pause alert at once
        assert len(h.monitors) == 1  # the old task is still there, nothing new could start
        first.released.set()  # the old round ends
        for _ in range(100):
            if len(h.monitors) == 2 and h.manager.is_running(ROOT):
                break
            await asyncio.sleep(0.01)
        assert len(h.monitors) == 2 and h.manager.is_running(ROOT)
        assert h.notifier.problems == []
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
        await asyncio.sleep(0)
        assert [p[0] for p in h.notifier.problems] == [f"monitor:{ROOT}"]
        assert "RuntimeError: boom" in h.notifier.problems[0][2]
        assert "restarts automatically" in (h.notifier.problems[0][3] or "")
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


async def test_auto_pause_from_inside_the_monitor_task(tmp_path: Path) -> None:
    h = Harness(tmp_path, kind=SelfPausingMonitor)
    await h.manager.start()
    try:
        await h.manager.add_site(ROOT)
        for _ in range(100):
            if not h.manager.is_running(ROOT):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)  # let the task's done-callback run
        assert not h.manager.is_running(ROOT)
        assert h.monitors[0].stopped
        assert h.state.load()[ROOT].paused is True
        assert h.notifier.problems == []  # a normal end is not a crash
        assert len(h.monitors) == 1  # and a paused site gets no restart
        resumed, restarted = await h.manager.resume_site(ROOT)
        assert not resumed.paused and not restarted and h.manager.is_running(ROOT)
        assert len(h.monitors) == 2
    finally:
        await h.manager.shutdown()


async def test_remove_while_running_cancels_and_awaits_the_task(tmp_path: Path) -> None:
    h = Harness(tmp_path, kind=CleaningUpMonitor)
    await h.manager.start()
    try:
        await h.manager.add_site(ROOT)
        await asyncio.sleep(0)
        monitor = h.monitors[0]
        assert isinstance(monitor, CleaningUpMonitor) and monitor.started
        await h.manager.remove_site(ROOT)
        assert monitor.cleaned_up  # remove_site came back only once the task was gone
        assert monitor.stopped and not h.manager.is_running(ROOT)
        assert h.manager.sites() == []
        assert h.notifier.problems == []  # a cancelled task is not a crash
    finally:
        await h.manager.shutdown()


async def test_crash_without_restart_delay_reports_no_restart_hint(tmp_path: Path) -> None:
    h = Harness(tmp_path, restart_delay=0, crash_first=True)
    await h.manager.start()
    try:
        await h.manager.add_site(ROOT)
        for _ in range(100):
            if h.notifier.problems:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)  # room for a restart that must not happen
        assert len(h.notifier.problems) == 1
        key, title, detail, hint = h.notifier.problems[0]
        assert key == f"monitor:{ROOT}" and title == f"Monitor for {ROOT} crashed"
        assert detail == "RuntimeError: boom"
        assert "will not restart" in (hint or "")
        assert len(h.monitors) == 1
        assert not h.manager.is_running(ROOT)
        assert h.state.load()[ROOT].paused is False  # still registered, just stopped
    finally:
        await h.manager.shutdown()


@pytest.mark.parametrize("action", ["pause", "remove"])
async def test_restart_is_skipped_when_site_paused_or_removed_meanwhile(
    tmp_path: Path, action: str
) -> None:
    h = Harness(tmp_path, restart_delay=0.03, crash_first=True)
    await h.manager.start()
    try:
        await h.manager.add_site(ROOT)
        await crash_reported(h)
        assert "restarts automatically" in (h.notifier.problems[0][3] or "")
        if action == "pause":
            await h.manager.pause_site(ROOT)
        else:
            await h.manager.remove_site(ROOT)
        await asyncio.sleep(0.06)  # the restart timer (30 ms) fires meanwhile
        assert len(h.monitors) == 1
        assert not h.manager.is_running(ROOT)
        assert [site.url for site in h.manager.sites()] == ([ROOT] if action == "pause" else [])
    finally:
        await h.manager.shutdown()


async def test_invalid_url_to_a_site_command_reads_as_not_tracked(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    with pytest.raises(SiteNotTracked) as info:
        await h.manager.pause_site("not a url")
    assert info.value.url == "not a url"
    with pytest.raises(SiteNotTracked):
        await h.manager.resume_site("not a url")
    with pytest.raises(SiteNotTracked):
        await h.manager.remove_site("not a url")
    with pytest.raises(SiteNotTracked):
        h.manager.set_notifications("not a url", True)
    with pytest.raises(SiteNotTracked):
        h.manager.set_unverified("not a url", True)


async def test_legacy_import_skipped_when_state_file_exists_but_is_empty(tmp_path: Path) -> None:
    (tmp_path / "monitored_sites.txt").write_text("https://a.io\n", encoding="utf-8")
    h = Harness(tmp_path, legacy=LegacyStateImporter(tmp_path))
    h.state.save({})  # every site was removed on purpose; the legacy list must stay dead
    await h.manager.start()
    try:
        assert h.manager.sites() == []
        assert not h.manager.is_running("https://a.io")
        assert h.state.load() == {}
    finally:
        await h.manager.shutdown()


async def test_pause_state_and_removal_are_reported_to_the_pending_watch(tmp_path: Path) -> None:
    removed: list[str] = []
    h = Harness(tmp_path)
    h.manager._on_removed = removed.append
    await h.manager.start()
    try:
        await h.manager.add_site(ROOT)
        assert not h.manager.is_paused(ROOT)
        await h.manager.pause_site(ROOT)
        assert h.manager.is_paused(ROOT) and not h.manager.is_paused("https://other.io")
        await h.manager.remove_site(ROOT)
        assert removed == [ROOT]  # its watched addresses are forgotten with it
    finally:
        await h.manager.shutdown()


class SwallowingMonitor(StubMonitor):
    """Ends normally when stopped, the way asyncio.wait_for on Python 3.11 lets it."""

    async def run(self) -> None:
        self.started = True
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            return


async def test_a_monitor_ending_normally_during_shutdown_is_not_restarted(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, kind=SwallowingMonitor)
    await h.manager.start()
    await h.manager.add_site(ROOT)
    await h.manager.shutdown()
    await asyncio.sleep(0.05)
    assert len(h.monitors) == 1 and not h.manager.is_running(ROOT)


async def test_a_crash_restart_timer_does_not_fire_after_shutdown(tmp_path: Path) -> None:
    h = Harness(tmp_path, restart_delay=0.05, crash_first=True)
    await h.manager.start()
    await h.manager.add_site(ROOT)
    await crash_reported(h)  # its restart is 0.05 s away
    await h.manager.shutdown()
    await asyncio.sleep(0.1)
    assert len(h.monitors) == 1 and not h.manager.is_running(ROOT)


async def test_a_damaged_state_file_keeps_the_snapshots_and_is_reported(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.snapshots.save(ROOT, Snapshot(SIGNATURE))
    (tmp_path / "state.json").write_text("{broken", encoding="utf-8")
    await h.manager.start()
    try:
        assert h.manager.sites() == []
        assert h.snapshots.load(ROOT) is not None  # not pruned: the site may come back
        [(key, title, detail, _hint)] = h.notifier.problems
        assert key == "state" and "damaged" in title and "broken-" in detail
    finally:
        await h.manager.shutdown()


async def test_a_failed_save_is_reported_and_the_command_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    await h.manager.start()

    def full_disk(_sites: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(h.state, "save", full_disk)
    try:
        site = await h.manager.add_site(ROOT)
        assert site.url == ROOT and h.manager.is_running(ROOT)
        assert [problem[0] for problem in h.notifier.problems] == ["state-save"]
    finally:
        await h.manager.shutdown()


async def test_a_site_stored_with_a_default_port_is_still_found(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    legacy = "https://example.com:443"  # how 2.1 stored "/add example.com:443"
    h.state.save({legacy: SiteState(legacy, paused=True)})
    await h.manager.start()
    try:
        assert [site.url for site in h.manager.sites()] == [ROOT]  # stored anew at load
        assert list(StateStore(tmp_path / "state.json").load()) == [ROOT]
        assert (await h.manager.remove_site("example.com:443")).url == ROOT
        assert h.manager.sites() == []
    finally:
        await h.manager.shutdown()


async def test_a_site_added_with_a_route_before_2_1_1_is_found_by_it(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.state.save({"https://app.io": SiteState("https://app.io", paused=True)})
    await h.manager.start()
    try:
        assert (await h.manager.remove_site("https://app.io/#/presale")).url == "https://app.io"
        await h.manager.add_site("https://app.io/v2/")
        assert (await h.manager.remove_site("app.io/v2/#/x")).url == "https://app.io/v2/"
    finally:
        await h.manager.shutdown()


async def test_removing_a_site_keeps_the_snapshots_another_site_renders(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.manager.start()
    try:
        await h.manager.add_site(ROOT)
        www = (await h.manager.add_site("www.example.com")).url
        assert h.manager.sharing_pages(ROOT) == [www]
        shared = "https://app.example.com"
        for page in (ROOT, shared, www):
            h.snapshots.save(page, Snapshot(SIGNATURE))
        await h.manager.remove_site(www)
        assert h.snapshots.load(www) is None
        assert h.snapshots.load(shared) is not None  # still rendered for example.com
        assert h.manager.sharing_pages(ROOT) == []
    finally:
        await h.manager.shutdown()


async def test_after_a_damaged_state_file_the_next_start_still_has_the_readable_sites(
    tmp_path: Path,
) -> None:
    good = SiteState(ROOT).to_dict()
    (tmp_path / "state.json").write_text(
        json.dumps({"sites": {ROOT: good, "https://b.io": {"added_at": "abc"}}}), encoding="utf-8"
    )
    (tmp_path / "monitored_sites.txt").write_text("https://old-v1.io\n", encoding="utf-8")
    first = Harness(tmp_path, legacy=LegacyStateImporter(tmp_path))
    first.snapshots.save("https://b.io", Snapshot(SIGNATURE))
    await first.manager.start()
    await first.manager.shutdown()
    second = Harness(tmp_path, legacy=LegacyStateImporter(tmp_path))
    await second.manager.start()
    try:
        assert [site.url for site in second.manager.sites()] == [ROOT]  # no v1 import either
        assert second.snapshots.load("https://b.io") is not None  # kept while the copy waits
        assert second.notifier.problems == []  # reported once, at the first start
    finally:
        await second.manager.shutdown()


async def test_a_damaged_state_file_that_cannot_be_set_aside_is_not_saved_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scraper_bot.storage.state as storage_module

    def refuse(*_args: Any) -> None:
        raise PermissionError("locked by another program")

    monkeypatch.setattr(storage_module.os, "replace", refuse)
    monkeypatch.setattr(storage_module.shutil, "copyfile", refuse)
    (tmp_path / "state.json").write_text("{broken", encoding="utf-8")
    h = Harness(tmp_path)
    await h.manager.start()
    try:
        await h.manager.add_site("https://new.io")
        assert (tmp_path / "state.json").read_text(encoding="utf-8") == "{broken"
        [(_, _, detail, _)] = h.notifier.problems
        assert "could not be moved aside" in detail
    finally:
        await h.manager.shutdown()


class StuckMonitor(LingeringMonitor):
    """Goes on after it is cancelled until released, like a monitor stuck in a browser call."""

    async def run(self) -> None:
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await self.released.wait()
            raise


async def test_remove_answers_even_when_the_monitor_does_not_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitor_manager, "STOP_WAIT", 0.05)
    h = Harness(tmp_path, kind=StuckMonitor)
    await h.manager.start()
    await h.manager.add_site(ROOT)
    await asyncio.sleep(0)
    monitor = h.monitors[0]
    assert isinstance(monitor, StuckMonitor) and monitor.started
    [task] = [task for task in asyncio.all_tasks() if task.get_name() == f"monitor:{ROOT}"]
    try:
        async with asyncio.timeout(5):
            await h.manager.remove_site(ROOT)
        assert h.manager.sites() == [] and not task.done()  # answered; the task ends later
    finally:
        monitor.released.set()
        await asyncio.wait({task})
        await h.manager.shutdown()
