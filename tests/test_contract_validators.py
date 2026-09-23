import asyncio
import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from scraper_bot.config import EvmNetwork
from scraper_bot.contract_validators import (
    ContractValidator,
    EvmContractValidator,
    RpcUnavailable,
    SolanaContractValidator,
    SuiCoinValidator,
)
from scraper_bot.evm import ERC20_SYMBOL, ERC20_TOTAL_SUPPLY
from scraper_bot.models import Address, Chain, ConfirmedContract, ValidationStatus
from scraper_bot.solana import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from scraper_bot.storage import AddressCache
from tests.conftest import FakeTuning

USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ETH = Address(Chain.ETHEREUM, USDC_ETH)
SOL = Address(Chain.SOLANA, USDC_SOL)
NETWORKS = (EvmNetwork("Alpha", "https://alpha.rpc"), EvmNetwork("Beta", "https://beta.rpc"))
Responder = Callable[[str, list[Any]], Any]


class RpcFailure(Exception):
    """Simulates a JSON-RPC error object (for example ``execution reverted``)."""

    def __init__(self, message: str, code: int = 3) -> None:
        super().__init__(message)
        self.code = code


class RateLimited(Exception):
    """Simulates an HTTP 429 from a public node."""


class FakeRpc:
    """Routes JSON-RPC requests by host to Python callables and records every call."""

    def __init__(self) -> None:
        self.routes: dict[str, Responder] = {}
        self.calls: list[tuple[str, str]] = []

    def route(self, host: str, responder: Responder) -> None:
        self.routes[host] = responder

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        host = request.url.host
        self.calls.append((host, body["method"]))
        responder = self.routes.get(host)
        if responder is None:
            raise httpx.ConnectError("unreachable", request=request)
        try:
            result = responder(body["method"], body["params"])
        except RpcFailure as exc:
            error = {"code": exc.code, "message": str(exc)}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": error})
        except RateLimited:
            return httpx.Response(429, text="too many requests")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def count(self, host: str, method: str | None = None) -> int:
        return sum(1 for h, m in self.calls if h == host and (method is None or m == method))


def erc20_contract(method: str, _params: list[Any]) -> Any:
    if method == "eth_getCode":
        return "0x6080604052"
    if method == "eth_call":
        return "0x" + "00" * 31 + "12"
    raise RpcFailure("unknown method")


def plain_contract(method: str, _params: list[Any]) -> Any:
    if method == "eth_getCode":
        return "0x6080604052"
    raise RpcFailure("execution reverted")


def no_contract(method: str, _params: list[Any]) -> Any:
    return "0x"


def throttled_contract(method: str, _params: list[Any]) -> Any:
    """Code is there, but every ERC-20 probe hits an HTTP 429."""
    if method == "eth_getCode":
        return "0x6080604052"
    raise RateLimited()


def throttled_by_json_error(method: str, _params: list[Any]) -> Any:
    """Code is there, but the node rate-limits through a JSON-RPC error object."""
    if method == "eth_getCode":
        return "0x6080604052"
    raise RpcFailure("rate limit exceeded", code=-32005)


def solana_account(
    *, executable: bool = False, owner: str = "Sysvar", data: bytes = b""
) -> Responder:
    encoded = base64.b64encode(data).decode()

    def responder(_method: str, _params: list[Any]) -> Any:
        value = {
            "executable": executable,
            "owner": owner,
            "data": [encoded, "base64"],
            "lamports": 1,
        }
        return {"context": {"slot": 1}, "value": value}

    return responder


def solana_missing(_method: str, _params: list[Any]) -> Any:
    return {"context": {"slot": 1}, "value": None}


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ── EvmContractValidator ────────────────────────────────────────────────────
async def test_first_network_with_erc20_code_wins() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)
    rpc.route("beta.rpc", erc20_contract)
    async with rpc.client() as http:
        validator = EvmContractValidator(
            http, NETWORKS, timeout=5, tuning=FakeTuning(require_erc20=True)
        )
        assert await validator.find_network(USDC_ETH) == "Beta"
    assert rpc.count("alpha.rpc", "eth_getCode") == 1
    assert rpc.count("alpha.rpc", "eth_call") == 0
    assert rpc.count("beta.rpc", "eth_call") == 3


