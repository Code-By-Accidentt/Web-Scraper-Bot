"""Comparing two page signatures and telling real changes from render noise."""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import Counter, defaultdict, deque
from difflib import SequenceMatcher
from itertools import accumulate

from scraper_bot.models.pages import ChangeReport, Node, Signature

# If one render has less than this share of the other's nodes, it's a truncated load.
_MIN_SIZE_RATIO = 0.6
_NUMBER_RE = re.compile(r"\d[\d.,]*")
# How many steps SequenceMatcher's inner loop gets on one part of an alignment: this many
# per node, but at least _MIN_STEPS. Real pages of a few thousand nodes need up to about 100.
_STEPS_PER_NODE = 128
_MIN_STEPS = 20_000

Opcode = tuple[str, int, int, int, int]
Part = tuple[int, int, int, int]


def shape(text: str) -> str:
    """Return ``text`` with every number replaced by ``#``: "Raised: 12.5 SOL" -> "Raised: # SOL".

    Two texts of the same shape are one element showing another value, like a timer or a
    counter.
    """
    return _NUMBER_RE.sub("#", text)


class ChangeDetector:
    """Compares node lists in order and can leave volatile nodes out.

    Volatile nodes differ between two renders taken a few seconds apart (timers, counters,
    banners); they and their previous values stay out of the report.
    """

    @staticmethod
    def differs(old: Signature, new: Signature) -> bool:
        """True when the two signatures are not identical."""
        return old != new

    def volatile_nodes(self, first: Signature, second: Signature) -> frozenset[Node]:
        """Return the nodes that differ between two consecutive renders of the same page."""
        volatile: set[Node] = set()
        for op, i1, i2, j1, j2 in _Alignment(first, second).opcodes():
            if op == "equal":
                continue
            before, after = first[i1:i2], second[j1:j2]
            if op == "unordered":
                before, after = _without_common(before, after)
            volatile.update(before)
            volatile.update(after)
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
        """Return only the change report of :meth:`diff`."""
        return self.diff(old, new, url, ignore)[0]

    def diff(
        self, old: Signature, new: Signature, url: str, ignore: frozenset[Node] = frozenset()
    ) -> tuple[ChangeReport, Signature]:
        """Return the changes from ``old`` to ``new`` and the signature to store as the snapshot.

        ``ignore`` holds the volatile nodes, which stay out of the report. The snapshot keeps
        only confirmed content: ``new`` without the volatile nodes ``old`` lacked, plus the old
        nodes that vanished only between the two renders. So a change made between the renders
        is reported by the next round, if it lasts.
        """
        known, confirmed = set(old), set(new)
        removed: list[Node] = []
        added: list[Node] = []
        modified: list[tuple[Node, Node]] = []
        accepted: list[Node] = []
        for op, i1, i2, j1, j2 in _Alignment(old, new).opcodes():
            if op == "equal":
                accepted.extend(new[j1:j2])
                continue
            accepted.extend(n for n in old[i1:i2] if n in ignore and n not in confirmed)
            accepted.extend(n for n in new[j1:j2] if n not in ignore or n in known)
            old_block, new_block = old[i1:i2], new[j1:j2]
            if op == "unordered":
                old_block, new_block = _without_common(old_block, new_block)
            old_block, new_block = self._without_volatile(old_block, new_block, ignore)
            if op in ("replace", "unordered"):
                old_block, new_block, pairs = self._pair_attribute_changes(old_block, new_block)
                modified.extend(pairs)
            removed.extend(old_block)
            added.extend(new_block)
        report = ChangeReport(url, tuple(removed), tuple(added), tuple(modified))
        return report, tuple(accepted)

    @staticmethod
    def _without_volatile(
        old_block: Signature, new_block: Signature, ignore: frozenset[Node]
    ) -> tuple[list[Node], list[Node]]:
        """Return both blocks without volatile nodes and, on the old side, their previous values.

        An old node is a volatile node's previous value when both show the same text with other
        attributes, or the same attributes with a text of the same shape (a timer read
        00:05:30, now 00:05:28). "Presale is live" turning into a ticking "Launch in 00:05:30"
        is a real change.
        """
        volatile_new = [node for node in new_block if node in ignore]
        stable_new = [node for node in new_block if node not in ignore]
        # Index them so thousands of rows pair up in one pass, not in a quadratic search.
        by_text: dict[tuple[str, str], deque[int]] = defaultdict(deque)
        by_shape: dict[tuple[str, tuple[tuple[str, str], ...], str], deque[int]] = defaultdict(
            deque
        )
        for index, node in enumerate(volatile_new):
            by_text[node.tag, node.text].append(index)
            by_shape[node.tag, node.attrs, shape(node.text)].append(index)
        used: set[int] = set()

        def first_unused(queue: deque[int] | None) -> int | None:
            """Return the earliest index in ``queue`` not paired yet, or None."""
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
        """Pair removed and added nodes with the same tag and text as attribute-only changes.

        Each old node takes the earliest new node left with its tag and text. Return the
        unpaired old nodes, the unpaired new nodes and the pairs.
        """
        waiting: dict[tuple[str, str], deque[int]] = defaultdict(deque)
        for index, node in enumerate(new_block):
            waiting[node.tag, node.text].append(index)
        taken: set[int] = set()
        unpaired_old: list[Node] = []
        pairs: list[tuple[Node, Node]] = []
        for node in old_block:
            queue = waiting.get((node.tag, node.text))
            if queue:
                index = queue.popleft()
                taken.add(index)
                pairs.append((node, new_block[index]))
            else:
                unpaired_old.append(node)
        remaining_new = [node for index, node in enumerate(new_block) if index not in taken]
        return unpaired_old, remaining_new, pairs


