import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest

from scraper_bot.address_extractor import AddressExtractor
from scraper_bot.blocking import BlockDetector
from scraper_bot.fetcher import FetchResult
from scraper_bot.models import Address, Chain, ConfirmedContract
from scraper_bot.storage import AddressCache
from scraper_bot.turbo import (
    ScheduledTurbo,
    TurboAlreadyRunning,
    TurboLimitReached,
    TurboLimits,
    TurboManager,
    TurboNotRunning,
    TurboSession,
)
from scraper_bot.urls import SubUrlDeriver
from tests.conftest import (
    FakeFetcher,
    FakeNotifier,
    FakeProber,
    FakeValidator,
    StubTurboSession,
)

ROOT = "https://example.com"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ADDRESS = Address(Chain.ETHEREUM, USDC)
EMPTY = "<html><body><h1>Soon</h1></body></html>"
WITH_ADDRESS = f"<html><body><h1>Live</h1><p>{USDC}</p></body></html>"
WITH_TWO = f"<html><body><p>{USDC}</p><p>{USDC_BASE}</p></body></html>"
CHALLENGE = "<html><head><title>Just a moment...</title></head></html>"
LIMITS = TurboLimits(
    interval=0.01,
    default_minutes=30,
    max_minutes=180,
    stop_after=1,
    max_sites=1,
    max_pages=4,
    recheck_interval=0.0,  # re-check on every cycle
)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        limits: TurboLimits = LIMITS,
        duration: float = 60.0,
        real_clock: bool = False,
    ) -> None:
        self.fetcher = FakeFetcher()
        self.validator = FakeValidator()
        self.notifier = FakeNotifier()
        self.cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
        self.cache.load()
        self.now = 1_000.0
        self.session = TurboSession(
            ROOT,
            [ROOT],
            fetcher=self.fetcher,
            extractor=AddressExtractor(),
            validator=self.validator,  # type: ignore[arg-type]
            cache=self.cache,
            notifier=self.notifier,  # type: ignore[arg-type]
            block_detector=BlockDetector(),
            limits=limits,
            duration=duration,
            clock=time.time if real_clock else (lambda: self.now),
        )


async def test_new_address_is_announced_then_confirmed_and_ends_the_session(tmp_path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)
    h.validator.reject_then_confirm(ADDRESS, "Base", rejections=1)
    stats = await h.session.run()
    contract = ConfirmedContract(ADDRESS, "Base")
    assert h.notifier.unverified == [(ADDRESS, ROOT)]
    assert h.notifier.confirmed == [(contract, ROOT)]
    assert stats.found == [contract]
    assert stats.stop_reason == "found 1 confirmed contract(s)"
    assert stats.cycles == 3 and stats.renders == 3
    assert h.notifier.turbo_started_calls == [(ROOT, 1, 0.01, 1.0, 1)]
    assert h.notifier.turbo_finished_calls == [(ROOT, "found 1 confirmed contract(s)", 1)]
    assert h.validator.negative_cache_calls and not any(h.validator.negative_cache_calls)


