import asyncio
import base64
import json
import logging
import time
from typing import Any

import httpx
import pytest

from scraper_bot.chains.enrichment import TokenEnricher, TokenInfo
from scraper_bot.chains.networks import token_links
from scraper_bot.models.addresses import Address, Chain, ConfirmedContract
from scraper_bot.settings.env import EvmNetwork
from tests.chains.evm.test_abi import abi_bytes32, abi_string, abi_uint
from tests.chains.solana.test_accounts import (
    METAPLEX,
    MINT,
    TOKEN_2022,
    USDC_METADATA,
    USDC_SOL,
    extension,
    t22_mint,
)
from tests.conftest import FakeTuning

USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
ETH_CONTRACT = ConfirmedContract(Address(Chain.ETHEREUM, USDC_ETH), "Ethereum")
SOL_CONTRACT = ConfirmedContract(Address(Chain.SOLANA, USDC_SOL), "Solana")
NETWORKS = (EvmNetwork("Ethereum", "https://alpha.rpc"),)


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


async def test_slow_sources_are_cut_off_at_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    async def at_once(self: TokenEnricher, contract: ConfirmedContract) -> dict[str, Any]:
        return {"name": "USD Coin"}  # done in its first step: no load makes it miss the budget

    monkeypatch.setattr(TokenEnricher, "_chain_details", at_once)
    backend = Backend()
    backend.dex_delay = 60  # DexScreener would answer long after the budget
    backend.pairs = [pair()]
    enrich = make(backend, enrich_timeout=0.05).enrich(ETH_CONTRACT)
    info = await asyncio.wait_for(enrich, timeout=5)  # cut off, not waited for
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


async def test_a_slow_other_network_does_not_cost_the_age_line() -> None:
    backend = Backend()
    backend.fresh = True  # deployed within the last 90 blocks
    hang = asyncio.Event()
    original = backend.handler

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slow.rpc":
            await hang.wait()  # another EVM network that never answers
        return await original(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    networks = (*NETWORKS, EvmNetwork("Base", "https://slow.rpc"))
    enricher = TokenEnricher(
        http,
        networks,
        "https://sol.rpc",
        timeout=5,
        tuning=FakeTuning(enrich_timeout=0.2),
        sui_rpc_url="https://sui.rpc",
    )
    info = await enricher.enrich(ETH_CONTRACT)
    assert info.deployed_within is not None  # the age came although "also on" timed out
    assert info.also_on == ()
    hang.set()
    await http.aclose()
