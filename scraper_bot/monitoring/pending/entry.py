"""One watched address and how it is saved."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scraper_bot.models.addresses import Address, address_key, parse_address_key
from scraper_bot.models.sightings import Sighting, SightingSource

_FAST_WINDOWS = ((15 * 60.0, 1.0), (3600.0, 5.0))  # (age below, seconds between checks)
SLOW_INTERVAL = 30.0


@dataclass(slots=True)
class PendingEntry:
    """One address the pending watch checks until it becomes a token or is let go."""

    address: Address
    site_url: str  # the site and page named in the alert: the first to show it
    page_url: str
    first_seen: float  # when a page showed it first (epoch seconds)
    fast: bool  # named as the token on the page, or seen by a turbo session
    sighting: Sighting | None = None
    classified: bool = False  # a check found nothing there yet (EVM: and not a wallet)
    # On a page when a turbo session started: news only where it was seen absent since.
    baseline: bool = False
    next_check: float = 0.0
    # every site showing it -> the page it is on there (site_url and page_url included)
    sites: dict[str, str] = field(default_factory=dict)
    # network -> the last check that found nothing there (EVM network name, or "Solana")
    absent_on: dict[str, float] = field(default_factory=dict)
    # networks that reported code after their check (not saved): confirmed at the next tick
    late: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        """Add the first site and its page to ``sites``."""
        self.sites.setdefault(self.site_url, self.page_url)


def check_interval(entry: PendingEntry, now: float, turbo: float | None = None) -> float:
    """Return how many seconds to wait before checking this address again.

    A fast entry is checked every second for its first 15 minutes, every 5 seconds up to an
    hour, then every 30 seconds like the rest. A turbo pace (``turbo``) can make it faster.
    """
    interval = SLOW_INTERVAL
    if entry.fast:
        age = now - entry.first_seen
        interval = next((pause for limit, pause in _FAST_WINDOWS if age < limit), SLOW_INTERVAL)
    return min(interval, turbo) if turbo is not None else interval


@dataclass(frozen=True, slots=True)
class Dropped:
    """An address the watch let go, kept out until ``until`` (epoch seconds)."""

    until: float
    hard: bool  # True: a wallet or a non-token; False: only given up on (expired)


def entry_to_dict(entry: PendingEntry) -> dict[str, Any]:
    """Return the entry as a JSON-ready dict for the pending watch file."""
    sighting = entry.sighting
    return {
        "address": address_key(entry.address),
        "site": entry.site_url,
        "page": entry.page_url,
        "sites": dict(entry.sites),
        "first_seen": entry.first_seen,
        "fast": entry.fast,
        "classified": entry.classified,
        "baseline": entry.baseline,
        "absent_on": dict(entry.absent_on),
        "source": sighting.source.name if sighting else None,
        "context": sighting.context if sighting else "",
        "hinted": sighting.hinted if sighting else False,
    }


def _sites_from(raw: Any, page_url: str) -> dict[str, str]:
    """Return the saved sites and their pages; an earlier build saved the sites alone, as a list."""
    if isinstance(raw, dict):
        return {str(site): str(page) for site, page in raw.items()}
    if isinstance(raw, list):
        return {str(site): page_url for site in raw}
    return {}


def entry_from_dict(raw: dict[str, Any], *, forget_baseline: bool = False) -> PendingEntry | None:
    """Rebuild an entry from its saved dict; None when the address cannot be parsed.

    With ``forget_baseline`` a turbo baseline comes back as an ordinary entry.
    """
    address = parse_address_key(str(raw["address"]))
    if address is None:
        return None
    source = raw.get("source")
    sighting = (
        Sighting(
            address, SightingSource[source], str(raw.get("context", "")), bool(raw.get("hinted"))
        )
        if isinstance(source, str) and source in SightingSource.__members__
        else None
    )
    absent = raw.get("absent_on")
    return PendingEntry(
        address,
        str(raw["site"]),
        str(raw["page"]),
        float(raw["first_seen"]),
        bool(raw.get("fast")),
        sighting,
        bool(raw.get("classified")),
        bool(raw.get("baseline")) and not forget_baseline,
        sites=_sites_from(raw.get("sites"), str(raw["page"])),
        absent_on=(
            {str(name): float(at) for name, at in absent.items()}
            if isinstance(absent, dict)
            else {}
        ),
    )
