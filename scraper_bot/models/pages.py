"""Page snapshots, change reports and page check results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from scraper_bot.models.addresses import Address


@dataclass(frozen=True, slots=True)
class Node:
    """One HTML element reduced to what matters for comparison."""

    tag: str
    attrs: tuple[tuple[str, str], ...]
    text: str

    def describe(self) -> str:
        """Return the element as one short line of HTML-like text for an alert."""
        attrs = " ".join(f'{k}="{shorten(v, 40)}"' for k, v in self.attrs)
        head = f"<{self.tag} {attrs}>" if attrs else f"<{self.tag}>"
        return shorten(f"{head} {self.text}".strip(), 120)

    def describe_attr_change(self, other: Node) -> str:
        """Return one short line listing the attributes that differ in ``other``.

        Each shows as ``name: old -> new``, with "(none)" for a missing attribute.
        """
        before, after = dict(self.attrs), dict(other.attrs)
        parts = []
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                was = shorten(before.get(key, "(none)"), 40)
                now = shorten(after.get(key, "(none)"), 40)
                parts.append(f"{key}: {was} -> {now}")
        text = f" {self.text}" if self.text else ""
        return shorten(f"<{self.tag}>{text}: " + "; ".join(parts), 160)


Signature = tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class NoiseEntry:
    """How often one element changed recently; elements that keep changing get silenced."""

    count: int
    last_seen: float


NoiseMemory = dict[str, NoiseEntry]


@dataclass(slots=True)
class Snapshot:
    """What is saved for one page: its last accepted signature and its noise memory.

    ``deep_seen`` holds the addresses the page's scripts and JSON already carried when the
    page was first deep-scanned (None: not recorded yet). Such an address is ignored while
    it is only there.
    """

    signature: Signature
    noise: NoiseMemory = field(default_factory=dict)
    deep_seen: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class ChangeCounts:
    """How many elements a change removed, added and modified."""

    removed: int = 0
    added: int = 0
    modified: int = 0


@dataclass(frozen=True, slots=True)
class ChangeReport:
    """The elements of a page that were removed, added or modified since its snapshot.

    Modified elements come as (before, after) pairs.
    """

    url: str
    removed: tuple[Node, ...] = ()
    added: tuple[Node, ...] = ()
    modified: tuple[tuple[Node, Node], ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when nothing changed."""
        return not (self.removed or self.added or self.modified)

    @property
    def counts(self) -> ChangeCounts:
        """How many elements were removed, added and modified."""
        return ChangeCounts(len(self.removed), len(self.added), len(self.modified))


@dataclass(frozen=True, slots=True)
class BlockSignal:
    """Evidence that the site is refusing the bot: a rate limit, a WAF challenge, an IP ban."""

    reason: str  # the answer: "HTTP 403", "challenge page with HTTP 200", a network error
    retry_after: float | None = None  # seconds, from a Retry-After header
    protection: str | None = None  # who refused, when recognisable: "Cloudflare challenge"

    def describe(self) -> str:
        """Return the reason, after the name of the protection when it is known."""
        return f"{self.protection} ({self.reason})" if self.protection else self.reason


class PageStatus(Enum):
    """The result of checking one page."""

    UNAVAILABLE = "unavailable"  # fetch failed or non-2xx: nothing was compared
    BASELINE = "baseline"  # first snapshot stored
    UNCHANGED = "unchanged"  # identical, or only volatile parts differed
    UNSTABLE = "unstable"  # two consecutive renders disagreed too much
    CHANGED = "changed"  # confirmed change, see ``report``


@dataclass(frozen=True, slots=True)
class PageCheckOutcome:
    """Everything one check of one page found out."""

    url: str
    status: PageStatus
    report: ChangeReport | None = None
    error: str | None = None
    block: BlockSignal | None = None
    candidates: tuple[Address, ...] = ()  # every syntactically valid address seen on the page
    timed_out: bool = False  # the document never arrived within the load timeout
    captcha: str | None = None  # a captcha widget the page shows, e.g. "hCaptcha"


def shorten(text: str, limit: int) -> str:
    """Return ``text`` cut to ``limit`` characters, ending in "..." when it was longer."""
    return text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."
