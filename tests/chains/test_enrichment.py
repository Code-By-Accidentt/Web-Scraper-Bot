import asyncio
import base64
import json
import logging
import time
from typing import Any

import base58
import httpx
import pytest

from scraper_bot.chains.enrichment import (
    TokenEnricher,
    TokenInfo,
    decode_abi_string,
    decode_abi_uint,
    format_amount,
    format_usd,
    is_on_curve,
    metadata_address,
    parse_metaplex_metadata,
    parse_mint,
    parse_token2022_metadata,
    token_links,
)
from scraper_bot.config import EvmNetwork
from scraper_bot.models import Address, Chain, ConfirmedContract
from tests.conftest import FakeTuning

USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_METADATA = "5x38Kp4hvdomTCnCrAny4UtMUt5rQBdB6px2K1Ui45Wq"
ETH_CONTRACT = ConfirmedContract(Address(Chain.ETHEREUM, USDC_ETH), "Ethereum")
SOL_CONTRACT = ConfirmedContract(Address(Chain.SOLANA, USDC_SOL), "Solana")
NETWORKS = (EvmNetwork("Ethereum", "https://alpha.rpc"),)


# ── encoders for fixtures ───────────────────────────────────────────────────
def abi_uint(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


def abi_string(value: str) -> str:
    raw = value.encode()
    padded = raw + b"\x00" * (-len(raw) % 32)
    head = (32).to_bytes(32, "big") + len(raw).to_bytes(32, "big")
    return "0x" + (head + padded).hex()


def abi_bytes32(value: str) -> str:
    return "0x" + value.encode().ljust(32, b"\x00").hex()


def borsh(value: str, pad_to: int = 0) -> bytes:
    raw = value.encode().ljust(pad_to, b"\x00")
    return len(raw).to_bytes(4, "little") + raw


MINT = bytes(36) + (123_456).to_bytes(8, "little") + bytes([6, 1]) + bytes(36)
METAPLEX = bytes([4]) + bytes(64) + borsh("USD Coin", 32) + borsh("USDC", 10) + borsh("", 200)
_T22_BODY = bytes(64) + borsh("PayPal USD") + borsh("PYUSD") + borsh("https://x")
TOKEN_2022 = (
    MINT.ljust(165, b"\x00")
    + b"\x01"
    + (7).to_bytes(2, "little")  # some other extension first
    + (2).to_bytes(2, "little")
    + b"\x00\x00"
    + (19).to_bytes(2, "little")
    + len(_T22_BODY).to_bytes(2, "little")
    + _T22_BODY
)


def pair(
    *,
    chain: str = "ethereum",
    base: str = USDC_ETH,
    quote: str = "WETH",
    liquidity: float | None = 1000.0,
    dex: str = "uniswap",
    labels: tuple[str, ...] = ("v3",),
) -> dict[str, Any]:
    return {
        "chainId": chain,
        "dexId": dex,
        "labels": list(labels),
        "url": f"https://dexscreener.com/{chain}/pair-{liquidity}",
        "baseToken": {"address": base, "name": "USD Coin", "symbol": "USDC"},
        "quoteToken": {"address": "0xquote", "symbol": quote},
        "priceUsd": "1.0",
        "liquidity": {"usd": liquidity},
        "fdv": 5_000_000_000,
        "pairCreatedAt": 1_000_000,
    }


class Backend:
    """Fake RPC nodes plus a fake DexScreener behind one httpx transport."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.pairs: Any = []
        self.dex_status = 200
        self.dex_delay = 0.0
        self.rpc_down = False
        self.sol_accounts: dict[str, bytes] = {}
        self.sui_coins: dict[str, dict[str, Any]] = {}
        self.sui_packages: dict[str, dict[str, list[str]]] = {}
        self.fresh = False  # EVM: no code 90 blocks ago
        self.block_age = 180  # EVM: seconds since the block 90 blocks back
        self.sol_signatures: Any = []

    def sui(self, method: str, params: list[Any]) -> Any:
        target = params[0]
        if method == "suix_getCoinMetadata":
            return self.sui_coins.get(target)
        if method == "suix_getTotalSupply":
            return {"value": "734000000"} if target in self.sui_coins else None
        if method == "sui_getObject":
            if target in self.sui_packages:
                return {"data": {"objectId": target, "type": "package"}}
            return {"error": {"code": "notExists", "object_id": target}}
        if method == "sui_getNormalizedMoveModulesByPackage":
            modules = self.sui_packages.get(target, {})
            return {m: {"structs": {s: {} for s in structs}} for m, structs in modules.items()}
        raise ValueError(method)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append((request.method, host, request.url.path))
        if host == "api.dexscreener.com":
            if self.dex_delay:
                await asyncio.sleep(self.dex_delay)
            # Hand-encoded so that NaN/Infinity tokens (json.dumps emits them) get through.
            body = json.dumps(self.pairs).encode()
            return httpx.Response(
                self.dex_status, content=body, headers={"content-type": "application/json"}
            )
        if self.rpc_down:
            raise httpx.ConnectError("down", request=request)
        body = json.loads(request.content)
        items = body if isinstance(body, list) else [body]
        answers = []
        for item in items:
            try:
                result = self.answer(host, item["method"], item["params"])
            except LookupError:
                raise httpx.ConnectError("unknown host", request=request) from None
            answers.append({"jsonrpc": "2.0", "id": item["id"], "result": result})
        return httpx.Response(200, json=answers if isinstance(body, list) else answers[0])

    def answer(self, host: str, method: str, params: list[Any]) -> Any:
        if host == "alpha.rpc":
            if method == "eth_blockNumber":
                return hex(10_000)
            if method == "eth_getBlockByNumber":
                return {"timestamp": hex(int(time.time()) - self.block_age)}
            if method == "eth_getCode":
                return "0x" if self.fresh else "0x6080"
            return {
                "0x06fdde03": abi_string("USD Coin"),
                "0x95d89b41": abi_bytes32("USDC"),
                "0x313ce567": abi_uint(6),
                "0x18160ddd": abi_uint(50_000_000 * 10**6),
            }[params[0]["data"]]
        if host == "sol.rpc":
            if method == "getSignaturesForAddress":
                return self.sol_signatures
            data = self.sol_accounts.get(params[0])
            value = None
            if data is not None:
                encoded = base64.b64encode(data).decode()
                value = {"executable": False, "owner": "x", "data": [encoded, "base64"]}
            return {"context": {"slot": 1}, "value": value}
        if host == "sui.rpc":
            return self.sui(method, params)
        raise LookupError(host)


def make(backend: Backend, **tuning: Any) -> TokenEnricher:
    http = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    return TokenEnricher(
        http,
        NETWORKS,
        "https://sol.rpc",
        timeout=5,
        tuning=FakeTuning(**tuning),
        sui_rpc_url="https://sui.rpc",
    )


# ── decoders and parsers ────────────────────────────────────────────────────
def test_abi_decoders() -> None:
    assert decode_abi_string(abi_string("USD Coin")) == "USD Coin"
    assert decode_abi_string(abi_bytes32("MKR")) == "MKR"
    assert decode_abi_string(abi_string("  padded\x00 name  ")) == "padded name"
    assert decode_abi_string("0x") is None
    assert decode_abi_string("0x" + "ff" * 64) is None  # offset beyond the payload
    assert decode_abi_string(RuntimeError("reverted")) is None
    assert decode_abi_string("0xzz") is None
    assert decode_abi_uint(abi_uint(6)) == 6
    assert decode_abi_uint(abi_uint(300), limit=36) is None
    assert decode_abi_uint("0x") is None and decode_abi_uint("nope") is None


def test_metadata_pda_matches_the_known_usdc_account() -> None:
    assert metadata_address(USDC_SOL) == USDC_METADATA
    assert is_on_curve(base58.b58decode(USDC_SOL))  # a real key pair
    assert not is_on_curve(base58.b58decode(USDC_METADATA))  # a program-derived address


def test_solana_account_parsers() -> None:
    assert parse_mint(MINT) == (123_456, 6)
    assert parse_mint(b"short") is None
    assert parse_metaplex_metadata(METAPLEX) == ("USD Coin", "USDC")
    assert parse_metaplex_metadata(b"\x04") == (None, None)
    assert parse_token2022_metadata(TOKEN_2022) == ("PayPal USD", "PYUSD")
    assert parse_token2022_metadata(MINT) == (None, None)
    assert parse_token2022_metadata(MINT.ljust(165, b"\x00") + b"\x02") == (None, None)


def test_format_helpers() -> None:
    assert format_amount(1_234) == "1.23K"
    assert format_amount(12_400_000) == "12.4M"
    assert format_amount(5e9) == "5B"
    assert format_amount(49_951_489_980.6) == "50B"
    assert format_amount(999) == "999" and format_amount(0.5) == "0.5"
    assert format_usd(1.0) == "$1.00"
    assert format_usd(12_400_000) == "$12.4M"
    assert format_usd(0.0009773) == "$0.0009773"
    assert format_usd(0.00000012) == "$1.2e-07"


def test_links_per_network() -> None:
    assert token_links(ETH_CONTRACT) == [
        ("Etherscan", f"https://etherscan.io/token/{USDC_ETH}"),
        ("DexScreener", f"https://dexscreener.com/ethereum/{USDC_ETH}"),
        ("Uniswap", f"https://app.uniswap.org/swap?outputCurrency={USDC_ETH}&chain=ethereum"),
    ]
    assert [label for label, _ in token_links(SOL_CONTRACT)] == [
        "Solscan",
        "DexScreener",
        "Jupiter",
    ]
    bsc = ConfirmedContract(ETH_CONTRACT.address, "BSC")
    assert [label for label, _ in token_links(bsc)] == ["BscScan", "DexScreener", "PancakeSwap"]
    fantom = ConfirmedContract(ETH_CONTRACT.address, "Fantom")
    assert [label for label, _ in token_links(fantom)] == ["FtmScan", "DexScreener"]
    assert token_links(ConfirmedContract(ETH_CONTRACT.address, "Nowhere")) == []


# ── the enricher ────────────────────────────────────────────────────────────
async def test_evm_enrichment_combines_chain_and_market_data() -> None:
    backend = Backend()
    backend.pairs = [
        pair(liquidity=10),
        pair(liquidity=500, quote="USDT"),
        pair(chain="pulsechain", liquidity=1e9),  # same address on another chain
        pair(base="0xother", liquidity=1e9),  # our token is only the quote side there
    ]
    info = await make(backend).enrich(ETH_CONTRACT)
    assert (info.name, info.symbol, info.decimals) == ("USD Coin", "USDC", 6)
    assert info.total_supply == 50_000_000 * 10**6 and info.supply == 50_000_000
    assert info.market_checked and info.dex == "Uniswap v3" and info.pair == "USDC/USDT"
    assert info.pair_url == "https://dexscreener.com/ethereum/pair-500"
    assert (info.price_usd, info.liquidity_usd, info.fdv_usd) == (1.0, 500.0, 5e9)
    assert info.pair_created_at == 1000.0
    assert ("GET", "api.dexscreener.com", f"/tokens/v1/ethereum/{USDC_ETH}") in backend.calls
    # four ERC-20 calls, then the age: the head block and one batch 90 blocks back
    assert sum(1 for _, host, _ in backend.calls if host == "alpha.rpc") == 6
    assert info.deployed_before is not None and 170 <= info.deployed_before <= 190


async def test_solana_enrichment_reads_mint_and_metaplex_metadata() -> None:
    backend = Backend()
    backend.sol_accounts = {USDC_SOL: MINT, USDC_METADATA: METAPLEX}
    info = await make(backend).enrich(SOL_CONTRACT)
    assert (info.name, info.symbol, info.decimals, info.total_supply) == (
        "USD Coin",
        "USDC",
        6,
        123_456,
    )
    assert info.market_checked and info.pair_url is None and info.dex is None


async def test_token2022_metadata_and_dexscreener_name_fallback() -> None:
    backend = Backend()
    backend.sol_accounts = {USDC_SOL: TOKEN_2022}
    info = await make(backend).enrich(SOL_CONTRACT)
    assert (info.name, info.symbol, info.decimals) == ("PayPal USD", "PYUSD", 6)

    backend.sol_accounts = {USDC_SOL: MINT}  # no metadata anywhere on chain
    backend.pairs = [pair(chain="solana", base=USDC_SOL, dex="raydium", labels=())]
    info = await make(backend).enrich(SOL_CONTRACT)
    assert (info.name, info.symbol, info.dex) == ("USD Coin", "USDC", "Raydium")


async def test_failures_only_shorten_the_details() -> None:
    backend = Backend()
    backend.rpc_down = True
    backend.pairs = [pair()]
    info = await make(backend).enrich(ETH_CONTRACT)
    assert info.decimals is None and info.total_supply is None
    assert info.pair == "USDC/WETH" and info.name == "USD Coin"  # DexScreener's copy

    backend = Backend()
    backend.dex_status = 500
    info = await make(backend).enrich(ETH_CONTRACT)
    assert info.name == "USD Coin" and info.decimals == 6
    assert not info.market_checked and info.pair is None


async def test_zero_budget_skips_every_lookup() -> None:
    backend = Backend()
    assert await make(backend, enrich_timeout=0).enrich(ETH_CONTRACT) == TokenInfo()
    assert backend.calls == []


async def test_slow_sources_are_cut_off_at_the_budget() -> None:
    backend = Backend()
    backend.dex_delay = 0.5
    backend.pairs = [pair()]
    started = time.perf_counter()
    info = await make(backend, enrich_timeout=0.05).enrich(ETH_CONTRACT)
    assert time.perf_counter() - started < 0.4
    assert info.name == "USD Coin" and not info.market_checked


SUI_PKG = "0x" + "ab" * 32
SUI_COIN = f"{SUI_PKG}::usdc::USDC"
SUI_CONTRACT = ConfirmedContract(Address(Chain.SUI, SUI_COIN), "Sui")
SUI_PACKAGE_CONTRACT = ConfirmedContract(Address(Chain.SUI, SUI_PKG), "Sui")


async def test_sui_coin_type_enrichment() -> None:
    backend = Backend()
    backend.sui_coins = {SUI_COIN: {"name": "USD Coin", "symbol": "USDC", "decimals": 6}}
    backend.pairs = [pair(chain="sui", base=SUI_COIN, quote="SUI", dex="cetus", labels=())]
    info = await make(backend).enrich(SUI_CONTRACT)
    assert (info.name, info.symbol, info.decimals) == ("USD Coin", "USDC", 6)
    assert info.total_supply == 734_000_000 and info.coin_type is None
    assert (info.dex, info.pair) == ("Cetus", "USDC/SUI")
    assert ("GET", "api.dexscreener.com", f"/tokens/v1/sui/{SUI_COIN}") in backend.calls
    assert token_links(SUI_CONTRACT) == [
        ("Suiscan", f"https://suiscan.xyz/mainnet/coin/{SUI_COIN}"),
        ("DexScreener", f"https://dexscreener.com/sui/{SUI_COIN}"),
    ]


async def test_sui_package_is_resolved_to_its_coin() -> None:
    backend = Backend()
    backend.sui_coins = {SUI_COIN: {"name": "USD Coin", "symbol": "USDC", "decimals": 6}}
    backend.sui_packages = {SUI_PKG: {"helpers": ["Config"], "usdc": ["USDC"]}}
    backend.pairs = [pair(chain="sui", base=SUI_COIN, quote="SUI", dex="cetus", labels=())]
    info = await make(backend).enrich(SUI_PACKAGE_CONTRACT)
    assert info.coin_type == SUI_COIN and info.symbol == "USDC" and info.pair == "USDC/SUI"
    assert token_links(SUI_PACKAGE_CONTRACT, info.coin_type)[0] == (
        "Suiscan",
        f"https://suiscan.xyz/mainnet/coin/{SUI_COIN}",
    )
    assert token_links(SUI_PACKAGE_CONTRACT) == [
        ("Suiscan", f"https://suiscan.xyz/mainnet/object/{SUI_PKG}")
    ]
    backend.sui_packages = {SUI_PKG: {"helpers": ["Config"]}}  # no coin inside
    assert await make(backend).enrich(SUI_PACKAGE_CONTRACT) == TokenInfo()


async def test_non_finite_market_numbers_are_dropped() -> None:
    backend = Backend()
    odd = {**pair(), "priceUsd": "NaN", "fdv": "Infinity", "pairCreatedAt": float("nan")}
    backend.pairs = [odd]
    info = await make(backend).enrich(ETH_CONTRACT)
    assert info.pair == "USDC/WETH" and info.liquidity_usd == 1000.0
    assert (info.price_usd, info.fdv_usd, info.pair_created_at) == (None, None, None)


def test_sui_types_match_case_sensitively_except_for_the_package() -> None:
    from scraper_bot.chains.enrichment import best_pair

    other_case = SUI_COIN.replace("::USDC", "::Usdc")
    assert best_pair([pair(chain="sui", base=other_case)], "sui", SUI_COIN, Chain.SUI) is None
    shouting = "0x" + SUI_PKG[2:].upper() + "::usdc::USDC"
    assert best_pair([pair(chain="sui", base=shouting)], "sui", SUI_COIN, Chain.SUI) is not None


def test_abi_decoder_edge_cases() -> None:
    head = (32).to_bytes(32, "big") + (100).to_bytes(32, "big")  # offset 32, length 100
    assert decode_abi_string("0x" + "00" * 40) is None  # neither bytes32 nor a full head
    assert decode_abi_string("0x" + (head + b"short").hex()) is None  # chunk shorter than said
    assert decode_abi_string("0x" + "00" * 64) is None  # a well-formed empty string
    assert decode_abi_string("0x" + "00" * 32) is None  # a blank bytes32
    assert decode_abi_string(abi_string("x" * 200)) == "x" * 64  # long names are cut
    assert decode_abi_uint("0xzz") is None
    assert decode_abi_uint(None) is None and decode_abi_uint(b"0x1") is None
    assert decode_abi_uint("0x0") == 0
    assert decode_abi_uint(abi_uint(36), limit=36) == 36
    assert decode_abi_uint(abi_uint(37), limit=36) is None


def test_token2022_edge_cases() -> None:
    padded = MINT.ljust(165, b"\x00")
    metadata = (19).to_bytes(2, "little") + len(_T22_BODY).to_bytes(2, "little") + _T22_BODY
    other = (7).to_bytes(2, "little") + (2).to_bytes(2, "little") + b"\x00\x00"
    nothing = (None, None)
    assert parse_token2022_metadata(padded + b"\x01") == nothing  # no extensions at all
    assert parse_token2022_metadata(padded + b"\x01" + other) == nothing  # no metadata one
    assert parse_token2022_metadata(padded + b"\x01" + other + metadata) == ("PayPal USD", "PYUSD")
    # The (0, 0) padding sentinel ends the walk even when bytes follow it.
    assert parse_token2022_metadata(padded + b"\x01" + bytes(4) + metadata) == nothing
    # Account-type byte 2 is a token account, whatever comes after it.
    assert parse_token2022_metadata(padded + b"\x02" + metadata) == nothing
    assert parse_token2022_metadata(padded + b"\x01" + metadata[:3]) == nothing  # cut header
    assert parse_token2022_metadata(padded + b"\x01" + metadata[:70]) == nothing  # cut body


async def test_market_lookup_edge_cases() -> None:
    from scraper_bot.chains.enrichment import _number, best_pair

    backend = Backend()
    backend.pairs = {"pairs": [pair()]}  # an object where a list is expected
    info = await make(backend).enrich(ETH_CONTRACT)
    assert not info.market_checked and info.pair is None
    assert info.decimals == 6  # the chain data is unaffected

    backend = Backend()
    backend.pairs = [{**pair(), "baseToken": {"address": 12345, "symbol": "USDC"}}]
    info = await make(backend).enrich(ETH_CONTRACT)
    assert info.market_checked and info.pair is None and info.dex is None

    backend = Backend()
    info = await make(backend).enrich(ConfirmedContract(ETH_CONTRACT.address, "Nowhere"))
    assert info == TokenInfo()
    assert backend.calls == []  # neither an RPC node nor DexScreener is known for it

    assert best_pair({"pairs": []}, "ethereum", USDC_ETH, Chain.ETHEREUM) is None
    assert best_pair([None, "x", 1], "ethereum", USDC_ETH, Chain.ETHEREUM) is None
    assert _number(True) is None and _number(False) is None
    assert _number("12.5") == 12.5 and _number(3) == 3.0
    assert _number("abc") is None and _number([1]) is None and _number(None) is None


async def test_unexpected_exceptions_in_a_source_are_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(self: TokenEnricher, contract: ConfirmedContract) -> dict[str, Any]:
        raise KeyError("odd payload")

    monkeypatch.setattr(TokenEnricher, "_chain_details", broken)
    backend = Backend()
    backend.pairs = [pair()]
    with caplog.at_level(logging.WARNING, logger="scraper_bot.chains.enrichment"):
        info = await make(backend).enrich(ETH_CONTRACT)
    assert info.market_checked and info.pair == "USDC/WETH" and info.name == "USD Coin"
    assert info.decimals is None and info.total_supply is None
    assert any("chain source raised" in r.getMessage() for r in caplog.records)

    monkeypatch.undo()
    backend = Backend()
    backend.pairs = [{**pair(), "quoteToken": ["not", "a", "dict"]}]  # trips the market code
    info = await make(backend).enrich(ETH_CONTRACT)
    assert info.decimals == 6 and info.name == "USD Coin"
    assert not info.market_checked and info.pair is None


# ── risk flags, age and other networks ──────────────────────────────────────
def extension(kind: int, body: bytes) -> bytes:
    return kind.to_bytes(2, "little") + len(body).to_bytes(2, "little") + body


def t22_mint(*extensions: bytes, authorities: bool = False) -> bytes:
    mint = bytearray(MINT)
    if authorities:
        mint[0:4] = (1).to_bytes(4, "little")  # mint authority kept
        mint[46:50] = (1).to_bytes(4, "little")  # freeze authority kept
    return bytes(mint).ljust(165, b"\x00") + b"\x01" + b"".join(extensions)


def fee(bps: int) -> bytes:
    older = bytes(16) + (0).to_bytes(2, "little")
    newer = bytes(16) + bps.to_bytes(2, "little")
    return bytes(72) + older + newer


def test_risky_token_2022_extensions_are_named() -> None:
    from scraper_bot.chains.enrichment import parse_authorities, parse_risky_extensions

    data = t22_mint(
        extension(1, fee(250)),
        extension(6, b"\x02"),
        extension(9, b""),
        extension(12, b"\x07" * 32),
        extension(14, bytes(32) + b"\x05" * 32),
        extension(26, bytes(32) + b"\x01"),
        authorities=True,
    )
    assert parse_risky_extensions(data) == (
        "transfer fee 2.5%",
        "new accounts start frozen",
        "non-transferable",
        "permanent delegate",
        "transfer hook",
        "paused",
    )
    assert parse_authorities(data) == (True, True)
    harmless = t22_mint(
        extension(1, fee(0)),
        extension(6, b"\x01"),
        extension(12, bytes(32)),
        extension(14, b"\x05" * 32 + bytes(32)),
    )
    assert parse_risky_extensions(harmless) == ()
    assert parse_authorities(MINT) == (False, False)
    assert parse_authorities(b"short") is None


async def test_solana_details_carry_authorities_flags_and_age() -> None:
    backend = Backend()
    backend.sol_accounts = {USDC_SOL: t22_mint(extension(12, b"\x07" * 32), authorities=True)}
    backend.sol_signatures = [{"blockTime": 1_790_000_100}, {"blockTime": 1_790_000_000}]
    info = await make(backend).enrich(SOL_CONTRACT)
    assert (info.mint_authority, info.freeze_authority) == (True, True)
    assert info.risky_extensions == ("permanent delegate",)
    assert info.created_at == 1_790_000_000.0 and not info.busy

    backend.sol_signatures = [{"blockTime": 1}] * 1000  # too many to find the first one
    assert (await make(backend).enrich(SOL_CONTRACT)).busy


async def test_evm_age_and_other_networks() -> None:
    backend = Backend()
    backend.fresh = True  # no code 90 blocks ago: deployed within that window
    info = await make(backend).enrich(ETH_CONTRACT)
    assert info.deployed_within is not None and info.deployed_before is None

    http = httpx.AsyncClient(transport=httpx.MockTransport(backend.handler))
    networks = (*NETWORKS, EvmNetwork("Base", "https://alpha.rpc"))
    enricher = TokenEnricher(
        http,
        networks,
        "https://sol.rpc",
        timeout=5,
        tuning=FakeTuning(),
        sui_rpc_url="https://sui.rpc",
    )
    backend.fresh = False
    assert (await enricher.enrich(ETH_CONTRACT)).also_on == ("Base",)
