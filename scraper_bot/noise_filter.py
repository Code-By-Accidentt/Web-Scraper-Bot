"""Silencing elements that keep changing: tickers, counters, rotating banners.

The two-render check in :mod:`page_checker` catches content that changes within
seconds. Content that changes slower (a counter updated once a minute, a banner
rotated every hour) passes that check every time and would alert on every round.
:class:`NoiseFilter` remembers, per page, how often each element produced an
alert; once an element crossed the threshold inside the window it is silenced
for as long as it keeps changing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from scraper_bot.models import ChangeReport, Node, NoiseEntry, NoiseMemory


class NoiseFilter:
    """Drops report items whose element already alerted ``threshold`` times in ``window``."""

    def __init__(self, window: float, threshold: int = 2) -> None:
        self._window = window
        self._threshold = max(threshold, 1)

    @property
    def enabled(self) -> bool:
        return self._window > 0

    def apply(
        self, report: ChangeReport, memory: Mapping[str, NoiseEntry], now: float
    ) -> tuple[ChangeReport, NoiseMemory]:
        """Return the filtered report and the updated memory (expired entries removed)."""
        if not self.enabled:
            return report, {}
        updated: NoiseMemory = {
            key: entry for key, entry in memory.items() if now - entry.last_seen < self._window
        }
        touched: set[str] = set()

        def keep(key: str) -> bool:
            touched.add(key)
            entry = updated.get(key)
            return entry is None or entry.count < self._threshold

        removed = tuple(node for node in report.removed if keep(self.node_key(node)))
        added = tuple(node for node in report.added if keep(self.node_key(node)))
        modified = tuple(pair for pair in report.modified if keep(self.pair_key(*pair)))
        for key in touched:
            entry = updated.get(key)
            updated[key] = NoiseEntry((entry.count if entry else 0) + 1, now)
        return ChangeReport(report.url, removed, added, modified), updated

    @staticmethod
    def node_key(node: Node) -> str:
        """Identity of an element whose *text* changes: tag + attributes.

        Elements without attributes fall back to tag + first word of the text, so
        a rotating ``<p>`` does not silence every other paragraph on the page.
        """
        if node.attrs:
            return f"{node.tag}|attrs|{_digest(repr(node.attrs))}"
        first_word = node.text.split(" ", 1)[0] if node.text else ""
        return f"{node.tag}|text|{_digest(first_word)}"

    @staticmethod
    def pair_key(old: Node, new: Node) -> str:
        """Identity of an element whose *attributes* change: tag + text."""
        return f"{old.tag}|pair|{_digest(old.text or new.text)}"


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
