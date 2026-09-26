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
from scraper_bot.config import EXPECTED_CHAIN_IDS, Settings

log = logging.getLogger(__name__)

WARM_INTERVAL = 30.0


class ConnectionWarmer:
    """Pings every configured endpoint periodically; never alerts on its own failures."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        *,
        interval: float = WARM_INTERVAL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        timeout = settings.rpc_timeout
        self._evm = [
            (net.name, JsonRpcClient(http, net.rpc_url, timeout, retry=NO_RETRY))
            for net in settings.evm_networks
        ]
        self._pings = [(name, client, "eth_chainId") for name, client in self._evm] + [
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

    async def wrong_chains(self) -> list[str]:
        """EVM endpoints that answer eth_chainId with another chain, e.g. ``Base: chain 1``."""
        answers = await asyncio.gather(
            *(client.call("eth_chainId", []) for _, client in self._evm), return_exceptions=True
        )
        wrong: list[str] = []
        for (name, _), answer in zip(self._evm, answers, strict=True):
            expected = EXPECTED_CHAIN_IDS.get(name)
            if expected is None or not isinstance(answer, str):
                continue  # unknown network or no answer now: the validators will notice
            try:
                actual = int(answer, 16)
            except ValueError:
                continue
            if actual != expected:
                wrong.append(f"{name}: chain {actual}, expected {expected}")
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
            await self._sleep(self._interval)
