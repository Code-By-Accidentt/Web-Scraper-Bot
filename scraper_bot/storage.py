"""Persistence: monitored-site state, page snapshots and the address caches."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scraper_bot.address_extractor import canonical_address, detect_chain
from scraper_bot.models import (
    Address,
    Chain,
    ConfirmedContract,
    Node,
    NoiseEntry,
    NoiseMemory,
    Signature,
    SiteState,
    Snapshot,
)
from scraper_bot.urls import InvalidUrlError, normalize_url, url_to_filename

log = logging.getLogger(__name__)

STATE_VERSION = 1
SNAPSHOT_VERSION = 2


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so that readers never observe a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class StateStore:
    """Single JSON file holding every monitored site and its flags."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def exists(self) -> bool:
        return self._path.is_file()

    def load(self) -> dict[str, SiteState]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("state file %s is unreadable (%s); starting with no sites", self._path, exc)
            return {}
        raw_sites = data.get("sites", {}) if isinstance(data, dict) else {}
        sites: dict[str, SiteState] = {}
        for url, raw in raw_sites.items():
            if isinstance(raw, dict):
                sites[url] = SiteState.from_dict(url, raw)
        return sites

    def save(self, sites: Mapping[str, SiteState]) -> None:
        payload = {
            "version": STATE_VERSION,
            "sites": {url: site.to_dict() for url, site in sorted(sites.items())},
        }
        atomic_write_text(self._path, json.dumps(payload, ensure_ascii=False, indent=2))


class LegacyStateImporter:
    """Reads the flat files written by version 1 of the bot (``monitored_sites.txt`` & co)."""

    SITES_FILE = "monitored_sites.txt"
    NOTIFICATIONS_FILE = "notifications_status.json"
    WALLETS_FILE = "found_wallets.txt"

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def has_data(self) -> bool:
        return (self._dir / self.SITES_FILE).is_file() or (self._dir / self.WALLETS_FILE).is_file()

    def sites(self) -> dict[str, SiteState]:
        path = self._dir / self.SITES_FILE
        if not path.is_file():
            return {}
        notifications = self._load_json(self._dir / self.NOTIFICATIONS_FILE)
        sites: dict[str, SiteState] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                url = normalize_url(raw)
            except InvalidUrlError:
                log.warning("legacy site %r skipped: not a valid URL", raw)
                continue
            enabled = bool(notifications.get(raw, notifications.get(url, True)))
            sites[url] = SiteState(url=url, notifications_enabled=enabled)
        return sites

    def known_addresses(self) -> list[Address]:
        path = self._dir / self.WALLETS_FILE
        if not path.is_file():
            return []
        addresses: list[Address] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw:
                continue
            chain = detect_chain(raw)
            address = canonical_address(chain, raw) if chain else None
            if address is not None:
                addresses.append(address)
        return addresses

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


