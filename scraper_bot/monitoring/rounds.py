"""What the regular monitor and turbo sessions share: the pages to render, the paced wait."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from scraper_bot.pages.fetching.prober import HttpProber
from scraper_bot.pages.urls import SubUrlDeriver

log = logging.getLogger(__name__)


async def live_pages(prober: HttpProber, deriver: SubUrlDeriver, root: str) -> list[str]:
    """Return the root page and every derived sub-page that may exist.

    A sub-page is left out only when its probe shows it certainly does not exist (see
    ProbeResult).
    """
    live, _ = await probe_pages(prober, deriver, root)
    return live


async def probe_pages(
    prober: HttpProber, deriver: SubUrlDeriver, root: str
) -> tuple[list[str], list[str]]:
    """Return the pages to render, as :func:`live_pages` does, and the pages worth keeping.

    Those are all the pages but the ones the server answered 404 or 410 for: a sub-page
    whose probe only failed (a refused connection, say) sits this round out, but what we
    know about it stays.
    """
    probes = await asyncio.gather(*(prober.probe(url) for url in deriver.derive(root)))
    skipped = [probe.url for probe in probes if not probe.worth_rendering]
    if skipped:
        log.debug("[%s] not rendering absent pages: %s", root, ", ".join(skipped))
    live = [root, *(probe.url for probe in probes if probe.worth_rendering)]
    return live, [root, *(probe.url for probe in probes if not probe.gone)]


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep ``seconds``, or less when ``stop`` is set meanwhile.

    It uses asyncio.timeout, not wait_for: on Python 3.11 wait_for swallows a cancellation
    that arrives while ``stop`` is already set, so a stopped monitor would not end cancelled.
    """
    if seconds <= 0:
        await asyncio.sleep(0)
        return
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await stop.wait()
