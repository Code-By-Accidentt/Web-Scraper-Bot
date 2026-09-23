import json
from pathlib import Path

from scraper_bot.models import (
    Address,
    Chain,
    ChangeCounts,
    ConfirmedContract,
    LastChange,
    NoiseEntry,
    SiteState,
    Snapshot,
)
from scraper_bot.storage import AddressCache, LegacyStateImporter, SnapshotStore, StateStore
from tests.conftest import node

USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ETH = Address(Chain.ETHEREUM, USDC_ETH)
SOL = Address(Chain.SOLANA, USDC_SOL)


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_cache(tmp_path: Path, clock: FakeClock, ttl: float = 100.0) -> AddressCache:
    cache = AddressCache(
        tmp_path / "known.tsv", tmp_path / "rejected.tsv", rejected_ttl=ttl, clock=clock
    )
    cache.load()
    return cache


# ── StateStore ──────────────────────────────────────────────────────────────
def test_state_roundtrip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    assert store.load() == {}
    assert not store.exists()
    site = SiteState(
        "https://a.io",
        notifications_enabled=False,
        paused=True,
        announced_pages={"https://app.a.io"},
        last_change=LastChange(123.0, "https://a.io", ChangeCounts(1, 2, 3)),
    )
    store.save({site.url: site, "https://b.io": SiteState("https://b.io")})
    assert store.exists()
    assert not list(tmp_path.glob("*.tmp"))

    loaded = store.load()
    assert set(loaded) == {"https://a.io", "https://b.io"}
    restored = loaded["https://a.io"]
    assert restored.notifications_enabled is False
    assert restored.paused is True
    assert restored.announced_pages == {"https://app.a.io"}
    assert restored.last_change is not None
    assert restored.last_change.counts == ChangeCounts(1, 2, 3)
    assert restored.last_change.page_url == "https://a.io"
    assert loaded["https://b.io"].notifications_enabled is True


def test_corrupt_state_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert StateStore(path).load() == {}


