"""Checks whether a Solana address is a token mint."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from scraper_bot.chains.rpc.client import JsonRpcClient, RetryPolicy, RpcError
from scraper_bot.chains.rpc.health import EndpointHealth, RpcUnavailable
from scraper_bot.chains.solana.accounts import account_data, is_mint, parse_mint
from scraper_bot.models.addresses import AddressKind


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
        """Create the RPC client; the endpoint's health is tracked under the name "Solana"."""
        self._client = JsonRpcClient(http, rpc_url, timeout, retry=retry)
        self.health = EndpointHealth(clock)

    async def inspect(self, address: str) -> AddressKind:
        """Return what the account at ``address`` is: a token mint, an NFT, missing and so on.

        Raises :class:`RpcUnavailable` when the node fails.
        """
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
            return AddressKind.NOT_YET_MINTED  # an NFT or a token not minted yet
        return AddressKind.TOKEN