async def test_address_present_from_the_start_is_watched_silently(tmp_path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(ROOT, WITH_ADDRESS)
    h.validator.reject_then_confirm(ADDRESS, "Ethereum", rejections=2)
    stats = await h.session.run()
    assert h.notifier.unverified == []
    assert h.notifier.confirmed == [(ConfirmedContract(ADDRESS, "Ethereum"), ROOT)]
    assert stats.stop_reason == "found 1 confirmed contract(s)"
    assert len(h.validator.calls) == 3


async def test_already_known_contracts_are_ignored(tmp_path) -> None:
    h = Harness(tmp_path, duration=0.05, real_clock=True)
    h.cache.add_known(ConfirmedContract(ADDRESS, "Base"))
    h.fetcher.html(ROOT, WITH_ADDRESS)
    stats = await h.session.run()
    assert stats.stop_reason == "time limit reached"
    assert stats.found == [] and h.notifier.unverified == []
    assert h.validator.calls == []


async def test_confirmation_by_the_regular_monitor_counts_without_a_second_alert(tmp_path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(ROOT, WITH_ADDRESS)
    h.validator.known(ADDRESS)
    stats = await h.session.run()
    assert stats.found == [ConfirmedContract(ADDRESS, "unknown network")]
    assert h.notifier.confirmed == []
    assert stats.stop_reason == "found 1 confirmed contract(s)"


async def test_block_signal_ends_the_session(tmp_path) -> None:
    h = Harness(tmp_path)
    h.fetcher.script(ROOT, FetchResult(ROOT, 200, EMPTY), FetchResult(ROOT, 403, CHALLENGE))
    stats = await h.session.run()
    assert stats.stop_reason == "possible IP block (HTTP 403 (just a moment))"
    assert stats.cycles == 2


async def test_time_limit_and_manual_stop(tmp_path) -> None:
    timed = Harness(tmp_path, duration=0.05, real_clock=True)
    timed.fetcher.html(ROOT, EMPTY)
    stats = await timed.session.run()
    assert stats.stop_reason == "time limit reached" and stats.cycles >= 1

    manual = Harness(tmp_path)
    manual.fetcher.html(ROOT, EMPTY)
    task = asyncio.create_task(manual.session.run())
    for _ in range(100):
        if manual.session.stats.cycles >= 2:
            break
        await asyncio.sleep(0.005)
    manual.session.request_stop()
    stats = await asyncio.wait_for(task, timeout=2)
    assert stats.stop_reason == "stopped manually"


async def test_stop_after_zero_keeps_running_after_a_find(tmp_path) -> None:
    limits = replace(LIMITS, stop_after=0)
    h = Harness(tmp_path, limits=limits, duration=0.05, real_clock=True)
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)
    h.validator.confirm(ADDRESS, "Base")
    stats = await h.session.run()
    assert stats.found == [ConfirmedContract(ADDRESS, "Base")]
    assert stats.stop_reason == "time limit reached"


async def test_watch_list_is_capped(tmp_path) -> None:
    limits = replace(LIMITS, max_watch=1)
    h = Harness(tmp_path, limits=limits, duration=0.05, real_clock=True)
    h.fetcher.html(ROOT, WITH_TWO)
    await h.session.run()
    assert all(len(batch) == 1 for batch in h.validator.calls)


# ── TurboManager ────────────────────────────────────────────────────────────
def make_manager(*, sleep=asyncio.sleep, **overrides) -> TurboManager:
    limits = replace(LIMITS, interval=2, **overrides)
    return TurboManager(
        limits=limits,
        deriver=SubUrlDeriver(("https://app.{domain}", "https://{domain}/docs")),
        prober=FakeProber({"https://app.example.com": 404}),
        session_factory=StubTurboSession,
        sleep=sleep,
    )


async def test_manager_starts_limits_and_stops_sessions() -> None:
    manager = make_manager()
    try:
        session = await manager.start("Example.com", 10)
        assert isinstance(session, StubTurboSession)
        assert session.pages == [ROOT, "https://example.com/docs"]
        assert session.duration == 600
        assert [s.url for s in manager.active()] == [ROOT]
        with pytest.raises(TurboAlreadyRunning):
            await manager.start(ROOT)
        with pytest.raises(TurboLimitReached):
            await manager.start("https://other.io")
        assert await manager.stop(ROOT) == ROOT
        assert session.stop_reason == "stopped manually"
        assert manager.active() == []
        with pytest.raises(TurboNotRunning):
            await manager.stop(ROOT)
    finally:
        await manager.shutdown()


async def test_manager_clamps_duration_and_caps_pages() -> None:
    manager = make_manager(max_pages=1, max_minutes=5)
    try:
        session = await manager.start(ROOT, 999)
        assert session.pages == [ROOT]
        assert session.duration == 5 * 60
        await manager.stop(ROOT)
        default = await manager.start(ROOT)
        assert default.duration == 5 * 60  # the default of 30 min is clamped as well
    finally:
        await manager.shutdown()


async def test_manager_schedules_and_cancels_sessions() -> None:
    manager = make_manager(sleep=lambda seconds: asyncio.sleep(seconds / 100))
    try:
        plan = await manager.start(ROOT, 5, start_at=time.time() + 5)
        assert isinstance(plan, ScheduledTurbo)
        assert manager.scheduled() == [plan] and manager.active() == []
        with pytest.raises(TurboAlreadyRunning):
            await manager.start(ROOT)
        for _ in range(100):
            if manager.active():
                break
            await asyncio.sleep(0.01)
        assert [s.url for s in manager.active()] == [ROOT] and manager.scheduled() == []
        await manager.stop(ROOT)

        later = await manager.start(ROOT, 5, start_at=time.time() + 60)
        assert isinstance(later, ScheduledTurbo)
        await manager.stop(ROOT)
        assert manager.scheduled() == []
    finally:
        await manager.shutdown()