async def test_no_code_on_any_network_means_not_a_contract() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        validator = EvmContractValidator(
            http, NETWORKS, timeout=5, tuning=FakeTuning(require_erc20=True)
        )
        assert await validator.find_network(USDC_ETH) is None


async def test_non_erc20_contract_only_counts_when_allowed() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", plain_contract)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        strict = EvmContractValidator(
            http, NETWORKS, timeout=5, tuning=FakeTuning(require_erc20=True)
        )
        assert await strict.find_network(USDC_ETH) is None
        lenient = EvmContractValidator(
            http, NETWORKS, timeout=5, tuning=FakeTuning(require_erc20=False)
        )
        assert await lenient.find_network(USDC_ETH) == "Alpha"


@pytest.mark.parametrize("responder", [throttled_contract, throttled_by_json_error])
async def test_throttled_erc20_probe_gives_no_verdict(responder: Responder, tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", responder)
    rpc.route("beta.rpc", no_contract)
    rpc.route("sol.rpc", solana_missing)
    async with rpc.client() as http:
        validator = EvmContractValidator(
            http, NETWORKS, timeout=5, tuning=FakeTuning(require_erc20=True)
        )
        with pytest.raises(RpcUnavailable, match="Alpha"):
            await validator.find_network(USDC_ETH)
        facade = build(rpc, http, tmp_path, FakeClock())
        assert (await facade.validate(ETH)).status is ValidationStatus.UNAVAILABLE
    assert not (tmp_path / "rejected.tsv").exists()  # nothing was cached as rejected


async def test_failed_endpoint_prevents_a_rejection_but_not_a_confirmation() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)  # beta.rpc is unreachable
    async with rpc.client() as http:
        validator = EvmContractValidator(
            http, NETWORKS, timeout=5, tuning=FakeTuning(require_erc20=True)
        )
        with pytest.raises(RpcUnavailable, match="Beta"):
            await validator.find_network(USDC_ETH)
        rpc.route("alpha.rpc", erc20_contract)
        assert await validator.find_network(USDC_ETH) == "Alpha"


# ── SolanaContractValidator ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("responder", "expected"),
    [
        (solana_account(executable=True), True),
        (solana_account(owner=TOKEN_PROGRAM, data=bytes(82)), True),
        (solana_account(owner=TOKEN_PROGRAM, data=bytes(165)), False),
        (solana_account(owner=TOKEN_2022_PROGRAM, data=bytes(82)), True),
        (
            solana_account(owner=TOKEN_2022_PROGRAM, data=bytes(165) + b"\x01" + bytes(10)),
            True,
        ),
        (
            solana_account(owner=TOKEN_2022_PROGRAM, data=bytes(165) + b"\x02" + bytes(10)),
            False,
        ),
        (solana_account(owner="SomeOtherProgram", data=bytes(82)), False),
        (solana_missing, False),
    ],
)
async def test_solana_verdicts(responder: Responder, expected: bool) -> None:
    rpc = FakeRpc()
    rpc.route("sol.rpc", responder)
    async with rpc.client() as http:
        validator = SolanaContractValidator(http, "https://sol.rpc", timeout=5)
        assert await validator.is_program_or_mint(USDC_SOL) is expected


async def test_solana_transport_error_is_unavailable() -> None:
    rpc = FakeRpc()
    async with rpc.client() as http:
        validator = SolanaContractValidator(http, "https://sol.rpc", timeout=5)
        with pytest.raises(RpcUnavailable):
            await validator.is_program_or_mint(USDC_SOL)


# ── ContractValidator (facade) ──────────────────────────────────────────────
def build(
    rpc: FakeRpc, http: httpx.AsyncClient, tmp_path: Path, clock: FakeClock
) -> ContractValidator:
    cache = AddressCache(
        tmp_path / "known.tsv", tmp_path / "rejected.tsv", rejected_ttl=1000, clock=clock
    )
    cache.load()
    return ContractValidator(
        EvmContractValidator(http, NETWORKS, timeout=5, tuning=FakeTuning(require_erc20=True)),
        SolanaContractValidator(http, "https://sol.rpc", timeout=5),
        cache,
        sui=SuiCoinValidator(http, "https://sui.rpc", timeout=5, tuning=FakeTuning()),
        max_concurrent=4,
        retry_delay=60,
        clock=clock,
    )


