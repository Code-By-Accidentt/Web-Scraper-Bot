"""The pending watch: addresses that are not a token yet, re-checked until they become one.

A launch page may show its contract address minutes before the token exists, and a node
may not see a new mint for a moment after a page shows it. Without the watch such an
address would be cached as "not a token" for hours. The watch re-checks it in cheap
batches (one request per EVM network, one per 100 Solana accounts), announces it with its
deployment window the moment it becomes a token, and gives up after a day. Wallets are
dropped at once: an EVM address that has sent a transaction (or delegates via EIP-7702)
has a key and can never become a contract, and a Solana address holding an account that
is not a mint can never become one either.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from scraper_bot.chains.contract_validators import ContractValidator
from scraper_bot.chains.evm import is_delegated_wallet
from scraper_bot.chains.rpc import JsonRpcClient, RetryPolicy, RpcError
from scraper_bot.chains.solana import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from scraper_bot.config import Settings
from scraper_bot.models import (
    MAY_APPEAR,
    Address,
    AddressKind,
    Chain,
    Sighting,
    SightingSource,
    Timeline,
    ValidationResult,
    ValidationStatus,
)
from scraper_bot.monitoring.alerts import AlertRegistry
from scraper_bot.storage import atomic_write_text
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)

WATCH_FOR = 24 * 3600.0  # an address that never becomes a token is given up after a day
REMEMBER_DROPPED = 7 * 86_400.0  # a dropped address is not taken back for a week
SITE_LIMIT = 100  # watched addresses per site
TOTAL_LIMIT = 1000
EVM_PER_TICK = 20  # addresses in one tick's batch per EVM network
SOLANA_PER_TICK = 100  # getMultipleAccounts takes up to 100 accounts
CONFIRMING_AT_ONCE = 4  # full validations of addresses that just appeared, in parallel
_FAST_WINDOWS = ((15 * 60.0, 1.0), (3600.0, 5.0))  # (age below, seconds between checks)
_SLOW_INTERVAL = 30.0
_SAVE_EVERY = 5.0
_STORE_VERSION = 2
_EMPTY_CODE = frozenset({"", "0x", "0x0"})
_MINT_OWNERS = frozenset({TOKEN_PROGRAM, TOKEN_2022_PROGRAM})
_WATCHED_CHAINS = frozenset({Chain.ETHEREUM, Chain.SOLANA})  # a Sui package cannot be known early
# A hint in the words before a link (pump.fun/, /token/, "CA") counts like one in text.
_VISIBLE = frozenset({SightingSource.TEXT, SightingSource.LINK})
_SOLANA = "Solana"  # the "network" of a Solana address in ``absent_on``

Listener = Callable[[Address], None]


@dataclass(slots=True)
class PendingEntry:
    address: Address
    site_url: str  # the site (and page, below) that showed it first: named in the alert
    page_url: str
    first_seen: float  # when a page showed it first (epoch seconds)
    fast: bool  # named as the token on the page, or seen by a turbo session
    sighting: Sighting | None = None
    classified: bool = False  # checked once: nothing lives there yet (EVM: and no wallet)
    # On a page when a turbo session started: news only where it was seen absent since.
    baseline: bool = False
    last_check: float | None = None  # the last check attempt (it may have failed or been partial)
    next_check: float = 0.0
    # every site showing it -> the page it is on there (site_url and page_url included)
    sites: dict[str, str] = field(default_factory=dict)
    # network -> the last check that found nothing there (EVM network name, or "Solana")
    absent_on: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.sites.setdefault(self.site_url, self.page_url)


def check_interval(entry: PendingEntry, now: float, turbo: float | None = None) -> float:
    """Seconds until the next check: every second while fresh, slower later."""
    interval = _SLOW_INTERVAL
    if entry.fast:
        age = now - entry.first_seen
        interval = next((pause for limit, pause in _FAST_WINDOWS if age < limit), _SLOW_INTERVAL)
    return min(interval, turbo) if turbo is not None else interval


@dataclass(frozen=True, slots=True)
class _Dropped:
    until: float
    hard: bool  # True: a wallet or a non-token; False: only given up on (expired)


class PendingWatch:
    """Addresses of every site that may still become a token, checked in batches."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        *,
        validator: ContractValidator,
        registry: AlertRegistry,
        notifier: Notifier,
        path: Path,
        turbo_interval: Callable[[], float],
        is_paused: Callable[[str], bool] = lambda _site: False,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick: float = 1.0,
        retry: RetryPolicy | None = None,
    ) -> None:
        timeout = settings.rpc_timeout
        self._evm = [
            (net.name, JsonRpcClient(http, net.rpc_url, timeout, retry=retry))
            for net in settings.evm_networks
        ]
        self._solana = JsonRpcClient(http, settings.solana_rpc_url, timeout, retry=retry)
        self._validator = validator
        self._registry = registry
        self._notifier = notifier
        self._path = path
        self._turbo_interval = turbo_interval
        self._is_paused = is_paused
        self._clock = clock
        self._sleep = sleep
        self._tick = tick
        self._entries: dict[Address, PendingEntry] = {}
        self._dropped: dict[Address, _Dropped] = {}
        self._turbo_sites: set[str] = set()
        self._listeners: dict[str, list[Listener]] = {}
        self._confirming = asyncio.Semaphore(CONFIRMING_AT_ONCE)
        self._dirty = False
        self._saved_at = 0.0

    # ── what others see and do ──────────────────────────────────────────────
    def count(self, site_url: str | None = None) -> int:
        if site_url is None:
            return len(self._entries)
        return sum(1 for entry in self._entries.values() if site_url in entry.sites)

    def offer(
        self,
        site_url: str,
        page_url: str,
        sighting: Sighting | None,
        result: ValidationResult,
        *,
        fast: bool = False,
    ) -> bool:
        """Take an address a check found not to be a token, if it may still become one.

        True when the address is newly watched. One already watched only gains the site,
        and the fast pace when this offer is urgent (``fast`` or hinted).
        """
        address = result.address
        if address.chain not in _WATCHED_CHAINS:
            return False
        if result.kind is not AddressKind.UNKNOWN and result.kind not in MAY_APPEAR:
            return False  # a contract, a wallet, an LP pair, a program: never a token later
        if result.status not in {
            ValidationStatus.REJECTED,
            ValidationStatus.ALREADY_REJECTED,  # a cached verdict: the first check will tell
            ValidationStatus.UNAVAILABLE,
        }:
            return False
        hinted = sighting is not None and sighting.hinted and sighting.source in _VISIBLE
        urgent = fast or hinted
        if self._join(address, site_url, page_url, urgent):
            return False
        # A missing Solana account needs no further look; an EVM address still does (nonce).
        known_absent = address.chain is Chain.SOLANA and result.kind in MAY_APPEAR
        entry = self._admit(address, site_url, page_url, sighting, urgent, known_absent, False)
        if entry is not None and known_absent:
            entry.absent_on[_SOLANA] = self._clock()
        return entry is not None

    def offer_baseline(self, site_url: str, page_url: str, sighting: Sighting) -> None:
        """An address a turbo session saw in its first cycle: news only if it does not exist."""
        address = sighting.address
        if address.chain in _WATCHED_CHAINS and not self._join(address, site_url, page_url, True):
            self._admit(address, site_url, page_url, sighting, True, False, True)

    def announced(self, site_url: str, address: Address) -> None:
        """Someone else (the regular monitor) announced a token of ``site_url``."""
        entry = self._entries.get(address)
        if entry is not None:
            self._remove(entry)
        self._tell({site_url, *(entry.sites if entry else ())}, address)

    def start_turbo(self, site_url: str, listener: Listener) -> None:
        """Check the site's addresses at the turbo pace and report its tokens to ``listener``."""
        self._turbo_sites.add(site_url)
        self._listeners.setdefault(site_url, []).append(listener)
        now = self._clock()
        for entry in self._entries.values():
            if site_url in entry.sites:
                entry.next_check = min(entry.next_check, now)  # not after a 30 s slow pause

    def stop_turbo(self, site_url: str, listener: Listener) -> None:
        self._turbo_sites.discard(site_url)
        listeners = self._listeners.get(site_url, [])
        if listener in listeners:
            listeners.remove(listener)
        if not listeners:
            self._listeners.pop(site_url, None)

    def forget_site(self, site_url: str) -> None:
        for entry in [e for e in self._entries.values() if site_url in e.sites]:
            self._detach(entry, site_url)

    # ── the loop ────────────────────────────────────────────────────────────
    async def run(self) -> None:
        """Check due addresses every ``tick`` seconds; nothing is sent while none is watched."""
        while True:
            await self._sleep(self._tick)
            if self._entries:
                try:
                    await self.check_due()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("checking the watched addresses failed")
            now = self._clock()
            # A clock stepped back must not hold saves off until it catches up.
            if self._dirty and not 0 <= now - self._saved_at < _SAVE_EVERY:
                self.save()

    async def check_due(self) -> None:
        now = self._clock()
        for address, entry in list(self._entries.items()):
            if now - entry.first_seen > WATCH_FOR:
                log.info("gave up on %s %s after a day", address.chain.value, address)
                self._drop(entry, now, hard=False)
            elif entry.next_check > now + _SLOW_INTERVAL:
                entry.next_check = now  # the clock stepped back: do not wait out the step
        due = sorted(
            (e for e in self._entries.values() if e.next_check <= now and not self._paused(e)),
            key=lambda e: (not e.fast, e.next_check),
        )
        evm = [e for e in due if e.address.chain is Chain.ETHEREUM][:EVM_PER_TICK]
        solana = [e for e in due if e.address.chain is Chain.SOLANA][:SOLANA_PER_TICK]
        looks = await asyncio.gather(self._look_evm(evm, now), self._look_solana(solana, now))
        await asyncio.gather(
            *(
                self._confirm(entry, networks, now)
                for appeared in looks
                for entry, networks in appeared
            )
        )

    # ── cheap existence checks ──────────────────────────────────────────────
    async def _look_evm(
        self, entries: list[PendingEntry], now: float
    ) -> list[tuple[PendingEntry, tuple[str, ...] | None]]:
        if not entries:
            return []
        unknown = [e for e in entries if not e.classified]
        calls = [("eth_getCode", [e.address.value, "latest"]) for e in entries]
        calls += [("eth_getTransactionCount", [e.address.value, "latest"]) for e in unknown]
        answers = await asyncio.gather(
            *(client.call_batch(calls) for _, client in self._evm), return_exceptions=True
        )
        per_network = [
            (name, answer)
            for (name, _), answer in zip(self._evm, answers, strict=True)
            if isinstance(answer, list)
        ]
        complete = len(per_network) == len(self._evm)
        appeared: list[tuple[PendingEntry, tuple[str, ...] | None]] = []
        for index, entry in enumerate(entries):
            codes = [(name, network[index]) for name, network in per_network]
            if any(isinstance(code, str) and is_delegated_wallet(code) for _, code in codes):
                self._drop(entry, now, reason="an EIP-7702 wallet")
                continue
            deployed = tuple(
                name
                for name, code in codes
                if isinstance(code, str) and code.lower() not in _EMPTY_CODE
            )
            if deployed:
                self._appeared(entry, deployed, now, appeared)
                continue
            empty = [name for name, code in codes if isinstance(code, str)]
            answered = complete and len(empty) == len(self._evm)
            if not entry.classified:
                at = len(entries) + unknown.index(entry)
                nonces = [network[at] for _, network in per_network]
                if any(_uint(nonce) for nonce in nonces):
                    self._drop(entry, now, reason="a wallet (it has sent transactions)")
                    continue
                answered = answered and all(isinstance(nonce, str) for nonce in nonces)
            self._absent(entry, empty, now, answered)
        return appeared

    async def _look_solana(
        self, entries: list[PendingEntry], now: float
    ) -> list[tuple[PendingEntry, tuple[str, ...] | None]]:
        if not entries:
            return []
        options = {
            "encoding": "base64",
            "commitment": "confirmed",
            "dataSlice": {"offset": 0, "length": 0},  # existence and owner are enough
        }
        try:
            result = await self._solana.call(
                "getMultipleAccounts", [[e.address.value for e in entries], options]
            )
        except RpcError as exc:
            log.debug("Solana existence check failed: %s", exc)
            result = None
        values = result.get("value") if isinstance(result, dict) else None
        if not isinstance(values, list) or len(values) != len(entries):
            for entry in entries:
                self._reschedule(entry, now)
            return []
        appeared: list[tuple[PendingEntry, tuple[str, ...] | None]] = []
        for entry, value in zip(entries, values, strict=True):
            if value is None:
                self._absent(entry, [_SOLANA], now, True)
            elif isinstance(value, dict) and value.get("owner") in _MINT_OWNERS:
                self._appeared(entry, (_SOLANA,), now, appeared)
            else:
                self._drop(entry, now, reason="an account that is not a mint")
        return appeared

    def _appeared(
        self,
        entry: PendingEntry,
        where: tuple[str, ...],
        now: float,
        appeared: list[tuple[PendingEntry, tuple[str, ...] | None]],
    ) -> None:
        if entry.baseline and not any(name in entry.absent_on for name in where):
            # It existed when turbo started (nobody saw it absent there since): the regular
            # monitor judges it. Not remembered as dropped: another session may still ask.
            log.info("%s %s: old news when turbo started", entry.address.chain.value, entry.address)
            self._remove(entry)
            return
        # EVM: validate on the networks that show the bytecode; Solana has one "network".
        appeared.append((entry, None if where == (_SOLANA,) else where))

    def _absent(
        self, entry: PendingEntry, where: Iterable[str], now: float, answered: bool
    ) -> None:
        for name in where:
            if name not in entry.absent_on:
                self._dirty = True  # what a restart needs to tell a deployment from old news
            entry.absent_on[name] = now
        if answered and not entry.classified:
            entry.classified = True
            self._dirty = True
        self._reschedule(entry, now)

    # ── confirmation ────────────────────────────────────────────────────────
    async def _confirm(
        self, entry: PendingEntry, networks: tuple[str, ...] | None, now: float
    ) -> None:
        async with self._confirming:
            results = await self._validator.validate_many(
                [entry.address], negative_cache=False, networks=networks, urgent=True
            )
        own = results[0]
        tokens = [result for result in results if result.contract is not None]
        if tokens:
            # Announced even if the entry left the watch meanwhile (evicted, /remove): the
            # token is in the known cache now, so nobody else would announce it.
            self._remove(entry)
            for result in tokens:
                contract = result.contract
                assert contract is not None
                if self._registry.claim(contract.address):
                    await self._notifier.contract_found(
                        contract,
                        entry.page_url,
                        sighting=entry.sighting,
                        timeline=self._timeline(entry, networks, via=result.via is not None),
                    )
                self._tell(entry.sites, contract.address)
            return
        if entry.address not in self._entries:
            return  # removed meanwhile (/remove, announced by the monitor)
        if own.status is ValidationStatus.ALREADY_KNOWN:
            self._remove(entry)  # announced by someone else meanwhile
            self._tell(entry.sites, own.address)
            return
        if own.status is ValidationStatus.UNAVAILABLE or own.kind in MAY_APPEAR:
            self._reschedule(entry, now)
        else:
            self._drop(entry, now, reason=own.kind.value)

    def _timeline(
        self, entry: PendingEntry, networks: tuple[str, ...] | None, *, via: bool = False
    ) -> Timeline:
        """When it was deployed: after the last check that saw nothing where it now is."""
        found_at = self._clock()  # once the node answered: the tick may predate the deployment
        where = networks or (_SOLANA,)
        misses = [entry.absent_on[name] for name in where if name in entry.absent_on]
        # Nobody saw it absent there (the first look already found it), or the address was
        # an LP pair: its deployment says nothing about when the token behind it was.
        if not misses or via:
            return Timeline(found_at, on_page_since=entry.first_seen)
        return Timeline(
            found_at, missing_at=max(misses), on_page_since=entry.first_seen, deployed=True
        )

    def _tell(self, sites: Iterable[str], address: Address) -> None:
        for site in list(sites):
            for listener in list(self._listeners.get(site, ())):
                listener(address)

    # ── bookkeeping ─────────────────────────────────────────────────────────
    def _join(self, address: Address, site_url: str, page_url: str, urgent: bool) -> bool:
        """An address already watched gains the site and, when urgent, the fast pace."""
        entry = self._entries.get(address)
        if entry is None:
            return False
        if site_url not in entry.sites:
            entry.sites[site_url] = page_url
            self._dirty = True
        if urgent and not entry.fast:
            entry.fast = True
            entry.next_check = min(entry.next_check, self._clock())
            self._dirty = True
        return True

    def _admit(
        self,
        address: Address,
        site_url: str,
        page_url: str,
        sighting: Sighting | None,
        fast: bool,
        classified: bool,
        baseline: bool,
    ) -> PendingEntry | None:
        now = self._clock()
        dropped = self._dropped.get(address)
        if dropped is not None:
            # A wallet stays out; an address only given up on comes back when it is urgent
            # (named as the CA, or a turbo session is on its site).
            if now < dropped.until and (dropped.hard or not fast):
                return None
            del self._dropped[address]
        if not self._make_room(site_url, fast):
            return None
        entry = PendingEntry(
            address, site_url, page_url, now, fast, sighting, classified, baseline, next_check=now
        )
        self._entries[address] = entry
        self._dirty = True
        log.info(
            "watching %s %s from %s until it becomes a token",
            address.chain.value,
            address,
            page_url,
        )
        return entry

    def _make_room(self, site_url: str, fast: bool) -> bool:
        """Room for one more entry; a fast one may push out the oldest slow one."""
        site_full = self.count(site_url) >= SITE_LIMIT
        if not site_full and len(self._entries) < TOTAL_LIMIT:
            return True
        if not fast:
            return False  # slow offers wait until room frees up, no churn
        slow = [
            e
            for e in self._entries.values()
            if not e.fast and (not site_full or site_url in e.sites)
        ]
        if not slow:
            return False
        oldest = min(slow, key=lambda e: e.first_seen)
        if site_full and len(self._entries) < TOTAL_LIMIT:
            self._detach(oldest, site_url)  # other sites showing it keep watching it
        else:
            self._remove(oldest)
        return True

    def _detach(self, entry: PendingEntry, site_url: str) -> None:
        """``site_url`` no longer shows the address; without any site it leaves the watch."""
        entry.sites.pop(site_url, None)
        if not entry.sites:
            self._remove(entry)
        elif entry.site_url == site_url:
            entry.site_url, entry.page_url = next(iter(entry.sites.items()))
        self._dirty = True

    def _reschedule(self, entry: PendingEntry, now: float) -> None:
        entry.last_check = now
        turbo = self._turbo_interval() if entry.sites.keys() & self._turbo_sites else None
        entry.next_check = now + check_interval(entry, now, turbo)

    def _drop(
        self, entry: PendingEntry, now: float, *, reason: str = "", hard: bool = True
    ) -> None:
        if reason:
            log.info("stopped watching %s %s: %s", entry.address.chain.value, entry.address, reason)
        self._remove(entry)
        self._dropped[entry.address] = _Dropped(now + REMEMBER_DROPPED, hard)
        self._dirty = True

    def _remove(self, entry: PendingEntry) -> None:
        if self._entries.pop(entry.address, None) is not None:
            self._dirty = True

    def _paused(self, entry: PendingEntry) -> bool:
        """True while every site showing it is paused and none is in turbo."""
        if entry.sites.keys() & self._turbo_sites:
            return False
        return all(self._is_paused(site) for site in entry.sites)

    # ── persistence ─────────────────────────────────────────────────────────
    def load(self) -> None:
        """Entries and dropped addresses of an earlier run; a broken file starts empty."""
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            now = self._clock()
            # A version-1 file has no per-network absence: its turbo baselines would all
            # turn into old news, so they start over as ordinary entries.
            old = int(data.get("version", 1)) < 2
            entries = [_entry_from(raw, forget_baseline=old) for raw in data.get("entries", [])]
            dropped = {
                address: _Dropped(float(until), hard)
                for section, hard in (("dropped", True), ("expired", False))
                for key, until in data.get(section, {}).items()
                if (address := _address_from(key)) is not None and float(until) > now
            }
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            log.warning("pending watch file %s is unreadable (%s); starting empty", self._path, exc)
            return
        self._entries = {entry.address: entry for entry in entries if entry is not None}
        self._dropped = dropped
        if self._entries:
            log.info("%d address(es) watched until they become a token", len(self._entries))

    def save(self) -> None:
        now = self._clock()
        self._dropped = {a: d for a, d in self._dropped.items() if d.until > now}
        payload = {
            "version": _STORE_VERSION,
            "entries": [_entry_to(entry) for entry in self._entries.values()],
            # wallets and non-tokens; "expired" holds addresses only given up on
            "dropped": {_key(a): d.until for a, d in self._dropped.items() if d.hard},
            "expired": {_key(a): d.until for a, d in self._dropped.items() if not d.hard},
        }
        try:
            atomic_write_text(self._path, json.dumps(payload, ensure_ascii=False))
        except OSError as exc:
            log.warning("saving the pending watch failed: %s", exc)
            return
        self._dirty = False
        self._saved_at = now


