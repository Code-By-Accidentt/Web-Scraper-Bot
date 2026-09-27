"""Keeping RPC connections warm and checking that every EVM endpoint is the chain it claims.

A cold connection (DNS, TCP and TLS) costs 0.4-1.2 s on the first call of an alert, and
httpx drops idle connections after ``keepalive_expiry``. One cheap call per endpoint every
``interval`` seconds keeps them open; with HTTP/2 every parallel call of a check then
shares that connection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from scraper_bot.chains.rpc import NO_RETRY, JsonRpcClient
from scraper_bot.config import Settings

log = logging.getLogger(__name__)

WARM_INTERVAL = 30.0

# Told an endpoint's failure: the endpoint health of the validators.
Recorder = Callable[[str, BaseException | None], None]


class ConnectionWarmer:
    """Pings every configured endpoint periodically; its failures feed the endpoint health.

    Only failures: a cheap ping that works says nothing about the heavier calls of an
    address check, whose own answers end an outage.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        *,
        interval: float = WARM_INTERVAL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        record: Recorder | None = None,
    ) -> None:
        timeout = settings.rpc_timeout
        self._evm = [
            (net, JsonRpcClient(http, net.rpc_url, timeout, retry=NO_RETRY))
            for net in settings.evm_networks
        ]
        self._pings = [(net.name, client, "eth_chainId") for net, client in self._evm] + [
            (
                "Solana",
                JsonRpcClient(http, settings.solana_rpc_url, timeout, retry=NO_RETRY),
                "getSlot",
            ),
            (
                "Sui",
                JsonRpcClient(http, settings.sui_rpc_url, timeout, retry=NO_RETRY),
                "sui_getChainIdentifier",
            ),
        ]
        self._interval = interval
        self._sleep = sleep
        self._record = record  # each failure feeds the endpoint health

    async def wrong_chains(self) -> list[str]:
        """EVM endpoints that answer eth_chainId with another chain, e.g. ``Base: chain 1``."""
        answers = await asyncio.gather(
            *(client.call("eth_chainId", []) for _, client in self._evm), return_exceptions=True
        )
        wrong: list[str] = []
        for (net, _), answer in zip(self._evm, answers, strict=True):
            if net.chain_id is None or not isinstance(answer, str):
                continue  # unknown network or no answer now: the validators will notice
            try:
                actual = int(answer, 16)
            except ValueError:
                continue
            if actual != net.chain_id:
                wrong.append(f"{net.name}: chain {actual}, expected {net.chain_id}")
        return wrong

    async def run(self) -> None:
        while True:
            results = await asyncio.gather(
                *(client.call(method, []) for _, client, method in self._pings),
                return_exceptions=True,
            )
            for (name, _, _), result in zip(self._pings, results, strict=True):
                if isinstance(result, BaseException):
                    log.debug("warm-up call to %s failed: %s", name, result)
                    if self._record is not None:
                        self._record(name, result)
            await self._sleep(self._interval)
