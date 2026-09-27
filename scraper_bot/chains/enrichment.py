"""Token details for a confirmed contract: on-chain metadata, age and DEX market data.

Everything here is best effort and bounded by ``enrich_timeout``. The alert is sent
before any of it runs; what arrives within the budget is edited into the message, and a
slow or failing source only leaves its lines out.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from scraper_bot.chains.addresses.formats import is_sui_coin_type
from scraper_bot.chains.evm.abi import (
    ERC20_DECIMALS,
    ERC20_NAME,
    ERC20_SYMBOL,
    ERC20_TOTAL_SUPPLY,
    decode_abi_string,
    decode_abi_uint,
    has_code,
    is_delegated_wallet,
)
from scraper_bot.chains.networks import NETWORK_LINKS
from scraper_bot.chains.rpc.client import JsonRpcClient, RpcError
from scraper_bot.chains.solana.accounts import (
    account_data,
    metadata_address,
    parse_authorities,
    parse_metaplex_metadata,
    parse_mint,
    parse_risky_extensions,
    parse_token2022_metadata,
)
from scraper_bot.chains.sui.objects import coin_metadata, find_coin, total_supply
from scraper_bot.models.addresses import Chain, ConfirmedContract, clean_label
from scraper_bot.settings.env import EvmNetwork
from scraper_bot.settings.live import Tuning

log = logging.getLogger(__name__)

DEXSCREENER_API = "https://api.dexscreener.com"
_MAX_DECIMALS = 36
# EVM age: public nodes keep ~100 blocks of state, so look 90 blocks back
_AGE_BLOCKS = 90
_SIGNATURE_PAGE = 1000  # Solana: one page of a mint's transactions


# ── result ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class TokenInfo:
    """What could be learned about a token; every field is optional."""

    name: str | None = None
    symbol: str | None = None
    decimals: int | None = None
    total_supply: int | None = None  # raw units, before decimals
    market_checked: bool = False  # DexScreener answered, so "no pair" is a fact
    dex: str | None = None  # e.g. "Uniswap v3"
    pair: str | None = None  # e.g. "USDC/WETH"
    pair_url: str | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    fdv_usd: float | None = None
    pair_created_at: float | None = None  # epoch seconds
    coin_type: str | None = None  # Sui: the coin found behind a bare package id
    # Solana: who can still change the token under its holders
    mint_authority: bool | None = None  # more can be minted
    freeze_authority: bool | None = None  # holders' accounts can be frozen
    risky_extensions: tuple[str, ...] = ()  # Token-2022 extensions that bind holders
    # How old it is (public nodes keep ~100 blocks of EVM state, so the EVM answer is a bound)
    deployed_within: float | None = None  # seconds: deployed at most this long ago
    deployed_before: float | None = None  # seconds: deployed longer ago than this
    created_at: float | None = None  # Solana: epoch seconds of the mint's first transaction
    busy: bool = False  # Solana: 1000+ transactions, too many to find the first one
    also_on: tuple[str, ...] = ()  # other EVM networks with a contract at this address

    @property
    def supply(self) -> float | None:
        if self.total_supply is None or self.decimals is None:
            return None
        return self.total_supply / 10**self.decimals


# ── DexScreener ─────────────────────────────────────────────────────────────
def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None  # JSON may carry NaN/Infinity


def _liquidity(pair: dict[str, Any]) -> float:
    liquidity = pair.get("liquidity")
    return (_number(liquidity.get("usd")) if isinstance(liquidity, dict) else None) or 0.0


def _same_address(left: Any, right: str, chain: Chain) -> bool:
    if not isinstance(left, str):
        return False
    if chain is Chain.SOLANA:
        return left == right
    if chain is Chain.SUI:
        # Move module and struct names are case-sensitive; only the package hex is not.
        left_package, _, left_type = left.partition("::")
        right_package, _, right_type = right.partition("::")
        return left_package.lower() == right_package.lower() and left_type == right_type
    return left.lower() == right.lower()


def best_pair(pairs: Any, chain_id: str, address: str, chain: Chain) -> dict[str, Any] | None:
    """The most liquid pair on ``chain_id`` where the token is the base asset."""
    if not isinstance(pairs, list):
        return None
    candidates = [
        pair
        for pair in pairs
        if isinstance(pair, dict)
        and pair.get("chainId") == chain_id
        and isinstance(pair.get("baseToken"), dict)
        and _same_address(pair["baseToken"].get("address"), address, chain)
    ]
    return max(candidates, key=_liquidity, default=None)


def _dex_name(pair: dict[str, Any]) -> str:
    name = str(pair.get("dexId") or "").replace("-", " ").replace("_", " ").title()
    labels = [str(label) for label in pair.get("labels") or [] if isinstance(label, str | int)]
    return " ".join([name, *labels]).strip() or "DEX"


# ── the enricher ────────────────────────────────────────────────────────────
class TokenEnricher:
    """Collects token details from the RPC nodes and DexScreener within a time budget."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        evm_networks: Sequence[EvmNetwork],
        solana_rpc_url: str,
        *,
        timeout: float,
        tuning: Tuning,
        sui_rpc_url: str,
    ) -> None:
        self._http = http
        self._timeout = timeout
        self._tuning = tuning
        self._evm = {net.name: JsonRpcClient(http, net.rpc_url, timeout) for net in evm_networks}
        self._solana = JsonRpcClient(http, solana_rpc_url, timeout)
        self._sui = JsonRpcClient(http, sui_rpc_url, timeout)

    async def enrich(self, contract: ConfirmedContract) -> TokenInfo:
        budget = float(self._tuning.enrich_timeout)
        if budget <= 0:
            return TokenInfo()
        value = contract.address.value
        if contract.address.chain is Chain.SUI and not is_sui_coin_type(value):
            # A bare package id: the coin type has to be found before the market lookup.
            started = time.monotonic()
            on_chain = await self._bounded(self._chain_details(contract), budget, "chain")
            coin_type = on_chain.get("coin_type")
            if not coin_type:
                return TokenInfo(**on_chain)
            left = max(budget - (time.monotonic() - started), 0.2)
            market = await self._bounded(self._market_details(contract, coin_type), left, "market")
        else:
            # Each source has its own budget: a slow network in "also on" must not cost the
            # age line, and neither of them the name or the market.
            on_chain, market, age, others = await asyncio.gather(
                self._bounded(self._chain_details(contract), budget, "chain"),
                self._bounded(self._market_details(contract), budget, "market"),
                self._bounded(self._age(contract), budget, "age"),
                self._bounded(self._also_on(contract), budget, "other networks"),
            )
            on_chain = {**age, **others, **on_chain}
        # What the chain says about the token wins over DexScreener's copy of it.
        return TokenInfo(**{**market, **on_chain})

    @staticmethod
    async def _bounded(coro: Any, budget: float, source: str) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(coro, budget)
        except TimeoutError:
            log.info("token details: %s source did not answer within %.1fs", source, budget)
        except (RpcError, httpx.HTTPError, ValueError) as exc:
            log.info("token details: %s source failed: %s", source, exc)
        except Exception:  # an odd payload must not cost the alert
            log.warning("token details: %s source raised", source, exc_info=True)
        return {}

    # ── on-chain ────────────────────────────────────────────────────────────
    async def _chain_details(self, contract: ConfirmedContract) -> dict[str, Any]:
        if contract.address.chain is Chain.ETHEREUM:
            client = self._evm.get(contract.network)
            return await self._erc20_details(client, contract.address.value) if client else {}
        if contract.address.chain is Chain.SOLANA:
            return await self._solana_details(contract.address.value)
        return await self._sui_details(self._sui, contract.address.value)

    @staticmethod
    async def _erc20_details(client: JsonRpcClient, address: str) -> dict[str, Any]:
        name, symbol, decimals, supply = await asyncio.gather(
            *(
                client.call("eth_call", [{"to": address, "data": selector}, "latest"])
                for selector in (ERC20_NAME, ERC20_SYMBOL, ERC20_DECIMALS, ERC20_TOTAL_SUPPLY)
            ),
            return_exceptions=True,
        )
        details = {
            "name": decode_abi_string(name),
            "symbol": decode_abi_string(symbol),
            "decimals": decode_abi_uint(decimals, limit=_MAX_DECIMALS),
            "total_supply": decode_abi_uint(supply),
        }
        return {key: value for key, value in details.items() if value is not None}

    async def _solana_details(self, mint: str) -> dict[str, Any]:
        # "confirmed", like the validator: at "finalized" a fresh mint does not exist yet.
        params = {"encoding": "base64", "commitment": "confirmed"}
        mint_account, metadata_account = await asyncio.gather(
            self._solana.call("getAccountInfo", [mint, params]),
            self._solana.call("getAccountInfo", [metadata_address(mint), params]),
            return_exceptions=True,
        )
        details: dict[str, Any] = {}
        mint_data = account_data(mint_account)
        parsed = parse_mint(mint_data)
        if parsed is not None:
            details["total_supply"], details["decimals"] = parsed
        authorities = parse_authorities(mint_data)
        if authorities is not None:
            details["mint_authority"], details["freeze_authority"] = authorities
        risky = parse_risky_extensions(mint_data)
        if risky:
            details["risky_extensions"] = risky
        name, symbol = parse_metaplex_metadata(account_data(metadata_account))
        if name is None and symbol is None:
            name, symbol = parse_token2022_metadata(mint_data)
        if name:
            details["name"] = name
        if symbol:
            details["symbol"] = symbol
        return details

    @staticmethod
    async def _sui_details(client: JsonRpcClient, value: str) -> dict[str, Any]:
        if is_sui_coin_type(value):
            coin_type, metadata = value, await coin_metadata(client, value)
        else:
            found = await find_coin(client, value)
            if found is None:
                return {}
            coin_type, metadata = found
        details: dict[str, Any] = {}
        if coin_type != value:
            details["coin_type"] = coin_type
        if metadata:
            for key in ("name", "symbol"):
                text = metadata.get(key)
                if isinstance(text, str) and clean_label(text):
                    details[key] = clean_label(text)
            decimals = metadata.get("decimals")
            if isinstance(decimals, int) and 0 <= decimals <= _MAX_DECIMALS:
                details["decimals"] = decimals
        supply = await total_supply(client, coin_type)
        if supply is not None:
            details["total_supply"] = supply
        return details

    # ── age and other networks ──────────────────────────────────────────────
    async def _age(self, contract: ConfirmedContract) -> dict[str, Any]:
        """How old the token is: an old token on a launch page is worth a second look."""
        if contract.address.chain is Chain.SOLANA:
            return await self._solana_age(contract.address.value)
        if contract.address.chain is not Chain.ETHEREUM:
            return {}
        client = self._evm.get(contract.network)
        if client is None:
            return {}  # a network this bot does not query
        return await self._evm_age(client, contract.address.value)

    async def _evm_age(self, client: JsonRpcClient, address: str) -> dict[str, Any]:
        head = int(await client.call("eth_blockNumber", []), 16)
        back = hex(max(head - _AGE_BLOCKS, 0))
        code, block = await client.call_batch(
            [("eth_getCode", [address, back]), ("eth_getBlockByNumber", [back, False])]
        )
        if not isinstance(code, str) or not isinstance(block, dict):
            return {}  # older state is pruned on this node
        window = max(time.time() - int(block["timestamp"], 16), 0.0)
        if not has_code(code):
            return {"deployed_within": window}
        return {"deployed_before": window}

    async def _also_on(self, contract: ConfirmedContract) -> dict[str, Any]:
        """Other EVM networks with code at the same address (a foreign token, perhaps)."""
        if contract.address.chain is not Chain.ETHEREUM or contract.network not in self._evm:
            return {}
        others = [(n, c) for n, c in self._evm.items() if n != contract.network]
        codes = await asyncio.gather(
            *(c.call("eth_getCode", [contract.address.value, "latest"]) for _, c in others),
            return_exceptions=True,
        )
        found = tuple(
            name
            for (name, _), code in zip(others, codes, strict=True)
            if isinstance(code, str) and has_code(code) and not is_delegated_wallet(code)
        )
        return {"also_on": found} if found else {}

    async def _solana_age(self, mint: str) -> dict[str, Any]:
        signatures = await self._solana.call(
            "getSignaturesForAddress", [mint, {"limit": _SIGNATURE_PAGE, "commitment": "confirmed"}]
        )
        if not isinstance(signatures, list) or not signatures:
            return {}
        if len(signatures) >= _SIGNATURE_PAGE:
            return {"busy": True}
        first = signatures[-1].get("blockTime") if isinstance(signatures[-1], dict) else None
        return {"created_at": float(first)} if isinstance(first, int | float) else {}

    # ── market ──────────────────────────────────────────────────────────────
    async def _market_details(
        self, contract: ConfirmedContract, identifier: str | None = None
    ) -> dict[str, Any]:
        links = NETWORK_LINKS.get(contract.network)
        if links is None:
            return {}
        address = identifier or contract.address.value
        url = f"{DEXSCREENER_API}/tokens/v1/{links.dex_chain}/{address}"
        response = await self._http.get(url, timeout=self._timeout)
        response.raise_for_status()
        pairs = response.json()
        if not isinstance(pairs, list):
            raise ValueError("unexpected DexScreener payload")
        details: dict[str, Any] = {"market_checked": True}
        pair = best_pair(pairs, links.dex_chain, address, contract.address.chain)
        if pair is None:
            return details
        base, quote = pair.get("baseToken") or {}, pair.get("quoteToken") or {}
        base_symbol, quote_symbol = base.get("symbol"), quote.get("symbol")
        details["dex"] = _dex_name(pair)
        if isinstance(base_symbol, str) and isinstance(quote_symbol, str):
            details["pair"] = f"{base_symbol}/{quote_symbol}"
        if isinstance(pair.get("url"), str):
            details["pair_url"] = pair["url"]
        details["price_usd"] = _number(pair.get("priceUsd"))
        details["liquidity_usd"] = _liquidity(pair) or None
        details["fdv_usd"] = _number(pair.get("fdv"))
        created = _number(pair.get("pairCreatedAt"))
        details["pair_created_at"] = created / 1000 if created else None
        for key in ("name", "symbol"):  # fallback when the chain gave no metadata
            value = base.get(key)
            if isinstance(value, str) and clean_label(value):
                details[key] = clean_label(value)
        return {key: value for key, value in details.items() if value is not None}