def _uint(answer: Any) -> int:
    try:
        return int(answer, 16) if isinstance(answer, str) else 0
    except ValueError:
        return 0


def _key(address: Address) -> str:
    return f"{address.chain.value}:{address.value}"


def _address_from(key: str) -> Address | None:
    chain, _, value = key.partition(":")
    try:
        return Address(Chain(chain), value) if value else None
    except ValueError:
        return None


def _entry_to(entry: PendingEntry) -> dict[str, Any]:
    sighting = entry.sighting
    return {
        "address": _key(entry.address),
        "site": entry.site_url,
        "page": entry.page_url,
        "sites": dict(entry.sites),
        "first_seen": entry.first_seen,
        "fast": entry.fast,
        "classified": entry.classified,
        "baseline": entry.baseline,
        "absent_on": dict(entry.absent_on),
        "source": sighting.source.name if sighting else None,
        "context": sighting.context if sighting else "",
        "hinted": sighting.hinted if sighting else False,
    }


def _sites_from(raw: Any, page_url: str) -> dict[str, str]:
    """Sites and their pages; an earlier build stored the sites alone, as a list."""
    if isinstance(raw, dict):
        return {str(site): str(page) for site, page in raw.items()}
    if isinstance(raw, list):
        return {str(site): page_url for site in raw}
    return {}


def _entry_from(raw: dict[str, Any], *, forget_baseline: bool = False) -> PendingEntry | None:
    address = _address_from(str(raw["address"]))
    if address is None:
        return None
    source = raw.get("source")
    sighting = (
        Sighting(
            address, SightingSource[source], str(raw.get("context", "")), bool(raw.get("hinted"))
        )
        if isinstance(source, str) and source in SightingSource.__members__
        else None
    )
    absent = raw.get("absent_on")
    return PendingEntry(
        address,
        str(raw["site"]),
        str(raw["page"]),
        float(raw["first_seen"]),
        bool(raw.get("fast")),
        sighting,
        bool(raw.get("classified")),
        bool(raw.get("baseline")) and not forget_baseline,
        sites=_sites_from(raw.get("sites"), str(raw["page"])),
        absent_on=(
            {str(name): float(at) for name, at in absent.items()}
            if isinstance(absent, dict)
            else {}
        ),
    )
