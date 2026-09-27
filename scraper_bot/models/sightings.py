"""Where an address was seen on a page and when a token showed up."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from scraper_bot.models.addresses import Address, ValidationResult


class SightingSource(Enum):
    """Where on a page an address was found; shown in the alert."""

    TEXT = "page text"  # the visible text of an element
    LINK = "link"  # an attribute of an element: href, src, data-*
    SOURCE = "page source"  # only in raw HTML (inline scripts, meta tags), frames or shadow roots
    SCRIPT = "loaded script"  # scripts and JSON the page loaded (deep scan)


@dataclass(frozen=True, slots=True)
class Sighting:
    """One address on a page, with what stands in front of it."""

    address: Address
    source: SightingSource
    context: str = ""  # up to 60 characters before the address, other addresses masked
    hinted: bool = False  # the words before it name it: CA, contract, mint, token, pump.fun/...


@dataclass(frozen=True, slots=True)
class Observation:
    """What one render of a page showed, reported before the page is compared.

    ``read`` is False for a render that did not show the page itself (a 2xx challenge, a
    blank page): it does not count as a moment the page was seen without the token.
    """

    page_url: str
    seen_at: float  # when the page content was read (epoch seconds)
    sightings: tuple[Sighting, ...]
    # verdicts for the sightings' addresses, then for the tokens behind LP pairs (``via``)
    results: tuple[ValidationResult, ...]
    # when the render began: what the page shows is from between this and ``seen_at``
    started_at: float | None = None
    read: bool = True


@dataclass(frozen=True, slots=True)
class Timeline:
    """When a token showed up, for the ⏱ line of its alert (epoch seconds)."""

    found_at: float  # the check that confirmed it
    missing_at: float | None = None  # the check before: the address was not there yet
    on_page_since: float | None = None  # first seen on the page, when that was earlier
    deployed: bool = False  # confirmed by the pending watch
