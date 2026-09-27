"""The last accepted snapshot of every page."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from pathlib import Path

from scraper_bot.models.pages import Node, NoiseEntry, NoiseMemory, Signature, Snapshot
from scraper_bot.pages.urls import url_to_filename
from scraper_bot.storage.files import atomic_write_text

log = logging.getLogger(__name__)


SNAPSHOT_VERSION = 2


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
            raw_deep = data.get("deep_seen")
            deep_seen = frozenset(str(v) for v in raw_deep) if isinstance(raw_deep, list) else None
            return Snapshot(signature, noise, deep_seen)
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
        if snapshot.deep_seen is not None:
            payload["deep_seen"] = sorted(snapshot.deep_seen)
        atomic_write_text(self.path_for(url), json.dumps(payload, ensure_ascii=False))

    def delete(self, url: str) -> None:
        self.path_for(url).unlink(missing_ok=True)

    def delete_many(self, urls: Iterable[str]) -> None:
        for url in urls:
            self.delete(url)

    def prune(self, keep_urls: Iterable[str]) -> int:
        keep = {self.path_for(url).name for url in keep_urls}
        removed = 0
        for path in self._dir.glob("*.json"):
            if path.name not in keep:
                path.unlink(missing_ok=True)
                removed += 1
        if removed:
            log.info("removed %d orphaned snapshot(s)", removed)
        return removed
