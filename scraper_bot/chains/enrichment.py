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
    looks_like_token,
)
from scraper_bot.chains.networks import NETWORK_LINKS
from scraper_bot.chains.rpc.client import JsonRpcClient, RpcError
from scraper_bot.chains.solana.accounts import (
    account_data,
    metadata_address,
    parse_authorities,
    parse_mint,
    parse_risky_extensions,
    token_names,
)
from scraper_bot.chains.sui.objects import coin_metadata, find_coin, total_supply
from scraper_bot.models.addresses import Chain, ConfirmedContract, clean_label
from scraper_bot.settings.env import EvmNetwork
from scraper_bot.settings.live import Tuning

log = logging.getLogger(__name__)

DEXSCREENER_API = "https://api.dexscreener.com"
_MAX_DECIMALS = 36
_AGE_BLOCKS = 90  # public nodes only keep about 100 blocks of EVM state
_SIGNATURE_PAGE = 1000  # Solana: one page of a mint's transactions
_ALSO_ON_MARGIN = 0.1  # "other networks" stops this early (s) to hand in what answered


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """What could be learned about a token; every field is optional.

    The EVM age is only a bound (``deployed_within`` or ``deployed_before``), because public
    nodes keep about 100 blocks of state.
    """

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
    mint_authority: bool | None = None  # Solana: more can be minted
    freeze_authority: bool | None = None  # Solana: holders' accounts can be frozen
    risky_extensions: tuple[str, ...] = ()  # Token-2022 extensions that work against holders
    deployed_within: float | None = None  # seconds: deployed at most this long ago
    deployed_before: float | None = None  # seconds: deployed longer ago than this
    created_at: float | None = None  # Solana: epoch seconds of the mint's first transaction
    busy: bool = False  # Solana: 1000+ transactions, too many to find the first one
    # (network, symbol) of the other EVM networks where this address is a token too
    also_on: tuple[tuple[str, str | None], ...] = ()

    @property
    def supply(self) -> float | None:
        """Total supply in whole tokens, or None when the supply or decimals are unknown."""
        if self.total_supply is None or self.decimals is None:
            return None
        return self.total_supply / 10**self.decimals


def _number(value: Any) -> float | None:
    """Return the value as a finite float, or None for booleans, junk, NaN and infinity."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _liquidity(pair: dict[str, Any]) -> float:
    """Return a DexScreener pair's liquidity in USD, or 0.0 when it is unknown."""
    liquidity = pair.get("liquidity")
    return (_number(liquidity.get("usd")) if isinstance(liquidity, dict) else None) or 0.0


def _same_address(left: Any, right: str, chain: Chain) -> bool:
    """Return True when ``left`` is the same address or coin type as ``right`` on ``chain``.

    EVM addresses ignore case and Solana ones do not. In a Sui coin type only the package
    hex ignores case: Move module and struct names are case-sensitive.
    """
    if not isinstance(left, str):
        return False
    if chain is Chain.SOLANA:
        return left == right
    if chain is Chain.SUI:
        left_package, _, left_type = left.partition("::")
        right_package, _, right_type = right.partition("::")
        return left_package.lower() == right_package.lower() and left_type == right_type
    return left.lower() == right.lower()


