"""On-chain validation of candidate addresses (EVM chains, Solana and Sui)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from scraper_bot.chains.address_extractor import is_sui_coin_type, sui_package, to_checksum_address
from scraper_bot.chains.evm import (
    TOKEN_PROBE,
    has_code,
    is_delegated_wallet,
    word_address,
    word_uint,
)
from scraper_bot.chains.rpc import JsonRpcClient, RetryPolicy, RpcCallError, RpcError
from scraper_bot.chains.solana import account_data, is_mint, parse_mint
from scraper_bot.chains.stoplist import StopList
from scraper_bot.chains.sui import coin_metadata, find_coin, object_kind
from scraper_bot.config import EvmNetwork
from scraper_bot.models import (
    Address,
    AddressKind,
    Chain,
    ValidationResult,
    ValidationStatus,
)
from scraper_bot.preferences import Tuning
from scraper_bot.storage import AddressCache

log = logging.getLogger(__name__)

# JSON-RPC errors that mean the EVM ran the call and it failed: a real "not a token".
_REVERT_MARKERS = ("revert", "invalid opcode", "out of gas", "vm execution")
_MAX_DECIMALS = 255  # decimals() is a uint8
_MAX_PAIRS = 10_000  # LP pairs remembered with the tokens they hold
# Lookups that answered early leave the slower networks running in the background; above
# this many such requests a lookup waits for all its networks, which bounds the fan-out.
_MAX_BACKGROUND = 64
# Once every required network has answered, the optional ones get this long (seconds) to
# answer too; a slow or hanging one then finishes in the background (they answer in about
# 50 ms when healthy).
OPTIONAL_GRACE = 0.5
# An endpoint must fail this long (seconds) before the user hears about it.
_PROBLEM_AFTER = 300.0
# ...and have failed again this recently: an endpoint nobody asked for an hour is not down.
_RECENT_FAILURE = 60.0
# Failures further apart than this are separate outages (RPC_RETRY_DELAY may space the
# checks of a stuck address minutes apart; that is still one outage).
_NEW_OUTAGE_AFTER = 1800.0
# An optional endpoint failing this many times in a row is left alone (no waiting on it),
# but for probes: the first right away, then after 2 s, the gap doubling up to 30 s. A
# probe's answer ends the rest.
_REST_AFTER = 2
_FIRST_PROBE_GAP = 2.0
_PROBE_EVERY = 30.0
_MAX_LATE = 1000  # tokens an optional network found after the verdict, kept for the next look
_MAX_PARTIAL = 20_000  # addresses remembered as rejected without an optional network's answer
# Re-checks of such rejections run in the background, this many at once: the page that
# shows the address never waits for them, and new addresses never queue behind them.
_RECHECKS_AT_ONCE = 2


class RpcUnavailable(Exception):
    """No verdict is possible: an endpoint failed."""


@dataclass(frozen=True, slots=True)
class FailingEndpoint:
    """An RPC endpoint that has stopped answering."""

    name: str
    seconds: float  # failing for this long
    error: str  # its latest failure
    optional: bool = False  # its failure holds up no verdict (see EvmNetwork.optional)


# Called with the endpoints failing for minutes and RPC_RETRY_DELAY; the Notifier sends a 🛑.
StuckReporter = Callable[[Sequence[FailingEndpoint], float], None]


class EndpointHealth:
    """Since when each RPC endpoint has been failing; its next answer clears the entry."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, optional: Collection[str] = ()
    ) -> None:
        self._clock = clock
        self._optional = frozenset(optional)
        self._since: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._errors: dict[str, str] = {}
        self._streak: dict[str, int] = {}  # failures in a row
        self._probe_at: dict[str, float] = {}
        self._probe_gap: dict[str, float] = {}

    def failed(self, name: str, error: BaseException) -> None:
        now = self._clock()
        last = self._last.get(name)
        self._last[name] = now
        self._errors[name] = str(error)
        self._streak[name] = self._streak.get(name, 0) + 1
        # Two hiccups an hour apart are two short outages, not one long one.
        if name not in self._since or (last is not None and now - last > _NEW_OUTAGE_AFTER):
            self._since[name] = now
            log.warning("RPC endpoint %s is failing: %s", name, error)

    def answered(self, name: str) -> None:
        self._last.pop(name, None)
        self._errors.pop(name, None)
        self._streak.pop(name, None)
        self._probe_at.pop(name, None)
        self._probe_gap.pop(name, None)
        if self._since.pop(name, None) is not None:
            log.info("RPC endpoint %s recovered", name)

    def resting(self, name: str) -> bool:
        """True while an optional endpoint that keeps failing is not asked at all.

        Probes still go through (this call lets one pass): at once, then 2 s later, the gap
        doubling up to ``_PROBE_EVERY``, so a short hiccup costs seconds, not half a minute.
        A probe's answer ends the rest. Required endpoints are always asked.
        """
        if name not in self._optional or self._streak.get(name, 0) < _REST_AFTER:
            return False
        now = self._clock()
        probed = self._probe_at.get(name)
        gap = self._probe_gap.get(name, _FIRST_PROBE_GAP)
        if probed is None or now - probed >= gap:
            if probed is not None:
                self._probe_gap[name] = min(gap * 2, _PROBE_EVERY)
            self._probe_at[name] = now
            return False
        return True

    def down(self) -> set[str]:
        """Endpoints in an outage: failed since their last answer."""
        return set(self._since)

    def failing(self, seconds: float = 0.0) -> list[FailingEndpoint]:
        """Endpoints down for ``seconds`` or more that also failed lately."""
        now = self._clock()
        return [
            FailingEndpoint(name, now - since, self._errors[name], name in self._optional)
            for name, since in self._since.items()
            if now - since >= seconds and now - self._last[name] <= _RECENT_FAILURE
        ]