class _Alignment:
    """Lines two node lists up and describes the result as SequenceMatcher opcodes.

    Each part goes to SequenceMatcher (autojunk off) while its work stays small, and its
    result is kept unless matching the common start and end first lines up more nodes (a
    node inserted into a run of identical nodes then stays one insertion). A bigger part is
    split at the nodes found exactly once on each side, the most of them in order (patience
    diff); one without such nodes becomes an "unordered" opcode, compared as a bag of nodes.
    """

    def __init__(self, a: Signature, b: Signature) -> None:
        """Align ``a`` with ``b`` at once; nodes are compared as small integers from here on."""
        ids: dict[Node, int] = {}
        self._a = [ids.setdefault(node, len(ids)) for node in a]
        self._b = [ids.setdefault(node, len(ids)) for node in b]
        self._blocks: list[tuple[int, int, int]] = []
        self._unordered: set[tuple[int, int]] = set()
        # All the parts together may spend four times the budget of the whole.
        self._steps_left = 4 * _budget(len(a) + len(b))
        todo: list[Part] = [(0, len(a), 0, len(b))]
        while todo:
            todo.extend(self._align(*todo.pop()))

    def opcodes(self) -> list[Opcode]:
        """Return the opcodes of SequenceMatcher.get_opcodes(), plus "unordered" ones."""
        blocks: list[tuple[int, int, int]] = []
        for i, j, size in sorted(self._blocks):
            if blocks and (i, j) == (blocks[-1][0] + blocks[-1][2], blocks[-1][1] + blocks[-1][2]):
                blocks[-1] = (blocks[-1][0], blocks[-1][1], blocks[-1][2] + size)
            else:
                blocks.append((i, j, size))
        blocks.append((len(self._a), len(self._b), 0))
        opcodes: list[Opcode] = []
        i = j = 0
        for ai, bj, size in blocks:
            if i < ai and j < bj:
                opcodes.append(
                    ("unordered" if (i, j) in self._unordered else "replace", i, ai, j, bj)
                )
            elif i < ai:
                opcodes.append(("delete", i, ai, j, bj))
            elif j < bj:
                opcodes.append(("insert", i, ai, j, bj))
            i, j = ai + size, bj + size
            if size:
                opcodes.append(("equal", ai, i, bj, j))
        return opcodes

    def _align(self, alo: int, ahi: int, blo: int, bhi: int) -> list[Part]:
        """Align one part, or split it at its anchors and return the parts left to align.

        SequenceMatcher's result for the whole part is kept unless matching the common
        start and end first lines up more nodes.
        """
        head, tail = self._common_ends(alo, ahi, blo, bhi)
        same = ahi - alo == bhi - blo == head + tail
        whole = self._sequence_matcher(alo, ahi, blo, bhi) if (head or tail) and not same else None
        if whole and _covers(whole, (alo, blo, head), (ahi, bhi, tail)):
            self._blocks.extend(whole)  # matching the ends first would change nothing
            return []
        ends = [(alo, blo, head)] if head else []
        alo, blo, ahi, bhi = alo + head, blo + head, ahi - tail, bhi - tail
        if tail:
            ends.append((ahi, bhi, tail))
        middle = [] if alo == ahi or blo == bhi else self._sequence_matcher(alo, ahi, blo, bhi)
        if whole is not None and (
            middle is None or _matched(whole) >= head + tail + _matched(middle)
        ):
            self._blocks.extend(whole)
            return []
        self._blocks.extend(ends)
        if middle is not None:
            self._blocks.extend(middle)
            return []
        anchors = self._anchors(alo, ahi, blo, bhi) if self._steps_left > 0 else []
        if not anchors:
            self._unordered.add((alo, blo))
            return []
        parts: list[Part] = []
        for i, j in anchors:
            self._blocks.append((i, j, 1))
            parts.append((alo, i, blo, j))
            alo, blo = i + 1, j + 1
        parts.append((alo, ahi, blo, bhi))
        return parts

    def _common_ends(self, alo: int, ahi: int, blo: int, bhi: int) -> tuple[int, int]:
        """Return how many nodes a part's two sides share at the start and at the end.

        When the two overlap (a repeated node between them), the longer one keeps the
        overlap, the start on a tie, as SequenceMatcher would decide.
        """
        a, b = self._a, self._b
        shorter = min(ahi - alo, bhi - blo)
        head = 0
        while head < shorter and a[alo + head] == b[blo + head]:
            head += 1
        tail = 0
        while tail < shorter and a[ahi - tail - 1] == b[bhi - tail - 1]:
            tail += 1
        if head + tail > shorter:
            if tail > head:
                head = shorter - tail
            else:
                tail = shorter - head
        return head, tail

    def _sequence_matcher(
        self, alo: int, ahi: int, blo: int, bhi: int
    ) -> list[tuple[int, int, int]] | None:
        """Return SequenceMatcher's matching blocks for one part, or None when too costly.

        It is not even started when its first search alone would use a quarter of the budget.
        """
        if self._steps_left <= 0:
            return None
        a, b = self._a[alo:ahi], self._b[blo:bhi]
        budget = min(self._steps_left, _budget(len(a) + len(b)))
        in_b = Counter(b)
        # cost[i] is the most steps a search over a[:i] can take: one per value, plus one for
        # each place in b where that value shows up.
        cost = list(accumulate((in_b[value] + 1 for value in a), initial=0))
        if 4 * cost[-1] > budget:
            return None
        matcher = _BudgetedMatcher(a, b, cost, budget)
        try:
            blocks = matcher.get_matching_blocks()
        except _OverBudget:
            return None
        finally:
            self._steps_left -= matcher.spent
        return [(alo + i, blo + j, size) for i, j, size in blocks if size]

    def _anchors(self, alo: int, ahi: int, blo: int, bhi: int) -> list[tuple[int, int]]:
        """Return (i, j) of the nodes found once on each side of a part, the most of them in order.

        Of equally long choices the one that starts earliest wins, as it would with
        SequenceMatcher.
        """
        self._steps_left -= ahi - alo + bhi - blo
        once_a, once_b = _found_once(self._a, alo, ahi), _found_once(self._b, blo, bhi)
        pairs = sorted((i, once_b[value]) for value, i in once_a.items() if value in once_b)
        # longest[k] is the most pairs we can take in order starting with pairs[k]. We fill it
        # in from the right.
        longest = [0] * len(pairs)
        tails: list[int] = []
        for k in range(len(pairs) - 1, -1, -1):
            key = -pairs[k][1]
            place = bisect_left(tails, key)
            if place == len(tails):
                tails.append(key)
            else:
                tails[place] = key
            longest[k] = place + 1
        chosen: list[tuple[int, int]] = []
        need, last_j = len(tails), -1
        for k, (i, j) in enumerate(pairs):
            if need and longest[k] == need and j > last_j:
                chosen.append((i, j))
                need, last_j = need - 1, j
        return chosen


