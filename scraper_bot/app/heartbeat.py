"""A file the bot rewrites regularly so the supervisor can tell it is alive."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from scraper_bot.storage.files import atomic_write_text

log = logging.getLogger(__name__)


class Heartbeat:
    """Rewrites a file every few seconds so the supervisor (rebooter.py) can spot a hung bot."""

    def __init__(
        self,
        path: Path,
        interval: float,
        report: Callable[[str, str, str, str], None] | None = None,
    ) -> None:
        """Write the heartbeat to ``path`` every ``interval`` seconds once started.

        ``report`` (the notifier's ``report_later``) hears when the writes start failing.
        """
        self._path = path
        self._interval = interval
        self._report = report
        self._failing = False

    async def run(self) -> None:
        """Write the current Unix time to the file, forever.

        A failed write is logged and the next one tries again; the first one of a streak is
        also reported to the chat.
        """
        while True:
            try:
                atomic_write_text(self._path, f"{time.time():.0f}\n")
            except OSError as exc:
                log.warning("heartbeat write failed: %s", exc)
                self._failed(exc)
            else:
                self._failing = False
            await asyncio.sleep(self._interval)

    def _failed(self, exc: OSError) -> None:
        """Tell the chat once per streak of failures, since the supervisor will act on it."""
        if self._failing:
            return
        self._failing = True
        if self._report is not None:
            self._report(
                "heartbeat",
                "The heartbeat file cannot be written",
                str(exc),
                "The supervisor (rebooter.py) restarts the bot every 5 min while the heartbeat "
                f"is stale. Make {self._path.parent} writable for the bot's user and check "
                "the free disk space.",
            )