class SnapshotStore:
    """One JSON file per page holding the last accepted signature and noise memory."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, url: str) -> Path:
        return self._dir / f"{url_to_filename(url)}.json"

    def load(self, url: str) -> Snapshot | None:
        path = self.path_for(url)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            signature: Signature = tuple(
                Node(str(tag), tuple((str(k), str(v)) for k, v in attrs), str(text))
                for tag, attrs, text in data["nodes"]
            )
            raw_noise = data.get("noise")
            if raw_noise is None:
                raw_noise = {}
            if not isinstance(raw_noise, dict):
                raise TypeError("noise must be an object")
            noise: NoiseMemory = {
                str(key): NoiseEntry(int(count), float(last_seen))
                for key, (count, last_seen) in raw_noise.items()
            }
            return Snapshot(signature, noise)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("snapshot %s is unreadable (%s); it will be rebuilt", path.name, exc)
            return None

    def save(self, url: str, snapshot: Snapshot) -> None:
        payload = {
            "version": SNAPSHOT_VERSION,
            "url": url,
            "saved_at": time.time(),
            "nodes": [[node.tag, list(node.attrs), node.text] for node in snapshot.signature],
            "noise": {key: [entry.count, entry.last_seen] for key, entry in snapshot.noise.items()},
        }
        atomic_write_text(self.path_for(url), json.dumps(payload, ensure_ascii=False))

    def delete(self, url: str) -> None:
        self.path_for(url).unlink(missing_ok=True)

    def delete_many(self, urls: Iterable[str]) -> None:
        for url in urls:
            self.delete(url)

    def prune(self, keep_urls: Iterable[str]) -> int:
        """Delete every snapshot that does not belong to one of ``keep_urls``."""
        keep = {self.path_for(url).name for url in keep_urls}
        removed = 0
        for path in self._dir.glob("*.json"):
            if path.name not in keep:
                path.unlink(missing_ok=True)
                removed += 1
        if removed:
            log.info("removed %d orphaned snapshot(s)", removed)
        return removed


class AddressCache:
    """Confirmed contracts (kept forever) and rejected addresses (kept for a TTL).

    Both caches are append-only text files (tab separated) mirrored in memory, so
    lookups are free and a crash never loses more than the last line.
    """

    def __init__(
        self,
        known_path: Path,
        rejected_path: Path,
        *,
        rejected_ttl: float | Callable[[], float],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._known_path = known_path
        self._rejected_path = rejected_path
        self._ttl = rejected_ttl if callable(rejected_ttl) else (lambda: float(rejected_ttl))
        self._clock = clock
        self._known: dict[Address, str] = {}
        self._rejected: dict[Address, float] = {}

    @property
    def known_count(self) -> int:
        return len(self._known)

    @property
    def rejected_count(self) -> int:
        return len(self._rejected)

    def load(self) -> None:
        self._known = {}
        for fields in self._read_rows(self._known_path):
            address = self._parse_address(fields)
            if address is not None:
                self._known[address] = fields[2] if len(fields) > 2 else ""

        self._rejected = {}
        now = self._clock()
        for fields in self._read_rows(self._rejected_path):
            address = self._parse_address(fields)
            if address is None or len(fields) < 3:
                continue
            try:
                rejected_at = float(fields[2])
            except ValueError:
                continue
            if not self._expired(rejected_at, now):
                self._rejected[address] = rejected_at
        self._rewrite_rejected()

    def is_known(self, address: Address) -> bool:
        return address in self._known

    def known_network(self, address: Address) -> str | None:
        """Network a confirmed contract was found on, ``None`` if it is not known."""
        return self._known.get(address)

    def is_rejected(self, address: Address) -> bool:
        rejected_at = self._rejected.get(address)
        if rejected_at is None:
            return False
        if self._expired(rejected_at, self._clock()):
            del self._rejected[address]
            return False
        return True

    def add_known(self, contract: ConfirmedContract) -> bool:
        """Remember a confirmed contract; False when it was already known."""
        if contract.address in self._known:
            return False
        self._known[contract.address] = contract.network
        self._rejected.pop(contract.address, None)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        self._append(
            self._known_path,
            (contract.address.chain.value, contract.address.value, contract.network, stamp),
        )
        return True

    def add_rejected(self, address: Address) -> None:
        now = self._clock()
        self._rejected[address] = now
        self._append(self._rejected_path, (address.chain.value, address.value, f"{now:.0f}"))

    def import_known(self, addresses: Iterable[Address]) -> int:
        return sum(
            1 for address in addresses if self.add_known(ConfirmedContract(address, "legacy"))
        )

    # ── internals ───────────────────────────────────────────────────────────
    def _expired(self, rejected_at: float, now: float) -> bool:
        ttl = self._ttl()
        return ttl > 0 and now - rejected_at >= ttl

    def _rewrite_rejected(self) -> None:
        lines = [
            f"{address.chain.value}\t{address.value}\t{rejected_at:.0f}"
            for address, rejected_at in self._rejected.items()
        ]
        if lines or self._rejected_path.is_file():
            atomic_write_text(self._rejected_path, "".join(line + "\n" for line in lines))

    @staticmethod
    def _read_rows(path: Path) -> list[list[str]]:
        if not path.is_file():
            return []
        rows: list[list[str]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.rstrip("\n").split("\t")
            if len(fields) >= 2 and fields[0] and fields[1]:
                rows.append(fields)
        return rows

    @staticmethod
    def _parse_address(fields: list[str]) -> Address | None:
        try:
            chain = Chain(fields[0])
        except ValueError:
            return None
        return canonical_address(chain, fields[1])

    @staticmethod
    def _append(path: Path, fields: tuple[str, ...]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\t".join(fields) + "\n")