class _OverBudget(Exception):
    """SequenceMatcher would take too long on this part."""


class _BudgetedMatcher(SequenceMatcher[int]):
    """A SequenceMatcher (autojunk off) that gives up before its work exceeds a budget."""

    def __init__(self, a: list[int], b: list[int], cost: list[int], budget: int) -> None:
        """Match ``a`` against ``b`` in at most ``budget`` steps of the inner loop.

        ``cost[i]`` is the most steps a search over ``a[:i]`` can take.
        """
        super().__init__(None, a, b, autojunk=False)
        self._cost = cost
        self._budget = budget
        self.spent = 0

    def find_longest_match(
        self, alo: int = 0, ahi: int | None = None, blo: int = 0, bhi: int | None = None
    ) -> tuple[int, int, int]:
        """Like SequenceMatcher's, but raise _OverBudget first if it might run over budget."""
        top = len(self.a) if ahi is None else ahi
        cost = self._cost[top] - self._cost[alo]
        if self.spent + cost > self._budget:
            raise _OverBudget
        self.spent += cost
        return super().find_longest_match(alo, ahi, blo, bhi)


def _budget(nodes: int) -> int:
    """Return the SequenceMatcher steps allowed on a part with this many nodes (both sides)."""
    return max(_MIN_STEPS, _STEPS_PER_NODE * nodes)


