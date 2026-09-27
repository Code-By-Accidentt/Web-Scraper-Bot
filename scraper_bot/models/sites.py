"""The saved state of each monitored site."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from scraper_bot.models.pages import ChangeCounts


@dataclass(slots=True)
class LastChange:
    at: float
    page_url: str
    counts: ChangeCounts

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "page_url": self.page_url,
            "removed": self.counts.removed,
            "added": self.counts.added,
            "modified": self.counts.modified,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LastChange:
        return cls(
            at=float(data.get("at", 0.0)),
            page_url=str(data.get("page_url", "")),
            counts=ChangeCounts(
                int(data.get("removed", 0)), int(data.get("added", 0)), int(data.get("modified", 0))
            ),
        )


@dataclass(slots=True)
class SiteState:
    """Persistent, user-visible state of one monitored root URL."""

    url: str
    notifications_enabled: bool = True
    paused: bool = False
    announced_pages: set[str] = field(default_factory=set)
    last_change: LastChange | None = None
    added_at: float = field(default_factory=time.time)
    unverified_enabled: bool = False
    # "Chain:address" -> when it was announced as unverified (watched until confirmed)
    unverified: dict[str, float] = field(default_factory=dict)
    # page -> the captcha widgets the user was told it shows (told once per page and widget)
    captcha_pages: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "notifications_enabled": self.notifications_enabled,
            "paused": self.paused,
            "announced_pages": sorted(self.announced_pages),
            "last_change": self.last_change.to_dict() if self.last_change else None,
            "added_at": self.added_at,
            "unverified_enabled": self.unverified_enabled,
            "unverified": dict(self.unverified),
            "captcha_pages": {page: list(widgets) for page, widgets in self.captcha_pages.items()},
        }

    @classmethod
    def from_dict(cls, url: str, data: dict[str, Any]) -> SiteState:
        if not isinstance(data, dict):
            raise TypeError("a site entry must be an object")
        last = data.get("last_change")
        raw_unverified = data.get("unverified")
        unverified = (
            {str(key): float(value) for key, value in raw_unverified.items()}
            if isinstance(raw_unverified, dict)
            else {}
        )
        raw_captcha = data.get("captcha_pages")
        captcha_pages = (
            {
                str(page): [str(w) for w in widgets]
                if isinstance(widgets, list)
                else [str(widgets)]
                for page, widgets in raw_captcha.items()
            }
            if isinstance(raw_captcha, dict)
            else {}
        )
        return cls(
            url=url,
            notifications_enabled=bool(data.get("notifications_enabled", True)),
            paused=bool(data.get("paused", False)),
            announced_pages=set(data.get("announced_pages", ())),
            last_change=LastChange.from_dict(last) if isinstance(last, dict) else None,
            added_at=float(data.get("added_at", 0.0)) or time.time(),
            unverified_enabled=bool(data.get("unverified_enabled", False)),
            unverified=unverified,
            captcha_pages=captcha_pages,
        )
