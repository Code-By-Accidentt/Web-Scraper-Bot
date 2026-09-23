"""Silencing elements that keep changing: tickers, counters, rotating banners.

The two-render check in :mod:`page_checker` only catches changes within seconds.
Slower churn (a counter updated once a minute) would alert on every round, so the
filter counts alerts per element and silences it after ``threshold`` in ``window``.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence

from scraper_bot.models import ChangeReport, Node, NoiseEntry, NoiseMemory, Signature


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
        touched: set[str] = set()

        def keep(key: str) -> bool:
            touched.add(key)
            entry = updated.get(key)
            return entry is None or entry.count < self._threshold

        old_positions, new_positions = _Positions(before), _Positions(after)
        removed = tuple(
            node
            for node in report.removed
            if keep(self.node_key(node, old_positions.among_attrs(node)))
        )
        added = tuple(
            node
            for node in report.added
            if keep(self.node_key(node, new_positions.among_attrs(node)))
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
        one memory entry. Elements without attributes fall back to tag + first word
        of the text, so a rotating ``<p>`` does not silence every other paragraph.
        """
        if node.attrs:
            return f"{node.tag}|attrs|{_digest(repr(node.attrs))}|{ordinal}"
        first_word = node.text.split(" ", 1)[0] if node.text else ""
        return f"{node.tag}|text|{_digest(first_word)}"

    @staticmethod
    def pair_key(old: Node, new: Node, ordinal: int = 0) -> str:
        """Identity of an element whose *attributes* change: tag + text + position."""
        return f"{old.tag}|pair|{_digest(old.text or new.text)}|{ordinal}"


class _Positions:
    """Where each node of one signature stands among the nodes that look like it."""

    def __init__(self, signature: Signature) -> None:
        self._by_attrs: dict[tuple[str, tuple[tuple[str, str], ...]], list[Node]] = defaultdict(
            list
        )
        self._by_text: dict[tuple[str, str], list[Node]] = defaultdict(list)
        for node in signature:
            self._by_attrs[node.tag, node.attrs].append(node)
            self._by_text[node.tag, node.text].append(node)

    def among_attrs(self, node: Node) -> int:
        return _position(self._by_attrs.get((node.tag, node.attrs), ()), node)

    def among_text(self, node: Node) -> int:
        return _position(self._by_text.get((node.tag, node.text), ()), node)


def _position(group: Sequence[Node], node: Node) -> int:
    for index, candidate in enumerate(group):
        if candidate == node:
            return index
    return 0


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
