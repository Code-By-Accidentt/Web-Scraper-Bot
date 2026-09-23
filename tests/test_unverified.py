from pathlib import Path

from scraper_bot.models import Address, Chain, ConfirmedContract, SiteState
from scraper_bot.storage import AddressCache
from scraper_bot.unverified import UnverifiedWatcher, address_key, parse_key
from tests.conftest import FakeNotifier, FakeTuning, FakeValidator

ROOT = "https://example.com"
PAGE = "https://example.com/app"
A = Address(Chain.ETHEREUM, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
B = Address(Chain.ETHEREUM, "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
S = Address(Chain.SOLANA, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")


class Harness:
    def __init__(self, tmp_path: Path, *, enabled: bool = True, **tuning) -> None:
        self.site = SiteState(ROOT, unverified_enabled=enabled)
        self.validator = FakeValidator()
        self.notifier = FakeNotifier()
        self.cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
        self.cache.load()
        self.tuning = FakeTuning(unverified_recheck=60, unverified_watch=3600, **tuning)
        self.persist_calls = 0
        self.now = 1_000.0

        def persist() -> None:
            self.persist_calls += 1

        self.watcher = UnverifiedWatcher(
            self.site,
            validator=self.validator,  # type: ignore[arg-type]
            cache=self.cache,
            notifier=self.notifier,  # type: ignore[arg-type]
            persist=persist,
            tuning=self.tuning,
            clock=lambda: self.now,
        )


def test_address_keys_round_trip() -> None:
    assert parse_key(address_key(A)) == A
    assert parse_key(address_key(S)) == S
    assert parse_key("Nope:0x1") is None and parse_key("Ethereum:") is None


async def test_new_addresses_are_announced_once_and_persisted(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.watcher.observe([A, S], PAGE)
    await h.watcher.observe([A, S], PAGE)
    assert h.notifier.unverified == [(A, PAGE), (S, PAGE)]
    assert set(h.site.unverified) == {address_key(A), address_key(S)}
    assert h.persist_calls == 1


async def test_known_contracts_and_disabled_mode_are_ignored(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.cache.add_known(ConfirmedContract(A, "Base"))
    await h.watcher.observe([A, B], PAGE)
    assert h.notifier.unverified == [(B, PAGE)]

    off = Harness(tmp_path, enabled=False)
    await off.watcher.observe([A, B], PAGE)
    await off.watcher.recheck()
    assert off.notifier.unverified == [] and off.validator.calls == []


async def test_recheck_confirms_due_addresses_and_stops_watching_them(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.validator.reject_then_confirm(A, "Base", rejections=1)
    await h.watcher.observe([A], PAGE)
    await h.watcher.recheck()  # first check: still rejected
    assert h.notifier.confirmed == []
    await h.watcher.recheck()  # too soon: no RPC call
    assert len(h.validator.calls) == 1
    h.now += 61
    await h.watcher.recheck()
    assert h.notifier.confirmed == [(ConfirmedContract(A, "Base"), PAGE)]
    assert h.site.unverified == {}
    assert h.validator.negative_cache_calls == [False, False]
    assert h.persist_calls == 2


async def test_confirmation_by_the_regular_monitor_is_silent(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.validator.known(A)
    await h.watcher.observe([A], PAGE)
    await h.watcher.recheck()
    assert h.notifier.confirmed == [] and h.site.unverified == {}


async def test_watch_window_expires(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.watcher.observe([A], PAGE)
    h.now += 3601
    await h.watcher.recheck()
    assert h.validator.calls == []
    assert address_key(A) in h.site.unverified  # still remembered, not announced again
    await h.watcher.observe([A], PAGE)
    assert len(h.notifier.unverified) == 1


async def test_alert_cap_turns_the_overflow_into_a_digest(tmp_path: Path) -> None:
    h = Harness(tmp_path, unverified_max_per_hour=1)
    await h.watcher.observe([A, B, S], PAGE)
    assert h.notifier.unverified == [(A, PAGE)]
    assert h.notifier.digests == [(ROOT, 2)]
    assert len(h.site.unverified) == 3


async def test_pending_overflow_is_flushed_by_rechecks_too(tmp_path: Path) -> None:
    h = Harness(tmp_path, unverified_max_per_hour=1)
    await h.watcher.observe([A, B], PAGE)  # A is alerted, B goes into the first digest
    assert h.notifier.digests == [(ROOT, 1)]
    await h.watcher.observe([S], PAGE)  # within the hour: counted, not sent yet
    assert h.notifier.digests == [(ROOT, 1)]
    h.now += 3601
    await h.watcher.recheck()  # the site stayed quiet: the round itself delivers it
    assert h.notifier.digests == [(ROOT, 1), (ROOT, 1)]