# ── SnapshotStore ───────────────────────────────────────────────────────────
def test_snapshot_roundtrip_delete_and_prune(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    signature = (node("a", "x", href="/y"), node("p", "text"))
    assert store.load("https://a.io") is None
    store.save("https://a.io", Snapshot(signature))
    store.save("https://b.io", Snapshot(signature))
    assert store.load("https://a.io").signature == signature

    assert store.prune(["https://a.io"]) == 1
    assert store.load("https://b.io") is None
    assert store.load("https://a.io").signature == signature

    store.delete("https://a.io")
    store.delete("https://a.io")  # idempotent
    assert store.load("https://a.io") is None


def test_snapshot_keeps_noise_memory_and_reads_version_1_files(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    signature = (node("p", "text"),)
    store.save("https://a.io", Snapshot(signature, {"k": NoiseEntry(3, 123.5)}))
    loaded = store.load("https://a.io")
    assert loaded is not None
    assert loaded.noise == {"k": NoiseEntry(3, 123.5)}

    legacy = store.path_for("https://old.io")
    legacy.write_text(json.dumps({"version": 1, "nodes": [["p", [], "x"]]}), encoding="utf-8")
    old = store.load("https://old.io")
    assert old is not None
    assert old.signature == (node("p", "x"),) and old.noise == {}


def test_corrupt_snapshot_is_treated_as_missing(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.path_for("https://a.io").write_text("garbage", encoding="utf-8")
    assert store.load("https://a.io") is None


# ── AddressCache ────────────────────────────────────────────────────────────
def test_address_cache_persists_known_and_rejected(tmp_path: Path) -> None:
    clock = FakeClock(1000.0)
    cache = make_cache(tmp_path, clock)
    assert cache.add_known(ConfirmedContract(ETH, "Base")) is True
    assert cache.add_known(ConfirmedContract(ETH, "Base")) is False
    cache.add_rejected(SOL)
    assert cache.is_known(ETH)
    assert cache.is_rejected(SOL)
    assert not cache.is_known(SOL)

    reloaded = make_cache(tmp_path, clock)
    assert reloaded.is_known(ETH)
    assert reloaded.is_rejected(SOL)
    assert (reloaded.known_count, reloaded.rejected_count) == (1, 1)
    known_line = (tmp_path / "known.tsv").read_text(encoding="utf-8").splitlines()[0]
    assert known_line.startswith(f"Ethereum\t{USDC_ETH}\tBase\t")


def test_rejected_entries_expire_and_are_compacted(tmp_path: Path) -> None:
    clock = FakeClock(1000.0)
    cache = make_cache(tmp_path, clock, ttl=100.0)
    cache.add_rejected(SOL)
    clock.now = 1099.0
    assert cache.is_rejected(SOL)
    clock.now = 1100.0
    assert not cache.is_rejected(SOL)
    assert cache.rejected_count == 0

    cache.add_rejected(SOL)
    clock.now = 1300.0
    reloaded = make_cache(tmp_path, clock, ttl=100.0)
    assert reloaded.rejected_count == 0
    assert (tmp_path / "rejected.tsv").read_text(encoding="utf-8") == ""


def test_zero_ttl_means_rejections_never_expire(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    cache = make_cache(tmp_path, clock, ttl=0.0)
    cache.add_rejected(SOL)
    clock.now = 10**9
    assert cache.is_rejected(SOL)


def test_confirmation_overrides_earlier_rejection(tmp_path: Path) -> None:
    cache = make_cache(tmp_path, FakeClock(0.0))
    cache.add_rejected(ETH)
    cache.add_known(ConfirmedContract(ETH, "Ethereum"))
    assert cache.is_known(ETH)
    assert not cache.is_rejected(ETH)


# ── LegacyStateImporter ─────────────────────────────────────────────────────
def test_legacy_files_are_imported(tmp_path: Path) -> None:
    (tmp_path / "monitored_sites.txt").write_text(
        "https://a.io/\nexample.com\nnot a url\n\n", encoding="utf-8"
    )
    (tmp_path / "notifications_status.json").write_text(
        json.dumps({"https://a.io/": False}), encoding="utf-8"
    )
    (tmp_path / "found_wallets.txt").write_text(
        f"{USDC_ETH.lower()}\n{USDC_SOL}\nbad\n", encoding="utf-8"
    )
    importer = LegacyStateImporter(tmp_path)
    assert importer.has_data()

    sites = importer.sites()
    assert set(sites) == {"https://a.io", "https://example.com"}
    assert sites["https://a.io"].notifications_enabled is False
    assert sites["https://example.com"].notifications_enabled is True

    assert importer.known_addresses() == [ETH, SOL]
    cache = make_cache(tmp_path, FakeClock(0.0))
    assert cache.import_known(importer.known_addresses()) == 2
    assert cache.import_known(importer.known_addresses()) == 0


def test_missing_legacy_files_are_harmless(tmp_path: Path) -> None:
    importer = LegacyStateImporter(tmp_path)
    assert not importer.has_data()
    assert importer.sites() == {}
    assert importer.known_addresses() == []


def test_snapshot_with_malformed_noise_is_treated_as_missing(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.path_for("https://a.io").write_text(
        json.dumps({"version": 2, "nodes": [], "noise": []}), encoding="utf-8"
    )
    assert store.load("https://a.io") is None
    store.path_for("https://b.io").write_text(
        json.dumps({"version": 2, "nodes": [], "noise": {"k": "bad"}}), encoding="utf-8"
    )
    assert store.load("https://b.io") is None


def test_rejected_ttl_can_be_a_live_provider(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    ttl = {"value": 100.0}
    cache = AddressCache(
        tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=lambda: ttl["value"], clock=clock
    )
    cache.load()
    cache.add_rejected(SOL)
    clock.now = 50.0
    assert cache.is_rejected(SOL)
    ttl["value"] = 10.0  # shortened at runtime through /set
    assert not cache.is_rejected(SOL)
