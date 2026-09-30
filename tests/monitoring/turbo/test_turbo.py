import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scraper_bot.chains.addresses.extractor import AddressExtractor
from scraper_bot.models.addresses import (
    Address,
    AddressKind,
    Chain,
    ConfirmedContract,
    ValidationResult,
    ValidationStatus,
)
from scraper_bot.models.sightings import Sighting, Timeline
from scraper_bot.monitoring.alerts import AlertRegistry
from scraper_bot.monitoring.pace import SitePace
from scraper_bot.monitoring.tokens import SiteTokens
from scraper_bot.monitoring.turbo.manager import (
    ScheduledTurbo,
    TurboAlreadyRunning,
    TurboLimitReached,
    TurboManager,
    TurboNotRunning,
)
from scraper_bot.monitoring.turbo.session import LimitsSource, TurboLimits, TurboSession, TurboStats
from scraper_bot.pages.fetching.blocking import BlockDetector
from scraper_bot.pages.fetching.result import BrowserUnavailable, FetchResult
from scraper_bot.pages.urls import SubUrlDeriver
from scraper_bot.storage.address_cache import AddressCache
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
WITH_ADDRESS = f"<html><body><h1>Live</h1><p>CA: {USDC}</p></body></html>"
WITH_TWO = f"<html><body><p>{USDC}</p><p>{USDC_BASE}</p></body></html>"
CHALLENGE = "<html><head><title>Just a moment...</title></head></html>"
LIMITS = TurboLimits(
    interval=0.01,
    default_minutes=30,
    max_minutes=180,
    stop_after=1,
    max_sites=1,
    max_pages=4,
)


class FakePending:
    """Stand-in for the pending watch: records what a session hands over."""

    def __init__(self) -> None:
        self.offers: list[tuple[Address, str, bool]] = []
        self.baseline: list[tuple[Address, str]] = []

    def offer(
        self,
        site_url: str,
        page_url: str,
        sighting: Sighting | None,
        result: ValidationResult,
        *,
        fast: bool = False,
    ) -> bool:
        self.offers.append((result.address, page_url, fast))
        return True

    def offer_baseline(self, site_url: str, page_url: str, sighting: Sighting) -> None:
        self.baseline.append((sighting.address, page_url))

    def announced(self, site_url: str, address: Address) -> frozenset[str]:
        return frozenset()


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
        self.pending = FakePending()
        self.cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
        self.cache.load()
        self.registry = AlertRegistry(self.cache)
        self.pace = SitePace()
        self.tokens = SiteTokens()
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
            pending=self.pending,  # type: ignore[arg-type]
            registry=self.registry,
            pace=self.pace,
            tokens=self.tokens,
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


async def test_a_new_token_is_announced_and_ends_the_session(tmp_path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)
    h.validator.confirm(ADDRESS, "Base")
    stats = await asyncio.wait_for(h.session.run(), timeout=2)
    contract = ConfirmedContract(ADDRESS, "Base")
    assert h.notifier.contracts == [(contract, ROOT)]
    [(sighting, timeline)] = h.notifier.contract_details
    assert sighting is not None and sighting.context == "Live CA:" and sighting.hinted
    assert timeline == Timeline(1_000.0, missing_at=1_000.0)  # the clock stood still
    assert h.notifier.unverified == []  # confirmed tokens only, never an early guess
    assert stats.found == [ConfirmedContract(ADDRESS, "Base")]  # recorded by the claim
    assert stats.stop_reason == "found 1 confirmed contract(s)"
    assert stats.cycles == 2 and stats.renders == 2
    assert h.notifier.turbo_started_calls == [(ROOT, 1, 0.01, 1.0, 1)]
    assert h.notifier.turbo_finished_calls == [(ROOT, "found 1 confirmed contract(s)", 1)]
    assert h.validator.negative_cache_calls == [False]
    assert not h.pace.light_fast(ROOT)  # the site is back to its usual pace


async def test_a_new_address_that_is_not_a_token_yet_waits_in_the_pending_watch(
    tmp_path,
) -> None:
    h = Harness(tmp_path, sleep=instant)
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)  # the validator rejects it: not deployed yet
    task = asyncio.create_task(h.session.run())
    await wait_for_cycles(h.session, 3)
    assert h.pending.offers == [(ADDRESS, ROOT, True)]  # handed over at the turbo pace
    assert h.notifier.contracts == []
    h.tokens.publish({ROOT}, ADDRESS, "Base")  # the watch saw it deployed and announced it
    stats = await asyncio.wait_for(task, timeout=2)
    assert stats.stop_reason == "found 1 confirmed contract(s)"
    assert len(h.validator.calls) == 1  # the session itself checked it once