@dataclass(frozen=True, slots=True)
class ChainVerdict:
    """What one chain says about an address."""

    kind: AddressKind
    network: str | None = None  # where the token (or the LP pair) lives
    sides: tuple[str, ...] = ()  # PAIR: the tokens it holds, stop-listed ones left out
    # optional networks that did not answer (failed, too slow, resting): it may change there
    missing: frozenset[str] = frozenset()

    @property
    def partial(self) -> bool:
        return bool(self.missing)


class EvmContractValidator:
    """Looks for bytecode on every configured network at once and classifies it.

    With ``require_erc20`` a contract counts only when it is a plain token: it answers
    totalSupply() and decimals(), and is neither an LP pair nor an ERC-4626 vault.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        networks: Sequence[EvmNetwork],
        *,
        timeout: float,
        tuning: Tuning,
        stoplist: StopList | None = None,
        retry: RetryPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clients = [
            (net.name, JsonRpcClient(http, net.rpc_url, timeout, retry=retry)) for net in networks
        ]
        self._optional = frozenset(net.name for net in networks if net.optional)
        self._tuning = tuning
        self._stoplist = stoplist
        self.health = EndpointHealth(clock, optional=self._optional)
        self._background: set[asyncio.Task[ChainVerdict]] = set()
        # A token or pair an optional network found after the verdict was given: the next
        # lookup of that address answers with it at once.
        self._late: dict[tuple[str, str], ChainVerdict] = {}
        # Told every answer an optional network gave after its lookup: (address, network,
        # verdict); a verdict made without it can then be completed.
        self.on_late: Callable[[str, str, ChainVerdict], None] | None = None

    async def find_network(
        self, address: str, networks: Collection[str] | None = None
    ) -> ChainVerdict:
        """The token's network, an LP pair's tokens, or what else the address is.

        ``networks`` limits the lookup to those networks (the pending watch knows where
        the bytecode appeared).

        The first network that finds a token or an LP pair answers at once and the slower
        ones finish in the background; with ``_MAX_BACKGROUND`` requests already there, the
        lookup waits for every network instead. Any other verdict needs the answer of every
        network that is not optional: :class:`RpcUnavailable` is raised when one of those
        failed, because a rejection based on partial information would be cached for hours.
        An optional network gets ``OPTIONAL_GRACE`` seconds after the others; one that
        failed or is still silent then is left out of the verdict, which is marked
        ``partial`` (unless no network answered at all: then there is no verdict).
        """
        asked = [(n, c) for n, c in self._clients if networks is None or n in networks]
        for name, _ in asked:
            late = self._late.pop((address, name), None)
            if late is not None:
                return dataclasses.replace(late, network=name)
        # An optional endpoint that keeps failing is not waited for: it is left out at once.
        resting = [name for name, _ in asked if self.health.resting(name)]
        clients = [(n, c) for n, c in asked if n not in resting]
        order = {name: index for index, (name, _) in enumerate(clients)}
        tasks = {
            asyncio.ensure_future(self._check(client, address)): name for name, client in clients
        }
        pending = set(tasks)
        failed: list[str] = list(resting)
        verdicts: list[tuple[str, ChainVerdict]] = []
        loop = asyncio.get_running_loop()
        grace_ends: float | None = None
        try:
            while pending:
                timeout = None
                # Only optional networks are left: wait for them a moment, not the whole
                # RPC timeout (while the background has room for their requests).
                only_optional = all(tasks[task] in self._optional for task in pending)
                if only_optional and verdicts and len(self._background) < _MAX_BACKGROUND:
                    if grace_ends is None:
                        grace_ends = loop.time() + OPTIONAL_GRACE
                    timeout = max(grace_ends - loop.time(), 0.0)
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    failed.extend(tasks[task] for task in pending)  # still silent: left out
                    self._leave_running(pending, tasks, address)
                    pending = set()
                    break
                ready = sorted(done, key=lambda t: order[tasks[t]])
                for position, task in enumerate(ready):
                    name = tasks[task]
                    verdict = self._settle(name, task)
                    if verdict is None:
                        failed.append(name)
                        continue
                    found = verdict.kind in {AddressKind.TOKEN, AddressKind.PAIR}
                    if found and len(self._background) < _MAX_BACKGROUND:
                        for other in ready[position + 1 :]:  # finished in the same step
                            self._finished(tasks[other], address, other, late=False)
                        self._leave_running(pending, tasks, address)
                        return dataclasses.replace(verdict, network=name)
                    verdicts.append((name, verdict))
        except BaseException:
            for task in pending:
                task.cancel()
            for task in tasks:  # mark the other failures as seen, so asyncio does not warn
                if task.done() and not task.cancelled():
                    task.exception()
            raise
        for wanted in (AddressKind.TOKEN, AddressKind.PAIR):
            for name, verdict in verdicts:
                if verdict.kind is wanted:
                    return dataclasses.replace(verdict, network=name)
        if any(name not in self._optional for name in failed) or (failed and not verdicts):
            raise RpcUnavailable(f"no answer from {', '.join(failed)}")
        missing = frozenset(failed)  # only optional networks are missing
        kinds = {verdict.kind for _, verdict in verdicts}
        for kind in (AddressKind.WALLET, AddressKind.VAULT, AddressKind.NOT_TOKEN):
            if kind in kinds:
                return ChainVerdict(kind, missing=missing)
        return ChainVerdict(AddressKind.NO_CODE, missing=missing)

    async def aclose(self) -> None:
        """Stop the requests still running for lookups that already answered."""
        leftovers = list(self._background)
        for task in leftovers:
            task.cancel()
        await asyncio.gather(*leftovers, return_exceptions=True)

    def _settle(self, name: str, task: asyncio.Future[ChainVerdict]) -> ChainVerdict | None:
        """The network's verdict, or ``None`` when its endpoint failed (health updated)."""
        error = task.exception()
        if error is None:
            self.health.answered(name)
            return task.result()
        if isinstance(error, RpcError):
            self.health.failed(name, error)
            return None
        raise error

    def _leave_running(
        self,
        pending: set[asyncio.Future[ChainVerdict]],
        names: dict[asyncio.Future[ChainVerdict], str],
        address: str,
    ) -> None:
        # Cancelling an HTTP request mid-flight would drop a warm connection; let it finish.
        for task in pending:
            self._background.add(task)  # type: ignore[arg-type]
            task.add_done_callback(
                lambda done, name=names[task]: self._finished(name, address, done)
            )

    def has_late(self, address: str) -> bool:
        """True when an optional network reported a token or pair at ``address`` late."""
        return any(key[0] == address for key in self._late)

    def remember_late(self, address: str, network: str, verdict: ChainVerdict) -> None:
        """The next lookup of ``address`` answers with ``verdict`` at once."""
        self._late.pop((address, network), None)
        if len(self._late) >= _MAX_LATE:
            del self._late[next(iter(self._late))]
        self._late[address, network] = verdict

    def _finished(
        self, name: str, address: str, task: asyncio.Future[ChainVerdict], *, late: bool = True
    ) -> None:
        """A request left running ended; ``late``: after its lookup had answered."""
        self._background.discard(task)  # type: ignore[arg-type]
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            self.health.answered(name)
            if not late or name not in self._optional:
                return
            verdict = task.result()
            if verdict.kind in {AddressKind.TOKEN, AddressKind.PAIR}:
                # Too late for the verdict it was asked for: the next look answers with it.
                self.remember_late(address, name, verdict)
                log.info("%s answered late for %s: %s", name, address, verdict.kind.value)
            if self.on_late is not None:
                self.on_late(address, name, verdict)
        elif isinstance(error, RpcError):
            self.health.failed(name, error)
        else:
            log.debug("background check on %s failed: %r", name, error)

    async def _check(self, client: JsonRpcClient, address: str) -> ChainVerdict:
        code = await client.call("eth_getCode", [address, "latest"])
        if not isinstance(code, str) or not has_code(code):
            return ChainVerdict(AddressKind.NO_CODE)
        if is_delegated_wallet(code):
            return ChainVerdict(AddressKind.WALLET)
        if not self._tuning.require_erc20:
            return ChainVerdict(AddressKind.TOKEN)  # lenient mode: any bytecode counts
        return await self._classify(client, address)

    async def _classify(self, client: JsonRpcClient, address: str) -> ChainVerdict:
        """Token, LP pair, vault or some other contract, from five read-only calls.

        Empty results and EVM reverts are answers ("no such function"). A rate limit or
        timeout is not: it is re-raised so the network counts as failed, instead of a
        real token being rejected and cached for hours.

        eth_getCode is repeated in the same batch, i.e. on the same node (a node without
        batches is asked about one block number): behind a load balancer the first answer
        may come from a node a block ahead, and on a node without the fresh contract every
        call answers "0x", which reads as "not a token".
        """
        answers = await client.call_batch(
            [
                ("eth_getCode", [address, "latest"]),
                *(
                    ("eth_call", [{"to": address, "data": selector}, "latest"])
                    for selector in TOKEN_PROBE
                ),
            ],
            same_block=True,
        )
        for answer in answers:
            if isinstance(answer, BaseException) and not _is_revert(answer):
                raise answer
        code, supply, decimals, token0, token1, asset = answers
        if not has_code(code):
            return ChainVerdict(AddressKind.NO_CODE)  # that node is behind: watch it, never reject
        first, second = word_address(token0), word_address(token1)
        # Pairs and pools of every common DEX keep their two tokens sorted by address.
        if first and second and first < second:
            sides = tuple(
                to_checksum_address(side) for side in (first, second) if not self._quote(side)
            )
            return ChainVerdict(AddressKind.PAIR, sides=sides)
        if word_address(asset):
            return ChainVerdict(AddressKind.VAULT)
        places = word_uint(decimals)
        if word_uint(supply) is not None and places is not None and places <= _MAX_DECIMALS:
            return ChainVerdict(AddressKind.TOKEN)
        return ChainVerdict(AddressKind.NOT_TOKEN)

    def _quote(self, address: str) -> bool:
        """A stop-listed side of a pair (WETH, USDC, ...) is never the launch."""
        return self._stoplist is not None and self._stoplist.contains_evm(address)


