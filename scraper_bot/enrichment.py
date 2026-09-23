"""Token details for a confirmed contract: on-chain metadata, DEX market data, links.

Everything here is best effort and bounded by ``enrich_timeout``: a slow or failing
source only makes the alert shorter, never later than the budget and never missing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import base58
import httpx

from scraper_bot.address_extractor import is_sui_coin_type
from scraper_bot.config import EvmNetwork
from scraper_bot.evm import ERC20_DECIMALS, ERC20_NAME, ERC20_SYMBOL, ERC20_TOTAL_SUPPLY
from scraper_bot.models import Chain, ConfirmedContract
from scraper_bot.preferences import Tuning
from scraper_bot.rpc import JsonRpcClient, RpcError
from scraper_bot.solana import (
    METAPLEX_METADATA_PROGRAM,
    MINT_SIZE,
    TOKEN_2022_MINT_TYPE,
    TOKEN_ACCOUNT_SIZE,
    account_data,
)
from scraper_bot.sui import coin_metadata, find_coin, total_supply

log = logging.getLogger(__name__)

DEXSCREENER_API = "https://api.dexscreener.com"
_MAX_DECIMALS = 36
_MAX_TEXT = 64
_TOKEN_METADATA_EXTENSION = 19


# ── links ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class NetworkLinks:
    explorer_name: str
    explorer: str  # token page, ``{address}`` placeholder
    dex_chain: str  # DexScreener chain id
    swap_name: str | None = None
    swap: str | None = None


_UNISWAP = "https://app.uniswap.org/swap?outputCurrency={address}&chain={chain}"

NETWORK_LINKS: dict[str, NetworkLinks] = {
    "Ethereum": NetworkLinks(
        "Etherscan",
        "https://etherscan.io/token/{address}",
        "ethereum",
        "Uniswap",
        _UNISWAP.format(address="{address}", chain="ethereum"),
    ),
    "Base": NetworkLinks(
        "Basescan",
        "https://basescan.org/token/{address}",
        "base",
        "Uniswap",
        _UNISWAP.format(address="{address}", chain="base"),
    ),
    "Arbitrum": NetworkLinks(
        "Arbiscan",
        "https://arbiscan.io/token/{address}",
        "arbitrum",
        "Uniswap",
        _UNISWAP.format(address="{address}", chain="arbitrum"),
    ),
    "Optimism": NetworkLinks(
        "Etherscan",
        "https://optimistic.etherscan.io/token/{address}",
        "optimism",
        "Uniswap",
        _UNISWAP.format(address="{address}", chain="optimism"),
    ),
    "BSC": NetworkLinks(
        "BscScan",
        "https://bscscan.com/token/{address}",
        "bsc",
        "PancakeSwap",
        "https://pancakeswap.finance/swap?outputCurrency={address}",
    ),
    "Fantom": NetworkLinks("FtmScan", "https://ftmscan.com/token/{address}", "fantom"),
    "Sonic": NetworkLinks("SonicScan", "https://sonicscan.org/token/{address}", "sonic"),
    "Avalanche": NetworkLinks(
        "Snowtrace",
        "https://snowtrace.io/token/{address}",
        "avalanche",
        "Uniswap",
        _UNISWAP.format(address="{address}", chain="avalanche"),
    ),
    "Solana": NetworkLinks(
        "Solscan",
        "https://solscan.io/token/{address}",
        "solana",
        "Jupiter",
        "https://jup.ag/swap/SOL-{address}",
    ),
    "Sui": NetworkLinks("Suiscan", "https://suiscan.xyz/mainnet/coin/{address}", "sui"),
}


def token_links(
    contract: ConfirmedContract, identifier: str | None = None
) -> list[tuple[str, str]]:
    """(label, url) pairs: explorer, DexScreener and, where known, a swap page.

    Every URL carries only the token's own identifier, never a pair address, so a bot
    that parses the chat for addresses cannot pick up a second contract.
    """
    links = NETWORK_LINKS.get(contract.network)
    if links is None:
        return []
    address = identifier or contract.address.value
    if contract.address.chain is Chain.SUI and not is_sui_coin_type(address):
        return [("Suiscan", f"https://suiscan.xyz/mainnet/object/{address}")]
    out = [
        (links.explorer_name, links.explorer.format(address=address)),
        ("DexScreener", f"https://dexscreener.com/{links.dex_chain}/{address}"),
    ]
    if links.swap and links.swap_name:
        out.append((links.swap_name, links.swap.format(address=address)))
    return out


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

    @property
    def supply(self) -> float | None:
        if self.total_supply is None or self.decimals is None:
            return None
        return self.total_supply / 10**self.decimals


# ── formatting ──────────────────────────────────────────────────────────────
def _short(value: float) -> str:
    if abs(value) >= 100:
        text = f"{value:.0f}"
    elif abs(value) >= 10:
        text = f"{value:.1f}"
    else:
        text = f"{value:.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_amount(value: float) -> str:
    """``1.23K``, ``12.4M``, ``5B``; plain numbers below a thousand."""
    for unit, threshold in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= threshold:
            return f"{_short(value / threshold)}{unit}"
    return _short(value)


def format_usd(value: float) -> str:
    if value >= 1000:
        return f"${format_amount(value)}"
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.0001:
        return f"${value:.4g}"
    return f"${value:.2g}"


# ── EVM ABI decoding ────────────────────────────────────────────────────────
def _clean(text: str) -> str | None:
    cleaned = " ".join(text.replace("\x00", " ").split())
    return cleaned[:_MAX_TEXT] or None


def decode_abi_string(result: Any) -> str | None:
    """Decode a ``string`` return value; ``bytes32`` symbols (MKR-style) are accepted."""
    if not isinstance(result, str) or not result.startswith("0x"):
        return None
    try:
        data = bytes.fromhex(result[2:])
    except ValueError:
        return None
    if not data:
        return None
    if len(data) == 32:
        return _clean(data.rstrip(b"\x00").decode("utf-8", "replace"))
    if len(data) < 64:
        return None
    offset = int.from_bytes(data[:32], "big")
    if offset + 32 > len(data):
        return None
    length = int.from_bytes(data[offset : offset + 32], "big")
    chunk = data[offset + 32 : offset + 32 + length]
    if len(chunk) < length:
        return None
    return _clean(chunk.decode("utf-8", "replace"))


def decode_abi_uint(result: Any, *, limit: int | None = None) -> int | None:
    if not isinstance(result, str) or not result.startswith("0x") or len(result) < 3:
        return None
    try:
        value = int(result, 16)
    except ValueError:
        return None
    if limit is not None and value > limit:
        return None
    return value


# ── Solana: PDA derivation and account layouts ──────────────────────────────
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def is_on_curve(point: bytes) -> bool:
    """True when the 32 bytes decompress to an ed25519 point (i.e. could hold a key)."""
    y = int.from_bytes(point, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return False
    y2 = y * y % _P
    x2 = (y2 - 1) * pow(_D * y2 + 1, _P - 2, _P) % _P
    if x2 == 0:
        return sign == 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    return (x * x - x2) % _P == 0


def find_program_address(seeds: Sequence[bytes], program_id: bytes) -> tuple[bytes, int]:
    """Solana's ``Pubkey::find_program_address``: first off-curve hash, bump from 255 down."""
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(
            b"".join(seeds) + bytes([bump]) + program_id + b"ProgramDerivedAddress"
        ).digest()
        if not is_on_curve(digest):
            return digest, bump
    raise ValueError("no program address found")


