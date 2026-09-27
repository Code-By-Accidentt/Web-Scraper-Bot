"""Comparing two page signatures and telling real changes from render noise."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from difflib import SequenceMatcher

from scraper_bot.models.pages import ChangeReport, Node, Signature

# A render with fewer than this share of the other render's nodes is a truncated load.
_MIN_SIZE_RATIO = 0.6
_NUMBER_RE = re.compile(r"\d[\d.,]*")


def shape(text: str) -> str:
    """``text`` with every number replaced by ``#``: ``Raised: 12.5 SOL`` -> ``Raised: # SOL``.

    Two texts of one shape are the same element showing another value (a timer, a counter).
    """
    return _NUMBER_RE.sub("#", text)


class ChangeDetector:
    """Ordered diff of node lists that can leave volatile nodes out.

    Volatile nodes differ between two renders taken a few seconds apart (timers,
    counters, banners); they and their previous values stay out of the report.
    """

    @staticmethod
    def differs(old: Signature, new: Signature) -> bool:
        return old != new

    def volatile_nodes(self, first: Signature, second: Signature) -> frozenset[Node]:
        """Nodes that are not identical in two consecutive renders of the same page."""
        volatile: set[Node] = set()
        for op, i1, i2, j1, j2 in self._matcher(first, second).get_opcodes():
            if op != "equal":
                volatile.update(first[i1:i2])
                volatile.update(second[j1:j2])
        return frozenset(volatile)

    @staticmethod
    def is_unstable(first: Signature, second: Signature) -> bool:
        """True when one render is much smaller than the other (a truncated load)."""
        larger = max(len(first), len(second))
        if larger == 0:
            return False
        return min(len(first), len(second)) / larger < _MIN_SIZE_RATIO

    def compare(
        self, old: Signature, new: Signature, url: str, ignore: frozenset[Node] = frozenset()
    ) -> ChangeReport:
        return self.diff(old, new, url, ignore)[0]

    def diff(
        self, old: Signature, new: Signature, url: str, ignore: frozenset[Node] = frozenset()
    ) -> tuple[ChangeReport, Signature]:
        """What changed from ``old`` to ``new`` (``ignore``: the volatile nodes), and what to
        keep as the next snapshot.

        Only confirmed content is kept: ``new`` without the volatile nodes ``old`` did not
        have, plus the old nodes that vanished only between the two renders. A change that
        happened between the renders is therefore reported by the next round, if it lasts.
        """
        known, confirmed = set(old), set(new)
        removed: list[Node] = []
        added: list[Node] = []
        modified: list[tuple[Node, Node]] = []
        accepted: list[Node] = []
        for op, i1, i2, j1, j2 in self._matcher(old, new).get_opcodes():
            if op == "equal":
                accepted.extend(new[j1:j2])
                continue
            accepted.extend(n for n in old[i1:i2] if n in ignore and n not in confirmed)
            accepted.extend(n for n in new[j1:j2] if n not in ignore or n in known)
            old_block, new_block = self._without_volatile(old[i1:i2], new[j1:j2], ignore)
            if op == "replace":
                old_block, new_block, pairs = self._pair_attribute_changes(old_block, new_block)
                modified.extend(pairs)
            removed.extend(old_block)
            added.extend(new_block)
        report = ChangeReport(url, tuple(removed), tuple(added), tuple(modified))
        return report, tuple(accepted)

    @staticmethod
    def _matcher(a: Signature, b: Signature) -> SequenceMatcher[Node]:
        return SequenceMatcher(None, a, b, autojunk=False)

    @staticmethod
    def _without_volatile(
        old_block: Signature, new_block: Signature, ignore: frozenset[Node]
    ) -> tuple[list[Node], list[Node]]:
        """Drop volatile nodes and, on the old side, their previous incarnations.

        An old node is the previous value of a volatile one when both show the same text
        with other attributes, or the same attributes with a text of the same shape (the
        timer read 00:05:30, now 00:05:28). "Presale is live" turning into a ticking
        "Launch in 00:05:30" is a change, not a previous value.
        """
        volatile_new = [node for node in new_block if node in ignore]
        stable_new = [node for node in new_block if node not in ignore]
        # Indexed, so that a block of thousands of rows is paired in one pass: the earliest
        # unused candidate by equal text, or by equal attributes and shape.
        by_text: dict[tuple[str, str], deque[int]] = defaultdict(deque)
        by_shape: dict[tuple[str, tuple[tuple[str, str], ...], str], deque[int]] = defaultdict(
            deque
        )
        for index, node in enumerate(volatile_new):
            by_text[node.tag, node.text].append(index)
            by_shape[node.tag, node.attrs, shape(node.text)].append(index)
        used: set[int] = set()

        def first_unused(queue: deque[int] | None) -> int | None:
            while queue and queue[0] in used:
                queue.popleft()
            return queue[0] if queue else None

        stable_old: list[Node] = []
        for node in old_block:
            if node in ignore:
                continue
            candidates = (
                first_unused(by_text.get((node.tag, node.text))),
                first_unused(by_shape.get((node.tag, node.attrs, shape(node.text)))),
            )
            counterpart = min((c for c in candidates if c is not None), default=None)
            if counterpart is None:
                stable_old.append(node)
            else:
                used.add(counterpart)
        return stable_old, stable_new

    @staticmethod
    def _pair_attribute_changes(
        old_block: list[Node], new_block: list[Node]
    ) -> tuple[list[Node], list[Node], list[tuple[Node, Node]]]:
        """Match removed/added nodes with equal tag and text: attribute-only changes."""
        remaining_new = list(new_block)
        unpaired_old: list[Node] = []
        pairs: list[tuple[Node, Node]] = []
        for node in old_block:
            match = next(
                (
                    index
                    for index, candidate in enumerate(remaining_new)
                    if candidate.tag == node.tag and candidate.text == node.text
                ),
                None,
            )
            if match is None:
                unpaired_old.append(node)
            else:
                pairs.append((node, remaining_new.pop(match)))
        return unpaired_old, remaining_new, pairs
