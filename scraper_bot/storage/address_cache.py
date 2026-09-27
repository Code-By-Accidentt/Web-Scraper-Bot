"""Addresses already known to be tokens, and those recently rejected."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from scraper_bot.chains.addresses.formats import canonical_address
from scraper_bot.models.addresses import Address, Chain, ConfirmedContract
from scraper_bot.storage.files import atomic_write_text

log = logging.getLogger(__name__)


_SWEEP_EVERY = 1000  # rejections added between two sweeps of the expired ones


class AddressCache:
    """Confirmed contracts (kept forever) and rejected addresses (kept for a TTL).

    Both are tab-separated text files mirrored in memory and only appended to while
    running, so lookups are free and a crash never loses more than the last line.
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
        self._since_sweep = 0  # rejections added since expired ones were last swept out

    @property
    def known_count(self) -> int:
        return len(self._known)

    @property
    def rejected_count(self) -> int:
        now = self._clock()
        return sum(1 for at in self._rejected.values() if not self._expired(at, now))

    def load(self) -> None:
        self._end_line(self._known_path)
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
        """Remember a confirmed contract; False when it was already known.

        A failed write (disk full, a file locked by another program) is logged, not
        raised: the token's alert matters more; after a restart it may come once more.
        """
        if contract.address in self._known:
            return False
        self._known[contract.address] = contract.network
        self._rejected.pop(contract.address, None)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        fields = (contract.address.chain.value, contract.address.value, contract.network, stamp)
        try:
            # Synced: after a power cut an announced token must not be announced again.
            self._append(self._known_path, fields, sync=True)
        except OSError as exc:
            log.error("could not record %s in %s: %s", contract.address, self._known_path, exc)
        return True

    def forget_rejected(self, chain: Chain) -> int:
        """Drop every rejection of ``chain``: they were made before a network was added."""
        stale = [address for address in self._rejected if address.chain is chain]
        for address in stale:
            del self._rejected[address]
        if stale:
            self._rewrite_rejected()
        return len(stale)

    def add_rejected(self, address: Address) -> None:
        now = self._clock()
        self._rejected[address] = now
        try:
            self._append(self._rejected_path, (address.chain.value, address.value, f"{now:.0f}"))
        except OSError as exc:
            log.warning("could not record a rejection in %s: %s", self._rejected_path, exc)
        self._since_sweep += 1
        if self._since_sweep >= _SWEEP_EVERY:
            self.sweep()

    def sweep(self) -> int:
        """Forget expired rejections, in memory and in the file; returns how many went."""
        now = self._clock()
        expired = [a for a, at in self._rejected.items() if self._expired(at, now)]
        for address in expired:
            del self._rejected[address]
        self._since_sweep = 0
        if expired:
            self._rewrite_rejected()
        return len(expired)

    def import_known(self, addresses: Iterable[Address]) -> int:
        return sum(
            1 for address in addresses if self.add_known(ConfirmedContract(address, "legacy"))
        )

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
    def _end_line(path: Path) -> None:
        """A crash mid-append leaves a torn last line; the next append must not join it."""
        try:
            with path.open("rb+") as handle:
                if handle.seek(0, os.SEEK_END) == 0:
                    return
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
        except FileNotFoundError:
            return

    @staticmethod
    def _append(path: Path, fields: tuple[str, ...], *, sync: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\t".join(fields) + "\n")
            if sync:
                handle.flush()
                os.fsync(handle.fileno())