def metadata_address(mint: str) -> str:
    """The Metaplex Token Metadata account of ``mint``."""
    program = base58.b58decode(METAPLEX_METADATA_PROGRAM)
    pda, _ = find_program_address([b"metadata", program, base58.b58decode(mint)], program)
    return base58.b58encode(pda).decode("ascii")


def parse_mint(data: bytes) -> tuple[int, int] | None:
    """(supply, decimals) of an SPL / Token-2022 mint account."""
    if len(data) < MINT_SIZE:
        return None
    return int.from_bytes(data[36:44], "little"), data[44]


def _read_borsh_string(data: bytes, pos: int) -> tuple[str | None, int]:
    if pos + 4 > len(data):
        return None, len(data)
    length = int.from_bytes(data[pos : pos + 4], "little")
    raw = data[pos + 4 : pos + 4 + length]
    if len(raw) < length:
        return None, len(data)
    return raw.decode("utf-8", "replace"), pos + 4 + length


def parse_metaplex_metadata(data: bytes) -> tuple[str | None, str | None]:
    """(name, symbol) from a Metaplex metadata account: key, authority, mint, then strings."""
    name, pos = _read_borsh_string(data, 1 + 32 + 32)
    symbol, _ = _read_borsh_string(data, pos)
    return _clean(name or ""), _clean(symbol or "")


def parse_token2022_metadata(data: bytes) -> tuple[str | None, str | None]:
    """(name, symbol) from the TokenMetadata extension embedded in a Token-2022 mint."""
    if len(data) <= TOKEN_ACCOUNT_SIZE + 1 or data[TOKEN_ACCOUNT_SIZE] != TOKEN_2022_MINT_TYPE:
        return None, None
    pos = TOKEN_ACCOUNT_SIZE + 1
    while pos + 4 <= len(data):
        kind = int.from_bytes(data[pos : pos + 2], "little")
        length = int.from_bytes(data[pos + 2 : pos + 4], "little")
        body = data[pos + 4 : pos + 4 + length]
        if kind == _TOKEN_METADATA_EXTENSION:
            name, after = _read_borsh_string(body, 32 + 32)  # update authority, mint
            symbol, _ = _read_borsh_string(body, after)
            return _clean(name or ""), _clean(symbol or "")
        if kind == 0 and length == 0:
            break  # padding
        pos += 4 + length
    return None, None


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
            on_chain, market = await asyncio.gather(
                self._bounded(self._chain_details(contract), budget, "chain"),
                self._bounded(self._market_details(contract), budget, "market"),
            )
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
        params = {"encoding": "base64"}
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
                if isinstance(text, str) and _clean(text):
                    details[key] = _clean(text)
            decimals = metadata.get("decimals")
            if isinstance(decimals, int) and 0 <= decimals <= _MAX_DECIMALS:
                details["decimals"] = decimals
        supply = await total_supply(client, coin_type)
        if supply is not None:
            details["total_supply"] = supply
        return details

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
            if isinstance(value, str) and _clean(value):
                details[key] = _clean(value)
        return {key: value for key, value in details.items() if value is not None}
