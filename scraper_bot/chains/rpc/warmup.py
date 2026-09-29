"""Keeps RPC connections warm and checks that every EVM endpoint serves the chain it claims.

A cold connection (DNS, TCP and TLS) costs 0.4-1.2 s on the first call of an alert, and
httpx drops idle connections after ``keepalive_expiry``. One cheap call per endpoint every
``interval`` seconds keeps them open; with HTTP/2, all parallel calls of a check then
share that connection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from scraper_bot.chains.rpc.client import NO_RETRY, JsonRpcClient
from scraper_bot.settings.env import Settings

log = logging.getLogger(__name__)

WARM_INTERVAL = 30.0

# Gets an endpoint's name and failure. In the app it feeds the validators' endpoint health.
Recorder = Callable[[str, BaseException | None], None]


class ConnectionWarmer:
    """Pings every configured endpoint on a timer and reports failures to the endpoint health.

    Successes are not reported: a cheap ping that works says nothing about the heavier
    calls of an address check, whose own answers end an outage.
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
        """Make one client without retries per configured endpoint; failures go to ``record``."""
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
        self._record = record

    async def wrong_chains(self) -> list[str]:
        """Return a line for each EVM endpoint whose eth_chainId names another chain.

        A line reads like ``Base: chain 1, expected 8453``. Endpoints that do not answer
        now are skipped.
        """
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
        """Ping every endpoint every ``interval`` seconds until cancelled, reporting failures."""
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
