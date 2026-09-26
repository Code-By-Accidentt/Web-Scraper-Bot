"""On-chain validation of candidate addresses (EVM chains, Solana and Sui)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass

import httpx

from scraper_bot.chains.address_extractor import is_sui_coin_type, sui_package, to_checksum_address
from scraper_bot.chains.evm import TOKEN_PROBE, is_delegated_wallet, word_address, word_uint
from scraper_bot.chains.rpc import JsonRpcClient, RetryPolicy, RpcCallError, RpcError
from scraper_bot.chains.solana import account_data, is_mint
from scraper_bot.chains.stoplist import StopList
from scraper_bot.chains.sui import coin_metadata, find_coin, object_kind
from scraper_bot.config import EvmNetwork
from scraper_bot.models import (
    Address,
    AddressKind,
    Chain,
    ConfirmedContract,
    ValidationResult,
    ValidationStatus,
)
from scraper_bot.preferences import Tuning
from scraper_bot.storage import AddressCache
from scraper_bot.telegram.notifier import Notifier
from scraper_bot.telegram.timefmt import format_duration

log = logging.getLogger(__name__)

_EMPTY_CODE = frozenset({"", "0x", "0x0"})
# JSON-RPC errors that mean the EVM ran the call and it failed: a real "not a token".
_REVERT_MARKERS = ("revert", "invalid opcode", "out of gas", "vm execution")
_MAX_DECIMALS = 255  # decimals() is a uint8
_MAX_PAIRS = 10_000  # LP pairs remembered with the tokens they hold
# Lookups that answered early leave the slower networks running in the background; above
# this many such requests a lookup waits for all its networks, which bounds the fan-out.
_MAX_BACKGROUND = 64
# An endpoint must fail this long (seconds) before the user hears about it.
_PROBLEM_AFTER = 300.0
# ...and have failed again this recently: an endpoint nobody asked for an hour is not down.
_RECENT_FAILURE = 60.0
# Failures further apart than this are separate outages (RPC_RETRY_DELAY may space the
# checks of a stuck address minutes apart; that is still one outage).
_NEW_OUTAGE_AFTER = 1800.0


class RpcUnavailable(Exception):
    """No verdict is possible: at least one endpoint failed."""


class EndpointHealth:
    """Since when each RPC endpoint has been failing; its next answer clears the entry."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._since: dict[str, float] = {}
        self._last: dict[str, float] = {}

    def failed(self, name: str, error: BaseException) -> None:
        now = self._clock()
        last = self._last.get(name)
        self._last[name] = now
        # Two hiccups an hour apart are two short outages, not one long one.
        if name not in self._since or (last is not None and now - last > _NEW_OUTAGE_AFTER):
            self._since[name] = now
            log.warning("RPC endpoint %s is failing: %s", name, error)

    def answered(self, name: str) -> None:
        self._last.pop(name, None)
        if self._since.pop(name, None) is not None:
            log.info("RPC endpoint %s recovered", name)

    def failing(self, seconds: float = 0.0) -> list[tuple[str, float]]:
        """(name, seconds failing) of endpoints down for ``seconds`` or more that failed lately."""
        now = self._clock()
        return [
            (name, now - since)
            for name, since in self._since.items()
            if now - since >= seconds and now - self._last[name] <= _RECENT_FAILURE
        ]

    def failing_for(self, seconds: float) -> list[str]:
        return [name for name, _ in self.failing(seconds)]


