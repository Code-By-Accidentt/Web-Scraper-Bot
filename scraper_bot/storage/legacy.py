"""Importing the data of bot version 1."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from scraper_bot.chains.addresses.formats import canonical_address, detect_chain
from scraper_bot.models.addresses import Address
from scraper_bot.models.sites import SiteState
from scraper_bot.pages.urls import InvalidUrlError, normalize_url

log = logging.getLogger(__name__)


class LegacyStateImporter:
    """Reads the flat files of bot version 1: sites, notification flags, found wallets."""

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
