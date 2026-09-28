"""One entry point for address checks: caching, merging of parallel checks, re-checks."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import Any

from scraper_bot.chains.evm.validator import EvmContractValidator
from scraper_bot.chains.rpc.health import FailingEndpoint, RpcUnavailable, StuckReporter
from scraper_bot.chains.solana.validator import SolanaContractValidator
from scraper_bot.chains.sui.validator import SuiCoinValidator
from scraper_bot.models.addresses import (
    Address,
    AddressKind,
    Chain,
    ChainVerdict,
    ValidationResult,
    ValidationStatus,
)
from scraper_bot.storage.address_cache import AddressCache

log = logging.getLogger(__name__)


_MAX_PAIRS = 10_000  # LP pairs remembered with the tokens they hold
_PROBLEM_AFTER = 300.0  # seconds an endpoint must fail before the user hears about it
_MAX_PARTIAL = 20_000  # entries kept in _partial, and in _retry_after
# A limit of their own, so new addresses never queue behind re-checks of partial rejections.
_RECHECKS_AT_ONCE = 2


@dataclass(frozen=True, slots=True)
class _Partial:
    """A rejection made without the answer of some optional networks."""

    due: float  # clock time when the missing networks are asked again
    missing: frozenset[str]
    kind: AddressKind  # what the networks that answered said


class ContractValidator:
    """Validates addresses once, remembers verdicts and merges concurrent checks.

    A rejection made without an optional network's answer is not cached as final: that
    network alone is asked again later.
    """

    def __init__(
        self,
        evm: EvmContractValidator,
        solana: SolanaContractValidator,
        cache: AddressCache,
        *,
        sui: SuiCoinValidator,
        max_concurrent: int,
        retry_delay: float,
        clock: Callable[[], float] = time.monotonic,
        on_stuck: StuckReporter | None = None,
    ) -> None:
        """Tie the chain validators to the address cache and hook into late EVM answers.

        At most ``max_concurrent`` regular checks run at once. An address without a verdict
        is asked again after ``retry_delay`` seconds. ``on_stuck`` hears of long outages.
        """
        self._evm = evm
        self._solana = solana
        self._sui = sui
        self._cache = cache
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._recheck_slots = asyncio.Semaphore(_RECHECKS_AT_ONCE)
        self._rechecks: set[asyncio.Task[None]] = set()
        self._retry_delay = retry_delay
        self._clock = clock
        self._on_stuck = on_stuck
        self._reported: set[str] = set()  # endpoints whose current outage was reported
        self._retry_after: dict[Address, float] = {}
        # Rejected without an optional network's answer: that network alone is asked again.
        self._partial: dict[Address, _Partial] = {}
        # Called with (address, network) when an optional network finds a token late.
        self.on_late_token: Callable[[Address, str], None] | None = None
        evm.on_late = self._late_answer
        self._inflight: dict[
            tuple[Address, frozenset[str] | None, bool], asyncio.Task[ValidationResult]
        ] = {}
        self._pairs: dict[Address, tuple[Address, ...]] = {}

    async def validate_many(
        self,
        addresses: Iterable[Address],
        *,
        negative_cache: bool = True,
        follow_pairs: bool = True,
        networks: Collection[str] | None = None,
        urgent: bool = False,
    ) -> list[ValidationResult]:
        """Check several addresses and return one result per address.

        Results come in the same order as the input. With ``follow_pairs`` the tokens held by
        any LP pair are checked too and added at the end with ``via`` set to the pair: a page
        often shows only a chart link, i.e. the pair's address. ``networks`` and ``urgent``
        apply only to the given addresses (see :meth:`validate`).
        """
        batch = list(addresses)
        results = await self._each(
            batch, negative_cache=negative_cache, networks=networks, urgent=urgent
        )
        if not follow_pairs:
            return results
        asked = set(batch)
        behind: dict[Address, Address] = {}
        for result in results:
            for token in result.resolved:
                if token not in asked:
                    behind.setdefault(token, result.address)
        if behind:
            extra = await self._each(
                list(behind), negative_cache=negative_cache, networks=None, urgent=urgent
            )
            results.extend(dataclasses.replace(r, via=behind[r.address]) for r in extra)
        return results

    async def _each(
        self,
        addresses: list[Address],
        *,
        negative_cache: bool,
        networks: Collection[str] | None,
        urgent: bool,
    ) -> list[ValidationResult]:
        """Validate the addresses together and return one result each, in input order.

        An unexpected error makes only its own address UNAVAILABLE; a cancellation (shutdown)
        is raised.
        """
        outcomes = await asyncio.gather(
            *(
                self.validate(a, negative_cache=negative_cache, networks=networks, urgent=urgent)
                for a in addresses
            ),
            return_exceptions=True,
        )
        results: list[ValidationResult] = []
        for address, outcome in zip(addresses, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                log.error(
                    "checking %s %s failed: %r",
                    address.chain.value,
                    address,
                    outcome,
                    exc_info=outcome,
                )
                outcome = ValidationResult(address, ValidationStatus.UNAVAILABLE)
            results.append(outcome)
        return results

    async def validate(
        self,
        address: Address,
        *,
        negative_cache: bool = True,
        networks: Collection[str] | None = None,
        urgent: bool = False,
    ) -> ValidationResult:
        """Return the verdict for ``address``, from the caches when they know it.

        With ``negative_cache=False`` (turbo, /unverified and the pending watch) earlier
        rejections and RPC failures are ignored and a new rejection is not remembered, so an
        address published before its contract is deployed can be re-checked freely.
        ``networks`` asks only those EVM networks; ``urgent`` skips the ``max_concurrent``
        queue (the pending watch confirming a deployment it just saw).
        """
        if self._cache.is_known(address):
            return ValidationResult(address, ValidationStatus.ALREADY_KNOWN)
        pair = self._pairs.get(address)
        if pair is not None:
            # The pair itself is cached as rejected; its tokens still need a verdict.
            return ValidationResult(
                address, ValidationStatus.ALREADY_REJECTED, kind=AddressKind.PAIR, resolved=pair
            )
        if negative_cache:
            if self._cache.is_rejected(address):
                return ValidationResult(address, ValidationStatus.ALREADY_REJECTED)
            not_before = self._retry_after.get(address)
            if not_before is not None:
                if self._clock() < not_before:
                    return ValidationResult(address, ValidationStatus.UNAVAILABLE)
                del self._retry_after[address]
            partial = self._partial.get(address)
            if partial is not None:
                if not self._evm.has_late(address.value):
                    if self._clock() >= partial.due:
                        self._recheck_later(address, partial)
                    return ValidationResult(
                        address, ValidationStatus.ALREADY_REJECTED, kind=partial.kind
                    )
                # An optional network found a token here late: the lookup below returns it.
                del self._partial[address]

        # Share only checks of the same kind: an urgent one must not queue behind a regular
        # one, and a check must not get a verdict from fewer networks than it asked.
        key = (address, None if networks is None else frozenset(networks), urgent)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._validate_uncached(address, negative_cache, networks, urgent)
            )
            self._inflight[key] = task
            task.add_done_callback(lambda _: self._inflight.pop(key, None))
        # shield: a cancelled caller must not cancel the check other callers wait for
        return await asyncio.shield(task)

    async def _validate_uncached(
        self,
        address: Address,
        negative_cache: bool,
        networks: Collection[str] | None,
        urgent: bool,
    ) -> ValidationResult:
        """Look the address up on its chain, log the verdict and remember a rejection.

        A token is not written to the known cache here: the alert's claim does that
        (AlertRegistry), so a caller cancelled meanwhile cannot hide the token.
        """
        async with contextlib.nullcontext() if urgent else self._semaphore:
            try:
                verdict = await self._lookup(address, networks)
            except RpcUnavailable as exc:
                # Routine per address (the validators log the endpoint failure), so INFO.
                log.info(
                    "%s %s: %s; retry in %.0fs",
                    address.chain.value,
                    address,
                    exc,
                    self._retry_delay,
                )
                if negative_cache:
                    self._retry_later(address)
                self._report_stuck_endpoints()
                return ValidationResult(address, ValidationStatus.UNAVAILABLE)
        # An optional network may have failed without holding up this verdict.
        self._report_stuck_endpoints()
        chain = address.chain.value
        self._partial.pop(address, None)
        if verdict.kind is AddressKind.TOKEN and verdict.network:
            log.info("%s %s confirmed as a token on %s", chain, address, verdict.network)
            return ValidationResult(
                address, ValidationStatus.CONFIRMED, verdict.network, kind=AddressKind.TOKEN
            )
        resolved = tuple(Address(address.chain, side) for side in verdict.sides)
        unanswered = " (an optional network did not answer)" if verdict.partial else ""
        if verdict.kind is AddressKind.PAIR:
            self._remember_pair(address, resolved)
            log.info(
                "%s %s is an LP pair on %s holding %s",
                chain,
                address,
                verdict.network,
                ", ".join(map(str, resolved)) or "only well-known tokens",
            )
        elif verdict.kind is AddressKind.STABLE_COPY:
            log.info(
                "%s %s is not a token: a copy of a stablecoin or wrapped coin (symbol %s) on %s%s",
                chain,
                address,
                verdict.symbol,
                verdict.network,
                unanswered,
            )
        else:
            log.info("%s %s is not a token (%s)%s", chain, address, verdict.kind.value, unanswered)
        # A decimals-0 mint with nothing minted may still become a token: never cache it.
        if negative_cache and verdict.kind is not AddressKind.NOT_YET_MINTED:
            if verdict.partial:
                # Not final without the optional network: ask it again later (at once when it
                # finds a token late) instead of hiding the address for rejected_ttl.
                self._set_partial(address, verdict.missing, verdict.kind)
            else:
                self._cache.add_rejected(address)
        return ValidationResult(
            address,
            ValidationStatus.REJECTED,
            verdict.network,
            kind=verdict.kind,
            resolved=resolved,
        )

    async def aclose(self) -> None:
        """Stop checks still running at shutdown, then the EVM background requests."""
        running: list[asyncio.Task[Any]] = [*self._inflight.values(), *self._rechecks]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await self._evm.aclose()

    def _recheck_later(self, address: Address, partial: _Partial) -> None:
        """Ask the networks missing from a rejection again, in the background.

        The page showing the address does not wait: a token found now is confirmed from
        this answer at the address's next check, and the pending watch hears of it at once.
        """
        # Moved on first, so checks of the address meanwhile start no second re-check.
        self._partial[address] = dataclasses.replace(partial, due=self._clock() + self._retry_delay)
        task = asyncio.create_task(self._recheck(address, partial))
        self._rechecks.add(task)
        task.add_done_callback(self._rechecks.discard)

    async def _recheck(self, address: Address, partial: _Partial) -> None:
        """Ask the missing networks again and update or settle the partial rejection.

        A token or pair found now is handled like a late answer. When the networks still do
        not answer, they are asked again after the retry delay.
        """
        async with self._recheck_slots:
            try:
                verdict = await self._evm.find_network(address.value, partial.missing)
            except RpcUnavailable:
                self._report_stuck_endpoints()
                return
            except Exception:
                log.exception("re-checking %s %s failed", address.chain.value, address)
                return
        self._report_stuck_endpoints()
        if verdict.kind in {AddressKind.TOKEN, AddressKind.PAIR} and verdict.network:
            self._evm.remember_late(address.value, verdict.network, verdict)
            self._late_answer(address.value, verdict.network, verdict)
        elif address in self._partial:
            if verdict.missing:
                self._set_partial(address, verdict.missing, partial.kind)
            else:
                self._settle_partial(address, partial.kind)

    def _settle_partial(self, address: Address, kind: AddressKind) -> None:
        """Every network has answered: turn the partial rejection into a cached one."""
        self._partial.pop(address, None)
        if kind is not AddressKind.NOT_YET_MINTED and not self._cache.is_known(address):
            self._cache.add_rejected(address)

    def _retry_later(self, address: Address) -> None:
        """Answer UNAVAILABLE for ``address`` until the retry delay has passed.

        The memory is bounded: expired entries go first, then the oldest one.
        """
        if len(self._retry_after) >= _MAX_PARTIAL:
            now = self._clock()
            self._retry_after = {a: t for a, t in self._retry_after.items() if t > now}
            if len(self._retry_after) >= _MAX_PARTIAL:
                del self._retry_after[next(iter(self._retry_after))]
        self._retry_after[address] = self._clock() + self._retry_delay

    def _set_partial(self, address: Address, missing: frozenset[str], kind: AddressKind) -> None:
        """Remember a rejection made without the ``missing`` networks, due after the delay."""
        self._partial.pop(address, None)
        if len(self._partial) >= _MAX_PARTIAL:
            del self._partial[next(iter(self._partial))]
        self._partial[address] = _Partial(self._clock() + self._retry_delay, missing, kind)

    def _late_answer(self, value: str, network: str, verdict: ChainVerdict) -> None:
        """Handle an answer an optional network gave after its lookup had finished.

        A token or pair makes the address due at once (the next lookup answers from it) and
        is reported to ``on_late_token``; "nothing here" completes a rejection made without
        that network.
        """
        address = Address(Chain.ETHEREUM, value)
        partial = self._partial.get(address)
        if verdict.kind in {AddressKind.TOKEN, AddressKind.PAIR}:
            if partial is not None:
                self._partial[address] = dataclasses.replace(partial, due=self._clock())
            self._retry_after.pop(address, None)
            if self.on_late_token is not None:
                self.on_late_token(address, network)
            return
        if partial is None or network not in partial.missing:
            return
        missing = partial.missing - {network}
        if missing:
            self._partial[address] = dataclasses.replace(partial, missing=missing)
        else:
            self._settle_partial(address, partial.kind)

    def record(self, name: str, error: BaseException | None) -> None:
        """Record an answer (``error`` is None) or a failure of endpoint ``name`` seen elsewhere.

        The warm-up and the pending watch call every endpoint all the time, so an endpoint
        that dies between launches is noticed before the next address needs it.
        """
        if name == "Solana":
            health = self._solana.health
        elif name == "Sui":
            health = self._sui.health
        else:
            health = self._evm.health
        if error is None:
            health.answered(name)
        else:
            health.failed(name, error)
        self._report_stuck_endpoints()

    def failing_endpoints(self, seconds: float = 0.0) -> list[FailingEndpoint]:
        """Return every endpoint that has been failing for ``seconds`` or more."""
        return [
            *self._evm.health.failing(seconds),
            *self._solana.health.failing(seconds),
            *self._sui.health.failing(seconds),
        ]

    def _report_stuck_endpoints(self) -> None:
        """Send one 🛑 report per outage, when an endpoint joins those silent for minutes.

        The report lists every endpoint silent that long. An endpoint that answers again ends
        its outage, so its next one is reported too; brief hiccups stay in the log. The
        reporter sends in the background, so this costs a verdict nothing.
        """
        if self._on_stuck is None:
            return
        self._reported &= (
            self._evm.health.down() | self._solana.health.down() | (self._sui.health.down())
        )
        stuck = self.failing_endpoints(_PROBLEM_AFTER)
        new = {endpoint.name for endpoint in stuck} - self._reported
        if new:
            self._reported |= new
            self._on_stuck(stuck, self._retry_delay)

    def _remember_pair(self, pair: Address, tokens: tuple[Address, ...]) -> None:
        """Remember the tokens an LP pair holds; beyond ``_MAX_PAIRS`` the oldest pair goes."""
        if len(self._pairs) >= _MAX_PAIRS:
            del self._pairs[next(iter(self._pairs))]
        self._pairs[pair] = tokens

    async def _lookup(self, address: Address, networks: Collection[str] | None) -> ChainVerdict:
        """Ask the validator of the address's chain and return its verdict.

        Solana and Sui name their network only for a token or a copy of a stablecoin.
        """
        if address.chain is Chain.ETHEREUM:
            return await self._evm.find_network(address.value, networks)
        if address.chain is Chain.SOLANA:
            return await self._solana.inspect(address.value)
        return await self._sui.inspect(address.value)