@dataclass(frozen=True, slots=True)
class ChainVerdict:
    """What one chain says about an address."""

    kind: AddressKind
    network: str | None = None  # where the token (or the LP pair) lives
    sides: tuple[str, ...] = ()  # PAIR: the tokens it holds, stop-listed ones left out


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
        self._tuning = tuning
        self._stoplist = stoplist
        self.health = EndpointHealth(clock)
        self._background: set[asyncio.Task[ChainVerdict]] = set()

    async def find_network(
        self, address: str, networks: Collection[str] | None = None
    ) -> ChainVerdict:
        """The token's network, an LP pair's tokens, or what else the address is.

        ``networks`` limits the lookup to those networks (the pending watch knows where
        the bytecode appeared).

        The first network that finds a token or an LP pair answers at once and the slower
        ones finish in the background; with ``_MAX_BACKGROUND`` requests already there, the
        lookup waits for every network instead. Any other verdict needs every network's
        answer: :class:`RpcUnavailable` is raised when one endpoint failed, because a
        rejection based on partial information would be cached for hours.
        """
        clients = [(n, c) for n, c in self._clients if networks is None or n in networks]
        order = {name: index for index, (name, _) in enumerate(clients)}
        tasks = {
            asyncio.ensure_future(self._check(client, address)): name for name, client in clients
        }
        pending = set(tasks)
        failed: list[str] = []
        verdicts: list[tuple[str, ChainVerdict]] = []
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
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
                            self._finished(tasks[other], other)
                        self._leave_running(pending, tasks)
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
        if failed:
            raise RpcUnavailable(f"no answer from {', '.join(failed)}")
        kinds = {verdict.kind for _, verdict in verdicts}
        for kind in (AddressKind.WALLET, AddressKind.VAULT, AddressKind.NOT_TOKEN):
            if kind in kinds:
                return ChainVerdict(kind)
        return ChainVerdict(AddressKind.NO_CODE)

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
    ) -> None:
        # Cancelling an HTTP request mid-flight would drop a warm connection; let it finish.
        for task in pending:
            self._background.add(task)  # type: ignore[arg-type]
            task.add_done_callback(lambda done, name=names[task]: self._finished(name, done))

    def _finished(self, name: str, task: asyncio.Future[ChainVerdict]) -> None:
        self._background.discard(task)  # type: ignore[arg-type]
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            self.health.answered(name)
        elif isinstance(error, RpcError):
            self.health.failed(name, error)
        else:
            log.debug("background check on %s failed: %r", name, error)

    async def _check(self, client: JsonRpcClient, address: str) -> ChainVerdict:
        code = await client.call("eth_getCode", [address, "latest"])
        if not isinstance(code, str) or code.lower() in _EMPTY_CODE:
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
        if not isinstance(code, str) or code.lower() in _EMPTY_CODE:
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
        if not is_mint(account.get("owner"), data):
            return AddressKind.NOT_TOKEN
        supply, decimals = int.from_bytes(data[36:44], "little"), data[44]
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
        notifier: Notifier | None = None,
    ) -> None:
        self._evm = evm
        self._solana = solana
        self._sui = sui
        self._cache = cache
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._retry_delay = retry_delay
        self._clock = clock
        self._notifier = notifier
        self._retry_after: dict[Address, float] = {}
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
        results = list(
            await asyncio.gather(
                *(
                    self.validate(
                        a, negative_cache=negative_cache, networks=networks, urgent=urgent
                    )
                    for a in batch
                )
            )
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
            extra = await asyncio.gather(
                *(
                    self.validate(token, negative_cache=negative_cache, urgent=urgent)
                    for token in behind
                )
            )
            results.extend(dataclasses.replace(r, via=behind[r.address]) for r in extra)
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
                    self._retry_after[address] = self._clock() + self._retry_delay
                await self._report_stuck_endpoints(address, exc)
                return ValidationResult(address, ValidationStatus.UNAVAILABLE)
        chain = address.chain.value
        if verdict.kind is AddressKind.TOKEN and verdict.network:
            self._cache.add_known(ConfirmedContract(address, verdict.network))
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
            log.info("%s %s is not a token (%s)", chain, address, verdict.kind.value)
        # A decimals-0 mint with nothing minted may still become a token: never cache it.
        if negative_cache and verdict.kind is not AddressKind.NOT_YET_MINTED:
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
        running = list(self._inflight.values())
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await self._evm.aclose()

    def failing_endpoints(self, seconds: float = 0.0) -> list[tuple[str, float]]:
        """(endpoint, seconds without an answer) for every endpoint failing right now."""
        return [
            *self._evm.health.failing(seconds),
            *self._solana.health.failing(seconds),
            *self._sui.health.failing(seconds),
        ]

    async def _report_stuck_endpoints(self, address: Address, error: RpcUnavailable) -> None:
        """One 🛑 when an endpoint has not answered for minutes; brief hiccups stay in the log."""
        if self._notifier is None:
            return
        stuck = self.failing_endpoints(_PROBLEM_AFTER)
        if not stuck:
            return
        listed = ", ".join(f"{name} (for {format_duration(age)})" for name, age in stuck)
        await self._notifier.problem(
            "rpc",
            "On-chain checks keep failing",
            f"No answer from: {listed}\nLast: {address.chain.value} {address}: {error}",
            hint=f"Addresses are re-checked every {self._retry_delay:.0f} s meanwhile. Replace "
            "the endpoint in .env (an empty EVM URL skips that network) and restart.",
        )

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
