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


_SWEEP_EVERY = 1000


class AddressCache:
    """Remembers which addresses are tokens and which were rejected recently.

    Tokens are kept forever, rejections only for a TTL. Both lists are tab-separated text
    files mirrored in memory and only appended to while the bot runs, so lookups cost
    nothing and a crash never loses more than the last line.
    """

    def __init__(
        self,
        known_path: Path,
        rejected_path: Path,
        *,
        rejected_ttl: float | Callable[[], float],
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Use the two files; nothing is read until load().

        ``rejected_ttl`` is in seconds, or a function returning it so that a settings change
        applies at once; 0 keeps rejections forever.
        """
        self._known_path = known_path
        self._rejected_path = rejected_path
        self._ttl = rejected_ttl if callable(rejected_ttl) else (lambda: float(rejected_ttl))
        self._clock = clock
        self._known: dict[Address, str] = {}
        self._rejected: dict[Address, float] = {}
        self._since_sweep = 0

    @property
    def known_count(self) -> int:
        """Number of addresses known to be tokens."""
        return len(self._known)

    @property
    def rejected_count(self) -> int:
        """Number of rejections that have not expired yet."""
        now = self._clock()
        return sum(1 for at in self._rejected.values() if not self._expired(at, now))

    def load(self) -> None:
        """Read both files into memory, leaving out expired rejections and unreadable lines.

        The rejected file is then rewritten without them. A torn last line in the known file
        is closed first, so the next append starts on a line of its own.
        """
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
        """Return True when the address is a known token."""
        return address in self._known

    def known_network(self, address: Address) -> str | None:
        """Return the network a known token was found on, or None if it is not known."""
        return self._known.get(address)

    def is_rejected(self, address: Address) -> bool:
        """Return True when the address was rejected and the rejection has not expired."""
        rejected_at = self._rejected.get(address)
        if rejected_at is None:
            return False
        if self._expired(rejected_at, self._clock()):
            del self._rejected[address]
            return False
        return True

    def add_known(self, contract: ConfirmedContract) -> bool:
        """Remember a confirmed token; return False if it was already known.

        The line is synced to disk, so a power cut cannot make the bot announce the token
        again. A failed write (disk full, a file locked by another program) is only logged:
        the alert matters more, and after a restart the token may be announced once more.
        """
        if contract.address in self._known:
            return False
        self._known[contract.address] = contract.network
        self._rejected.pop(contract.address, None)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        fields = (contract.address.chain.value, contract.address.value, contract.network, stamp)
        try:
            self._append(self._known_path, fields, sync=True)
        except OSError as exc:
            log.error("could not record %s in %s: %s", contract.address, self._known_path, exc)
        return True

    def forget_rejected(self, chain: Chain) -> int:
        """Drop every rejection on ``chain`` and return how many there were.

        Used after a network is added: those rejections were made without asking it.
        """
        stale = [address for address in self._rejected if address.chain is chain]
        for address in stale:
            del self._rejected[address]
        if stale:
            self._rewrite_rejected()
        return len(stale)

    def add_rejected(self, address: Address) -> None:
        """Remember that the address is not a token, as of now.

        A failed write is only logged. Every ``_SWEEP_EVERY`` new rejections the expired
        ones are swept out.
        """
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
        """Forget expired rejections, in memory and in the file; return how many went."""
        now = self._clock()
        expired = [a for a, at in self._rejected.items() if self._expired(at, now)]
        for address in expired:
            del self._rejected[address]
        self._since_sweep = 0
        if expired:
            self._rewrite_rejected()
        return len(expired)

    def import_known(self, addresses: Iterable[Address]) -> int:
        """Add the tokens found by bot version 1 as known and return how many were new."""
        return sum(
            1 for address in addresses if self.add_known(ConfirmedContract(address, "legacy"))
        )

    def _expired(self, rejected_at: float, now: float) -> bool:
        """Return True when a rejection made at ``rejected_at`` is older than the TTL.

        With a TTL of 0 nothing expires.
        """
        ttl = self._ttl()
        return ttl > 0 and now - rejected_at >= ttl

    def _rewrite_rejected(self) -> None:
        """Replace the rejected file with the rejections in memory.

        With no rejections and no file yet, no empty file is created.
        """
        lines = [
            f"{address.chain.value}\t{address.value}\t{rejected_at:.0f}"
            for address, rejected_at in self._rejected.items()
        ]
        if lines or self._rejected_path.is_file():
            atomic_write_text(self._rejected_path, "".join(line + "\n" for line in lines))

    @staticmethod
    def _read_rows(path: Path) -> list[list[str]]:
        """Return the tab-separated rows of a file that have at least a chain and an address.

        A missing file gives an empty list.
        """
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
        """Return the canonical address in a row, or None if its chain or address is invalid."""
        try:
            chain = Chain(fields[0])
        except ValueError:
            return None
        return canonical_address(chain, fields[1])

    @staticmethod
    def _end_line(path: Path) -> None:
        """Add a newline at the end of the file if it does not end with one.

        A crash mid-append leaves a torn last line, and the next append must not join it.
        """
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
        """Append one tab-separated line to a file; with ``sync`` it goes to disk at once."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\t".join(fields) + "\n")
            if sync:
                handle.flush()
                os.fsync(handle.fileno())
