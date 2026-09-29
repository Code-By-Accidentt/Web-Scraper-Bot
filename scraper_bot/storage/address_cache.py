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

# Tells the chat about a problem (key, title, detail, hint), such as Notifier.report_later.
Report = Callable[[str, str, str, str], None]

_SWEEP_EVERY = 1000
_BROKEN_KNOWN_HINT = (
    "The rest of the file was loaded. Fix or delete those lines while the bot is stopped "
    "(UTF-8 text, one tab-separated entry per line); until then every start reports them."
)
_BROKEN_REJECTED_HINT = (
    "The rest of the file was loaded and it is saved again without them; those addresses "
    "are checked again when a page shows them."
)
_UNREADABLE_FILE_HINT = (
    "The bot runs without what the file holds: a known token may be announced again, a "
    "rejected address is checked again. Make the file readable and restart the bot."
)
_UNSAVED_HINT = (
    "The bot keeps running with the list in memory, but what is not saved is lost at a "
    "restart: a token found meanwhile may be announced again, a rejection is checked again. "
    "Make the file writable: it may be read-only, owned by another user or open in another "
    "program."
)


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
        report: Report | None = None,
    ) -> None:
        """Use the two files; nothing is read until load().

        ``rejected_ttl`` is in seconds, or a function returning it so that a settings change
        applies at once; 0 keeps rejections forever. ``report`` hears about files that
        cannot be read or saved.
        """
        self._known_path = known_path
        self._rejected_path = rejected_path
        self._ttl = rejected_ttl if callable(rejected_ttl) else (lambda: float(rejected_ttl))
        self._clock = clock
        self._report = report
        self._known: dict[Address, str] = {}
        self._rejected: dict[Address, float] = {}
        self._since_sweep = 0
        # Files whose last write failed. We report that once, until a write works again.
        self._unsaved: set[Path] = set()

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

        The rejected file is then rewritten without them, unless it could not be read. A torn
        last line in the known file is closed first, so the next append starts on a line of
        its own. Unreadable lines and files that cannot be read or saved are reported; the
        bot goes on with what it has.
        """
        try:
            self._end_line(self._known_path)
        except OSError as exc:
            log.error("cannot write to %s: %s", self._known_path, exc)
            self._save_failed(self._known_path, exc)
        self._known = {}
        rows, broken = _rows(self._read_lines(self._known_path) or [])
        for number, fields in rows:
            address = self._parse_address(fields)
            if address is None:
                broken.append(number)
            else:
                self._known[address] = fields[2] if len(fields) > 2 else ""
        self._report_broken(self._known_path, broken, _BROKEN_KNOWN_HINT)

        self._rejected = {}
        now = self._clock()
        lines = self._read_lines(self._rejected_path)
        rows, broken = _rows(lines or [])
        for number, fields in rows:
            address = self._parse_address(fields)
            rejected_at = self._parse_stamp(fields)
            if address is None or rejected_at is None:
                broken.append(number)
            elif not self._expired(rejected_at, now):
                self._rejected[address] = rejected_at
        self._report_broken(self._rejected_path, broken, _BROKEN_REJECTED_HINT)
        if lines is not None:
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
        again. A failed write (disk full, a file locked by another program) is logged and
        reported once: the alert matters more, and after a restart the token may be
        announced once more.
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
            self._save_failed(self._known_path, exc)
        else:
            self._unsaved.discard(self._known_path)
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

        A failed write is logged and reported once. Every ``_SWEEP_EVERY`` new rejections
        the expired ones are swept out.
        """
        now = self._clock()
        self._rejected[address] = now
        try:
            self._append(self._rejected_path, (address.chain.value, address.value, f"{now:.0f}"))
        except OSError as exc:
            log.warning("could not record a rejection in %s: %s", self._rejected_path, exc)
            self._save_failed(self._rejected_path, exc)
        else:
            self._unsaved.discard(self._rejected_path)
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

        With no rejections and no file yet, no empty file is created. A failed write is
        logged and reported once; the rejections stay in memory.
        """
        lines = [
            f"{address.chain.value}\t{address.value}\t{rejected_at:.0f}"
            for address, rejected_at in self._rejected.items()
        ]
        if not lines and not self._rejected_path.is_file():
            return
        try:
            atomic_write_text(self._rejected_path, "".join(line + "\n" for line in lines))
        except OSError as exc:
            log.warning("could not save %s: %s", self._rejected_path, exc)
            self._save_failed(self._rejected_path, exc)
        else:
            self._unsaved.discard(self._rejected_path)

    def _read_lines(self, path: Path) -> list[bytes] | None:
        """Return the lines of a file, [] when it is missing, None when it cannot be read.

        A file that cannot be read is logged and reported.
        """
        try:
            return path.read_bytes().splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.error("cannot read %s: %s", path, exc)
            self._tell(
                f"cache-read-{path.name}",
                f"{path.name} could not be read",
                str(exc),
                _UNREADABLE_FILE_HINT,
            )
            return None

    def _report_broken(self, path: Path, lines: list[int], hint: str) -> None:
        """Log and report the lines of ``path`` skipped as unreadable, if there are any."""
        if not lines:
            return
        shown = ", ".join(str(number) for number in sorted(lines)[:10])
        if len(lines) > 10:
            shown += ", ..."
        log.warning("%s: %d unreadable line(s) skipped (%s)", path, len(lines), shown)
        self._tell(
            f"cache-lines-{path.name}",
            f"Some lines of {path.name} were unreadable and skipped",
            f"{len(lines)} line(s) of {path}: {shown}",
            hint,
        )

    def _save_failed(self, path: Path, error: OSError) -> None:
        """Report that ``path`` cannot be written, once until a write to it succeeds again."""
        if path in self._unsaved:
            return
        self._unsaved.add(path)
        title = f"Saving to {path.name} fails"
        self._tell(f"cache-save-{path.name}", title, str(error), _UNSAVED_HINT)

    def _tell(self, key: str, title: str, detail: str, hint: str) -> None:
        """Pass one problem to ``report``, if there is one."""
        if self._report is not None:
            self._report(key, title, detail, hint)

    @staticmethod
    def _parse_address(fields: list[str]) -> Address | None:
        """Return the canonical address in a row, or None if its chain or address is invalid."""
        try:
            chain = Chain(fields[0])
        except ValueError:
            return None
        return canonical_address(chain, fields[1])

    @staticmethod
    def _parse_stamp(fields: list[str]) -> float | None:
        """Return the time a rejection row was written, or None when it has no valid one."""
        try:
            return float(fields[2]) if len(fields) > 2 else None
        except ValueError:
            return None

    @staticmethod
    def _end_line(path: Path) -> None:
        """Add a newline at the end of the file if it does not end with one.

        A crash mid-append leaves a torn last line, and the next append must not join it.
        Raises OSError when the file exists but cannot be written.
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


def _rows(lines: list[bytes]) -> tuple[list[tuple[int, list[str]]], list[int]]:
    """Return the rows of a file's lines with their line numbers, and the broken lines' numbers.

    A row is a line of tab-separated fields starting with a chain and an address. A line
    that is not UTF-8 or lacks those fields is broken (torn by a crash, edited in another
    encoding); a blank line is nothing.
    """
    rows: list[tuple[int, list[str]]] = []
    broken: list[int] = []
    for number, raw in enumerate(lines, 1):
        try:
            line = raw.decode("utf-8-sig")  # -sig strips a BOM left by an editor
        except UnicodeDecodeError:
            broken.append(number)
            continue
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) >= 2 and fields[0] and fields[1]:
            rows.append((number, fields))
        else:
            broken.append(number)
    return rows, broken
