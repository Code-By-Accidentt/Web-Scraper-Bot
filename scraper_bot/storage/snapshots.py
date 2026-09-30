"""The last accepted snapshot of every page."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from scraper_bot.models.pages import (
    Node,
    NoiseEntry,
    NoiseMemory,
    Signature,
    Snapshot,
    VolatileEntry,
    VolatileMemory,
)
from scraper_bot.pages.urls import url_to_filename
from scraper_bot.storage.address_cache import Report
from scraper_bot.storage.files import atomic_write_text

log = logging.getLogger(__name__)


SNAPSHOT_VERSION = 2
_UNSAVED_HINT = (
    "Pages are still checked, against snapshots kept in memory, but those are lost at a "
    "restart: a page may then report a change again. Make the snapshots folder writable: the "
    "disk may be full, or the folder read-only or owned by another user."
)


class SnapshotStore:
    """Keeps one JSON file per page with its last accepted signature and its memories.

    When a file can't be written, the snapshot is kept in memory instead, so the next check
    still compares with it. The user hears about it once, until every snapshot is saved again.
    """

    def __init__(self, directory: Path, *, report: Report | None = None) -> None:
        """Keep the snapshots in ``directory``, creating it if needed.

        ``report`` hears when snapshot files can't be written or removed.
        """
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._report = report
        self._unsaved: dict[str, Snapshot] = {}  # page -> snapshot whose file couldn't be written
        self._failing = False  # we told the user; cleared once nothing is left unsaved

    def path_for(self, url: str) -> Path:
        """Return the snapshot file of a page."""
        return self._dir / f"{url_to_filename(url)}.json"

    def load(self, url: str) -> Snapshot | None:
        """Return the saved snapshot of a page, or None when there is none or it is unreadable.

        An unreadable file is logged; the next save replaces it. A snapshot that couldn't be
        written comes from memory.
        """
        unsaved = self._unsaved.get(url)
        if unsaved is not None:
            return unsaved
        path = self.path_for(url)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            signature: Signature = tuple(
                Node(str(tag), tuple((str(k), str(v)) for k, v in attrs), str(text))
                for tag, attrs, text in data["nodes"]
            )
            noise: NoiseMemory = {
                str(key): NoiseEntry(int(count), float(last_seen))
                for key, (count, last_seen) in _object(data, "noise").items()
            }
            raw_deep = data.get("deep_seen")
            deep_seen = frozenset(str(v) for v in raw_deep) if isinstance(raw_deep, list) else None
            volatile: VolatileMemory = {
                str(key): VolatileEntry(int(count), float(last_seen), frozenset(map(int, places)))
                for key, (count, last_seen, places) in _object(data, "volatile").items()
            }
            return Snapshot(signature, noise, deep_seen, volatile, data.get("baseline") is True)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("snapshot %s is unreadable (%s); it will be rebuilt", path.name, exc)
            return None

    def save(self, url: str, snapshot: Snapshot) -> None:
        """Write the snapshot of a page, replacing the old one, or keep it in memory.

        A file that can't be written is logged and reported once; the snapshot stays in
        memory until a later save of the page works.
        """
        payload = {
            "version": SNAPSHOT_VERSION,
            "url": url,
            "saved_at": time.time(),
            "nodes": [[node.tag, list(node.attrs), node.text] for node in snapshot.signature],
            "noise": {key: [entry.count, entry.last_seen] for key, entry in snapshot.noise.items()},
            "volatile": {
                key: [entry.count, entry.last_seen, sorted(entry.positions)]
                for key, entry in snapshot.volatile.items()
            },
        }
        if snapshot.deep_seen is not None:
            payload["deep_seen"] = sorted(snapshot.deep_seen)
        if snapshot.baseline:
            payload["baseline"] = True
        path = self.path_for(url)
        try:
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False))
        except OSError as exc:
            self._unsaved[url] = snapshot
            self._failed(f"could not save {path.name}", exc)
        else:
            self._unsaved.pop(url, None)
            if not self._unsaved:
                self._failing = False

    def delete(self, url: str) -> None:
        """Remove the snapshot of a page, if there is one; a failure is logged and reported."""
        self._unsaved.pop(url, None)
        path = self.path_for(url)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self._failed(f"could not remove {path.name}", exc)

    def delete_many(self, urls: Iterable[str]) -> None:
        """Remove the snapshots of several pages."""
        for url in urls:
            self.delete(url)

    def prune(self, keep_urls: Iterable[str]) -> int:
        """Remove every snapshot whose page is not in ``keep_urls``; return how many went.

        A file that can't be removed is logged and reported, and the others still go.
        """
        keep = {self.path_for(url).name for url in keep_urls}
        removed = 0
        for path in self._dir.glob("*.json"):
            if path.name in keep:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                self._failed(f"could not remove {path.name}", exc)
            else:
                removed += 1
        if removed:
            log.info("removed %d orphaned snapshot(s)", removed)
        return removed

    def _failed(self, what: str, error: OSError) -> None:
        """Log a snapshot file that couldn't be written or removed; report the first one.

        The next failures are only logged, until every snapshot is saved again.
        """
        log.warning("snapshots: %s: %s", what, error)
        if self._failing:
            return
        self._failing = True
        if self._report is not None:
            self._report(
                "snapshots", "Saving page snapshots fails", f"{what}: {error}", _UNSAVED_HINT
            )


def _object(data: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the JSON object a snapshot file keeps under ``name``, {} when it has none.

    Anything else there makes the file unreadable.
    """
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value