async def test_verdicts_are_cached(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", erc20_contract)
    rpc.route("beta.rpc", no_contract)
    rpc.route("sol.rpc", solana_missing)
    async with rpc.client() as http:
        validator = build(rpc, http, tmp_path, FakeClock())
        first = await validator.validate(ETH)
        assert first.status is ValidationStatus.CONFIRMED
        assert first.contract == ConfirmedContract(ETH, "Alpha")
        assert (await validator.validate(ETH)).status is ValidationStatus.ALREADY_KNOWN
        assert rpc.count("alpha.rpc", "eth_getCode") == 1

        assert (await validator.validate(SOL)).status is ValidationStatus.REJECTED
        assert (await validator.validate(SOL)).status is ValidationStatus.ALREADY_REJECTED
        assert rpc.count("sol.rpc") == 1
        assert [r.status for r in await validator.validate_many([ETH, SOL])] == [
            ValidationStatus.ALREADY_KNOWN,
            ValidationStatus.ALREADY_REJECTED,
        ]


async def test_unavailable_verdict_is_retried_after_delay(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)  # beta unreachable
    clock = FakeClock()
    async with rpc.client() as http:
        validator = build(rpc, http, tmp_path, clock)
        assert (await validator.validate(ETH)).status is ValidationStatus.UNAVAILABLE
        assert (await validator.validate(ETH)).status is ValidationStatus.UNAVAILABLE
        assert rpc.count("alpha.rpc") == 1  # no second round-trip inside the retry delay
        clock.now = 61
        rpc.route("beta.rpc", no_contract)
        assert (await validator.validate(ETH)).status is ValidationStatus.REJECTED
        assert rpc.count("alpha.rpc") == 2


async def test_concurrent_checks_of_one_address_share_a_single_lookup(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", erc20_contract)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        validator = build(rpc, http, tmp_path, FakeClock())
        results = await asyncio.gather(*(validator.validate(ETH) for _ in range(3)))
    assert [r.status for r in results] == [ValidationStatus.CONFIRMED] * 3
    assert rpc.count("alpha.rpc", "eth_getCode") == 1


async def test_turbo_checks_bypass_and_do_not_write_the_negative_cache(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        validator = build(rpc, http, tmp_path, FakeClock())
        first = await validator.validate(ETH, negative_cache=False)
        second = await validator.validate(ETH, negative_cache=False)
        assert (first.status, second.status) == (
            ValidationStatus.REJECTED,
            ValidationStatus.REJECTED,
        )
        assert rpc.count("alpha.rpc", "eth_getCode") == 2  # nothing was cached
        assert (await validator.validate(ETH)).status is ValidationStatus.REJECTED
        assert (await validator.validate(ETH)).status is ValidationStatus.ALREADY_REJECTED
        # the regular rejection is ignored by a turbo check
        assert (await validator.validate(ETH, negative_cache=False)).status is (
            ValidationStatus.REJECTED
        )
        assert rpc.count("alpha.rpc", "eth_getCode") == 4


async def test_rpc_failures_are_reported_through_the_notifier(tmp_path: Path) -> None:
    from tests.conftest import FakeNotifier

    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)  # beta unreachable
    notifier = FakeNotifier()
    clock = FakeClock()
    async with rpc.client() as http:
        cache = AddressCache(
            tmp_path / "known.tsv", tmp_path / "rejected.tsv", rejected_ttl=1000, clock=clock
        )
        cache.load()
        validator = ContractValidator(
            EvmContractValidator(http, NETWORKS, timeout=5, tuning=FakeTuning()),
            SolanaContractValidator(http, "https://sol.rpc", timeout=5),
            cache,
            sui=SuiCoinValidator(http, "https://sui.rpc", timeout=5, tuning=FakeTuning()),
            max_concurrent=4,
            retry_delay=60,
            clock=clock,
            notifier=notifier,  # type: ignore[arg-type]
        )
        assert (await validator.validate(ETH)).status is ValidationStatus.UNAVAILABLE
    assert len(notifier.problems) == 1
    key, title, detail, hint = notifier.problems[0]
    assert key == "rpc" and "failing" in title and "Beta" in detail and "60 s" in (hint or "")


# ── SuiCoinValidator ────────────────────────────────────────────────────────
SUI_PKG = "0x" + "ab" * 32
SUI_COIN = f"{SUI_PKG}::usdc::USDC"
SUI_STRUCT = f"{SUI_PKG}::helpers::Config"
SUI_WALLET = "0x" + "cd" * 32
SUI_OBJECT = "0x" + "ef" * 32


def sui_node(
    *,
    coins: dict[str, dict[str, Any]],
    packages: dict[str, dict[str, list[str]]],
    objects: set[str] = frozenset(),
) -> Responder:
    def responder(method: str, params: list[Any]) -> Any:
        target = params[0]
        if method == "suix_getCoinMetadata":
            return coins.get(target)
        if method == "sui_getObject":
            if target in packages:
                return {"data": {"objectId": target, "type": "package"}}
            if target in objects:
                return {"data": {"objectId": target, "type": "0x2::coin::Coin<0x2::sui::SUI>"}}
            return {"error": {"code": "notExists", "object_id": target}}
        if method == "sui_getNormalizedMoveModulesByPackage":
            modules = packages.get(target, {})
            return {m: {"structs": {s: {} for s in structs}} for m, structs in modules.items()}
        raise RpcFailure("unknown method")

    return responder


async def test_sui_coin_types_and_packages() -> None:
    rpc = FakeRpc()
    rpc.route(
        "sui.rpc",
        sui_node(
            coins={SUI_COIN: {"name": "USDC", "symbol": "USDC", "decimals": 6}},
            packages={SUI_PKG: {"helpers": ["Config"], "usdc": ["USDC"]}},
            objects={SUI_OBJECT},
        ),
    )
    async with rpc.client() as http:
        strict = SuiCoinValidator(
            http, "https://sui.rpc", timeout=5, tuning=FakeTuning(require_erc20=True)
        )
        assert await strict.is_coin_or_package(SUI_COIN)
        assert await strict.is_coin_or_package(SUI_PKG)  # the package defines a coin
        assert not await strict.is_coin_or_package(SUI_STRUCT)  # a type, not a coin
        assert not await strict.is_coin_or_package(SUI_WALLET)
        assert not await strict.is_coin_or_package(SUI_OBJECT)
        lenient = SuiCoinValidator(
            http, "https://sui.rpc", timeout=5, tuning=FakeTuning(require_erc20=False)
        )
        assert await lenient.is_coin_or_package(SUI_STRUCT)  # any type of a real package
        assert not await lenient.is_coin_or_package(SUI_WALLET)

        rpc.route("sui.rpc", sui_node(coins={}, packages={SUI_PKG: {"helpers": ["Config"]}}))
        assert not await strict.is_coin_or_package(SUI_PKG)  # no coin inside
        assert await lenient.is_coin_or_package(SUI_PKG)


async def test_sui_transport_error_is_unavailable() -> None:
    rpc = FakeRpc()  # nothing routed: every call fails to connect
    async with rpc.client() as http:
        validator = SuiCoinValidator(http, "https://sui.rpc", timeout=5, tuning=FakeTuning())
        with pytest.raises(RpcUnavailable):
            await validator.is_coin_or_package(SUI_COIN)
        with pytest.raises(RpcUnavailable):
            await validator.is_coin_or_package(SUI_PKG)


async def test_facade_routes_sui_addresses(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route(
        "sui.rpc",
        sui_node(coins={SUI_COIN: {"decimals": 6}}, packages={SUI_PKG: {"usdc": ["USDC"]}}),
    )
    clock = FakeClock()
    async with rpc.client() as http:
        cache = AddressCache(
            tmp_path / "known.tsv", tmp_path / "rejected.tsv", rejected_ttl=1000, clock=clock
        )
        cache.load()
        validator = ContractValidator(
            EvmContractValidator(http, NETWORKS, timeout=5, tuning=FakeTuning()),
            SolanaContractValidator(http, "https://sol.rpc", timeout=5),
            cache,
            max_concurrent=4,
            retry_delay=60,
            clock=clock,
            sui=SuiCoinValidator(http, "https://sui.rpc", timeout=5, tuning=FakeTuning()),
        )
        confirmed = await validator.validate(Address(Chain.SUI, SUI_COIN))
        assert confirmed.status is ValidationStatus.CONFIRMED and confirmed.network == "Sui"
        rejected = await validator.validate(Address(Chain.SUI, SUI_WALLET))
        assert rejected.status is ValidationStatus.REJECTED
        assert rpc.count("sol.rpc") == 0 and rpc.count("alpha.rpc") == 0


async def test_packages_with_many_structs_are_searched_completely() -> None:
    rpc = FakeRpc()
    coin = f"{SUI_PKG}::coin::MEME"  # witness name differs from the module: sorts last
    helpers = {"lib": [f"Struct{i}" for i in range(20)], "coin": ["MEME"]}
    rpc.route("sui.rpc", sui_node(coins={coin: {"decimals": 9}}, packages={SUI_PKG: helpers}))
    async with rpc.client() as http:
        strict = SuiCoinValidator(http, "https://sui.rpc", timeout=5, tuning=FakeTuning())
        assert await strict.is_coin_or_package(SUI_PKG)
    assert rpc.count("sui.rpc", "suix_getCoinMetadata") == 21


# ── ERC-20 probe: reverts vs. real failures ─────────────────────────────────
def mixed_probe_failures(method: str, params: list[Any]) -> Any:
    """Code is there; one ERC-20 probe reverts, another one is throttled."""
    if method == "eth_getCode":
        return "0x6080604052"
    if params[0]["data"] == ERC20_SYMBOL:
        raise RateLimited()
    raise RpcFailure("execution reverted")


def one_answer_rest_throttled(method: str, params: list[Any]) -> Any:
    """Only totalSupply() answers; the other probes hit an HTTP 429."""
    if method == "eth_getCode":
        return "0x6080604052"
    if params[0]["data"] == ERC20_TOTAL_SUPPLY:
        return "0x" + "00" * 31 + "12"
    raise RateLimited()


async def test_mixed_revert_and_throttle_gives_no_verdict() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", mixed_probe_failures)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        validator = EvmContractValidator(
            http, NETWORKS, timeout=5, tuning=FakeTuning(require_erc20=True)
        )
        with pytest.raises(RpcUnavailable, match="Alpha"):
            await validator.find_network(USDC_ETH)
    assert rpc.count("alpha.rpc", "eth_call") == 3


async def test_one_erc20_answer_outweighs_throttled_probes() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", one_answer_rest_throttled)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        validator = EvmContractValidator(
            http, NETWORKS, timeout=5, tuning=FakeTuning(require_erc20=True)
        )
        assert await validator.find_network(USDC_ETH) == "Alpha"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("execution reverted", 3),
        ("anything with code 3", 3),
        ("execution reverted: no such function", -32000),
        ("invalid opcode: INVALID", -32015),
        ("out of gas", -32000),
        ("VM execution error.", -32015),
    ],
)
async def test_every_revert_flavour_means_not_a_token(message: str, code: int) -> None:
    def reverting(method: str, _params: list[Any]) -> Any:
        if method == "eth_getCode":
            return "0x6080604052"
        raise RpcFailure(message, code=code)

    rpc = FakeRpc()
    rpc.route("alpha.rpc", reverting)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        validator = EvmContractValidator(
            http, NETWORKS, timeout=5, tuning=FakeTuning(require_erc20=True)
        )
        assert await validator.find_network(USDC_ETH) is None


async def test_non_rpc_exceptions_propagate_from_find_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(self: EvmContractValidator, client: Any, address: str) -> bool:
        raise ValueError("programming error")

    monkeypatch.setattr(EvmContractValidator, "_check", broken)
    rpc = FakeRpc()
    async with rpc.client() as http:
        validator = EvmContractValidator(http, NETWORKS, timeout=5, tuning=FakeTuning())
        with pytest.raises(ValueError, match="programming error"):
            await validator.find_network(USDC_ETH)
    assert rpc.calls == []


async def test_solana_bad_base64_is_not_a_mint() -> None:
    def responder(_method: str, _params: list[Any]) -> Any:
        value = {"executable": False, "owner": TOKEN_PROGRAM, "data": ["not*base64!", "base64"]}
        return {"context": {"slot": 1}, "value": value}

    rpc = FakeRpc()
    rpc.route("sol.rpc", responder)
    async with rpc.client() as http:
        validator = SolanaContractValidator(http, "https://sol.rpc", timeout=5)
        assert await validator.is_program_or_mint(USDC_SOL) is False