def _covers(
    blocks: list[tuple[int, int, int]], head: tuple[int, int, int], tail: tuple[int, int, int]
) -> bool:
    """Return True when the blocks match the whole ``head`` run and the whole ``tail`` run.

    Each run is (i, j, size): the head's i and j are where it starts, the tail's where it ends.
    """
    (first_i, first_j, first_size), (last_i, last_j, last_size) = blocks[0], blocks[-1]
    starts = (first_i, first_j) == head[:2] and first_size >= head[2]
    ends = (last_i + last_size, last_j + last_size) == tail[:2] and last_size >= tail[2]
    return (starts or not head[2]) and (ends or not tail[2])


def _matched(blocks: list[tuple[int, int, int]]) -> int:
    """Return how many nodes the matching blocks line up."""
    return sum(size for _, _, size in blocks)


def _found_once(values: list[int], lo: int, hi: int) -> dict[int, int]:
    """Return {value: index} for the values found exactly once in ``values[lo:hi]``."""
    counts = Counter(values[lo:hi])
    return {value: index for index, value in enumerate(values[lo:hi], lo) if counts[value] == 1}


def _without_common(before: Signature, after: Signature) -> tuple[Signature, Signature]:
    """Return both blocks without the nodes they share, as often as both have them."""
    common = Counter(before) & Counter(after)
    if not common:
        return before, after
    return _drop(before, common.copy()), _drop(after, common)


def _drop(block: Signature, counts: Counter[Node]) -> Signature:
    """Return ``block`` without its first ``counts[node]`` copies of each node."""
    kept: list[Node] = []
    for node in block:
        if counts[node]:
            counts[node] -= 1
        else:
            kept.append(node)
    return tuple(kept)
