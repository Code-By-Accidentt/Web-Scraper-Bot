"""Checks whether a Sui value is a coin or a package that defines one."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from scraper_bot.chains.addresses.formats import is_sui_coin_type, sui_package
from scraper_bot.chains.rpc.client import JsonRpcClient, RetryPolicy, RpcError
from scraper_bot.chains.rpc.health import EndpointHealth, RpcUnavailable
from scraper_bot.chains.sui.objects import coin_metadata, find_coin, object_kind
from scraper_bot.settings.live import Tuning


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
        """Create the RPC client; the endpoint's health is tracked under the name "Sui"."""
        self._client = JsonRpcClient(http, rpc_url, timeout, retry=retry)
        self._tuning = tuning
        self.health = EndpointHealth(clock)

    async def is_coin_or_package(self, value: str) -> bool:
        """Return True when ``value`` is a coin with metadata or a package that defines one.

        Raises :class:`RpcUnavailable` when the node fails.
        """
        try:
            verdict = await self._is_coin_or_package(value)
        except RpcError as exc:
            self.health.failed("Sui", exc)
            raise RpcUnavailable(str(exc)) from exc
        self.health.answered("Sui")
        return verdict

    async def _is_coin_or_package(self, value: str) -> bool:
        """Do the check of :meth:`is_coin_or_package`, letting RPC errors through.

        With ``require_erc20`` off any real package counts, and so does any type in one,
        like any EVM contract does.
        """
        if is_sui_coin_type(value):
            if await coin_metadata(self._client, value) is not None:
                return True
            if self._tuning.require_erc20:
                return False
            return await object_kind(self._client, sui_package(value)) == "package"
        if await object_kind(self._client, value) != "package":
            return False
        if not self._tuning.require_erc20:
            return True
        return await find_coin(self._client, value) is not None
