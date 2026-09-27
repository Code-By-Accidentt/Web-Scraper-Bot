"""The pending watch: addresses that are not a token yet, re-checked until they become one.

A page may show a token's address before the token is deployed. The watch keeps such
addresses and checks them in cheap batches (code and nonce on every EVM network, the
account on Solana). When something appears there, a full validation decides, and a token
is announced with the moment it was deployed. Wallets and other non-tokens are dropped for
a week; an address that never becomes a token is given up after a day.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Iterable
from itertools import islice
from pathlib import Path
from typing import Any

import httpx

from scraper_bot.chains.evm.abi import has_code, is_delegated_wallet
from scraper_bot.chains.evm.validator import OPTIONAL_GRACE
from scraper_bot.chains.rpc.client import JsonRpcClient, RetryPolicy, RpcError
from scraper_bot.chains.solana.accounts import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from scraper_bot.chains.validator import ContractValidator
from scraper_bot.models.addresses import (
    MAY_APPEAR,
    Address,
    AddressKind,
    Chain,
    ValidationResult,
    ValidationStatus,
    address_key,
    parse_address_key,
)
from scraper_bot.models.sightings import Sighting, SightingSource, Timeline
from scraper_bot.monitoring.alerts import AlertRegistry
from scraper_bot.monitoring.pending.entry import (
    SLOW_INTERVAL,
    Dropped,
    PendingEntry,
    check_interval,
    entry_from_dict,
    entry_to_dict,
)
from scraper_bot.settings.env import Settings
from scraper_bot.storage.files import atomic_write_text
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)


WATCH_FOR = 24 * 3600.0
REMEMBER_DROPPED = 7 * 86_400.0
DROPPED_LIMIT = 20_000
SITE_LIMIT = 100  # watched addresses per site
TOTAL_LIMIT = 1000
EVM_PER_TICK = 20  # addresses in one tick's batch per EVM network
SOLANA_PER_TICK = 100  # getMultipleAccounts takes up to 100 accounts
CONFIRMING_AT_ONCE = 4  # full validations of addresses that just appeared, in parallel
_SAVE_EVERY = 5.0
_STORE_VERSION = 2
_MINT_OWNERS = frozenset({TOKEN_PROGRAM, TOKEN_2022_PROGRAM})
_WATCHED_CHAINS = frozenset({Chain.ETHEREUM, Chain.SOLANA})  # a Sui package cannot be known early
# A hint in the words before a link (pump.fun/, /token/, "CA") counts like one in text.
_VISIBLE = frozenset({SightingSource.TEXT, SightingSource.LINK})
_SOLANA = "Solana"  # the "network" of a Solana address in ``absent_on``

Listener = Callable[[Address], None]
# Gets each endpoint's failure, or None for an answer: the endpoint health of the validators.
HealthRecorder = Callable[[str, BaseException | None], None]


class PendingWatch:
    """Watches the addresses of every site that may still become a token.

    One loop runs per chain family, so a hanging EVM node never holds back a Solana mint.
    The watch is saved to a JSON file and loaded again at the next start.
    """

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
        health: HealthRecorder | None = None,
        stale_absences: Collection[str] = (),
        new_networks: Collection[str] = (),
    ) -> None:
        """Set up the RPC clients of every network; call :meth:`load`, then :meth:`run`.

        ``turbo_interval`` returns the turbo pace in seconds. ``new_networks`` are the EVM
        networks added since the last run, and ``stale_absences`` the networks switched off
        and on again, whose saved absences are out of date.
        """
        timeout = settings.rpc_timeout
        self._evm = [
            (net.name, JsonRpcClient(http, net.rpc_url, timeout, retry=retry))
            for net in settings.evm_networks
        ]
        self._required = frozenset(net.name for net in settings.evm_networks if not net.optional)
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
        self._dropped: dict[Address, Dropped] = {}
        self._turbo_sites: set[str] = set()
        self._listeners: dict[str, list[Listener]] = {}
        # One queue per chain family: EVM confirmations that hang never hold up a Solana mint.
        self._confirming = {
            Chain.ETHEREUM: asyncio.Semaphore(CONFIRMING_AT_ONCE),
            Chain.SOLANA: asyncio.Semaphore(CONFIRMING_AT_ONCE),
        }
        self._health = health
        self._stale_absences = frozenset(stale_absences)
        # Optional-network batches still running after their tick -> (their entries, sent at).
        self._late_batches: dict[asyncio.Future[list[Any]], tuple[list[PendingEntry], float]] = {}
        self._in_flight: set[str] = set()  # networks whose last batch has not come back
        self._new_networks = frozenset(new_networks)
        self._dirty = False
        self._saved_at = 0.0

    def count(self, site_url: str | None = None) -> int:
        """Return how many addresses are watched, in all or for one site."""
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
        """Watch an address a check found not to be a token, if it may still become one.

        Return True when the address is newly watched. One already watched only gains the
        site, and the fast pace when the offer is urgent (``fast``, or a hint on the page).
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
        """Watch an address a turbo session saw on its first read of a page.

        It is news only if a check finds it absent before it appears; one that already
        exists is left to the regular monitor.
        """
        address = sighting.address
        if address.chain in _WATCHED_CHAINS and not self._join(address, site_url, page_url, True):
            self._admit(address, site_url, page_url, sighting, True, False, True)

    def announced(self, site_url: str, address: Address) -> None:
        """Note that the regular monitor announced a token of ``site_url``.

        The address leaves the watch, and the turbo sessions of every site showing it hear
        about it.
        """
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
        """Return the site's addresses to their normal pace and stop telling ``listener``."""
        self._turbo_sites.discard(site_url)
        listeners = self._listeners.get(site_url, [])
        if listener in listeners:
            listeners.remove(listener)
        if not listeners:
            self._listeners.pop(site_url, None)

    def forget_site(self, site_url: str) -> None:
        """Forget a removed site; its addresses stay watched only if another site shows them."""
        for entry in [e for e in self._entries.values() if site_url in e.sites]:
            self._detach(entry, site_url)

    async def run(self) -> None:
        """Check due addresses every ``tick`` seconds; nothing is sent while none is watched.

        An error never ends the loop: it is logged, reported (a 🛑 at most every ten
        minutes) and the next tick tries again.
        """
        await asyncio.gather(self._loop(Chain.ETHEREUM), self._loop(Chain.SOLANA))

    async def _loop(self, chain: Chain) -> None:
        """Check one chain family's due addresses every tick and save the watch when needed."""
        while True:
            await self._sleep(self._tick)
            try:
                if self._entries:
                    await self.check_due(chain)
                now = self._clock()
                # A clock stepped back must not hold saves off until it catches up.
                if self._dirty and not 0 <= now - self._saved_at < _SAVE_EVERY:
                    self.save()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("the pending watch failed")
                self._notifier.report_later(
                    "pending",
                    "The watch of addresses published before their token failed",
                    f"{type(exc).__name__}: {exc}",
                    hint="It keeps running and tries again every second; the log has the "
                    "traceback.",
                )

    async def check_due(self, chain: Chain | None = None) -> None:
        """Check the addresses that are due, only those of ``chain`` when it is given.

        Addresses watched for over a day are given up first. Fast entries go first, up to
        ``EVM_PER_TICK`` EVM and ``SOLANA_PER_TICK`` Solana addresses per tick.
        """
        now = self._clock()
        for address, entry in list(self._entries.items()):
            if chain is not None and address.chain is not chain:
                continue
            if now - entry.first_seen > WATCH_FOR:
                log.info("gave up on %s %s after a day", address.chain.value, address)
                self._drop(entry, now, hard=False)
            elif entry.next_check > now + SLOW_INTERVAL:
                entry.next_check = now  # the clock stepped back: do not wait out the step
        due = sorted(
            (e for e in self._entries.values() if e.next_check <= now and not self._paused(e)),
            key=lambda e: (not e.fast, e.next_check),
        )
        evm = [e for e in due if e.address.chain is Chain.ETHEREUM][:EVM_PER_TICK]
        solana = [e for e in due if e.address.chain is Chain.SOLANA][:SOLANA_PER_TICK]
        if chain is not None:
            evm = evm if chain is Chain.ETHEREUM else []
            solana = solana if chain is Chain.SOLANA else []
        await asyncio.gather(
            self._pass(self._look_evm(evm, now), now),
            self._pass(self._look_solana(solana, now), now),
        )

    async def _pass(
        self,
        look: Awaitable[list[tuple[PendingEntry, tuple[str, ...] | None]]],
        now: float,
    ) -> None:
        """Wait for one existence check, then fully validate every address that appeared."""
        appeared = await look
        await asyncio.gather(*(self._confirm(entry, networks, now) for entry, networks in appeared))

    async def _look_evm(
        self, entries: list[PendingEntry], now: float
    ) -> list[tuple[PendingEntry, tuple[str, ...] | None]]:
        """Ask every EVM network for code (and nonces) and return the entries that appeared.

        Each appeared entry comes with the networks that show code. Wallets (a nonce or an
        EIP-7702 delegation) are dropped; the rest are marked absent and rescheduled.
        """
        appeared: list[tuple[PendingEntry, tuple[str, ...] | None]] = []
        # Code an optional network reported late is confirmed now, without another look.
        hurried = [e for e in entries if e.late]
        for entry in hurried:
            where, entry.late = tuple(sorted(entry.late)), set()
            self._appeared(entry, where, now, appeared)
        entries = [e for e in entries if e not in hurried and e.address in self._entries]
        if not entries:
            return appeared
        unknown = [e for e in entries if not e.classified]
        calls = [("eth_getCode", [e.address.value, "latest"]) for e in entries]
        calls += [("eth_getTransactionCount", [e.address.value, "latest"]) for e in unknown]
        answers = await self._batches(calls, entries, now)
        per_network = [(name, got) for name, got in answers.items() if isinstance(got, list)]
        for index, entry in enumerate(entries):
            codes = [(name, network[index]) for name, network in per_network]
            if any(isinstance(code, str) and is_delegated_wallet(code) for _, code in codes):
                self._drop(entry, now, reason="an EIP-7702 wallet")
                continue
            deployed = tuple(name for name, code in codes if has_code(code))
            if deployed:
                self._appeared(entry, deployed, now, appeared)
                continue
            empty = [name for name, code in codes if isinstance(code, str)]
            # Only required networks decide "classified", so a failing optional one does not
            # keep the nonce calls going on every tick.
            answered = bool(empty) and self._required <= set(empty)
            if not entry.classified:
                at = len(entries) + unknown.index(entry)
                nonces = {name: network[at] for name, network in per_network}
                if any(_uint(nonce) for nonce in nonces.values()):
                    self._drop(entry, now, reason="a wallet (it has sent transactions)")
                    continue
                answered = answered and all(
                    isinstance(nonces.get(name), str) for name in self._required
                )
            self._absent(entry, empty, now, answered)
        return appeared

    async def _batches(
        self, calls: list[tuple[str, list[Any]]], entries: list[PendingEntry], sent_at: float
    ) -> dict[str, Any]:
        """Send ``calls`` to every EVM network and return each one's answer list or error.

        Optional networks get ``OPTIONAL_GRACE`` seconds more once the required ones have
        answered; a batch still running then finishes in the background, and what it says
        about ``entries`` still counts. A network whose last batch has not come back is
        skipped, so a hanging node gets one request at a time.
        """
        tasks: dict[asyncio.Future[list[Any]], str] = {}
        for name, client in self._evm:
            if name in self._in_flight:
                continue
            task = asyncio.ensure_future(client.call_batch(calls))
            self._in_flight.add(name)
            task.add_done_callback(lambda done, network=name: self._batch_done(network, done))
            tasks[task] = name
        answers: dict[str, Any] = {}
        pending = set(tasks)
        loop = asyncio.get_running_loop()
        grace_ends: float | None = None
        try:
            while pending:
                timeout = None
                if all(tasks[task] not in self._required for task in pending):
                    if grace_ends is None:
                        grace_ends = loop.time() + OPTIONAL_GRACE
                    timeout = max(grace_ends - loop.time(), 0.0)
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    for task in pending:  # too late for this tick, not for the next one
                        self._late_batches[task] = (entries, sent_at)
                    break
                for task in done:
                    answers[tasks[task]] = task.exception() or task.result()
        except BaseException:
            for task in pending:
                task.cancel()
            raise
        return answers

    def _batch_done(self, name: str, task: asyncio.Future[list[Any]]) -> None:
        """Free the network of a finished batch, record its health and use a late answer.

        Late code hurries the entry to confirmation; no code counts as an absence as of
        when the batch was sent.
        """
        self._in_flight.discard(name)
        late = self._late_batches.pop(task, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.debug("existence check on %s failed: %s", name, error)
        if self._health is not None:
            self._health(name, error)
        if late is None or error is not None:
            return
        entries, sent_at = late
        codes = task.result()
        for index, entry in enumerate(entries):
            code = codes[index] if index < len(codes) else None
            if not isinstance(code, str) or entry.address not in self._entries:
                continue
            if has_code(code):
                self.hurry(entry.address, name)
            elif name not in entry.absent_on or entry.absent_on[name] < sent_at:
                # Nothing there yet: what tells a deployment there from old news later.
                entry.absent_on[name] = sent_at
                self._dirty = True

    def hurry(self, address: Address, network: str) -> None:
        """Confirm ``address`` on ``network`` at the next tick, without another look.

        Used when an optional network reports code after its check (or its verdict) was over.
        """
        entry = self._entries.get(address)
        if entry is None:
            return
        entry.late.add(network)
        entry.next_check = min(entry.next_check, self._clock())

    async def _look_solana(
        self, entries: list[PendingEntry], now: float
    ) -> list[tuple[PendingEntry, tuple[str, ...] | None]]:
        """Ask Solana which addresses exist and return the entries that became a mint.

        Missing accounts are marked absent, and an account that is not a mint is dropped.
        When the call fails, every entry is rescheduled.
        """
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
            if self._health is not None:
                self._health(_SOLANA, exc)
            result = None
        else:
            if self._health is not None:
                self._health(_SOLANA, None)
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
        """Queue an entry that just appeared for a full validation on the networks ``where``.

        A turbo baseline that nobody saw absent there existed all along: it is old news and
        leaves the watch, and the regular monitor judges it.
        """
        if entry.baseline and not any(name in entry.absent_on for name in where):
            # Removed, not dropped: another turbo session may still offer it.
            log.info("%s %s: old news when turbo started", entry.address.chain.value, entry.address)
            self._remove(entry)
            return
        # Solana has one "network": None validates it the usual way.
        appeared.append((entry, None if where == (_SOLANA,) else where))

    def _absent(
        self, entry: PendingEntry, where: Iterable[str], now: float, answered: bool
    ) -> None:
        """Record that the entry is absent on the networks ``where`` and reschedule it.

        With ``answered`` the entry counts as classified, which ends its nonce checks.
        """
        for name in where:
            if name not in entry.absent_on:
                self._dirty = True  # what a restart needs to tell a deployment from old news
            entry.absent_on[name] = now
        if answered and not entry.classified:
            entry.classified = True
            self._dirty = True
        self._reschedule(entry, now)

    async def _confirm(
        self, entry: PendingEntry, networks: tuple[str, ...] | None, now: float
    ) -> None:
        """Fully validate an entry that just appeared and announce the tokens found.

        Without a token the entry stays watched if it may still become one, and is dropped
        otherwise.
        """
        async with self._confirming[entry.address.chain]:
            results = await self._validator.validate_many(
                [entry.address], negative_cache=False, networks=networks, urgent=True
            )
        own = results[0]
        tokens = [result for result in results if result.contract is not None]
        if tokens:
            # Announce even if the entry left meanwhile: the token is known now, no one else will.
            self._remove(entry)
            for result in tokens:
                contract = result.contract
                assert contract is not None
                if self._registry.claim(contract):
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
            self._remove(entry)
            self._tell(entry.sites, own.address)
            return
        if own.status is ValidationStatus.UNAVAILABLE or own.kind in MAY_APPEAR:
            self._reschedule(entry, now)
        else:
            self._drop(entry, now, reason=own.kind.value)

    def _timeline(
        self, entry: PendingEntry, networks: tuple[str, ...] | None, *, via: bool = False
    ) -> Timeline:
        """Return the alert's timeline: deployed after the last check that found nothing.

        Without such a check, or for a token found behind an LP pair (``via``), it only
        says since when the page showed the address.
        """
        found_at = self._clock()  # once the node answered: the tick may predate the deployment
        where = networks or (_SOLANA,)
        misses = [entry.absent_on[name] for name in where if name in entry.absent_on]
        if not misses or via:
            return Timeline(found_at, on_page_since=entry.first_seen)
        return Timeline(
            found_at, missing_at=max(misses), on_page_since=entry.first_seen, deployed=True
        )

    def _tell(self, sites: Iterable[str], address: Address) -> None:
        """Tell the turbo sessions of ``sites`` that ``address`` is a token."""
        for site in list(sites):
            for listener in list(self._listeners.get(site, ())):
                listener(address)

    def _join(self, address: Address, site_url: str, page_url: str, urgent: bool) -> bool:
        """Add the site to an address already watched; return False if it is not watched.

        When ``urgent``, the entry also switches to the fast pace.
        """
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
        """Start watching a new address and return its entry, or None if it is kept out.

        A recent hard drop (a wallet, a non-token) stays out; an address only given up on
        comes back when the offer is urgent. None also means there was no room.
        """
        now = self._clock()
        dropped = self._dropped.get(address)
        if dropped is not None:
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
        """Return True if one more entry fits; a fast one may push out the oldest slow one.

        When only the site is full, the oldest slow entry just loses this site.
        """
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
        """Remove ``site_url`` from an entry; an entry left without sites leaves the watch.

        If that site was the one named in the alert, another site and its page take over.
        """
        entry.sites.pop(site_url, None)
        if not entry.sites:
            self._remove(entry)
        elif entry.site_url == site_url:
            entry.site_url, entry.page_url = next(iter(entry.sites.items()))
        self._dirty = True

    def _reschedule(self, entry: PendingEntry, now: float) -> None:
        """Set the entry's next check by its pace, or to now when late code waits for it."""
        if entry.late:
            entry.next_check = now
            return
        turbo = self._turbo_interval() if entry.sites.keys() & self._turbo_sites else None
        entry.next_check = now + check_interval(entry, now, turbo)

    def _drop(
        self, entry: PendingEntry, now: float, *, reason: str = "", hard: bool = True
    ) -> None:
        """Stop watching an entry and keep it out for a week.

        A ``hard`` drop (a wallet, a non-token) stays out; one only given up on may come
        back when it is offered urgently.
        """
        if reason:
            log.info("stopped watching %s %s: %s", entry.address.chain.value, entry.address, reason)
        self._remove(entry)
        self._dropped.pop(entry.address, None)
        self._dropped[entry.address] = Dropped(now + REMEMBER_DROPPED, hard)
        self._trim_dropped()
        self._dirty = True

    def _trim_dropped(self) -> None:
        """Forget the oldest dropped addresses beyond ``DROPPED_LIMIT``.

        A forgotten wallet costs one more check, not a wrong alert.
        """
        excess = max(len(self._dropped) - DROPPED_LIMIT, 0)
        for address in list(islice(self._dropped, excess)):
            del self._dropped[address]

    def _remove(self, entry: PendingEntry) -> None:
        """Take the entry out of the watch, if it is still there."""
        if self._entries.pop(entry.address, None) is not None:
            self._dirty = True

    def _paused(self, entry: PendingEntry) -> bool:
        """Return True while every site showing the entry is paused and none is in turbo."""
        if entry.sites.keys() & self._turbo_sites:
            return False
        return all(self._is_paused(site) for site in entry.sites)

    def load(self) -> None:
        """Load the entries and dropped addresses of an earlier run; a broken file starts empty.

        After new EVM networks were added, the saved EVM entries become baselines (see
        :meth:`_forget_old_verdicts`).
        """
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            now = self._clock()
            # Version 1 saved no absences per network: its baselines would all be old news.
            old = int(data.get("version", 1)) < 2
            entries = [entry_from_dict(raw, forget_baseline=old) for raw in data.get("entries", [])]
            dropped = {
                address: Dropped(float(until), hard)
                for section, hard in (("dropped", True), ("expired", False))
                for key, until in data.get(section, {}).items()
                if (address := parse_address_key(key)) is not None and float(until) > now
            }
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            log.warning("pending watch file %s is unreadable (%s); starting empty", self._path, exc)
            return
        self._entries = {entry.address: entry for entry in entries if entry is not None}
        self._dropped = dict(sorted(dropped.items(), key=lambda item: item[1].until))
        self._trim_dropped()
        if self._new_networks:
            self._forget_old_verdicts()
        if self._entries:
            log.info("%d address(es) watched until they become a token", len(self._entries))

    def _forget_old_verdicts(self) -> None:
        """Turn the saved EVM entries into baselines after new EVM networks were added.

        They were never checked there, so code found there now is old news, not a
        deployment; the regular monitor announces such tokens the usual way. EVM addresses
        only given up on may come back.
        """
        evm = [entry for entry in self._entries.values() if entry.address.chain is Chain.ETHEREUM]
        for entry in evm:
            entry.baseline = True
            for name in self._stale_absences:
                # Seen absent there before the network was switched off: stale news.
                entry.absent_on.pop(name, None)
        self._dropped = {
            address: dropped
            for address, dropped in self._dropped.items()
            if dropped.hard or address.chain is not Chain.ETHEREUM
        }
        self._dirty = True
        log.info(
            "new EVM network(s) %s: %d watched EVM address(es) are baselines now",
            ", ".join(sorted(self._new_networks)),
            len(evm),
        )

    def save(self) -> None:
        """Write the watch to its file, forgetting expired drops first.

        A failed write is logged, and the loop tries again at its next tick.
        """
        now = self._clock()
        self._dropped = {a: d for a, d in self._dropped.items() if d.until > now}
        payload = {
            "version": _STORE_VERSION,
            "entries": [entry_to_dict(entry) for entry in self._entries.values()],
            # wallets and non-tokens; "expired" holds addresses only given up on
            "dropped": {address_key(a): d.until for a, d in self._dropped.items() if d.hard},
            "expired": {address_key(a): d.until for a, d in self._dropped.items() if not d.hard},
        }
        try:
            atomic_write_text(self._path, json.dumps(payload, ensure_ascii=False))
        except OSError as exc:
            log.warning("saving the pending watch failed: %s", exc)
            return
        self._dirty = False
        self._saved_at = now


def _uint(answer: Any) -> int:
    """Return a hex quantity from an RPC answer as an int, or 0 when it is not one."""
    try:
        return int(answer, 16) if isinstance(answer, str) else 0
    except ValueError:
        return 0