def _is_revert(error: BaseException) -> bool:
    if not isinstance(error, RpcCallError):
        return False
    lowered = error.message.lower()
    return error.code == 3 or any(marker in lowered for marker in _REVERT_MARKERS)


class SolanaContractValidator:
    """Accepts SPL / Token-2022 mints; programs, wallets and NFTs are not tokens.

    Reads at ``confirmed`` commitment: a node's default (``finalized``) trails the chain
    by about 13 s, long enough to reject a mint a launch page shows right after creation.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        rpc_url: str,
        *,
        timeout: float,
        retry: RetryPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = JsonRpcClient(http, rpc_url, timeout, retry=retry)
        self.health = EndpointHealth(clock)

    async def inspect(self, address: str) -> AddressKind:
        params = {"encoding": "base64", "commitment": "confirmed"}
        try:
            result = await self._client.call("getAccountInfo", [address, params])
        except RpcError as exc:
            self.health.failed("Solana", exc)
            raise RpcUnavailable(str(exc)) from exc
        self.health.answered("Solana")
        account = result.get("value") if isinstance(result, dict) else None
        if not isinstance(account, dict):
            return AddressKind.MISSING
        data = account_data(result)
        parsed = parse_mint(data)
        if parsed is None or not is_mint(account.get("owner"), data):
            return AddressKind.NOT_TOKEN
        supply, decimals = parsed
        if decimals == 0 and supply == 1:
            return AddressKind.NFT
        if decimals == 0 and supply == 0:
            return AddressKind.NOT_YET_MINTED  # an NFT or a token between creation and minting
        return AddressKind.TOKEN


class SuiCoinValidator:
    """Accepts coin types that have metadata and packages that define such a coin."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        rpc_url: str,
        *,
        timeout: float,
        tuning: Tuning,
        retry: RetryPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = JsonRpcClient(http, rpc_url, timeout, retry=retry)
        self._tuning = tuning
        self.health = EndpointHealth(clock)

    async def is_coin_or_package(self, value: str) -> bool:
        try:
            verdict = await self._is_coin_or_package(value)
        except RpcError as exc:
            self.health.failed("Sui", exc)
            raise RpcUnavailable(str(exc)) from exc
        self.health.answered("Sui")
        return verdict

    async def _is_coin_or_package(self, value: str) -> bool:
        if is_sui_coin_type(value):
            if await coin_metadata(self._client, value) is not None:
                return True
            if self._tuning.require_erc20:
                return False
            # Lenient mode counts any type of a real package, like any EVM contract.
            return await object_kind(self._client, sui_package(value)) == "package"
        if await object_kind(self._client, value) != "package":
            return False  # a wallet, an owned object or nothing at all
        if not self._tuning.require_erc20:
            return True
        return await find_coin(self._client, value) is not None