async def test_addresses_present_from_the_start_are_left_to_the_watch(tmp_path) -> None:
    h = Harness(tmp_path, duration=0.05, real_clock=True)
    h.fetcher.html(ROOT, WITH_ADDRESS)
    stats = await asyncio.wait_for(h.session.run(), timeout=2)
    assert h.pending.baseline == [(ADDRESS, ROOT)]  # news only if it does not exist yet
    assert h.validator.calls == [] and h.notifier.contracts == []
    assert stats.stop_reason == "time limit reached"


async def test_known_tokens_are_ignored(tmp_path) -> None:
    h = Harness(tmp_path, duration=0.05, real_clock=True)
    h.cache.add_known(ConfirmedContract(ADDRESS, "Base"))
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)
    stats = await asyncio.wait_for(h.session.run(), timeout=2)
    assert stats.stop_reason == "time limit reached"
    assert stats.found == [] and h.validator.calls == [] and h.pending.offers == []


async def test_confirmation_by_the_regular_monitor_counts_without_a_second_alert(tmp_path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)
    h.validator.known(ADDRESS)
    validate_many = h.validator.validate_many

    async def announced_meanwhile(addresses, **options):
        # The regular monitor announced it while this check ran; its claim recorded it.
        h.registry.claim(ConfirmedContract(ADDRESS, "Base"))
        return await validate_many(addresses, **options)

    h.validator.validate_many = announced_meanwhile  # type: ignore[method-assign]
    stats = await asyncio.wait_for(h.session.run(), timeout=2)
    assert stats.found == [ConfirmedContract(ADDRESS, "Base")]
    assert h.notifier.contracts == []
    assert stats.stop_reason == "found 1 confirmed contract(s)"


async def test_a_token_claimed_elsewhere_is_not_announced_twice(tmp_path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)
    h.validator.confirm(ADDRESS, "Base")
    validate_many = h.validator.validate_many

    async def claimed_meanwhile(addresses, **options):
        # Another page gets the same shared verdict and claims the token first.
        h.registry.claim(ConfirmedContract(ADDRESS, "Base"))
        return await validate_many(addresses, **options)

    h.validator.validate_many = claimed_meanwhile  # type: ignore[method-assign]
    stats = await asyncio.wait_for(h.session.run(), timeout=2)
    assert h.notifier.contracts == []
    assert stats.stop_reason == "found 1 confirmed contract(s)"


async def test_block_signal_ends_the_session(tmp_path) -> None:
    h = Harness(tmp_path)
    h.fetcher.script(ROOT, FetchResult(ROOT, 200, EMPTY), FetchResult(ROOT, 403, CHALLENGE))
    stats = await asyncio.wait_for(h.session.run(), timeout=2)
    assert stats.stop_reason == "possible IP block (HTTP 403 (just a moment))"
    assert stats.cycles == 2


async def test_time_limit_and_manual_stop(tmp_path) -> None:
    timed = Harness(tmp_path, duration=0.05, real_clock=True)
    timed.fetcher.html(ROOT, EMPTY)
    stats = await asyncio.wait_for(timed.session.run(), timeout=2)
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
    async def pause(_seconds: float) -> None:
        h.now += LIMITS.interval  # each pause moves the fake clock on by one interval

    h = Harness(tmp_path, limits=replace(LIMITS, stop_after=0), duration=0.05, sleep=pause)
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)
    h.validator.confirm(ADDRESS, "Base")
    stats = await asyncio.wait_for(h.session.run(), timeout=2)
    assert [contract.address for contract in stats.found] == [ADDRESS]
    assert stats.stop_reason == "time limit reached" and stats.cycles > 2


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
    tuning = FakeTuning(turbo_interval=2)
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
        assert manager.scheduled() == [plan] and manager.active() == []  # still reserved
        with pytest.raises(TurboAlreadyRunning):
            await manager.start(ROOT)
        for _ in range(100):
            if manager.active():
                break
            await asyncio.sleep(0.01)
        assert [s.url for s in manager.active()] == [ROOT]
        assert manager.scheduled() == []
    finally:
        await manager.shutdown()


