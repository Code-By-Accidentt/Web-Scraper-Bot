"""Checks whether a Solana address is a token mint."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from scraper_bot.chains.addresses.stoplist import is_stable_symbol
from scraper_bot.chains.rpc.client import JsonRpcClient, RetryPolicy, RpcError
from scraper_bot.chains.rpc.health import EndpointHealth, RpcUnavailable
from scraper_bot.chains.solana.accounts import (
    account_data,
    is_mint,
    metadata_address,
    parse_mint,
    token_names,
)
from scraper_bot.models.addresses import AddressKind, ChainVerdict
from scraper_bot.settings.live import Tuning


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
        tuning: Tuning,
        retry: RetryPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the RPC client; the endpoint's health is tracked under the name "Solana"."""
        self._client = JsonRpcClient(http, rpc_url, timeout, retry=retry)
        self._tuning = tuning
        self.health = EndpointHealth(clock)

    async def inspect(self, address: str) -> ChainVerdict:
        """Return what the account at ``address`` is: a token mint, an NFT, missing and so on.

        The mint's Metaplex metadata account is read in the same call, so with
        ``skip_stable_copies`` a mint named like a stablecoin or wrapped coin is told apart
        as a copy at no extra cost. Raises :class:`RpcUnavailable` when the node fails.
        """
        params = {"encoding": "base64", "commitment": "confirmed"}
        try:
            result = await self._client.call("getMultipleAccounts", [_accounts_of(address), params])
        except RpcError as exc:
            self.health.failed("Solana", exc)
            raise RpcUnavailable(str(exc)) from exc
        self.health.answered("Solana")
        accounts = result.get("value") if isinstance(result, dict) else None
        if not isinstance(accounts, list) or not accounts or not isinstance(accounts[0], dict):
            return ChainVerdict(AddressKind.MISSING)
        data = account_data({"value": accounts[0]})
        parsed = parse_mint(data)
        if parsed is None or not is_mint(accounts[0].get("owner"), data):
            return ChainVerdict(AddressKind.NOT_TOKEN)
        supply, decimals = parsed
        if decimals == 0 and supply == 1:
            return ChainVerdict(AddressKind.NFT)
        if decimals == 0 and supply == 0:
            return ChainVerdict(AddressKind.NOT_YET_MINTED)  # an NFT or a token not minted yet
        metadata = account_data({"value": accounts[1]}) if len(accounts) > 1 else b""
        _, symbol = token_names(data, metadata)
        if self._tuning.skip_stable_copies and is_stable_symbol(symbol):
            return ChainVerdict(AddressKind.STABLE_COPY, "Solana", symbol=symbol)
        return ChainVerdict(AddressKind.TOKEN, "Solana")


def _accounts_of(address: str) -> list[str]:
    """Return the accounts to read for ``address``: itself and its Metaplex metadata account.

    A value that is not base58 has no metadata account; the node then refuses it anyway.
    """
    try:
        return [address, metadata_address(address)]
    except ValueError:
        return [address]
