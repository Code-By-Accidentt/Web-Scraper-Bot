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
        attrs = " ".join(f'{k}="{shorten(v, 40)}"' for k, v in self.attrs)
        head = f"<{self.tag} {attrs}>" if attrs else f"<{self.tag}>"
        return shorten(f"{head} {self.text}".strip(), 120)

    def describe_attr_change(self, other: Node) -> str:
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
    """How often an element changed recently; used to silence flapping elements."""

    count: int
    last_seen: float


NoiseMemory = dict[str, NoiseEntry]


@dataclass(slots=True)
class Snapshot:
    """What is stored per page: the last accepted signature plus the noise memory."""

    signature: Signature
    noise: NoiseMemory = field(default_factory=dict)
    # Deep scan: addresses the page's scripts and JSON carried when it was first checked
    # (None: not recorded yet). Such an address is ignored while it is only there.
    deep_seen: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class ChangeCounts:
    removed: int = 0
    added: int = 0
    modified: int = 0


@dataclass(frozen=True, slots=True)
class ChangeReport:
    url: str
    removed: tuple[Node, ...] = ()
    added: tuple[Node, ...] = ()
    modified: tuple[tuple[Node, Node], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.removed or self.added or self.modified)

    @property
    def counts(self) -> ChangeCounts:
        return ChangeCounts(len(self.removed), len(self.added), len(self.modified))


@dataclass(frozen=True, slots=True)
class BlockSignal:
    """Evidence that the site is refusing us (rate limit, WAF challenge, IP ban)."""

    reason: str  # the answer: "HTTP 403", "challenge page with HTTP 200", a network error
    retry_after: float | None = None
    protection: str | None = None  # who refused, when recognisable: "Cloudflare challenge"

    def describe(self) -> str:
        return f"{self.protection} ({self.reason})" if self.protection else self.reason


class PageStatus(Enum):
    UNAVAILABLE = "unavailable"  # fetch failed or non-2xx: nothing was compared
    BASELINE = "baseline"  # first snapshot stored
    UNCHANGED = "unchanged"  # identical, or only volatile parts differed
    UNSTABLE = "unstable"  # two consecutive renders disagreed too much
    CHANGED = "changed"  # confirmed change, see ``report``


@dataclass(frozen=True, slots=True)
class PageCheckOutcome:
    url: str
    status: PageStatus
    report: ChangeReport | None = None
    error: str | None = None
    block: BlockSignal | None = None
    candidates: tuple[Address, ...] = ()  # every syntactically valid address seen on the page
    timed_out: bool = False  # the document never arrived within the load timeout
    captcha: str | None = None  # a captcha widget the page shows, e.g. "hCaptcha"


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."