def best_pair(pairs: Any, chain_id: str, address: str, chain: Chain) -> dict[str, Any] | None:
    """Return the most liquid pair on ``chain_id`` whose base token is ``address``, or None."""
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
    """Return a readable DEX name with the pair's labels, e.g. "Uniswap v3", or "DEX"."""
    name = str(pair.get("dexId") or "").replace("-", " ").replace("_", " ").title()
    labels = [str(label) for label in pair.get("labels") or [] if isinstance(label, str | int)]
    return " ".join([name, *labels]).strip() or "DEX"


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
        """Create one RPC client per EVM network, plus one for Solana and one for Sui."""
        self._http = http
        self._timeout = timeout
        self._tuning = tuning
        self._evm = {net.name: JsonRpcClient(http, net.rpc_url, timeout) for net in evm_networks}
        self._solana = JsonRpcClient(http, solana_rpc_url, timeout)
        self._sui = JsonRpcClient(http, sui_rpc_url, timeout)

    async def enrich(self, contract: ConfirmedContract) -> TokenInfo:
        """Collect what can be learned about the token within ``enrich_timeout`` seconds.

        Each source has its own budget, so a slow network in "also on" costs neither the age
        line nor the name or the market. A bare Sui package whose coin type the validator
        did not pass on needs it before the market lookup, which then gets what is left. The
        chain's facts win over DexScreener's.
        """
        budget = float(self._tuning.enrich_timeout)
        if budget <= 0:
            return TokenInfo()
        value = contract.address.value
        bare_package = contract.address.chain is Chain.SUI and not is_sui_coin_type(value)
        if bare_package and not contract.coin_type:
            started = time.monotonic()
            on_chain = await self._bounded(self._chain_details(contract), budget, "chain")
            coin_type = on_chain.get("coin_type")
            if not coin_type:
                return TokenInfo(**on_chain)
            left = max(budget - (time.monotonic() - started), 0.2)
            market = await self._bounded(self._market_details(contract, coin_type), left, "market")
        else:
            on_chain, market, age, others = await asyncio.gather(
                self._bounded(self._chain_details(contract), budget, "chain"),
                self._bounded(self._market_details(contract, contract.coin_type), budget, "market"),
                self._bounded(self._age(contract), budget, "age"),
                # We keep the networks that answer in time. The outer bound is just a safety net.
                self._bounded(
                    self._also_on(contract, max(budget - _ALSO_ON_MARGIN, 0.1)),
                    budget,
                    "other networks",
                ),
            )
            on_chain = {**age, **others, **on_chain}
        return TokenInfo(**{**market, **on_chain})

    @staticmethod
    async def _bounded(coro: Any, budget: float, source: str) -> dict[str, Any]:
        """Run one source within ``budget`` seconds; return its details, or {} if it fails.

        Any error is only logged: an odd payload must not cost the alert.
        """
        try:
            return await asyncio.wait_for(coro, budget)
        except TimeoutError:
            log.info("token details: %s source did not answer within %.1fs", source, budget)
        except (RpcError, httpx.HTTPError, ValueError) as exc:
            log.info("token details: %s source failed: %s", source, exc)
        except Exception:
            log.warning("token details: %s source raised", source, exc_info=True)
        return {}

    async def _chain_details(self, contract: ConfirmedContract) -> dict[str, Any]:
        """Return what the token's own chain says about it: name, symbol, supply and so on."""
        if contract.address.chain is Chain.ETHEREUM:
            client = self._evm.get(contract.network)
            return await self._erc20_details(client, contract.address.value) if client else {}
        if contract.address.chain is Chain.SOLANA:
            return await self._solana_details(contract.address.value)
        return await self._sui_details(self._sui, contract.address.value, contract.coin_type)

    @staticmethod
    async def _erc20_details(client: JsonRpcClient, address: str) -> dict[str, Any]:
        """Read name, symbol, decimals and total supply; calls that fail are left out."""
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
        """Read the mint and its Metaplex metadata account and return what they say.

        Name and symbol fall back to the Token-2022 metadata extension. Reads at "confirmed",
        like the validator: at "finalized" a fresh mint does not exist yet.
        """
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
        name, symbol = token_names(mint_data, account_data(metadata_account))
        if name:
            details["name"] = name
        if symbol:
            details["symbol"] = symbol
        return details

    @staticmethod
    async def _sui_details(
        client: JsonRpcClient, value: str, coin_type: str | None = None
    ) -> dict[str, Any]:
        """Return the Sui coin's name, symbol, decimals and total supply.

        A bare package id is first resolved to the coin it defines, unless ``coin_type``
        already names it; that coin type is returned too.
        """
        if coin_type is None and not is_sui_coin_type(value):
            found = await find_coin(client, value)
            if found is None:
                return {}
            coin_type, metadata = found
        else:
            coin_type = coin_type or value
            metadata = await coin_metadata(client, coin_type)
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

    async def _age(self, contract: ConfirmedContract) -> dict[str, Any]:
        """Return how old the token is: an old token on a launch page is worth a second look."""
        if contract.address.chain is Chain.SOLANA:
            return await self._solana_age(contract.address.value)
        if contract.address.chain is not Chain.ETHEREUM:
            return {}
        client = self._evm.get(contract.network)
        if client is None:
            return {}
        return await self._evm_age(client, contract.address.value)

    async def _evm_age(self, client: JsonRpcClient, address: str) -> dict[str, Any]:
        """Bound the contract's age by looking for its code ``_AGE_BLOCKS`` blocks back.

        Code there means it is older than that window, none means it was deployed within it.
        Returns {} when the node has already pruned that state.
        """
        head = int(await client.call("eth_blockNumber", []), 16)
        back = hex(max(head - _AGE_BLOCKS, 0))
        code, block = await client.call_batch(
            [("eth_getCode", [address, back]), ("eth_getBlockByNumber", [back, False])]
        )
        if not isinstance(code, str) or not isinstance(block, dict):
            return {}
        window = max(time.time() - int(block["timestamp"], 16), 0.0)
        if not has_code(code):
            return {"deployed_within": window}
        return {"deployed_before": window}

    async def _also_on(
        self, contract: ConfirmedContract, budget: float | None = None
    ) -> dict[str, Any]:
        """Return the other EVM networks where the address is a token too, with its symbol.

        Every network gets one batch, all at once; one that has not answered within
        ``budget`` seconds is left out, so a slow node never costs the others. A symbol
        that cannot be read leaves just the network's name.
        """
        if contract.address.chain is not Chain.ETHEREUM or contract.network not in self._evm:
            return {}
        tasks = [
            asyncio.ensure_future(self._token_on(name, client, contract.address.value))
            for name, client in self._evm.items()
            if name != contract.network
        ]
        if not tasks:
            return {}
        done, pending = await asyncio.wait(tasks, timeout=budget)
        for task in pending:
            task.cancel()
        found = tuple(
            answer
            for task in tasks
            if task in done and task.exception() is None and (answer := task.result())
        )
        return {"also_on": found} if found else {}

    @staticmethod
    async def _token_on(
        name: str, client: JsonRpcClient, address: str
    ) -> tuple[str, str | None] | None:
        """Return (network, symbol) when ``address`` is a token on this network, else None.

        It is one when its bytecode is no delegated wallet and it answers totalSupply() and
        decimals(), the validator's test for a token.
        """
        code, supply, decimals, symbol = await client.call_batch(
            [
                ("eth_getCode", [address, "latest"]),
                *(
                    ("eth_call", [{"to": address, "data": selector}, "latest"])
                    for selector in (ERC20_TOTAL_SUPPLY, ERC20_DECIMALS, ERC20_SYMBOL)
                ),
            ]
        )
        if not has_code(code) or is_delegated_wallet(code):
            return None
        return (name, decode_abi_string(symbol)) if looks_like_token(supply, decimals) else None

    async def _solana_age(self, mint: str) -> dict[str, Any]:
        """Return when the mint's first transaction happened, or ``busy`` when it has too many.

        One page of signatures is read; a full page means the first one is out of reach.
        """
        signatures = await self._solana.call(
            "getSignaturesForAddress", [mint, {"limit": _SIGNATURE_PAGE, "commitment": "confirmed"}]
        )
        if not isinstance(signatures, list) or not signatures:
            return {}
        if len(signatures) >= _SIGNATURE_PAGE:
            return {"busy": True}
        first = signatures[-1].get("blockTime") if isinstance(signatures[-1], dict) else None
        return {"created_at": float(first)} if isinstance(first, int | float) else {}

    async def _market_details(
        self, contract: ConfirmedContract, identifier: str | None = None
    ) -> dict[str, Any]:
        """Return the token's best DexScreener pair: DEX, price, liquidity, FDV and age.

        ``identifier`` replaces the address in the lookup (a Sui coin type found behind a
        package). Name and symbol are included as a fallback for missing chain metadata.
        """
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
        for key in ("name", "symbol"):
            value = base.get(key)
            if isinstance(value, str) and clean_label(value):
                details[key] = clean_label(value)
        return {key: value for key, value in details.items() if value is not None}
