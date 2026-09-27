"""A file the bot touches regularly so the supervisor can tell it is alive."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from scraper_bot.storage.files import atomic_write_text

log = logging.getLogger(__name__)


class Heartbeat:
    """Touches a file periodically so an external supervisor can detect a hung process."""

    def __init__(self, path: Path, interval: float) -> None:
        self._path = path
        self._interval = interval

    async def run(self) -> None:
        while True:
            try:
                atomic_write_text(self._path, f"{time.time():.0f}\n")
            except OSError as exc:
                log.warning("heartbeat write failed: %s", exc)
            await asyncio.sleep(self._interval)
