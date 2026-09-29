"""Checks whether an EVM address is a token by asking every network at once."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable, Collection, Sequence

import httpx

from scraper_bot.chains.addresses.formats import to_checksum_address
from scraper_bot.chains.addresses.stoplist import StopList, is_stable_symbol
from scraper_bot.chains.evm.abi import (
    TOKEN_PROBE,
    decode_abi_string,
    has_code,
    is_delegated_wallet,
    looks_like_token,
    word_address,
)
from scraper_bot.chains.rpc.client import JsonRpcClient, RetryPolicy, RpcCallError, RpcError
from scraper_bot.chains.rpc.health import EndpointHealth, RpcUnavailable
from scraper_bot.models.addresses import AddressKind, ChainVerdict
from scraper_bot.settings.env import EvmNetwork
from scraper_bot.settings.live import Tuning

log = logging.getLogger(__name__)


_REVERT_MARKERS = ("revert", "invalid opcode", "out of gas", "vm execution")
# With this many requests already in the background, a lookup waits for all its networks.
_MAX_BACKGROUND = 64
# Seconds optional networks get after the required ones answer. A healthy one takes about 50 ms.
OPTIONAL_GRACE = 0.5
_MAX_LATE = 1000  # late tokens and pairs kept for the next lookup
# If no network found a token or pair, the verdict is the first of these kinds any network gave.
_REJECTIONS = (
    AddressKind.WALLET,
    AddressKind.VAULT,
    AddressKind.STABLE_COPY,
    AddressKind.NOT_TOKEN,
)


class EvmContractValidator:
    """Looks for bytecode on every configured network at once and classifies it.

    With ``require_erc20`` a contract counts only when it is a plain token: it answers
    totalSupply() and decimals(), and is neither an LP pair nor an ERC-4626 vault. With
    ``skip_stable_copies`` as well, a token whose symbol is a stablecoin's or wrapped
    coin's (USDC, WETH, ...) is only a copy on that network.
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
        """Create one RPC client per network and note which networks are optional.

        The stop list names the well-known tokens (WETH, USDC, ...) left out of a pair's sides.
        """
        self._clients = [
            (net.name, JsonRpcClient(http, net.rpc_url, timeout, retry=retry)) for net in networks
        ]
        self._optional = frozenset(net.name for net in networks if net.optional)
        self._tuning = tuning
        self._stoplist = stoplist
        self.health = EndpointHealth(clock, optional=self._optional)
        self._background: set[asyncio.Task[ChainVerdict]] = set()
        # (address, network) -> a token or pair found too late. The next lookup returns it.
        self._late: dict[tuple[str, str], ChainVerdict] = {}
        # Called with (address, network, verdict) for every late answer from an optional network.
        self.on_late: Callable[[str, str, ChainVerdict], None] | None = None

    async def find_network(
        self, address: str, networks: Collection[str] | None = None
    ) -> ChainVerdict:
        """Return what ``address`` is: a token or LP pair with its network, or something else.

        The first network that finds a token or pair answers at once and the slower ones
        finish in the background (while fewer than ``_MAX_BACKGROUND`` requests run there).
        Any other verdict, a copy of a stablecoin included, needs every required network,
        else :class:`RpcUnavailable` is raised: a rejection based on partial answers would
        be cached for hours.

        Optional networks get ``OPTIONAL_GRACE`` more seconds; one that failed or is still
        silent is left out and listed in ``missing`` (no answer at all raises as well).
        ``networks`` limits the lookup, e.g. to where the pending watch saw the bytecode.
        """
        asked = [(n, c) for n, c in self._clients if networks is None or n in networks]
        for name, _ in asked:
            late = self._late.pop((address, name), None)
            if late is not None:
                return dataclasses.replace(late, network=name)
        # An optional endpoint that keeps failing isn't asked at all. It's just left out.
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
                # Only optional networks left, so give them the grace, not the whole RPC timeout.
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
                        for other in ready[position + 1 :]:  # also done: record their health
                            self._finished(tasks[other], address, other, late=False)
                        self._leave_running(pending, tasks, address)
                        return dataclasses.replace(verdict, network=name)
                    verdicts.append((name, verdict))
        except BaseException:
            for task in pending:
                task.cancel()
            for task in tasks:  # retrieve the other errors so asyncio doesn't warn
                if task.done() and not task.cancelled():
                    task.exception()
            raise
        for wanted in (AddressKind.TOKEN, AddressKind.PAIR):
            for name, verdict in verdicts:
                if verdict.kind is wanted:
                    return dataclasses.replace(verdict, network=name)
        if any(name not in self._optional for name in failed) or (failed and not verdicts):
            raise RpcUnavailable(f"no answer from {', '.join(failed)}")
        missing = frozenset(failed)
        for wanted in _REJECTIONS:
            for name, verdict in verdicts:
                if verdict.kind is wanted:
                    return dataclasses.replace(verdict, network=name, missing=missing)
        return ChainVerdict(AddressKind.NO_CODE, missing=missing)

    async def aclose(self) -> None:
        """Stop the requests still running for lookups that already answered."""
        leftovers = list(self._background)
        for task in leftovers:
            task.cancel()
        await asyncio.gather(*leftovers, return_exceptions=True)

    def _settle(self, name: str, task: asyncio.Future[ChainVerdict]) -> ChainVerdict | None:
        """Return the network's verdict, or None when its endpoint failed.

        The endpoint's health is updated either way; an error that is not an RPC error is
        raised.
        """
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
        """Let unfinished requests run on in the background and handle them when they end.

        They are not cancelled: cancelling an HTTP request mid-flight drops a warm connection.
        """
        for task in pending:
            self._background.add(task)  # type: ignore[arg-type]
            task.add_done_callback(
                lambda done, name=names[task]: self._finished(name, address, done)
            )

    def has_late(self, address: str) -> bool:
        """Return True when an optional network found a token or pair at ``address`` late."""
        return any(key[0] == address for key in self._late)

    def remember_late(self, address: str, network: str, verdict: ChainVerdict) -> None:
        """Keep ``verdict`` so the next lookup of ``address`` returns it at once.

        Beyond ``_MAX_LATE`` entries the oldest one is dropped.
        """
        self._late.pop((address, network), None)
        if len(self._late) >= _MAX_LATE:
            del self._late[next(iter(self._late))]
        self._late[address, network] = verdict

    def _finished(
        self, name: str, address: str, task: asyncio.Future[ChainVerdict], *, late: bool = True
    ) -> None:
        """Record the outcome of a request whose answer the lookup did not use.

        With ``late`` (its lookup had already answered) a token or pair an optional network
        found is kept for the next lookup, and ``on_late`` hears every such answer.
        """
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
                self.remember_late(address, name, verdict)
                log.info("%s answered late for %s: %s", name, address, verdict.kind.value)
            if self.on_late is not None:
                self.on_late(address, name, verdict)
        elif isinstance(error, RpcError):
            self.health.failed(name, error)
        else:
            log.debug("background check on %s failed: %r", name, error)

    async def _check(self, client: JsonRpcClient, address: str) -> ChainVerdict:
        """Return one network's verdict: no code, a delegated wallet, or what the code is.

        With ``require_erc20`` off any bytecode counts as a token.
        """
        code = await client.call("eth_getCode", [address, "latest"])
        if not isinstance(code, str) or not has_code(code):
            return ChainVerdict(AddressKind.NO_CODE)
        if is_delegated_wallet(code):
            return ChainVerdict(AddressKind.WALLET)
        if not self._tuning.require_erc20:
            return ChainVerdict(AddressKind.TOKEN)
        return await self._classify(client, address)

    async def _classify(self, client: JsonRpcClient, address: str) -> ChainVerdict:
        """Tell a token, LP pair, vault, copy or other contract apart with six read-only calls.

        Empty results and EVM reverts are answers ("no such function"). A rate limit or
        timeout is raised instead, so the network counts as failed rather than a real token
        being rejected and cached for hours. symbol() only tells a copy of a stablecoin or
        wrapped coin, so a symbol that fails or cannot be read never stops a token.

        eth_getCode is asked again in the same batch, so every answer comes from one node
        (or one block number when the node takes no batches). Behind a load balancer the
        first eth_getCode may come from a node a block ahead; a node without the fresh
        contract answers "0x" to every call, which would read as "not a token".
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
        code, supply, decimals, token0, token1, asset, symbol = answers
        for answer in (code, supply, decimals, token0, token1, asset):
            if isinstance(answer, BaseException) and not _is_revert(answer):
                raise answer
        if not has_code(code):
            return ChainVerdict(AddressKind.NO_CODE)  # node is behind: watch it, don't rule it out
        first, second = word_address(token0), word_address(token1)
        # Pairs and pools of every common DEX keep their two tokens sorted by address.
        if first and second and first < second:
            sides = tuple(
                to_checksum_address(side) for side in (first, second) if not self._quote(side)
            )
            return ChainVerdict(AddressKind.PAIR, sides=sides)
        if word_address(asset):
            return ChainVerdict(AddressKind.VAULT)
        if not looks_like_token(supply, decimals):
            return ChainVerdict(AddressKind.NOT_TOKEN)
        label = decode_abi_string(symbol)
        if self._tuning.skip_stable_copies and is_stable_symbol(label):
            return ChainVerdict(AddressKind.STABLE_COPY, symbol=label)
        return ChainVerdict(AddressKind.TOKEN)

    def _quote(self, address: str) -> bool:
        """Return True for a stop-listed pair side (WETH, USDC, ...), which is never the launch."""
        return self._stoplist is not None and self._stoplist.contains_evm(address)


def _is_revert(error: BaseException) -> bool:
    """Return True when the EVM ran the call and it failed: an answer, not an outage.

    That is JSON-RPC error code 3 or an error text such as "revert" or "out of gas".
    """
    if not isinstance(error, RpcCallError):
        return False
    lowered = error.message.lower()
    return error.code == 3 or any(marker in lowered for marker in _REVERT_MARKERS)