async def test_turbo_stop_cancels_a_scheduled_session_while_it_launches() -> None:
    manager = make_slow_manager()
    try:
        await manager.start(ROOT, 5, start_at=time.time() + 5)
        await asyncio.sleep(0.02)  # timer fired, launch is probing sub-pages
        assert await manager.stop(ROOT) == ROOT
        await asyncio.sleep(0.08)  # the probe would have finished by now
        assert manager.active() == [] and manager.scheduled() == []
    finally:
        await manager.shutdown()


async def test_no_session_starts_during_a_shutdown() -> None:
    manager = make_slow_manager()
    await manager.start(ROOT, 5, start_at=time.time() + 5)
    await asyncio.sleep(0.02)  # the launch is probing sub-pages
    await manager.shutdown()
    await asyncio.sleep(0.08)  # the probe would have finished by now
    assert manager.active() == []


async def test_zero_minutes_is_clamped_to_the_minimum() -> None:
    manager = make_manager()
    try:
        session = await manager.start(ROOT, 0)
        assert session.duration == 60
    finally:
        await manager.shutdown()


# ── TurboSession: pacing, skipped pages and properties ──────────────────────
async def test_fetch_errors_inside_a_cycle_are_skipped(tmp_path) -> None:
    docs, app = "https://example.com/docs", "https://app.example.com"
    h = Harness(tmp_path, pages=[ROOT, docs, app])
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)
    h.fetcher.html(app, WITH_TWO, status=500)  # server error: no block, not scanned either
    h.validator.confirm(ADDRESS, "Base")
    fetch = h.fetcher.fetch

    async def browser_gone_on_docs(url: str, **options: Any) -> FetchResult:
        result = await fetch(url, **options)
        if url == docs:
            raise BrowserUnavailable("browser could not start")  # fails the page, not the session
        return result

    h.fetcher.fetch = browser_gone_on_docs  # type: ignore[method-assign]
    stats = await asyncio.wait_for(h.session.run(), timeout=2)
    assert stats.stop_reason == "found 1 confirmed contract(s)"
    assert (stats.cycles, stats.renders) == (2, 6)
    assert h.notifier.contracts == [(ConfirmedContract(ADDRESS, "Base"), ROOT)]
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
    stats = await asyncio.wait_for(session.run(), timeout=2)
    assert stats.cycles == 2 and stats.stop_reason == "stopped manually"
    assert pauses == [0.0, 0.0]

    pauses.clear()
    fast = Harness(tmp_path, limits=replace(LIMITS, interval=1.0), sleep=record)
    session = fast.session
    fast.fetcher.html(ROOT, EMPTY)
    await asyncio.wait_for(session.run(), timeout=2)
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
        self.pending = FakePending()
        self.pace = SitePace()
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
            pending=self.pending,  # type: ignore[arg-type]
            registry=AlertRegistry(),
            pace=self.pace,
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
    assert not factory.pace.light_fast(ROOT)  # the site is back to its usual pace


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


async def test_a_page_that_failed_its_first_cycle_gets_its_baseline_later(tmp_path) -> None:
    docs = f"{ROOT}/docs"
    h = Harness(tmp_path, pages=[ROOT, docs], sleep=instant)
    h.fetcher.html(ROOT, EMPTY)
    h.fetcher.script(docs, FetchResult(docs, None, error="Timeout 30000ms exceeded"))
    h.fetcher.html(docs, WITH_ADDRESS)  # renders from the second cycle on
    h.validator.confirm(ADDRESS, "Base")  # an old token, not in the known cache
    task = asyncio.create_task(h.session.run())
    await wait_for_cycles(h.session, 3)
    h.session.request_stop()
    stats = await asyncio.wait_for(task, timeout=2)
    assert h.pending.baseline == [(ADDRESS, docs)]  # old news unless it does not exist yet
    assert h.notifier.contracts == [] and stats.found == [] and h.validator.calls == []


async def test_an_old_token_behind_a_new_lp_pair_does_not_end_the_session(tmp_path) -> None:
    pair = Address(Chain.ETHEREUM, USDC_BASE)
    h = Harness(tmp_path, sleep=instant)
    h.fetcher.html(ROOT, EMPTY, f"<html><body><a href='/chart/{USDC_BASE}'>Chart</a></body></html>")

    async def pair_of_a_known_token(addresses, **_options):  # type: ignore[no-untyped-def]
        return [
            ValidationResult(pair, ValidationStatus.REJECTED, "Base", kind=AddressKind.PAIR),
            ValidationResult(ADDRESS, ValidationStatus.ALREADY_KNOWN, via=pair),
        ]

    h.validator.validate_many = pair_of_a_known_token  # type: ignore[method-assign]
    task = asyncio.create_task(h.session.run())
    await wait_for_cycles(h.session, 3)
    h.session.request_stop()
    stats = await asyncio.wait_for(task, timeout=2)
    assert stats.found == [] and stats.stop_reason == "stopped manually"


