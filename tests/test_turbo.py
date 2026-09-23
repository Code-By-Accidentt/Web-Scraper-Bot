import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import pytest

import scraper_bot.turbo as turbo_module
from scraper_bot.address_extractor import AddressExtractor
from scraper_bot.blocking import BlockDetector
from scraper_bot.fetcher import FetchResult
from scraper_bot.models import Address, Chain, ConfirmedContract
from scraper_bot.storage import AddressCache
from scraper_bot.turbo import (
    LimitsSource,
    ScheduledTurbo,
    TurboAlreadyRunning,
    TurboLimitReached,
    TurboLimits,
    TurboManager,
    TurboNotRunning,
    TurboSession,
    TurboStats,
)
from scraper_bot.urls import SubUrlDeriver
from tests.conftest import (
    FakeFetcher,
    FakeNotifier,
    FakeProber,
    FakeTuning,
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
        limits: LimitsSource = LIMITS,
        duration: float = 60.0,
        real_clock: bool = False,
        pages: list[str] | None = None,
        fetcher: FakeFetcher | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.fetcher = fetcher or FakeFetcher()
        self.validator = FakeValidator()
        self.notifier = FakeNotifier()
        self.cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
        self.cache.load()
        self.now = 1_000.0
        self.session = TurboSession(
            ROOT,
            pages or [ROOT],
            fetcher=self.fetcher,
            extractor=AddressExtractor(),
            validator=self.validator,  # type: ignore[arg-type]
            cache=self.cache,
            notifier=self.notifier,  # type: ignore[arg-type]
            block_detector=BlockDetector(),
            limits=limits,
            duration=duration,
            clock=time.time if real_clock else (lambda: self.now),
            sleep=sleep,
        )


async def instant(_seconds: float) -> None:
    """Stand-in for the between-cycle pause: yields once, sleeps for nothing."""
    await asyncio.sleep(0)


async def wait_for_cycles(session: TurboSession, count: int) -> None:
    for _ in range(400):
        if session.stats.cycles >= count:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"session did not reach {count} cycle(s)")


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


async def test_watch_list_is_capped(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turbo_module, "_MAX_WATCH", 1)
    h = Harness(tmp_path, duration=0.05, real_clock=True)
    h.fetcher.html(ROOT, WITH_TWO)
    await h.session.run()
    assert all(len(batch) == 1 for batch in h.validator.calls)


async def test_running_session_follows_live_limit_changes(tmp_path) -> None:
    live = replace(LIMITS, stop_after=0)
    h = Harness(tmp_path, limits=lambda: live, duration=5.0, real_clock=True)
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)
    h.validator.confirm(ADDRESS, "Base")
    task = asyncio.create_task(h.session.run())
    for _ in range(400):
        if h.session.stats.found:
            break
        await asyncio.sleep(0.005)
    assert h.session.stats.found and not task.done()  # stop_after=0: keeps going
    live = replace(live, stop_after=1)  # what /set turbo_stop_after 1 does meanwhile
    stats = await asyncio.wait_for(task, timeout=2)
    assert stats.stop_reason == "found 1 confirmed contract(s)"


# ── TurboManager ────────────────────────────────────────────────────────────
def manager_tuning(**overrides) -> FakeTuning:
    tuning = FakeTuning(turbo_interval=2, turbo_recheck_interval=5)
    for key, value in overrides.items():
        setattr(tuning, f"turbo_{key}", value)
    return tuning