@dataclass(frozen=True, slots=True)
class _Partial:
    """A rejection made without the answer of some optional networks."""

    due: float  # when those networks are asked again
    missing: frozenset[str]
    kind: AddressKind  # what the networks that answered said


class ContractValidator:
    """Validates addresses once, remembers verdicts and merges concurrent checks."""

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
        # Told when an optional network found a token too late for its verdict (the pending
        # watch confirms it at once instead of at its next look).
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
        """Verdicts in input order, then (``follow_pairs``) the tokens behind LP pairs.

        A page often shows only a chart link, i.e. the pair's address; its tokens are
        checked like any other candidate and come back with ``via`` set to the pair.
        ``networks`` and ``urgent`` apply to the given addresses (see :meth:`validate`).
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
        """One verdict per address; an unexpected error costs only its own address."""
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
                raise outcome  # the checks were stopped (shutdown)
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
        """Verdict for ``address``.

        With ``negative_cache=False`` (turbo, /unverified and the pending watch) earlier
        rejections and RPC failures are ignored and a new rejection is not remembered, so
        an address published before its contract is deployed can be re-checked freely.
        ``networks`` asks only those EVM networks; ``urgent`` skips the queue of
        ``max_concurrent`` checks (the pending watch confirming a deployment it just saw).
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
                # An optional network found a token here after the verdict: the lookup
                # below answers from what it said, at once.
                del self._partial[address]

        # Only checks of the same kind are shared: an urgent one must not wait in the
        # queue behind a regular one, and a page check must not get a verdict from fewer
        # networks than it asked.
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
        async with contextlib.nullcontext() if urgent else self._semaphore:
            try:
                verdict = await self._lookup(address, networks)
            except RpcUnavailable as exc:
                # Endpoint-level failures are logged once by the validators; per address
                # this is routine, so keep it at INFO.
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
            # Not written to the known cache here: the alert's claim does that
            # (AlertRegistry), so a caller cancelled meanwhile cannot hide the token.
            log.info("%s %s confirmed as a token on %s", chain, address, verdict.network)
            return ValidationResult(
                address, ValidationStatus.CONFIRMED, verdict.network, kind=AddressKind.TOKEN
            )
        resolved = tuple(Address(address.chain, side) for side in verdict.sides)
        if verdict.kind is AddressKind.PAIR:
            self._remember_pair(address, resolved)
            log.info(
                "%s %s is an LP pair on %s holding %s",
                chain,
                address,
                verdict.network,
                ", ".join(map(str, resolved)) or "only well-known tokens",
            )
        else:
            unanswered = " (an optional network did not answer)" if verdict.partial else ""
            log.info("%s %s is not a token (%s)%s", chain, address, verdict.kind.value, unanswered)
        # A decimals-0 mint with nothing minted may still become a token: never cache it.
        if negative_cache and verdict.kind is not AddressKind.NOT_YET_MINTED:
            if verdict.partial:
                # Without the optional network's answer the rejection is not final: that
                # network alone is asked again after the retry delay (and at once when it
                # finds a token late), instead of hiding the address for rejected_ttl.
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
        # Moved on first, so the checks of the address meanwhile start no second one.
        self._partial[address] = dataclasses.replace(partial, due=self._clock() + self._retry_delay)
        task = asyncio.create_task(self._recheck(address, partial))
        self._rechecks.add(task)
        task.add_done_callback(self._rechecks.discard)

    async def _recheck(self, address: Address, partial: _Partial) -> None:
        async with self._recheck_slots:
            try:
                verdict = await self._evm.find_network(address.value, partial.missing)
            except RpcUnavailable:
                self._report_stuck_endpoints()
                return  # still silent (or resting): asked again after the retry delay
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
        """Every network has answered: a plain rejection now."""
        self._partial.pop(address, None)
        if kind is not AddressKind.NOT_YET_MINTED and not self._cache.is_known(address):
            self._cache.add_rejected(address)

    def _retry_later(self, address: Address) -> None:
        """No verdict now: ask again after the retry delay (a bounded memory of such)."""
        if len(self._retry_after) >= _MAX_PARTIAL:
            now = self._clock()
            self._retry_after = {a: t for a, t in self._retry_after.items() if t > now}
            if len(self._retry_after) >= _MAX_PARTIAL:
                del self._retry_after[next(iter(self._retry_after))]
        self._retry_after[address] = self._clock() + self._retry_delay

    def _set_partial(self, address: Address, missing: frozenset[str], kind: AddressKind) -> None:
        self._partial.pop(address, None)
        if len(self._partial) >= _MAX_PARTIAL:
            del self._partial[next(iter(self._partial))]
        self._partial[address] = _Partial(self._clock() + self._retry_delay, missing, kind)

    def _late_answer(self, value: str, network: str, verdict: ChainVerdict) -> None:
        """An optional network answered after its lookup.

        A token there is looked at again at once (the next look answers from what it said);
        "nothing here" completes a rejection that was made without it.
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
        """An answer (``error`` None) or a failure of endpoint ``name`` seen elsewhere.

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
            health.answered(name)  # an outage ends: the next one is reported again
        else:
            health.failed(name, error)
        self._report_stuck_endpoints()

    def failing_endpoints(self, seconds: float = 0.0) -> list[FailingEndpoint]:
        """Every endpoint failing for ``seconds`` or more right now."""
        return [
            *self._evm.health.failing(seconds),
            *self._solana.health.failing(seconds),
            *self._sui.health.failing(seconds),
        ]

    def _report_stuck_endpoints(self) -> None:
        """One 🛑 per outage: when an endpoint joins those silent for minutes.

        The report lists every endpoint silent for minutes then; an endpoint that answers
        again ends its outage, so its next one is reported too. Brief hiccups stay in the
        log. The reporter sends in the background, so this costs a verdict nothing.
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
        if len(self._pairs) >= _MAX_PAIRS:
            del self._pairs[next(iter(self._pairs))]
        self._pairs[pair] = tokens

    async def _lookup(self, address: Address, networks: Collection[str] | None) -> ChainVerdict:
        if address.chain is Chain.ETHEREUM:
            return await self._evm.find_network(address.value, networks)
        if address.chain is Chain.SOLANA:
            kind = await self._solana.inspect(address.value)
            return ChainVerdict(kind, "Solana" if kind is AddressKind.TOKEN else None)
        if await self._sui.is_coin_or_package(address.value):
            return ChainVerdict(AddressKind.TOKEN, "Sui")
        return ChainVerdict(AddressKind.NOT_TOKEN)
