"""Plain data types shared between the modules."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any


class Chain(StrEnum):
    ETHEREUM = "Ethereum"
    SOLANA = "Solana"
    SUI = "Sui"


@dataclass(frozen=True, slots=True)
class Address:
    """A syntactically valid, canonical address (checksummed for EVM)."""

    chain: Chain
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ConfirmedContract:
    """An address that an RPC node confirmed to be a contract (or SPL mint)."""

    address: Address
    network: str


class ValidationStatus(Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ALREADY_KNOWN = "already_known"
    ALREADY_REJECTED = "already_rejected"
    UNAVAILABLE = "unavailable"  # every RPC call failed; nothing was cached


@dataclass(frozen=True, slots=True)
class ValidationResult:
    address: Address
    status: ValidationStatus
    network: str | None = None

    @property
    def contract(self) -> ConfirmedContract | None:
        if self.status is ValidationStatus.CONFIRMED and self.network:
            return ConfirmedContract(self.address, self.network)
        return None


@dataclass(frozen=True, slots=True)
class Node:
    """One HTML element reduced to what matters for comparison."""

    tag: str
    attrs: tuple[tuple[str, str], ...]
    text: str

    def describe(self, limit: int = 120) -> str:
        attrs = " ".join(f'{k}="{shorten(v, 40)}"' for k, v in self.attrs)
        head = f"<{self.tag} {attrs}>" if attrs else f"<{self.tag}>"
        return shorten(f"{head} {self.text}".strip(), limit)

    def describe_attr_change(self, other: Node, limit: int = 160) -> str:
        before, after = dict(self.attrs), dict(other.attrs)
        parts = []
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                was = shorten(before.get(key, "(none)"), 40)
                now = shorten(after.get(key, "(none)"), 40)
                parts.append(f"{key}: {was} -> {now}")
        text = f" {self.text}" if self.text else ""
        return shorten(f"<{self.tag}>{text}: " + "; ".join(parts), limit)


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


@dataclass(frozen=True, slots=True)
class ChangeCounts:
    removed: int = 0
    added: int = 0
    modified: int = 0

    @property
    def total(self) -> int:
        return self.removed + self.added + self.modified


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


@dataclass(slots=True)
class LastChange:
    at: float
    page_url: str
    counts: ChangeCounts

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "page_url": self.page_url,
            "removed": self.counts.removed,
            "added": self.counts.added,
            "modified": self.counts.modified,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LastChange:
        return cls(
            at=float(data.get("at", 0.0)),
            page_url=str(data.get("page_url", "")),
            counts=ChangeCounts(
                int(data.get("removed", 0)), int(data.get("added", 0)), int(data.get("modified", 0))
            ),
        )


@dataclass(slots=True)
class SiteState:
    """Persistent, user-visible state of one monitored root URL."""

    url: str
    notifications_enabled: bool = True
    paused: bool = False
    announced_pages: set[str] = field(default_factory=set)
    last_change: LastChange | None = None
    added_at: float = field(default_factory=time.time)
    unverified_enabled: bool = False
    # "Chain:address" -> when it was announced as unverified (watched until confirmed)
    unverified: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "notifications_enabled": self.notifications_enabled,
            "paused": self.paused,
            "announced_pages": sorted(self.announced_pages),
            "last_change": self.last_change.to_dict() if self.last_change else None,
            "added_at": self.added_at,
            "unverified_enabled": self.unverified_enabled,
            "unverified": dict(self.unverified),
        }

    @classmethod
    def from_dict(cls, url: str, data: dict[str, Any]) -> SiteState:
        last = data.get("last_change")
        raw_unverified = data.get("unverified")
        unverified = (
            {str(key): float(value) for key, value in raw_unverified.items()}
            if isinstance(raw_unverified, dict)
            else {}
        )
        return cls(
            url=url,
            notifications_enabled=bool(data.get("notifications_enabled", True)),
            paused=bool(data.get("paused", False)),
            announced_pages=set(data.get("announced_pages", ())),
            last_change=LastChange.from_dict(last) if isinstance(last, dict) else None,
            added_at=float(data.get("added_at", 0.0)) or time.time(),
            unverified_enabled=bool(data.get("unverified_enabled", False)),
            unverified=unverified,
        )


@dataclass(frozen=True, slots=True)
class BlockSignal:
    """Evidence that the site is refusing us (rate limit, WAF challenge, IP ban)."""

    reason: str
    retry_after: float | None = None


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
    contracts: tuple[ConfirmedContract, ...] = ()
    report: ChangeReport | None = None
    error: str | None = None
    block: BlockSignal | None = None
    candidates: tuple[Address, ...] = ()  # every syntactically valid address seen on the page


def shorten(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, marking the cut with an ellipsis."""
    return text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."
