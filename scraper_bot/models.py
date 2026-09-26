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
    """An address an RPC node confirmed to be a contract, an SPL mint or a Sui coin/package."""

    address: Address
    network: str


class ValidationStatus(Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ALREADY_KNOWN = "already_known"
    ALREADY_REJECTED = "already_rejected"
    # no verdict: an endpoint failed (EVM: at least one network, and none found a token);
    # nothing was cached
    UNAVAILABLE = "unavailable"


class AddressKind(Enum):
    """What the chain says an address is (beyond "a token or not")."""

    TOKEN = "token"  # ERC-20, SPL / Token-2022 mint or Sui coin: the only kind announced
    NO_CODE = "no_code"  # EVM: no bytecode anywhere, a plain wallet or not deployed yet
    WALLET = "wallet"  # EVM: an EIP-7702 delegated wallet (its "code" is 0xef0100 + address)
    PAIR = "pair"  # an LP pair or pool; the tokens it holds are in ``resolved``
    VAULT = "vault"  # ERC-4626 vault shares: a token, but never a launch
    NOT_TOKEN = "not_token"  # a contract, program or account that is not a token
    MISSING = "missing"  # Solana: no account at this address yet
    NOT_YET_MINTED = "not_yet_minted"  # Solana: a decimals-0 mint with nothing minted yet
    NFT = "nft"  # Solana: a decimals-0 mint with a supply of exactly one
    UNKNOWN = "unknown"  # a cached verdict or no verdict at all


# Kinds that may still turn into a token later: the address is worth watching.
MAY_APPEAR = frozenset({AddressKind.NO_CODE, AddressKind.MISSING, AddressKind.NOT_YET_MINTED})


@dataclass(frozen=True, slots=True)
class ValidationResult:
    address: Address
    status: ValidationStatus
    network: str | None = None
    kind: AddressKind = AddressKind.UNKNOWN
    resolved: tuple[Address, ...] = ()  # PAIR: its tokens (stop-listed ones left out)
    via: Address | None = None  # this token was found through that LP pair

    @property
    def contract(self) -> ConfirmedContract | None:
        if self.status is ValidationStatus.CONFIRMED and self.network:
            return ConfirmedContract(self.address, self.network)
        return None


class SightingSource(Enum):
    """Where on a page an address was found; shown in the alert."""

    TEXT = "page text"  # the visible text of an element
    LINK = "link"  # an attribute of an element: href, src, data-*
    SOURCE = "page source"  # only in the raw HTML (inline scripts, meta tags) or in frames
    # and shadow roots (deep scan)
    SCRIPT = "loaded script"  # scripts and JSON the page loaded (deep scan)


@dataclass(frozen=True, slots=True)
class Sighting:
    """One address on a page, with what stands in front of it."""

    address: Address
    source: SightingSource
    context: str = ""  # up to 60 characters before the address, other addresses masked
    hinted: bool = False  # the words before it name it: CA, contract, mint, token, pump.fun/...


@dataclass(frozen=True, slots=True)
class Observation:
    """What one render of a page showed, reported before the page is compared."""

    page_url: str
    seen_at: float  # when the page content was read (epoch seconds)
    sightings: tuple[Sighting, ...]
    # verdicts for the sightings' addresses, then for the tokens behind LP pairs (``via``)
    results: tuple[ValidationResult, ...]
    # when the render began: what the page shows is from between this and ``seen_at``
    started_at: float | None = None


@dataclass(frozen=True, slots=True)
class Timeline:
    """When a token showed up, for the ⏱ line of its alert (epoch seconds)."""

    found_at: float  # the check that confirmed it
    missing_at: float | None = None  # the check before: the address was not there yet
    on_page_since: float | None = None  # first seen on the page, when that was earlier
    deployed: bool = False  # confirmed by the pending watch


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
    timed_out: bool = False  # the document never arrived within the load timeout


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."