async def test_a_late_page_does_not_hide_news_seen_on_a_page_read_before(tmp_path) -> None:
    app = f"{ROOT}/app"
    h = Harness(tmp_path, pages=[ROOT, app], sleep=instant)
    h.fetcher.script(ROOT, FetchResult(ROOT, None, error="Timeout 30000ms exceeded"))
    h.fetcher.html(ROOT, WITH_ADDRESS)  # the root renders from cycle two on
    h.fetcher.html(app, EMPTY, WITH_ADDRESS)  # /app was read empty in cycle one
    h.validator.confirm(ADDRESS, "Base")
    stats = await asyncio.wait_for(h.session.run(), timeout=2)
    assert h.notifier.contracts == [(ConfirmedContract(ADDRESS, "Base"), app)]
    assert h.pending.baseline == [] and stats.found


async def test_the_appearance_window_opens_when_the_previous_render_began(tmp_path) -> None:
    h = Harness(tmp_path, sleep=instant)
    h.fetcher.script(ROOT, FetchResult(ROOT, 200, EMPTY, started_at=990.0))
    h.fetcher.html(ROOT, WITH_ADDRESS)
    h.validator.confirm(ADDRESS, "Base")
    await asyncio.wait_for(h.session.run(), timeout=2)
    [(_, timeline)] = h.notifier.contract_details
    assert timeline is not None and timeline.missing_at == 990.0  # not the cycle's own start


async def test_a_block_on_one_page_does_not_cost_the_ca_another_page_shows(tmp_path) -> None:
    docs = "https://example.com/docs"
    h = Harness(tmp_path, pages=[ROOT, docs])
    h.fetcher.html(ROOT, EMPTY, WITH_ADDRESS)
    h.fetcher.html(docs, EMPTY, CHALLENGE)  # the second cycle: the docs page is challenged
    h.validator.confirm(ADDRESS, "Base")
    stats = await asyncio.wait_for(h.session.run(), timeout=2)
    assert h.notifier.contracts == [(ConfirmedContract(ADDRESS, "Base"), ROOT)]
    assert stats.found == [ConfirmedContract(ADDRESS, "Base")]
    assert stats.stop_reason == "found 1 confirmed contract(s)"


async def test_a_2xx_page_that_looks_like_a_challenge_is_still_read(tmp_path) -> None:
    h = Harness(tmp_path)
    looks_blocked = WITH_ADDRESS.replace("<html>", "<html><title>Just a moment...</title>")
    h.fetcher.html(ROOT, EMPTY, looks_blocked)
    h.validator.confirm(ADDRESS, "Base")
    stats = await asyncio.wait_for(h.session.run(), timeout=2)
    assert h.notifier.contracts == [(ConfirmedContract(ADDRESS, "Base"), ROOT)]
    assert stats.stop_reason == "found 1 confirmed contract(s)"  # the block would end it too
    assert stats.cycles == 2


async def test_a_direct_start_still_probing_at_shutdown_does_not_start() -> None:
    manager = make_slow_manager()
    launching = asyncio.create_task(manager.start(ROOT, 5))  # /turbo, not scheduled
    await asyncio.sleep(0.02)  # the launch is probing sub-pages
    await manager.shutdown()
    with pytest.raises(TurboNotRunning):
        await launching
    assert manager.active() == []


async def test_a_blank_first_render_is_not_the_baseline(tmp_path) -> None:
    h = Harness(tmp_path)
    blank = "<html><body></body></html>"
    h.fetcher.html(ROOT, blank, WITH_ADDRESS, WITH_ADDRESS)
    h.validator.confirm(ADDRESS, "Base")
    await h.session._cycle()  # hydrating: nothing to read yet
    await h.session._cycle()  # the first real read: the token there is old news
    assert h.notifier.contracts == []
    assert [address for address, _ in h.pending.baseline] == [ADDRESS]


async def test_stop_during_a_launch_calls_it_off() -> None:
    manager = make_slow_manager()
    try:
        launching = asyncio.create_task(manager.start(ROOT, 5))  # probing its sub-pages
        await asyncio.sleep(0.02)
        assert await manager.stop(ROOT) == ROOT
        with pytest.raises(TurboNotRunning):
            await launching
        assert manager.active() == []
    finally:
        await manager.shutdown()
