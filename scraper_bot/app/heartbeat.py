"""A file the bot rewrites regularly so the supervisor can tell it is alive."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from scraper_bot.storage.files import atomic_write_text

log = logging.getLogger(__name__)


class Heartbeat:
    """Rewrites a file every few seconds so the supervisor (rebooter.py) can spot a hung bot."""

    def __init__(self, path: Path, interval: float) -> None:
        """Write the heartbeat to ``path`` every ``interval`` seconds once started."""
        self._path = path
        self._interval = interval

    async def run(self) -> None:
        """Write the current Unix time to the file, forever.

        A failed write is only logged; the next one tries again.
        """
        while True:
            try:
                atomic_write_text(self._path, f"{time.time():.0f}\n")
            except OSError as exc:
                log.warning("heartbeat write failed: %s", exc)
            await asyncio.sleep(self._interval)
