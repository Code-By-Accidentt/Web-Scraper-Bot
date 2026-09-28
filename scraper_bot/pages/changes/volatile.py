"""Remembering the shapes of elements that keep ticking, so they cost no second render.

A timer or a live clock makes every render differ from the snapshot. Once a round saw an
element of some shape tick between its two renders, a later render that differs from the
snapshot only in the elements of that shape that ticked needs no confirming render.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping

from scraper_bot.models.pages import Node, Signature, VolatileEntry, VolatileMemory
from scraper_bot.pages.changes.detector import shape

_MAX_AGE = 24 * 3600.0  # seconds a shape stays known after it last ticked
_LIMIT = 200  # shapes kept per page, the most recently seen ones


class VolatileShapes:
    """Learns which elements tick on a page and tells when only they changed.

    A shape is an element's tag, attributes and text with every number masked, so a timer
    reading 00:05:30 and later 00:05:28 keeps its shape. An element is told apart from others
    of its shape by its position among them, so a still cell next to a ticking one is not
    taken for it.
    """

    def __init__(self, max_age: float = _MAX_AGE, limit: int = _LIMIT) -> None:
        """Forget a shape ``max_age`` seconds after it last ticked; keep at most ``limit``."""
        self._max_age = max_age
        self._limit = limit

    @staticmethod
    def key(node: Node) -> str:
        """Return the memory key of the node's shape: its tag and a short digest of the rest."""
        attrs = tuple((name, shape(value)) for name, value in node.attrs)
        digest = hashlib.sha1(repr((attrs, shape(node.text))).encode("utf-8")).hexdigest()
        return f"{node.tag}|{digest[:16]}"

    def learn(
        self,
        memory: Mapping[str, VolatileEntry],
        stored: Signature,
        first: Signature,
        second: Signature,
        now: float,
    ) -> VolatileMemory:
        """Return ``memory`` with the elements that ticked between a round's two renders.

        A shape ticked when one element of it went and another came, unless the one that went
        is in the ``stored`` snapshot: that is a change landing between the renders. Each shape
        keeps how many elements of it ``second`` shows and at which of those places it ticked
        (earlier places too while the count stays); stale and surplus entries go.
        """
        (old, one, two), nodes = _numbered(stored, first, second)
        in_first, in_second = Counter(one), Counter(two)
        came, went = in_second - in_first, in_first - in_second
        ticking: set[str] = set()
        if came and went:
            known = set(old)
            ticking = {self.key(nodes[i]) for i in went if i not in known}
            ticking &= {self.key(nodes[i]) for i in came}
        updated = {
            key: entry for key, entry in memory.items() if now - entry.last_seen < self._max_age
        }
        if ticking:
            counts, places = self._places(nodes, two, ticking, set(came))
            for key in ticking:
                previous = updated.get(key)
                kept: frozenset[int] = frozenset()
                if previous is not None and previous.count == counts[key]:
                    kept = previous.positions
                updated[key] = VolatileEntry(counts[key], now, kept | places[key])
        if len(updated) > self._limit:
            newest = sorted(updated.items(), key=lambda item: item[1].last_seen, reverse=True)
            updated = dict(newest[: self._limit])
        return updated

    def explains(
        self, memory: Mapping[str, VolatileEntry], stored: Signature, render: Signature, now: float
    ) -> bool:
        """True when ``render`` differs from ``stored`` only in elements known to tick.

        Every element that changed must have a known shape and, if it is new, stand where
        that shape ticked before. The other elements must keep their order, and each shape
        must show as many elements as when it last ticked, so a new row or a still cell that
        looks like a ticking one is a change.
        """
        (old, new), nodes = _numbered(stored, render)
        before, after = Counter(old), Counter(new)
        came = after - before
        changed = {*came, *(before - after)}
        if not changed:
            return False
        keys: set[str] = set()
        for index in changed:
            key = self.key(nodes[index])
            entry = memory.get(key)
            if entry is None or now - entry.last_seen >= self._max_age:
                return False
            keys.add(key)
        if [i for i in old if i not in changed] != [i for i in new if i not in changed]:
            return False
        counts, places = self._places(nodes, new, keys, set(came))
        return all(
            counts[key] == memory[key].count and places[key] <= memory[key].positions
            for key in keys
        )

    def _places(
        self, nodes: list[Node], render: list[int], keys: set[str], marked: set[int]
    ) -> tuple[Counter[str], dict[str, set[int]]]:
        """Count the elements of ``render`` with each of ``keys`` and note where the marked stand.

        ``render`` holds indexes into ``nodes`` in page order. A marked element's place is its
        position among the elements of its shape.
        """
        counts: Counter[str] = Counter()
        places: dict[str, set[int]] = {key: set() for key in keys}
        tags = {key.rpartition("|")[0] for key in keys}
        shapes: dict[int, str | None] = {}
        for index in render:
            if index not in shapes:
                node = nodes[index]
                shapes[index] = self.key(node) if node.tag in tags else None
            key = shapes[index]
            if key is None or key not in keys:
                continue
            if index in marked:
                places[key].add(counts[key])
            counts[key] += 1
        return counts, places


def _numbered(*signatures: Signature) -> tuple[list[list[int]], list[Node]]:
    """Return each signature as integers, one per distinct node, and the nodes by integer.

    Hashing a node is slow, so the counting and comparing is done on the integers.
    """
    ids: dict[Node, int] = {}
    numbered = [[ids.setdefault(node, len(ids)) for node in signature] for signature in signatures]
    return numbered, list(ids)
