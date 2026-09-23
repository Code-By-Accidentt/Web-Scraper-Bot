"""Comparing two page signatures and telling real changes from render noise."""

from __future__ import annotations

from difflib import SequenceMatcher

from scraper_bot.models import ChangeReport, Node, Signature

# A render with fewer than this share of the other render's nodes is a truncated load.
_MIN_SIZE_RATIO = 0.6


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
        removed: list[Node] = []
        added: list[Node] = []
        modified: list[tuple[Node, Node]] = []
        for op, i1, i2, j1, j2 in self._matcher(old, new).get_opcodes():
            if op == "equal":
                continue
            old_block, new_block = self._without_volatile(old[i1:i2], new[j1:j2], ignore)
            if op == "replace":
                old_block, new_block, pairs = self._pair_attribute_changes(old_block, new_block)
                modified.extend(pairs)
            removed.extend(old_block)
            added.extend(new_block)
        return ChangeReport(url, tuple(removed), tuple(added), tuple(modified))

    @staticmethod
    def _matcher(a: Signature, b: Signature) -> SequenceMatcher[Node]:
        return SequenceMatcher(None, a, b, autojunk=False)

    @staticmethod
    def _without_volatile(
        old_block: Signature, new_block: Signature, ignore: frozenset[Node]
    ) -> tuple[list[Node], list[Node]]:
        """Drop volatile nodes and, on the old side, their previous incarnations."""
        volatile_new = [node for node in new_block if node in ignore]
        stable_new = [node for node in new_block if node not in ignore]
        stable_old: list[Node] = []
        for node in old_block:
            if node in ignore:
                continue
            counterpart = next(
                (
                    index
                    for index, candidate in enumerate(volatile_new)
                    if candidate.tag == node.tag
                    and (candidate.attrs == node.attrs or candidate.text == node.text)
                ),
                None,
            )
            if counterpart is None:
                stable_old.append(node)
            else:
                volatile_new.pop(counterpart)
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
