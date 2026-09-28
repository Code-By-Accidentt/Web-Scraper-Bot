"""Addresses a page shows while it is still loading, checked and reported at once."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from scraper_bot.chains.addresses.extractor import AddressExtractor
from scraper_bot.models.addresses import Address, ValidationResult
from scraper_bot.models.pages import Node, Signature
from scraper_bot.models.sightings import Observation, Sighting
from scraper_bot.pages.fetching.result import EarlyText
from scraper_bot.pages.sightings import find_sightings, shown_on_a_challenge

log = logging.getLogger(__name__)

Validate = Callable[[tuple[Sighting, ...]], Awaitable[tuple[ValidationResult, ...]]]
Observe = Callable[[Observation], Awaitable[None]]


class EarlyReader:
    """Turns the pieces one loading page reports into observations, each address once.

    Only what a person sees counts, as on a page that may be a challenge: visible text and
    links that name a token. Everything else waits for the full read of the page.
    """

    def __init__(
        self,
        page_url: str,
        *,
        extractor: AddressExtractor,
        validate: Validate,
        observe: Observe,
    ) -> None:
        """Report what ``page_url`` shows through ``observe``, checked with ``validate``."""
        self._page_url = page_url
        self._extractor = extractor
        self._validate = validate
        self._observe = observe
        self._seen: set[Address] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def take(self, item: EarlyText) -> None:
        """Start checking the new addresses in one piece of the page; called as it loads."""
        if self._closed:
            return
        found = find_sightings(self._extractor, _signature(item), "")
        fresh = tuple(s for s in shown_on_a_challenge(found) if s.address not in self._seen)
        if not fresh:
            return
        self._seen.update(sighting.address for sighting in fresh)
        task = asyncio.ensure_future(self._report(fresh, item.seen_at))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """Take no more pieces and wait until the ones taken are checked and reported."""
        self._closed = True
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def cancel(self) -> None:
        """Take no more pieces and drop the checks still running (the page check was cancelled)."""
        self._closed = True
        for task in self._tasks:
            task.cancel()

    async def _report(self, sightings: tuple[Sighting, ...], seen_at: float) -> None:
        """Check the addresses and report them as seen at ``seen_at``, before the page is read."""
        try:
            results = await self._validate(sightings)
            await self._observe(
                Observation(self._page_url, seen_at, sightings, results, read=False)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("checking the addresses %s showed while loading failed", self._page_url)


def _signature(item: EarlyText) -> Signature:
    """Return a piece of the page as the nodes the full read would have made of it.

    A text follows the text in front of it, so the alert quotes the same words ("CA:").
    """
    if item.kind == "attr":
        return (Node("early", ((item.before, item.text),), ""),)
    return (Node("early", (), item.before), Node("early", (), item.text))
