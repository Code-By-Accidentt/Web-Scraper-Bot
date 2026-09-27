"""Checking whether an EVM address is a token, on every network at once."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable, Collection, Sequence

import httpx

from scraper_bot.chains.addresses.formats import to_checksum_address
from scraper_bot.chains.addresses.stoplist import StopList
from scraper_bot.chains.evm.abi import (
    TOKEN_PROBE,
    has_code,
    is_delegated_wallet,
    word_address,
    word_uint,
)
from scraper_bot.chains.rpc.client import JsonRpcClient, RetryPolicy, RpcCallError, RpcError
from scraper_bot.chains.rpc.health import EndpointHealth, RpcUnavailable
from scraper_bot.models.addresses import AddressKind, ChainVerdict
from scraper_bot.settings.env import EvmNetwork
from scraper_bot.settings.live import Tuning

log = logging.getLogger(__name__)


# JSON-RPC errors that mean the EVM ran the call and it failed: a real "not a token".
_REVERT_MARKERS = ("revert", "invalid opcode", "out of gas", "vm execution")
_MAX_DECIMALS = 255  # decimals() is a uint8
# Lookups that answered early leave the slower networks running in the background; above
# this many such requests a lookup waits for all its networks, which bounds the fan-out.
_MAX_BACKGROUND = 64
# Once every required network has answered, the optional ones get this long (seconds) to
# answer too; a slow or hanging one then finishes in the background (they answer in about
# 50 ms when healthy).
OPTIONAL_GRACE = 0.5
_MAX_LATE = 1000  # tokens an optional network found after the verdict, kept for the next look


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