def make_manager(*, sleep=asyncio.sleep, tuning: FakeTuning | None = None, **overrides):
    return TurboManager(
        tuning=tuning or manager_tuning(**overrides),
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


async def test_manager_hands_sessions_live_limits() -> None:
    tuning = manager_tuning()
    manager = make_manager(tuning=tuning)
    try:
        session = await manager.start(ROOT)
        assert session.limits().interval == 2
        tuning.turbo_interval = 7  # what /set turbo_interval 7 does
        assert session.limits().interval == 7
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


class StubbornSession(StubTurboSession):
    """Ignores request_stop(), like a session stuck in a long render."""

    def request_stop(self) -> None:
        self.stop_reason = "stopped manually"


async def test_cancelling_a_pending_stop_is_not_swallowed() -> None:
    manager = TurboManager(
        tuning=manager_tuning(),
        deriver=SubUrlDeriver(()),
        prober=FakeProber(),
        session_factory=StubbornSession,
    )
    try:
        await manager.start(ROOT)
        stopping = asyncio.create_task(manager.stop(ROOT))
        await asyncio.sleep(0)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
    finally:
        await manager.shutdown()


class SlowProber(FakeProber):
    """Probe that takes a moment, opening the launch window a concurrent /turbo could abuse."""

    async def probe(self, url: str):
        await asyncio.sleep(0.05)
        return await super().probe(url)


def make_slow_manager(**overrides) -> TurboManager:
    return TurboManager(
        tuning=manager_tuning(**overrides),
        deriver=SubUrlDeriver(("https://app.{domain}",)),
        prober=SlowProber({"https://app.example.com": 404}),
        session_factory=StubTurboSession,
        sleep=lambda seconds: asyncio.sleep(seconds / 1000),
    )


async def test_concurrent_starts_during_launch_are_rejected() -> None:
    manager = make_slow_manager()
    try:
        results = await asyncio.gather(
            manager.start(ROOT), manager.start(ROOT), return_exceptions=True
        )
        assert sum(isinstance(r, StubTurboSession) for r in results) == 1
        assert sum(isinstance(r, TurboAlreadyRunning) for r in results) == 1
        assert len(manager.active()) == 1
    finally:
        await manager.shutdown()


async def test_launch_in_progress_occupies_the_site_slot() -> None:
    manager = make_slow_manager()
    try:
        first = asyncio.create_task(manager.start(ROOT))
        await asyncio.sleep(0.01)  # first start is probing sub-pages now
        with pytest.raises(TurboLimitReached):
            await manager.start("https://other.io")
        assert isinstance(await first, StubTurboSession)
    finally:
        await manager.shutdown()


async def test_scheduled_launch_keeps_its_reservation() -> None:
    manager = make_slow_manager()
    try:
        plan = await manager.start(ROOT, 5, start_at=time.time() + 5)
        assert isinstance(plan, ScheduledTurbo)
        await asyncio.sleep(0.02)  # timer fired, launch is probing sub-pages
        assert manager.scheduled() == [] and manager.active() == []
        with pytest.raises(TurboAlreadyRunning):
            await manager.start(ROOT)
        for _ in range(100):
            if manager.active():
                break
            await asyncio.sleep(0.01)
        assert [s.url for s in manager.active()] == [ROOT]
    finally:
        await manager.shutdown()


async def test_zero_minutes_is_clamped_to_the_minimum() -> None:
    manager = make_manager()
    try:
        session = await manager.start(ROOT, 0)
        assert session.duration == 60
    finally:
        await manager.shutdown()


# ── TurboSession: pacing, skipped pages and properties ──────────────────────
async def test_recheck_interval_is_honoured(tmp_path) -> None:
    limits = replace(LIMITS, recheck_interval=5.0, stop_after=0)
    h = Harness(tmp_path, limits=limits, duration=600.0, sleep=instant)
    h.fetcher.html(ROOT, WITH_ADDRESS)
    task = asyncio.create_task(h.session.run())
    try:
        await wait_for_cycles(h.session, 3)
        assert h.validator.calls == [[ADDRESS]]  # never checked before: due at once
        h.now += 4
        await wait_for_cycles(h.session, h.session.stats.cycles + 2)
        assert len(h.validator.calls) == 1  # 4 s is less than the recheck interval
        h.now += 1
        await wait_for_cycles(h.session, h.session.stats.cycles + 2)
        assert h.validator.calls == [[ADDRESS], [ADDRESS]]
        await wait_for_cycles(h.session, h.session.stats.cycles + 2)
        assert len(h.validator.calls) == 2  # the clock stood still again
    finally:
        h.session.request_stop()
        await asyncio.wait_for(task, timeout=2)


async def test_fetch_errors_inside_a_cycle_are_skipped(tmp_path) -> None:
    docs, app = "https://example.com/docs", "https://app.example.com"
    h = Harness(tmp_path, pages=[ROOT, docs, app])
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)
    h.fetcher.script(docs, FetchResult(docs, None, error="Timeout 30000ms exceeded"))
    h.fetcher.html(app, WITH_TWO, status=500)  # server error: no block, not scanned either
    h.validator.confirm(ADDRESS, "Base")
    stats = await h.session.run()
    assert stats.stop_reason == "found 1 confirmed contract(s)"
    assert (stats.cycles, stats.renders) == (2, 6)
    assert h.notifier.unverified == [(ADDRESS, ROOT)]
    assert h.validator.calls == [[ADDRESS]]  # the addresses on the 500 page were never seen
    assert h.fetcher.calls.count(docs) == 2 and h.fetcher.calls.count(app) == 2


class SlowFetcher(FakeFetcher):
    """Every render takes longer than the turbo interval."""

    async def fetch(self, url: str) -> FetchResult:
        await asyncio.sleep(0.02)
        return await super().fetch(url)


async def test_slow_cycle_does_not_sleep(tmp_path) -> None:
    pauses: list[float] = []

    async def record(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) == 2:
            session.request_stop()

    slow = Harness(tmp_path, fetcher=SlowFetcher(), sleep=record)  # 0.01 s interval, 0.02 s render
    session = slow.session
    slow.fetcher.html(ROOT, EMPTY)
    stats = await session.run()
    assert stats.cycles == 2 and stats.stop_reason == "stopped manually"
    assert pauses == [0.0, 0.0]

    pauses.clear()
    fast = Harness(tmp_path, limits=replace(LIMITS, interval=1.0), sleep=record)
    session = fast.session
    fast.fetcher.html(ROOT, EMPTY)
    await session.run()
    assert len(pauses) == 2 and all(0.5 < pause <= 1.0 for pause in pauses)  # the remainder


