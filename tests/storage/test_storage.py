import json
import os
from pathlib import Path

import pytest

from scraper_bot.models.addresses import Address, Chain, ConfirmedContract
from scraper_bot.models.pages import ChangeCounts, NoiseEntry, Snapshot
from scraper_bot.models.sites import LastChange, SiteState
from scraper_bot.storage.address_cache import AddressCache
from scraper_bot.storage.files import atomic_write_text
from scraper_bot.storage.legacy import LegacyStateImporter
from scraper_bot.storage.snapshots import SnapshotStore
from scraper_bot.storage.state import StateStore
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
    assert old.deep_seen is None


def test_snapshot_keeps_the_deep_scan_memory_only_once_recorded(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    signature = (node("p", "text"),)
    store.save("https://a.io", Snapshot(signature, deep_seen=frozenset({"0xB", "0xA"})))
    assert store.load("https://a.io").deep_seen == {"0xA", "0xB"}
    store.save("https://b.io", Snapshot(signature, deep_seen=frozenset()))
    assert store.load("https://b.io").deep_seen == frozenset()  # recorded, nothing found
    store.save("https://c.io", Snapshot(signature))
    assert "deep_seen" not in store.path_for("https://c.io").read_text(encoding="utf-8")
    assert store.load("https://c.io").deep_seen is None


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


def test_legacy_wallet_lines_of_every_chain_are_recognised(tmp_path: Path) -> None:
    from scraper_bot.storage.legacy import LegacyStateImporter

    sui = "0x" + "ab" * 32
    (tmp_path / LegacyStateImporter.WALLETS_FILE).write_text(
        f"{USDC_ETH.lower()}\n{USDC_SOL}\n{sui}\n{sui}::usdc::USDC\nnot-an-address\n",
        encoding="utf-8",
    )
    found = [(a.chain, a.value) for a in LegacyStateImporter(tmp_path).known_addresses()]
    assert found == [
        (Chain.ETHEREUM, USDC_ETH),
        (Chain.SOLANA, USDC_SOL),
        (Chain.SUI, sui),
        (Chain.SUI, f"{sui}::usdc::USDC"),
    ]


def test_state_roundtrip_keeps_unverified_fields_and_added_at(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    watched = {"Ethereum:0xabc": 1_700_000_100.0, "Solana:mint": 1_700_000_200.0}
    site = SiteState(
        "https://a.io", added_at=1_700_000_000.5, unverified_enabled=True, unverified=watched
    )
    store.save({site.url: site})
    restored = store.load()["https://a.io"]
    assert restored.added_at == 1_700_000_000.5
    assert restored.unverified_enabled is True
    assert restored.unverified == watched

    # A file written before these fields existed, and one with a mangled watch list.
    sites = {"https://old.io": {"paused": True}, "https://odd.io": {"unverified": ["x"]}}
    path.write_text(json.dumps({"version": 1, "sites": sites}), encoding="utf-8")
    loaded = store.load()
    old = loaded["https://old.io"]
    assert old.paused is True and old.unverified_enabled is False and old.unverified == {}
    assert old.added_at > 0  # "now" stands in for the missing timestamp
    assert loaded["https://odd.io"].unverified == {}


def test_corrupt_cache_rows_are_skipped_and_compacted(tmp_path: Path) -> None:
    other = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    (tmp_path / "known.tsv").write_text(
        f"Ethereum\t{USDC_ETH}\tBase\t2024-01-01T00:00:00+00:00\n"
        "Dogecoin\tDBXu2kgc3xtvCUWFcxFE3r9hEYgmuaaCyD\tDoge\n"  # unknown chain
        "Ethereum\tnot-an-address\tBase\n"  # not an address
        f"Solana\t{USDC_SOL}\n"  # two fields: still a known address
        "\n",
        encoding="utf-8",
    )
    (tmp_path / "rejected.tsv").write_text(
        f"Ethereum\t{USDC_ETH}\t900\n"
        "Bitcoin\t1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2\t900\n"  # unknown chain
        f"Solana\t{USDC_SOL}\n"  # no stamp
        f"Ethereum\t{other}\tyesterday\n"  # non-numeric stamp
        "Ethereum\t\t900\n"  # empty address
        f"Ethereum\t{other.lower()}\t940\n",  # valid; canonicalised on load
        encoding="utf-8",
    )
    cache = make_cache(tmp_path, FakeClock(950.0), ttl=100.0)
    assert cache.known_count == 2
    assert cache.is_known(ETH) and cache.is_known(SOL)
    assert cache.known_network(ETH) == "Base"
    assert cache.rejected_count == 2
    assert cache.is_rejected(ETH) and cache.is_rejected(Address(Chain.ETHEREUM, other))
    assert not cache.is_rejected(SOL)
    assert (tmp_path / "rejected.tsv").read_text(encoding="utf-8") == (
        f"Ethereum\t{USDC_ETH}\t900\nEthereum\t{other}\t940\n"
    )


def test_atomic_write_keeps_the_old_file_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save({"https://a.io": SiteState("https://a.io")})
    before = path.read_text(encoding="utf-8")

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        store.save({"https://b.io": SiteState("https://b.io")})
    with pytest.raises(OSError, match="disk full"):
        atomic_write_text(path, "garbage")
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == before
    assert set(store.load()) == {"https://a.io"}


def test_corrupt_legacy_notifications_file_means_alerts_on(tmp_path: Path) -> None:
    (tmp_path / LegacyStateImporter.SITES_FILE).write_text("https://a.io\n", encoding="utf-8")
    flags = tmp_path / LegacyStateImporter.NOTIFICATIONS_FILE
    for content in ("{not json", "[false]", '"https://a.io"'):
        flags.write_text(content, encoding="utf-8")
        sites = LegacyStateImporter(tmp_path).sites()
        assert sites["https://a.io"].notifications_enabled is True, content


def test_a_damaged_state_file_is_moved_aside_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"sites": {"https://a.io": {},}}', encoding="utf-8")  # a stray comma
    store = StateStore(path)
    assert store.load() == {}
    assert store.broken is not None and "unreadable" in store.broken[0]
    moved = store.broken[1]
    assert moved is not None and moved.read_text(encoding="utf-8").startswith('{"sites"')
    assert not path.exists()  # the next save starts a new file; the old one is kept aside


def test_one_bad_site_entry_keeps_the_others_and_the_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    good = SiteState("https://a.io").to_dict()
    path.write_text(
        json.dumps({"sites": {"https://a.io": good, "https://b.io": {"added_at": "abc"}}}),
        encoding="utf-8",
    )
    store = StateStore(path)
    assert list(store.load()) == ["https://a.io"]
    assert store.broken is not None and "https://b.io" in store.broken[0]
    assert store.broken[1] is not None and store.broken[1].is_file()


def test_rejections_of_one_chain_can_be_forgotten(tmp_path: Path) -> None:
    cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=3600)
    cache.load()
    evm = Address(Chain.ETHEREUM, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
    other = Address(Chain.ETHEREUM, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
    sol = Address(Chain.SOLANA, "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump")
    cache.add_rejected(evm)
    cache.add_rejected(sol)
    cache.add_rejected(other)
    assert cache.forget_rejected(Chain.ETHEREUM) == 2
    reloaded = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=3600)
    reloaded.load()
    assert reloaded.is_rejected(sol) and reloaded.rejected_count == 1
    assert not reloaded.is_rejected(evm) and not reloaded.is_rejected(other)


def test_the_captcha_pages_of_a_site_survive_a_save() -> None:
    site = SiteState("https://a.io", captcha_pages={"https://a.io/claim": ["hCaptcha"]})
    again = SiteState.from_dict("https://a.io", site.to_dict())
    assert again.captcha_pages == {"https://a.io/claim": ["hCaptcha"]}
    assert SiteState.from_dict("https://a.io", {}).captcha_pages == {}  # saved by 2.1


def test_a_torn_last_line_does_not_swallow_the_next_known_contract(tmp_path: Path) -> None:
    known = tmp_path / "k.tsv"
    first = Address(Chain.ETHEREUM, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
    known.write_text(f"Ethereum\t{first.value}\tBase\t2026-09-27\nSolana\t2qEHjDL", "utf-8")
    cache = AddressCache(known, tmp_path / "r.tsv", rejected_ttl=3600)
    cache.load()
    later = Address(Chain.SOLANA, "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump")
    assert cache.add_known(ConfirmedContract(later, "Solana"))
    again = AddressCache(known, tmp_path / "r.tsv", rejected_ttl=3600)
    again.load()
    assert again.is_known(first) and again.is_known(later)


def test_expired_rejections_are_swept_out_and_not_counted(tmp_path: Path) -> None:
    now = [1_000.0]
    rejected = tmp_path / "r.tsv"
    cache = AddressCache(tmp_path / "k.tsv", rejected, rejected_ttl=100, clock=lambda: now[0])
    cache.load()
    old = Address(Chain.ETHEREUM, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
    cache.add_rejected(old)
    now[0] += 150
    fresh = Address(Chain.SOLANA, "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump")
    cache.add_rejected(fresh)
    assert cache.rejected_count == 1  # the expired one no longer counts
    assert cache.sweep() == 1
    assert old.value not in rejected.read_text(encoding="utf-8")
    assert cache.is_rejected(fresh)


def test_expired_rejections_are_swept_while_rejections_come_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scraper_bot.storage.address_cache as storage_module

    monkeypatch.setattr(storage_module, "_SWEEP_EVERY", 2)
    now = [1_000.0]
    rejected = tmp_path / "r.tsv"
    cache = AddressCache(tmp_path / "k.tsv", rejected, rejected_ttl=100, clock=lambda: now[0])
    cache.load()
    old = Address(Chain.ETHEREUM, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
    cache.add_rejected(old)
    now[0] += 150
    cache.add_rejected(Address(Chain.ETHEREUM, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"))
    assert old.value not in rejected.read_text(encoding="utf-8")  # the second add swept
