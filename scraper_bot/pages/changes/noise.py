"""Silencing elements that keep changing: tickers, counters, rotating banners.

The two-render check in :mod:`page_checker` only catches changes within seconds.
Slower churn (a counter updated once a minute) would alert on every round, so the
filter counts alerts per element and silences it after ``threshold`` in ``window``.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping

from scraper_bot.models.pages import ChangeReport, Node, NoiseEntry, NoiseMemory, Signature
from scraper_bot.pages.changes.detector import shape

_WHOLESALE_FROM = 20  # nodes a page needs before a report can swap out most of it
_WHOLESALE_KEY = "page|wholesale"


class NoiseFilter:
    """Drops report items whose element already alerted ``threshold`` times in ``window``."""

    def __init__(self, window: float, threshold: int = 2) -> None:
        self._window = window
        self._threshold = max(threshold, 1)

    @property
    def enabled(self) -> bool:
        return self._window > 0

    def apply(
        self,
        report: ChangeReport,
        memory: Mapping[str, NoiseEntry],
        now: float,
        *,
        before: Signature,
        after: Signature,
    ) -> tuple[ChangeReport, NoiseMemory]:
        """Return the filtered report and the updated memory (expired entries removed).

        ``before`` and ``after`` are the signatures the report was built from; an
        element's position among its look-alikes there is part of its identity.
        """
        if not self.enabled:
            return report, {}
        updated: NoiseMemory = {
            key: entry for key, entry in memory.items() if now - entry.last_seen < self._window
        }
        if len(before) >= _WHOLESALE_FROM and len(report.removed) > len(before) // 2:
            # Most of the page swapped out (an interstitial, a redesign): counted for the
            # page as a whole, not per element, or every element would be silenced for the
            # window. A page that keeps swapping is silenced like any flapping element.
            entry = updated.get(_WHOLESALE_KEY)
            count = entry.count if entry else 0
            updated[_WHOLESALE_KEY] = NoiseEntry(count + 1, now)
            if count >= self._threshold:
                return ChangeReport(report.url), updated
            return report, updated
        touched: set[str] = set()

        def keep(key: str) -> bool:
            touched.add(key)
            entry = updated.get(key)
            return entry is None or entry.count < self._threshold

        old_positions, new_positions = _Positions(before), _Positions(after)
        removed = tuple(
            node
            for node in report.removed
            if keep(self.node_key(node, old_positions.among_look_alikes(node)))
        )
        added = tuple(
            node
            for node in report.added
            if keep(self.node_key(node, new_positions.among_look_alikes(node)))
        )
        modified = tuple(
            (old, new)
            for old, new in report.modified
            if keep(self.pair_key(old, new, new_positions.among_text(new)))
        )
        for key in touched:
            entry = updated.get(key)
            updated[key] = NoiseEntry((entry.count if entry else 0) + 1, now)
        return ChangeReport(report.url, removed, added, modified), updated

    @staticmethod
    def node_key(node: Node, ordinal: int = 0) -> str:
        """Identity of an element whose *text* changes: tag + attributes + position.

        The position among look-alikes keeps two ``<span class="stat">`` from sharing
        one memory entry. Elements without attributes fall back to tag + the first word of
        the text with numbers masked (``<td>1203</td>`` stays one element while it counts)
        + position among such elements, so a rotating ``<p>`` does not silence the others.
        """
        if node.attrs:
            return f"{node.tag}|attrs|{_digest(repr(node.attrs))}|{ordinal}"
        return f"{node.tag}|word|{_digest(_first_word(node))}|{ordinal}"

    @staticmethod
    def pair_key(old: Node, new: Node, ordinal: int = 0) -> str:
        """Identity of an element whose *attributes* change: tag + text + position."""
        return f"{old.tag}|pair|{_digest(old.text or new.text)}|{ordinal}"


class _Positions:
    """Where each node of one signature stands among the nodes that look like it."""

    def __init__(self, signature: Signature) -> None:
        # The first position of each node in its group, counted in one pass.
        self._look_alike: dict[Node, int] = {}
        self._same_text: dict[Node, int] = {}
        look_alikes: Counter[tuple[object, ...]] = Counter()
        same_text: Counter[tuple[str, str]] = Counter()
        for node in signature:
            group = (node.tag, "attrs", node.attrs) if node.attrs else (node.tag, _first_word(node))
            self._look_alike.setdefault(node, look_alikes[group])
            look_alikes[group] += 1
            self._same_text.setdefault(node, same_text[node.tag, node.text])
            same_text[node.tag, node.text] += 1

    def among_look_alikes(self, node: Node) -> int:
        """Position among nodes with the same attributes (attribute-less: the same word)."""
        return self._look_alike.get(node, 0)

    def among_text(self, node: Node) -> int:
        return self._same_text.get(node, 0)


def _first_word(node: Node) -> str:
    return shape(node.text.split(" ", 1)[0]) if node.text else ""


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