async def test_real_session_properties(tmp_path) -> None:
    docs = "https://example.com/docs"
    limits = replace(LIMITS, stop_after=0)
    h = Harness(tmp_path, limits=limits, duration=120.0, pages=[ROOT, docs], sleep=instant)
    assert h.session.url == ROOT
    pages = h.session.pages
    pages.append("https://example.com/evil")
    assert h.session.pages == [ROOT, docs]  # a copy, not the session's own list
    assert h.session.limits == limits
    assert h.session.seconds_left == 120.0  # not started yet: the whole duration
    assert h.session.stats.started_at is None and h.session.stats.deadline is None

    h.fetcher.html(ROOT, EMPTY)
    h.fetcher.html(docs, EMPTY)
    task = asyncio.create_task(h.session.run())
    await wait_for_cycles(h.session, 1)
    assert (h.session.stats.started_at, h.session.stats.deadline) == (1000.0, 1120.0)
    assert h.session.seconds_left == 120.0
    h.now += 45
    assert h.session.seconds_left == 75.0
    h.now += 100  # past the deadline: never negative, and the session notices by itself
    assert h.session.seconds_left == 0.0
    stats = await asyncio.wait_for(task, timeout=2)
    assert stats.stop_reason == "time limit reached"
    assert stats.renders == 2 * stats.cycles


# ── TurboManager with real sessions, crashes and cancelled timers ───────────
class RealSessions:
    """Session factory building real TurboSession objects over the shared fakes."""

    def __init__(self, tmp_path: Path) -> None:
        self.fetcher = FakeFetcher()
        self.validator = FakeValidator()
        self.notifier = FakeNotifier()
        self.cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
        self.cache.load()

    def __call__(
        self, url: str, pages: list[str], duration: float, limits: LimitsSource
    ) -> TurboSession:
        return TurboSession(
            url,
            pages,
            fetcher=self.fetcher,
            extractor=AddressExtractor(),
            validator=self.validator,  # type: ignore[arg-type]
            cache=self.cache,
            notifier=self.notifier,  # type: ignore[arg-type]
            block_detector=BlockDetector(),
            limits=limits,
            duration=duration,
        )


async def test_real_session_is_cancelled_on_shutdown(tmp_path) -> None:
    factory = RealSessions(tmp_path)
    factory.fetcher.html(ROOT, EMPTY)
    manager = TurboManager(
        tuning=manager_tuning(),
        deriver=SubUrlDeriver(()),
        prober=FakeProber(),
        session_factory=factory,
        notifier=factory.notifier,  # type: ignore[arg-type]
    )
    session = await manager.start(ROOT)
    assert isinstance(session, TurboSession)
    await wait_for_cycles(session, 1)  # now waiting out the 2 s interval
    await manager.shutdown()
    await asyncio.sleep(0)  # lets the task's done-callback run
    assert session.stats.stop_reason == "bot is shutting down"
    assert factory.notifier.turbo_finished_calls == [(ROOT, "bot is shutting down", 0)]
    assert manager.active() == [] and manager.scheduled() == []
    assert factory.notifier.problems == []  # a cancellation is not a crash


class CrashingSession(StubTurboSession):
    async def run(self) -> TurboStats:
        raise RuntimeError("renderer died")


async def test_crashed_session_is_reported_as_a_problem() -> None:
    notifier = FakeNotifier()
    manager = TurboManager(
        tuning=manager_tuning(),
        deriver=SubUrlDeriver(()),
        prober=FakeProber(),
        session_factory=CrashingSession,
        notifier=notifier,  # type: ignore[arg-type]
    )
    try:
        await manager.start(ROOT)
        for _ in range(100):
            if notifier.problems:
                break
            await asyncio.sleep(0.005)
        assert [(key, detail) for key, _, detail, _ in notifier.problems] == [
            (f"turbo:{ROOT}", "RuntimeError: renderer died")
        ]
        _, title, _, hint = notifier.problems[0]
        assert ROOT in title and hint is not None and "/turbo" in hint
        assert manager.active() == []
        assert isinstance(await manager.start(ROOT), CrashingSession)  # the slot is free again
    finally:
        await manager.shutdown()


async def test_cancelling_a_scheduled_plan_clears_the_timer() -> None:
    prober = FakeProber()
    manager = TurboManager(
        tuning=manager_tuning(),
        deriver=SubUrlDeriver(("https://app.{domain}",)),
        prober=prober,
        session_factory=StubTurboSession,
        sleep=lambda seconds: asyncio.sleep(seconds / 100),
    )
    try:
        plan = await manager.start(ROOT, 5, start_at=time.time() + 2)
        assert isinstance(plan, ScheduledTurbo)
        timers = [t for t in asyncio.all_tasks() if t.get_name() == f"turbo-timer:{ROOT}"]
        assert len(timers) == 1
        await manager.stop(ROOT)
        assert manager.scheduled() == []
        await asyncio.sleep(0.05)  # well past the (scaled) start time
        assert timers[0].cancelled()
        assert prober.calls == [] and manager.active() == []  # nothing was launched
        assert not any(t.get_name() == f"turbo:{ROOT}" for t in asyncio.all_tasks())
        assert isinstance(await manager.start(ROOT), StubTurboSession)  # the slot is free
    finally:
        await manager.shutdown()
