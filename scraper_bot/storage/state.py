"""The list of monitored sites, saved in ``state.json``."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Mapping
from pathlib import Path

from scraper_bot.models.sites import SiteState
from scraper_bot.storage.files import atomic_write_text

log = logging.getLogger(__name__)


STATE_VERSION = 1


class StateStore:
    """Keeps every monitored site and its flags in one JSON file."""

    def __init__(self, path: Path) -> None:
        """Use the state file at ``path``; nothing is read until load()."""
        self._path = path
        # Set by load() on a damaged file: (what was wrong, where it was moved or None).
        self.broken: tuple[str, Path | None] | None = None

    def exists(self) -> bool:
        """Return True when the state file exists."""
        return self._path.is_file()

    def broken_copies(self) -> list[Path]:
        """Return the damaged files moved aside at earlier starts and not cleaned up yet."""
        return sorted(self._path.parent.glob(f"{self._path.name}.broken-*"))

    def load(self) -> dict[str, SiteState]:
        """Return every readable site, or an empty dict when there is no file.

        If the file or any site in it cannot be read, the file is moved aside and
        ``broken`` says why; a damaged file is never overwritten.
        """
        self.broken = None
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw_sites = data.get("sites", {}) if isinstance(data, dict) else {}
            if not isinstance(raw_sites, dict):
                raise ValueError("'sites' is not an object")
        except (OSError, ValueError) as exc:
            self._set_aside(f"unreadable: {exc}")
            return {}
        sites: dict[str, SiteState] = {}
        bad: list[str] = []
        for url, raw in raw_sites.items():
            try:
                sites[url] = SiteState.from_dict(url, raw)
            except (TypeError, ValueError, AttributeError) as exc:
                bad.append(f"{url}: {exc}")
        if bad:
            self._set_aside("sites left out: " + "; ".join(bad))
        return sites

    def _set_aside(self, problem: str) -> None:
        """Move the damaged file aside for a person to repair; the next save starts a new one.

        If it can be neither moved nor copied, ``broken`` names no copy, and the caller must
        then not save over it.
        """
        target: Path | None = self._path.with_name(
            f"{self._path.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        try:
            os.replace(self._path, target)  # type: ignore[arg-type]
        except OSError:
            try:
                shutil.copyfile(self._path, target)  # type: ignore[arg-type]
            except OSError:
                target = None
        self.broken = (problem, target)
        log.error(
            "state file %s: %s; the damaged file was moved to %s", self._path, problem, target
        )

    def save(self, sites: Mapping[str, SiteState]) -> None:
        """Write all sites to the state file, replacing it in one atomic step."""
        payload = {
            "version": STATE_VERSION,
            "sites": {url: site.to_dict() for url, site in sorted(sites.items())},
        }
        atomic_write_text(self._path, json.dumps(payload, ensure_ascii=False, indent=2))
