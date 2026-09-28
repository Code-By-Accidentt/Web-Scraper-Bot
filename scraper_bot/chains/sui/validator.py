"""Checks whether a Sui value is a coin or a package that defines one."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from scraper_bot.chains.addresses.formats import is_sui_coin_type, sui_package
from scraper_bot.chains.addresses.stoplist import is_stable_symbol
from scraper_bot.chains.rpc.client import JsonRpcClient, RetryPolicy, RpcError
from scraper_bot.chains.rpc.health import EndpointHealth, RpcUnavailable
from scraper_bot.chains.sui.objects import coin_metadata, find_coin, object_kind
from scraper_bot.models.addresses import AddressKind, ChainVerdict, clean_label
from scraper_bot.settings.live import Tuning

_TOKEN = ChainVerdict(AddressKind.TOKEN, "Sui")
_NOT_TOKEN = ChainVerdict(AddressKind.NOT_TOKEN)


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

    async def inspect(self, value: str) -> ChainVerdict:
        """Return a token on "Sui" for a coin with metadata or a package that defines one.

        A coin named like a stablecoin or wrapped coin is a copy instead, and anything else
        is not a token. Raises :class:`RpcUnavailable` when the node fails.
        """
        try:
            verdict = await self._inspect(value)
        except RpcError as exc:
            self.health.failed("Sui", exc)
            raise RpcUnavailable(str(exc)) from exc
        self.health.answered("Sui")
        return verdict

    async def _inspect(self, value: str) -> ChainVerdict:
        """Do the check of :meth:`inspect`, letting RPC errors through.

        With ``require_erc20`` off any real package counts, and so does any type in one,
        like any EVM contract does.
        """
        if is_sui_coin_type(value):
            metadata = await coin_metadata(self._client, value)
            if metadata is not None:
                return self._coin(metadata)
            if self._tuning.require_erc20:
                return _NOT_TOKEN
            kind = await object_kind(self._client, sui_package(value))
            return _TOKEN if kind == "package" else _NOT_TOKEN
        if await object_kind(self._client, value) != "package":
            return _NOT_TOKEN
        if not self._tuning.require_erc20:
            return _TOKEN
        found = await find_coin(self._client, value)
        return _NOT_TOKEN if found is None else self._coin(found[1])

    def _coin(self, metadata: dict[str, Any]) -> ChainVerdict:
        """Return the verdict on a coin with ``metadata``: a token, or a copy of a stablecoin.

        As on EVM, a copy is told apart only with ``require_erc20`` and ``skip_stable_copies``.
        """
        symbol = metadata.get("symbol")
        tuning = self._tuning
        copies = tuning.require_erc20 and tuning.skip_stable_copies
        if copies and isinstance(symbol, str) and is_stable_symbol(symbol):
            return ChainVerdict(AddressKind.STABLE_COPY, "Sui", symbol=clean_label(symbol))
        return _TOKEN
