"""Checks whether a Sui value is a coin or a package that defines one."""

from __future__ import annotations

import dataclasses
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
# A node a block or two behind the page may not have a new coin's package yet (s).
LAG_WINDOW = 60.0
_MISSING_REMEMBERED = 10_000  # coin types whose package was missing, newest kept
_SEARCHES_REMEMBERED = 100  # package searches that failed halfway, kept for their retry


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
        self._timeout = timeout
        self._tuning = tuning
        self._clock = clock
        self.health = EndpointHealth(clock)
        # coin type -> when we first found its package missing
        self._missing_since: dict[str, float] = {}
        # package -> the candidates that had no metadata, while its search has no verdict
        self._no_coin: dict[str, set[str]] = {}

    async def inspect(self, value: str) -> ChainVerdict:
        """Return a token on "Sui" for a coin with metadata or a package that defines one.

        A coin named like a stablecoin or wrapped coin is a copy instead, and anything else
        is not a token. A bare package's coin type comes with the verdict. Raises
        :class:`RpcUnavailable` when the node fails, and for a coin whose package the node
        hasn't got during the ``LAG_WINDOW`` after we first found it missing.
        """
        try:
            verdict = await self._inspect(value)
        except RpcError as exc:
            self.health.failed("Sui", exc)
            raise RpcUnavailable(str(exc)) from exc
        self.health.answered("Sui")
        if verdict is None:
            raise RpcUnavailable("its package is not on the node yet")
        return verdict

    async def _inspect(self, value: str) -> ChainVerdict | None:
        """Do the check of :meth:`inspect`, letting RPC errors through.

        Returns None while a coin's package may just not have reached the node. With
        ``require_erc20`` off any real package counts, and so does any type in one, like any
        EVM contract does.
        """
        if is_sui_coin_type(value):
            metadata = await coin_metadata(self._client, value)
            if metadata is not None:
                return self._coin(metadata)
            kind = await object_kind(self._client, sui_package(value))
            if kind == "missing" and self._just_published(value):
                return None
            return _TOKEN if kind == "package" and not self._tuning.require_erc20 else _NOT_TOKEN
        if await object_kind(self._client, value) != "package":
            return _NOT_TOKEN
        if not self._tuning.require_erc20:
            return _TOKEN
        found = await self._find_coin(value)
        if found is None:
            return _NOT_TOKEN
        coin_type, metadata = found
        return dataclasses.replace(self._coin(metadata), coin_type=coin_type)

    def _just_published(self, coin_type: str) -> bool:
        """Return True during the ``LAG_WINDOW`` after we first found the coin's package missing.

        Past it, a missing package means the coin type is made up. Only the newest
        ``_MISSING_REMEMBERED`` coin types are kept.
        """
        now = self._clock()
        since = self._missing_since.get(coin_type)
        if since is None:
            if len(self._missing_since) >= _MISSING_REMEMBERED:
                del self._missing_since[next(iter(self._missing_since))]
            self._missing_since[coin_type] = since = now
        return now - since < LAG_WINDOW

    async def _find_coin(self, package: str) -> tuple[str, dict[str, Any]] | None:
        """Search ``package`` for the coin it defines, within the RPC timeout as a whole.

        When the search fails halfway, the candidates that had no metadata are kept, so its
        retry only asks the rest.
        """
        no_coin = self._no_coin.pop(package, set())
        try:
            return await find_coin(self._client, package, no_coin=no_coin, budget=self._timeout)
        except RpcError:
            if len(self._no_coin) >= _SEARCHES_REMEMBERED:
                del self._no_coin[next(iter(self._no_coin))]
            self._no_coin[package] = no_coin
            raise

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
