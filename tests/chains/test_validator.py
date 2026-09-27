import asyncio
import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from scraper_bot.chains.addresses.stoplist import StopList
from scraper_bot.chains.evm.abi import (
    ERC20_DECIMALS,
    ERC20_TOTAL_SUPPLY,
    PAIR_TOKEN0,
    PAIR_TOKEN1,
    TOKEN_PROBE,
    VAULT_ASSET,
)
from scraper_bot.chains.evm.validator import EvmContractValidator
from scraper_bot.chains.rpc.client import NO_RETRY, RpcError
from scraper_bot.chains.rpc.health import FailingEndpoint, RpcUnavailable
from scraper_bot.chains.solana.accounts import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from scraper_bot.chains.solana.validator import SolanaContractValidator
from scraper_bot.chains.sui.validator import SuiCoinValidator
from scraper_bot.chains.validator import ContractValidator
from scraper_bot.models.addresses import (
    Address,
    AddressKind,
    Chain,
    ChainVerdict,
    ConfirmedContract,
    ValidationStatus,
)
from scraper_bot.settings.env import EvmNetwork
from scraper_bot.storage.address_cache import AddressCache
from tests.conftest import FakeTuning

USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"  # stop-listed
MEME = "0x6982508145454Ce325dDbE47a25d4ec3d2311933"  # sorts below WETH
OTHER = "0x" + "7" * 40
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


class LostAnswer(Exception):
    """Simulates a node that leaves one call out of its batch answer."""


class FakeRpc:
    """Routes JSON-RPC requests by host to Python callables and records every call."""

    def __init__(self) -> None:
        self.routes: dict[str, Responder] = {}
        self.calls: list[tuple[str, str]] = []
        self.params: list[tuple[str, str, list[Any]]] = []

    def route(self, host: str, responder: Responder) -> None:
        self.routes[host] = responder

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        host = request.url.host
        responder = self.routes.get(host)
        items = body if isinstance(body, list) else [body]
        answers = []
        for item in items:
            self.calls.append((host, item["method"]))
            self.params.append((host, item["method"], item["params"]))
            if responder is None:
                raise httpx.ConnectError("unreachable", request=request)
            try:
                result = {"result": responder(item["method"], item["params"])}
            except RpcFailure as exc:
                result = {"error": {"code": exc.code, "message": str(exc)}}
            except RateLimited:  # a 429 refuses the whole request, batch or not
                return httpx.Response(429, text="too many requests")
            except LostAnswer:
                continue
            answers.append({"jsonrpc": "2.0", "id": item["id"], **result})
        return httpx.Response(200, json=answers if isinstance(body, list) else answers[0])

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def count(self, host: str, method: str | None = None) -> int:
        return sum(1 for h, m in self.calls if h == host and (method is None or m == method))


def word(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


def address_word(address: str) -> str:
    return "0x" + "00" * 12 + address[2:].lower()


def evm_node(answers: dict[str, str] | None = None, code: str = "0x6080604052") -> Responder:
    """A node with ``code`` at every address; eth_call answers by selector, else reverts."""
    table = answers or {}

    def responder(method: str, params: list[Any]) -> Any:
        if method == "eth_getCode":
            return code
        if method == "eth_call" and params[0]["data"] in table:
            return table[params[0]["data"]]
        raise RpcFailure("execution reverted")

    return responder


erc20_contract = evm_node({ERC20_TOTAL_SUPPLY: word(1000 * 10**18), ERC20_DECIMALS: word(18)})
plain_contract = evm_node()


def no_contract(method: str, _params: list[Any]) -> Any:
    return "0x"


def throttled_contract(method: str, _params: list[Any]) -> Any:
    """Code is there, but every probe hits an HTTP 429."""
    if method == "eth_getCode":
        return "0x6080604052"
    raise RateLimited()


def throttled_by_json_error(method: str, _params: list[Any]) -> Any:
    """Code is there, but the node rate-limits through a JSON-RPC error object."""
    if method == "eth_getCode":
        return "0x6080604052"
    raise RpcFailure("rate limit exceeded", code=-32005)


def mint_data(supply: int = 10**15, decimals: int = 6, extra: bytes = b"") -> bytes:
    """An 82-byte SPL mint layout (authority options zeroed) plus ``extra``."""
    body = bytearray(82)
    body[36:44] = supply.to_bytes(8, "little")
    body[44] = decimals
    return bytes(body) + extra


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


def evm(http: httpx.AsyncClient, *, require_erc20: bool = True, **kwargs: Any):
    tuning = FakeTuning(require_erc20=require_erc20)
    kwargs.setdefault("retry", NO_RETRY)
    return EvmContractValidator(http, NETWORKS, timeout=5, tuning=tuning, **kwargs)


def solana(http: httpx.AsyncClient, **kwargs: Any) -> SolanaContractValidator:
    return SolanaContractValidator(http, "https://sol.rpc", timeout=5, retry=NO_RETRY, **kwargs)


def sui(http: httpx.AsyncClient, **kwargs: Any) -> SuiCoinValidator:
    tuning = kwargs.pop("tuning", FakeTuning())
    return SuiCoinValidator(
        http, "https://sui.rpc", timeout=5, tuning=tuning, retry=NO_RETRY, **kwargs
    )


# ── EvmContractValidator ────────────────────────────────────────────────────
async def test_first_network_with_a_token_wins() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)
    rpc.route("beta.rpc", erc20_contract)
    async with rpc.client() as http:
        assert await evm(http).find_network(USDC_ETH) == ChainVerdict(AddressKind.TOKEN, "Beta")
    assert rpc.count("alpha.rpc", "eth_getCode") == 1
    assert rpc.count("alpha.rpc", "eth_call") == 0
    assert rpc.count("beta.rpc", "eth_call") == len(TOKEN_PROBE)


async def test_no_code_on_any_network() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        assert await evm(http).find_network(USDC_ETH) == ChainVerdict(AddressKind.NO_CODE)


async def test_other_contracts_only_count_when_allowed() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", plain_contract)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        assert (await evm(http).find_network(USDC_ETH)).kind is AddressKind.NOT_TOKEN
        lenient = await evm(http, require_erc20=False).find_network(USDC_ETH)
        assert lenient == ChainVerdict(AddressKind.TOKEN, "Alpha")


async def test_a_delegated_wallet_is_never_a_token() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", evm_node(code="0xef0100" + "5a" * 20))  # EIP-7702, like vitalik.eth
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        assert (await evm(http).find_network(USDC_ETH)).kind is AddressKind.WALLET
        assert (await evm(http, require_erc20=False).find_network(USDC_ETH)).kind is (
            AddressKind.WALLET
        )
    assert rpc.count("alpha.rpc", "eth_call") == 0


@pytest.mark.parametrize(
    ("answers", "why"),
    [
        ({ERC20_DECIMALS: word(6)}, "a system contract with decimals() only (GasPriceOracle)"),
        ({ERC20_TOTAL_SUPPLY: word(10_000)}, "an NFT collection without decimals()"),
        ({ERC20_TOTAL_SUPPLY: word(1), ERC20_DECIMALS: word(300)}, "decimals beyond uint8"),
    ],
)
async def test_contracts_that_are_not_plain_tokens(answers: dict[str, str], why: str) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", evm_node(answers))
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        assert (await evm(http).find_network(USDC_ETH)).kind is AddressKind.NOT_TOKEN, why


async def test_lp_pairs_and_pools_name_their_tokens_without_the_quote_side() -> None:
    v2_pair = {
        ERC20_TOTAL_SUPPLY: word(10**18),
        ERC20_DECIMALS: word(18),
        PAIR_TOKEN0: address_word(MEME),
        PAIR_TOKEN1: address_word(WETH),
    }
    v3_pool = {PAIR_TOKEN0: address_word(MEME), PAIR_TOKEN1: address_word(WETH)}
    for answers in (v2_pair, v3_pool):
        rpc = FakeRpc()
        rpc.route("alpha.rpc", no_contract)
        rpc.route("beta.rpc", evm_node(answers))
        async with rpc.client() as http:
            verdict = await evm(http, stoplist=StopList()).find_network(USDC_ETH)
            assert verdict == ChainVerdict(AddressKind.PAIR, "Beta", (MEME,))
            both = await evm(http).find_network(USDC_ETH)  # no stop-list: both sides
            assert both.sides == (MEME, WETH)


@pytest.mark.parametrize(
    ("token0", "token1"),
    [
        (MEME, MEME),  # the same token twice
        (WETH, MEME),  # not sorted: a real pair always is
        ("0x" + "0" * 40, MEME),  # the zero address
    ],
)
async def test_only_two_sorted_distinct_tokens_make_a_pair(token0: str, token1: str) -> None:
    answers = {
        ERC20_TOTAL_SUPPLY: word(5),
        ERC20_DECIMALS: word(9),
        PAIR_TOKEN0: address_word(token0),
        PAIR_TOKEN1: address_word(token1),
    }
    rpc = FakeRpc()
    rpc.route("alpha.rpc", evm_node(answers))
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        assert (await evm(http).find_network(USDC_ETH)).kind is AddressKind.TOKEN


async def test_vault_shares_are_not_a_launch() -> None:
    answers = {
        ERC20_TOTAL_SUPPLY: word(10**18),
        ERC20_DECIMALS: word(18),
        VAULT_ASSET: address_word(USDC_ETH),
    }
    rpc = FakeRpc()
    rpc.route("alpha.rpc", evm_node(answers))
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        assert (await evm(http).find_network(USDC_ETH)).kind is AddressKind.VAULT


@pytest.mark.parametrize("responder", [throttled_contract, throttled_by_json_error])
async def test_throttled_probe_gives_no_verdict(responder: Responder, tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", responder)
    rpc.route("beta.rpc", no_contract)
    rpc.route("sol.rpc", solana_missing)
    async with rpc.client() as http:
        with pytest.raises(RpcUnavailable, match="Alpha"):
            await evm(http).find_network(USDC_ETH)
        facade = build(rpc, http, tmp_path, FakeClock())
        assert (await facade.validate(ETH)).status is ValidationStatus.UNAVAILABLE
    assert not (tmp_path / "rejected.tsv").exists()  # nothing was cached as rejected


async def test_failed_endpoint_prevents_a_rejection_but_not_a_confirmation() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)  # beta.rpc is unreachable
    async with rpc.client() as http:
        validator = evm(http)
        with pytest.raises(RpcUnavailable, match="Beta"):
            await validator.find_network(USDC_ETH)
        rpc.route("alpha.rpc", erc20_contract)
        assert await validator.find_network(USDC_ETH) == ChainVerdict(AddressKind.TOKEN, "Alpha")


def mixed_probe_failures(method: str, params: list[Any]) -> Any:
    """Code is there; totalSupply answers, decimals reverts, token0 is throttled."""
    if method == "eth_getCode":
        return "0x6080604052"
    selector = params[0]["data"]
    if selector == PAIR_TOKEN0:
        raise RateLimited()
    if selector == ERC20_TOTAL_SUPPLY:
        return word(1)
    raise RpcFailure("execution reverted")


async def test_any_throttled_probe_withholds_the_verdict() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", mixed_probe_failures)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        with pytest.raises(RpcUnavailable, match="Alpha"):
            await evm(http).find_network(USDC_ETH)


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
        assert (await evm(http).find_network(USDC_ETH)).kind is AddressKind.NOT_TOKEN


@pytest.mark.parametrize(
    "failure",
    [RpcFailure("header not found", code=-32000), LostAnswer()],
    ids=["node-error", "lost-answer"],
)
async def test_a_node_error_or_a_lost_answer_on_one_probe_gives_no_verdict(
    failure: Exception,
) -> None:
    def behind(method: str, params: list[Any]) -> Any:
        if method == "eth_call" and params[0]["data"] == ERC20_TOTAL_SUPPLY:
            raise failure  # no real answer to that call: an outage, not "no such function"
        return erc20_contract(method, params)

    rpc = FakeRpc()
    rpc.route("alpha.rpc", behind)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        with pytest.raises(RpcUnavailable, match="Alpha"):
            await evm(http).find_network(USDC_ETH)


async def test_non_rpc_exceptions_propagate_from_find_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(self: EvmContractValidator, client: Any, address: str) -> ChainVerdict:
        raise ValueError("programming error")

    monkeypatch.setattr(EvmContractValidator, "_check", broken)
    rpc = FakeRpc()
    async with rpc.client() as http:
        with pytest.raises(ValueError, match="programming error"):
            await evm(http).find_network(USDC_ETH)
    assert rpc.calls == []


# ── SolanaContractValidator ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("responder", "expected"),
    [
        (solana_account(executable=True, owner="BPFLoaderUpgradeab1e"), AddressKind.NOT_TOKEN),
        (solana_account(owner=TOKEN_PROGRAM, data=mint_data()), AddressKind.TOKEN),
        (solana_account(owner=TOKEN_PROGRAM, data=bytes(165)), AddressKind.NOT_TOKEN),
        (solana_account(owner=TOKEN_2022_PROGRAM, data=mint_data()), AddressKind.TOKEN),
        (
            solana_account(owner=TOKEN_2022_PROGRAM, data=mint_data(extra=bytes(83) + b"\x01")),
            AddressKind.TOKEN,
        ),
        (
            solana_account(owner=TOKEN_2022_PROGRAM, data=bytes(165) + b"\x02" + bytes(10)),
            AddressKind.NOT_TOKEN,
        ),
        (solana_account(owner="SomeOtherProgram", data=mint_data()), AddressKind.NOT_TOKEN),
        (solana_account(owner="11111111111111111111111111111111"), AddressKind.NOT_TOKEN),
        (solana_account(owner=TOKEN_PROGRAM, data=mint_data(1, 0)), AddressKind.NFT),
        (solana_account(owner=TOKEN_PROGRAM, data=mint_data(0, 0)), AddressKind.NOT_YET_MINTED),
        (solana_account(owner=TOKEN_PROGRAM, data=mint_data(0, 6)), AddressKind.TOKEN),
        (solana_missing, AddressKind.MISSING),
    ],
)
async def test_solana_verdicts(responder: Responder, expected: AddressKind) -> None:
    rpc = FakeRpc()
    rpc.route("sol.rpc", responder)
    async with rpc.client() as http:
        validator = solana(http)
        assert await validator.inspect(USDC_SOL) is expected


async def test_solana_is_read_at_confirmed_commitment() -> None:
    rpc = FakeRpc()
    rpc.route("sol.rpc", solana_missing)
    async with rpc.client() as http:
        await solana(http).inspect(USDC_SOL)
    [(_, method, params)] = rpc.params
    assert method == "getAccountInfo" and params[1]["commitment"] == "confirmed"


async def test_solana_transport_error_is_unavailable() -> None:
    rpc = FakeRpc()
    async with rpc.client() as http:
        validator = solana(http)
        with pytest.raises(RpcUnavailable):
            await validator.inspect(USDC_SOL)


async def test_solana_bad_base64_is_not_a_mint() -> None:
    def responder(_method: str, _params: list[Any]) -> Any:
        value = {"executable": False, "owner": TOKEN_PROGRAM, "data": ["not*base64!", "base64"]}
        return {"context": {"slot": 1}, "value": value}

    rpc = FakeRpc()
    rpc.route("sol.rpc", responder)
    async with rpc.client() as http:
        validator = solana(http)
        assert await validator.inspect(USDC_SOL) is AddressKind.NOT_TOKEN


# ── ContractValidator (facade) ──────────────────────────────────────────────
def build(
    rpc: FakeRpc,
    http: httpx.AsyncClient,
    tmp_path: Path,
    clock: FakeClock,
    stoplist: StopList | None = None,
) -> ContractValidator:
    cache = AddressCache(
        tmp_path / "known.tsv", tmp_path / "rejected.tsv", rejected_ttl=1000, clock=clock
    )
    cache.load()
    return ContractValidator(
        evm(http, stoplist=stoplist),
        solana(http),
        cache,
        sui=sui(http),
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
        assert first.status is ValidationStatus.CONFIRMED and first.kind is AddressKind.TOKEN
        assert first.contract == ConfirmedContract(ETH, "Alpha")
        # A token becomes known when its alert is claimed (AlertRegistry), not here: a
        # caller that vanished meanwhile must not leave it known but never announced.
        assert (await validator.validate(ETH)).status is ValidationStatus.CONFIRMED
        assert rpc.count("alpha.rpc", "eth_getCode") == 4  # checked twice, each with a batch
        validator._cache.add_known(first.contract)  # what the claim does
        assert (await validator.validate(ETH)).status is ValidationStatus.ALREADY_KNOWN
        assert rpc.count("alpha.rpc", "eth_getCode") == 4

        missing = await validator.validate(SOL)
        assert (missing.status, missing.kind) == (ValidationStatus.REJECTED, AddressKind.MISSING)
        assert (await validator.validate(SOL)).status is ValidationStatus.ALREADY_REJECTED
        assert rpc.count("sol.rpc") == 1
        assert [r.status for r in await validator.validate_many([ETH, SOL])] == [
            ValidationStatus.ALREADY_KNOWN,
            ValidationStatus.ALREADY_REJECTED,
        ]


async def test_a_mint_without_supply_is_never_cached_as_rejected(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("sol.rpc", solana_account(owner=TOKEN_PROGRAM, data=mint_data(0, 0)))
    async with rpc.client() as http:
        validator = build(rpc, http, tmp_path, FakeClock())
        for _ in range(2):
            result = await validator.validate(SOL)
            assert (result.status, result.kind) == (
                ValidationStatus.REJECTED,
                AddressKind.NOT_YET_MINTED,
            )
        rpc.route("sol.rpc", solana_account(owner=TOKEN_PROGRAM, data=mint_data(10**9, 0)))
        assert (await validator.validate(SOL)).status is ValidationStatus.CONFIRMED
    assert rpc.count("sol.rpc") == 3


async def test_the_token_behind_an_lp_pair_is_checked_and_remembered(tmp_path: Path) -> None:
    pair = Address(Chain.ETHEREUM, USDC_ETH)
    token = Address(Chain.ETHEREUM, MEME)
    pair_answers = {
        ERC20_TOTAL_SUPPLY: word(10**18),
        ERC20_DECIMALS: word(18),
        PAIR_TOKEN0: address_word(MEME),
        PAIR_TOKEN1: address_word(WETH),
    }
    token_answers = {ERC20_TOTAL_SUPPLY: word(10**27), ERC20_DECIMALS: word(18)}

    def node(method: str, params: list[Any]) -> Any:
        target = params[0] if method == "eth_getCode" else params[0]["to"]
        return evm_node(pair_answers if target == USDC_ETH else token_answers)(method, params)

    rpc = FakeRpc()
    rpc.route("alpha.rpc", node)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        validator = build(rpc, http, tmp_path, FakeClock(), stoplist=StopList())
        first = await validator.validate_many([pair])
        assert [(r.address, r.status, r.kind) for r in first] == [
            (pair, ValidationStatus.REJECTED, AddressKind.PAIR),
            (token, ValidationStatus.CONFIRMED, AddressKind.TOKEN),
        ]
        assert first[0].resolved == (token,) and first[1].via == pair
        assert first[1].contract is not None
        validator._cache.add_known(first[1].contract)  # its alert was claimed
        # The pair is cached as rejected, but it still points at its token.
        again = await validator.validate_many([pair])
        assert [(r.status, r.via) for r in again] == [
            (ValidationStatus.ALREADY_REJECTED, None),
            (ValidationStatus.ALREADY_KNOWN, pair),
        ]
        assert again[0].resolved == (token,)
        alone = await validator.validate_many([pair], follow_pairs=False)
        assert [r.address for r in alone] == [pair]


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
    assert rpc.count("alpha.rpc", "eth_call") == 5  # one probe batch for three callers


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
        assert first.kind is AddressKind.NO_CODE
        assert rpc.count("alpha.rpc", "eth_getCode") == 2  # nothing was cached
        assert (await validator.validate(ETH)).status is ValidationStatus.REJECTED
        assert (await validator.validate(ETH)).status is ValidationStatus.ALREADY_REJECTED
        # the regular rejection is ignored by a turbo check
        assert (await validator.validate(ETH, negative_cache=False)).status is (
            ValidationStatus.REJECTED
        )
        assert rpc.count("alpha.rpc", "eth_getCode") == 4


async def test_endpoints_failing_for_minutes_are_reported(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)  # beta unreachable
    reports: list[tuple[list[FailingEndpoint], float]] = []
    clock = FakeClock()
    async with rpc.client() as http:
        cache = AddressCache(
            tmp_path / "known.tsv", tmp_path / "rejected.tsv", rejected_ttl=1000, clock=clock
        )
        cache.load()
        validator = ContractValidator(
            evm(http, clock=clock),
            solana(http, clock=clock),
            cache,
            sui=sui(http, clock=clock),
            max_concurrent=4,
            retry_delay=60,
            clock=clock,
            on_stuck=lambda endpoints, delay: reports.append((list(endpoints), delay)),
        )
        assert (await validator.validate(ETH)).status is ValidationStatus.UNAVAILABLE
        assert reports == []  # a hiccup stays in the log
        for clock.now in (100, 200, 301):  # it keeps failing for five minutes
            assert (await validator.validate(ETH)).status is ValidationStatus.UNAVAILABLE
    [(endpoints, delay)] = reports
    assert delay == 60
    [beta] = endpoints
    assert (beta.name, beta.seconds, beta.optional) == ("Beta", 301, False)
    assert "eth_getCode via beta.rpc" in beta.error


async def test_an_optional_network_never_holds_up_a_verdict(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)  # beta, the optional one, is unreachable
    reports: list[list[FailingEndpoint]] = []
    clock = FakeClock()
    networks = (NETWORKS[0], EvmNetwork("Beta", "https://beta.rpc", optional=True))
    async with rpc.client() as http:
        cache = AddressCache(
            tmp_path / "known.tsv", tmp_path / "rejected.tsv", rejected_ttl=1000, clock=clock
        )
        cache.load()
        lenient = EvmContractValidator(
            http, networks, timeout=5, tuning=FakeTuning(), retry=NO_RETRY, clock=clock
        )
        validator = ContractValidator(
            lenient,
            solana(http, clock=clock),
            cache,
            sui=sui(http, clock=clock),
            max_concurrent=4,
            retry_delay=60,
            clock=clock,
            on_stuck=lambda endpoints, _delay: reports.append(list(endpoints)),
        )
        result = await validator.validate(ETH)
        assert (result.status, result.kind) == (ValidationStatus.REJECTED, AddressKind.NO_CODE)
        # Not cached for rejected_ttl: without Beta's answer Beta alone is asked again after
        # the retry delay (and the pending watch keeps asking Beta about it meanwhile).
        assert not cache.is_rejected(ETH)
        again = await validator.validate(ETH)
        assert (again.status, again.kind) == (
            ValidationStatus.ALREADY_REJECTED,
            AddressKind.NO_CODE,
        )
        for clock.now in (100, 200, 301):
            await validator.validate(ETH, negative_cache=False)
        assert [[(e.name, e.optional) for e in endpoints] for endpoints in reports] == [
            [("Beta", True)]
        ]
        with pytest.raises(RpcUnavailable):  # nothing answered at all: still no verdict
            await lenient.find_network(USDC_ETH, networks={"Beta"})
        rpc.route("alpha.rpc", erc20_contract)  # a token on the required network wins
        assert await lenient.find_network(OTHER) == ChainVerdict(AddressKind.TOKEN, "Alpha")


async def test_an_answer_clears_the_failing_endpoint(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)  # beta unreachable
    clock = FakeClock()
    async with rpc.client() as http:
        validator = evm(http, clock=clock)
        with pytest.raises(RpcUnavailable):
            await validator.find_network(USDC_ETH)
        clock.now = 400
        assert validator.health.failing(300) == []  # nobody asked since: not "down"
        with pytest.raises(RpcUnavailable):
            await validator.find_network(USDC_ETH)
        assert [(e.name, e.seconds) for e in validator.health.failing(300)] == [("Beta", 400)]
        rpc.route("beta.rpc", no_contract)
        await validator.find_network(USDC_ETH)
        assert validator.health.failing(0) == []


async def test_hiccups_far_apart_are_not_one_long_outage() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)  # beta unreachable
    clock = FakeClock()
    async with rpc.client() as http:
        validator = evm(http, clock=clock)
        for clock.now in (0, 600, 1200):  # a stuck address retried every RPC_RETRY_DELAY=600
            with pytest.raises(RpcUnavailable):
                await validator.find_network(USDC_ETH)
        assert [(e.name, e.seconds) for e in validator.health.failing(300)] == [("Beta", 1200)]
        rpc.route("beta.rpc", no_contract)
        await validator.find_network(USDC_ETH)
        rpc.routes.pop("beta.rpc")
        for clock.now in (5000, 9000):  # two hiccups an hour apart
            with pytest.raises(RpcUnavailable):
                await validator.find_network(USDC_ETH)
        assert validator.health.failing(300) == []


async def test_a_token_is_reported_without_waiting_for_slower_networks() -> None:
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        items = body if isinstance(body, list) else [body]
        if request.url.host == "beta.rpc":
            await release.wait()  # a node that hangs
            answers = [{"jsonrpc": "2.0", "id": i["id"], "result": "0x"} for i in items]
        else:
            answers = [{"jsonrpc": "2.0", "id": i["id"], "result": erc20_answer(i)} for i in items]
        return httpx.Response(200, json=answers if isinstance(body, list) else answers[0])

    def erc20_answer(item: dict[str, Any]) -> str:
        if item["method"] == "eth_getCode":
            return "0x6080604052"
        selector = item["params"][0]["data"]
        return {ERC20_TOTAL_SUPPLY: word(10**18), ERC20_DECIMALS: word(18)}.get(selector, "0x")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        validator = evm(http)
        verdict = await asyncio.wait_for(validator.find_network(USDC_ETH), timeout=2)
        assert verdict == ChainVerdict(AddressKind.TOKEN, "Alpha")
        assert len(validator._background) == 1  # Beta's request is still running
        release.set()
        for _ in range(100):
            if not validator._background:
                break
            await asyncio.sleep(0.01)
        assert not validator._background and validator.health.failing(0) == []

        release.clear()
        await validator.find_network(USDC_ETH)
        assert len(validator._background) == 1
        await validator.aclose()  # shutdown stops what is left
        assert not validator._background


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
        strict = sui(http, tuning=FakeTuning(require_erc20=True))
        assert await strict.is_coin_or_package(SUI_COIN)
        assert await strict.is_coin_or_package(SUI_PKG)  # the package defines a coin
        assert not await strict.is_coin_or_package(SUI_STRUCT)  # a type, not a coin
        assert not await strict.is_coin_or_package(SUI_WALLET)
        assert not await strict.is_coin_or_package(SUI_OBJECT)
        lenient = sui(http, tuning=FakeTuning(require_erc20=False))
        assert await lenient.is_coin_or_package(SUI_STRUCT)  # any type of a real package
        assert not await lenient.is_coin_or_package(SUI_WALLET)

        rpc.route("sui.rpc", sui_node(coins={}, packages={SUI_PKG: {"helpers": ["Config"]}}))
        assert not await strict.is_coin_or_package(SUI_PKG)  # no coin inside
        assert await lenient.is_coin_or_package(SUI_PKG)


async def test_sui_transport_error_is_unavailable() -> None:
    rpc = FakeRpc()  # nothing routed: every call fails to connect
    async with rpc.client() as http:
        validator = sui(http)
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
            evm(http),
            solana(http),
            cache,
            max_concurrent=4,
            retry_delay=60,
            clock=clock,
            sui=sui(http),
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
        strict = sui(http)
        assert await strict.is_coin_or_package(SUI_PKG)
    assert rpc.count("sui.rpc", "suix_getCoinMetadata") == 21


async def test_a_node_behind_the_balancer_makes_it_no_code_never_not_a_token() -> None:
    seen: list[str] = []

    def lagging(method: str, params: list[Any]) -> Any:
        # The single eth_getCode reaches a node at the new block, the probe batch one
        # block behind, where the contract does not exist and every call answers "0x".
        seen.append(method)
        if method == "eth_getCode":
            return "0x6080604052" if seen.count("eth_getCode") == 1 else "0x"
        return "0x"

    rpc = FakeRpc()
    rpc.route("alpha.rpc", lagging)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        assert (await evm(http).find_network(USDC_ETH)).kind is AddressKind.NO_CODE


async def test_a_failure_finishing_with_the_token_still_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rpc = FakeRpc()
    async with rpc.client() as http:
        validator = evm(http)
        release: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def at_the_same_moment(client: Any, _address: str) -> ChainVerdict:
            await release  # both networks answer in the same event-loop step
            if client is validator._clients[0][1]:
                return ChainVerdict(AddressKind.TOKEN)
            raise RpcError("beta failed")

        validator._check = at_the_same_moment  # type: ignore[method-assign]
        lookup = asyncio.create_task(validator.find_network(USDC_ETH))
        await asyncio.sleep(0)
        release.set_result(None)
        assert await lookup == ChainVerdict(AddressKind.TOKEN, "Alpha")
        assert [e.name for e in validator.health.failing(0)] == ["Beta"]
    assert "never retrieved" not in caplog.text


async def test_a_lookup_can_be_limited_to_some_networks() -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", erc20_contract)
    rpc.route("beta.rpc", erc20_contract)
    async with rpc.client() as http:
        verdict = await evm(http).find_network(USDC_ETH, networks={"Beta"})
    assert verdict == ChainVerdict(AddressKind.TOKEN, "Beta")
    assert rpc.count("alpha.rpc") == 0


async def test_urgent_checks_skip_the_queue(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", erc20_contract)
    rpc.route("beta.rpc", no_contract)
    async with rpc.client() as http:
        validator = build(rpc, http, tmp_path, FakeClock())
        slots = validator._semaphore
        async with slots, slots, slots, slots:  # every one of the four slots is taken
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(validator.validate(SOL), timeout=0.05)
            result = await asyncio.wait_for(validator.validate(ETH, urgent=True), 1)
    assert result.status is ValidationStatus.CONFIRMED


async def test_closing_cancels_checks_still_running(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def hanging(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(hanging)) as http:
        cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=1000)
        validator = ContractValidator(
            evm(http), solana(http), cache, sui=sui(http), max_concurrent=4, retry_delay=60
        )
        caller = asyncio.create_task(validator.validate(ETH))
        await started.wait()
        await validator.aclose()
        assert validator._inflight == {}
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


async def test_a_node_without_batches_answers_about_one_block() -> None:
    seen: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(400, json={"error": "batch requests are not supported"})
        method, params = body["method"], body["params"]
        seen.append((method, params[-1] if params else None))
        if method == "eth_blockNumber":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x10"})
        if method == "eth_getCode":
            result: object = "0x6080604052"
        elif params[0]["data"] in {ERC20_TOTAL_SUPPLY, ERC20_DECIMALS}:
            result = word(18)
        else:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "revert"}}
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        validator = EvmContractValidator(
            http, NETWORKS[:1], timeout=5, tuning=FakeTuning(), retry=NO_RETRY
        )
        assert (await validator.find_network(USDC_ETH)).kind is AddressKind.TOKEN
    probes = [block for method, block in seen if method in {"eth_call", "eth_getCode"}][1:]
    assert probes and set(probes) == {"0x10"}  # after the first look: all at block 0x10


async def test_checks_of_different_kinds_are_not_shared(tmp_path: Path) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)
    rpc.route("beta.rpc", erc20_contract)
    async with rpc.client() as http:
        validator = build(rpc, http, tmp_path, FakeClock())
        restricted, full = await asyncio.gather(
            validator.validate(ETH, negative_cache=False, networks={"Alpha"}, urgent=True),
            validator.validate(ETH),
        )
    assert restricted.kind is AddressKind.NO_CODE  # asked Alpha only
    assert full.contract == ConfirmedContract(ETH, "Beta")  # not handed Alpha's verdict


async def test_a_cancelled_caller_does_not_leave_the_token_known(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await release.wait()  # the node answers after the caller is gone
        body = json.loads(request.content)
        items = body if isinstance(body, list) else [body]
        answers = []
        for item in items:
            if item["method"] == "eth_getCode":
                answers.append({"jsonrpc": "2.0", "id": item["id"], "result": "0x6080604052"})
            elif item["params"][0]["data"] in {ERC20_TOTAL_SUPPLY, ERC20_DECIMALS}:
                answers.append({"jsonrpc": "2.0", "id": item["id"], "result": word(18)})
            else:
                error = {"code": 3, "message": "execution reverted"}
                answers.append({"jsonrpc": "2.0", "id": item["id"], "error": error})
        return httpx.Response(200, json=answers if isinstance(body, list) else answers[0])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        cache = AddressCache(tmp_path / "known.tsv", tmp_path / "rejected.tsv", rejected_ttl=60)
        cache.load()
        validator = ContractValidator(
            evm(http), solana(http), cache, sui=sui(http), max_concurrent=4, retry_delay=60
        )
        round_task = asyncio.create_task(validator.validate_many([ETH]))
        await asyncio.sleep(0.05)
        round_task.cancel()  # /pause, /remove or a shutdown during the check
        await asyncio.gather(round_task, return_exceptions=True)
        release.set()
        await asyncio.sleep(0.05)  # the shared lookup finishes on its own
        assert not cache.is_known(ETH)
        again = await validator.validate(ETH)
        assert again.status is ValidationStatus.CONFIRMED  # so the next round announces it


async def test_one_failing_address_does_not_cost_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rpc = FakeRpc()
    rpc.route("alpha.rpc", no_contract)
    rpc.route("beta.rpc", no_contract)
    rpc.route("sol.rpc", solana_missing)
    async with rpc.client() as http:
        validator = build(rpc, http, tmp_path, FakeClock())
        original = validator._lookup

        async def lookup(address: Address, networks: Any) -> ChainVerdict:
            if address == ETH:
                raise ValueError("an odd answer")
            return await original(address, networks)

        monkeypatch.setattr(validator, "_lookup", lookup)
        results = await validator.validate_many([ETH, SOL])
    assert [r.status for r in results] == [
        ValidationStatus.UNAVAILABLE,
        ValidationStatus.REJECTED,
    ]
    assert "an odd answer" in caplog.text


async def test_a_silent_optional_network_costs_a_verdict_only_the_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scraper_bot.chains.evm import validator as evm_validator

    monkeypatch.setattr(evm_validator, "OPTIONAL_GRACE", 0.05)
    hang = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.host == "beta.rpc":
            await hang.wait()  # the optional network hangs
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0x"})

    networks = (NETWORKS[0], EvmNetwork("Beta", "https://beta.rpc", optional=True))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        validator = EvmContractValidator(
            http, networks, timeout=5, tuning=FakeTuning(), retry=NO_RETRY
        )
        verdict = await asyncio.wait_for(validator.find_network(USDC_ETH), timeout=1)
        assert verdict == ChainVerdict(AddressKind.NO_CODE, missing=frozenset({"Beta"}))
        assert len(validator._background) == 1  # Beta's request finishes in the background
        await validator.aclose()


def test_a_long_outage_is_reported_once_and_again_after_recovery() -> None:
    from scraper_bot.chains.evm.validator import EvmContractValidator
    from scraper_bot.chains.solana.validator import SolanaContractValidator
    from scraper_bot.chains.sui.validator import SuiCoinValidator
    from scraper_bot.chains.validator import ContractValidator
    from scraper_bot.settings.env import EvmNetwork
    from scraper_bot.storage.address_cache import AddressCache

    clock = FakeClock()
    http = httpx.AsyncClient()
    networks = [
        EvmNetwork("Base", "https://base.rpc"),
        EvmNetwork("Arc", "https://arc.rpc", 5042, True),
    ]
    reports: list[tuple[str, ...]] = []
    validator = ContractValidator(
        EvmContractValidator(http, networks, timeout=1, tuning=FakeTuning(), clock=clock),
        SolanaContractValidator(http, "https://sol.rpc", timeout=1, clock=clock),
        AddressCache(Path("unused-k.tsv"), Path("unused-r.tsv"), rejected_ttl=0),
        sui=SuiCoinValidator(http, "https://sui.rpc", timeout=1, tuning=FakeTuning(), clock=clock),
        max_concurrent=4,
        retry_delay=10,
        clock=clock,
        on_stuck=lambda endpoints, _delay: reports.append(tuple(e.name for e in endpoints)),
    )
    boom = RpcError("timed out")
    for minute in range(0, 180, 1):  # three hours of an Arc outage, a failure a minute
        clock.now = minute * 60.0
        validator.record("Arc", boom)
    assert reports == [("Arc",)]  # one message for the whole outage
    clock.now += 30
    validator.record("Base", boom)
    for _ in range(6):
        clock.now += 60
        validator.record("Base", boom)
        validator.record("Arc", boom)
    assert reports == [("Arc",), ("Arc", "Base")]  # Base joined: said once, both listed
    validator.record("Arc", None)  # Arc answers again: its outage is over
    for _ in range(6):
        clock.now += 60
        validator.record("Arc", boom)
    # A new Arc outage (Base, not asked for minutes, is no longer listed as failing).
    assert reports[-1] == ("Arc",) and len(reports) == 3


def test_an_optional_endpoint_that_keeps_failing_rests_between_probes() -> None:
    from scraper_bot.chains.rpc.health import EndpointHealth

    clock = FakeClock()
    health = EndpointHealth(clock, optional={"Arc"})
    boom = RpcError("timed out")
    health.failed("Arc", boom)
    assert not health.resting("Arc")  # one failure: still asked
    health.failed("Arc", boom)
    probes = []
    for second in range(70):
        clock.now = float(second)
        if not health.resting("Arc"):
            probes.append(second)
    assert probes == [0, 2, 6, 14, 30, 60]  # at once, then 2, 4, 8, 16 and 30 s apart
    health.answered("Arc")
    assert not health.resting("Arc") and not health.resting("Arc")  # back in service
    health.failed("Base", boom)
    health.failed("Base", boom)
    assert not health.resting("Base") and not health.resting("Base")  # required: always


def slow_network(delays: dict[str, float], code: dict[str, str], requests: list[str]):
    """JSON-RPC nodes by host: each answers after its delay; ``code`` gives bytecode."""

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        requests.append(host)
        await asyncio.sleep(delays.get(host, 0))
        body = json.loads(request.content)
        items = body if isinstance(body, list) else [body]
        answers = []
        for item in items:
            method = item["method"]
            if method == "eth_getCode":
                result: Any = code.get(host, "0x")
            elif method == "eth_blockNumber":
                result = "0x10"
            elif method == "eth_call":
                data = item["params"][0].get("data", "")
                table = {"0x18160ddd": "0x" + "0" * 63 + "1", "0x313ce567": "0x" + "0" * 62 + "12"}
                if data not in table:
                    answers.append(
                        {
                            "jsonrpc": "2.0",
                            "id": item["id"],
                            "error": {"code": 3, "message": "revert"},
                        }
                    )
                    continue
                result = table[data]
            else:
                result = "0x0"
            answers.append({"jsonrpc": "2.0", "id": item["id"], "result": result})
        return httpx.Response(200, json=answers if isinstance(body, list) else answers[0])

    return handler


async def until(condition: Callable[[], object]) -> None:
    """Let the background work run until ``condition()`` holds; fail after about five seconds."""
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the background work did not finish")


def optional_validator(http: httpx.AsyncClient, tmp_path: Path, clock: FakeClock):
    networks = (NETWORKS[0], EvmNetwork("Beta", "https://beta.rpc", optional=True))
    evm_validator = EvmContractValidator(
        http, networks, timeout=5, tuning=FakeTuning(), retry=NO_RETRY, clock=clock
    )
    cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=1000, clock=clock)
    cache.load()
    return ContractValidator(
        evm_validator,
        solana(http, clock=clock),
        cache,
        sui=sui(http, clock=clock),
        max_concurrent=4,
        retry_delay=60,
        clock=clock,
    )


async def test_a_token_an_optional_network_finds_too_late_is_confirmed_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scraper_bot.chains.evm import validator as evm_validator

    monkeypatch.setattr(evm_validator, "OPTIONAL_GRACE", 0.02)
    requests: list[str] = []
    handler = slow_network({"beta.rpc": 0.1}, {"beta.rpc": "0x6080"}, requests)
    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        validator = optional_validator(http, tmp_path, clock)
        told: list[tuple[Address, str]] = []
        validator.on_late_token = lambda address, network: told.append((address, network))
        first = await validator.validate(ETH)
        assert (first.status, first.kind) == (ValidationStatus.REJECTED, AddressKind.NO_CODE)
        await asyncio.sleep(0.4)  # Beta answers now: a token, too late for that verdict
        assert told == [(ETH, "Beta")]
        before = len(requests)
        again = await validator.validate(ETH)  # the next round: no waiting for the delay
        assert (again.status, again.network) == (ValidationStatus.CONFIRMED, "Beta")
        assert len(requests) == before  # answered from what Beta said, nobody asked again


async def test_a_partial_rejection_asks_only_the_silent_network_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scraper_bot.chains.evm import validator as evm_validator

    monkeypatch.setattr(evm_validator, "OPTIONAL_GRACE", 0.02)
    requests: list[str] = []
    answer = slow_network({}, {}, requests)
    beta_down = True

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "beta.rpc" and beta_down:
            requests.append("beta.rpc")
            return httpx.Response(502)
        return await answer(request)

    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        validator = optional_validator(http, tmp_path, clock)
        assert (await validator.validate(ETH)).kind is AddressKind.NO_CODE
        assert not validator._cache.is_rejected(ETH)  # Beta failed: not final
        beta_down = False
        requests.clear()
        clock.now = 61  # the retry delay has passed
        result = await validator.validate(ETH)  # answered at once; Beta is asked meanwhile
        assert (result.status, result.kind) == (
            ValidationStatus.ALREADY_REJECTED,
            AddressKind.NO_CODE,
        )
        await until(lambda: validator._cache.is_rejected(ETH))  # cached like any rejection now
        assert requests == ["beta.rpc"]  # Alpha already said "no code"


async def test_a_resting_optional_network_is_not_waited_for(tmp_path: Path) -> None:
    requests: list[str] = []
    clock = FakeClock()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(slow_network({}, {}, requests))
    ) as http:
        validator = optional_validator(http, tmp_path, clock)
        health = validator._evm.health
        boom = RpcError("HTTP 502")
        health.failed("Beta", boom)
        health.failed("Beta", boom)
        assert not health.resting("Beta")  # this probe slot is used up here
        verdict = await validator._evm.find_network(ETH.value)
        assert verdict.missing == frozenset({"Beta"}) and "beta.rpc" not in requests


async def test_a_late_no_code_completes_the_rejection_without_a_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scraper_bot.chains.evm import validator as evm_validator

    monkeypatch.setattr(evm_validator, "OPTIONAL_GRACE", 0.02)
    requests: list[str] = []
    answer = slow_network({}, {}, requests)
    beta_answers = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "beta.rpc":
            await beta_answers.wait()  # late for sure, however long Alpha takes
        return await answer(request)

    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        validator = optional_validator(http, tmp_path, clock)
        assert (await validator.validate(ETH)).kind is AddressKind.NO_CODE
        assert not validator._cache.is_rejected(ETH)  # Beta has not answered yet
        beta_answers.set()  # now it does: nothing there either
        await until(lambda: validator._cache.is_rejected(ETH))
        assert ETH not in validator._partial
        requests.clear()
        clock.now = 61
        assert (await validator.validate(ETH)).status is ValidationStatus.ALREADY_REJECTED
        assert requests == []  # no wave of re-checks


async def test_a_recheck_while_the_optional_network_is_still_down_waits_again(
    tmp_path: Path,
) -> None:
    requests: list[str] = []
    answer = slow_network({}, {}, requests)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "beta.rpc":
            requests.append("beta.rpc")
            return httpx.Response(502)
        return await answer(request)

    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        validator = optional_validator(http, tmp_path, clock)
        await validator.validate(ETH)
        clock.now = 61
        await validator.validate(ETH)  # Beta alone, in the background, and it fails again
        await asyncio.sleep(0.05)
        assert validator._partial[ETH].due == 61 + 60 and not validator._cache.is_rejected(ETH)


def test_the_memories_of_pending_rechecks_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scraper_bot.chains import validator

    monkeypatch.setattr(validator, "_MAX_PARTIAL", 3)
    clock = FakeClock()
    validator = optional_validator(httpx.AsyncClient(), tmp_path, clock)
    addresses = [Address(Chain.ETHEREUM, f"0x{i:040x}") for i in range(1, 6)]
    for address in addresses:
        validator._set_partial(address, frozenset({"Beta"}), AddressKind.NO_CODE)
        validator._retry_later(address)
    assert list(validator._partial) == addresses[2:]  # the oldest are forgotten first
    assert len(validator._retry_after) == 3
    clock.now = 1_000  # all expired: dropped at once when the next one comes
    validator._retry_later(addresses[0])
    assert list(validator._retry_after) == [addresses[0]]


def test_late_answers_from_two_optional_networks_complete_a_rejection(tmp_path: Path) -> None:
    clock = FakeClock()
    validator = optional_validator(httpx.AsyncClient(), tmp_path, clock)
    validator._set_partial(ETH, frozenset({"Beta", "Gamma"}), AddressKind.NO_CODE)
    nothing = ChainVerdict(AddressKind.NO_CODE)
    validator._late_answer(ETH.value, "Delta", nothing)  # not one it waits for
    validator._late_answer(ETH.value, "Beta", nothing)
    assert validator._partial[ETH].missing == frozenset({"Gamma"})
    validator._late_answer(ETH.value, "Gamma", nothing)
    assert ETH not in validator._partial and validator._cache.is_rejected(ETH)


async def test_a_recheck_never_holds_up_the_page_that_shows_the_address(tmp_path: Path) -> None:
    requests: list[str] = []
    answer = slow_network({}, {}, requests)
    beta = {"down": True}
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "beta.rpc":
            if beta["down"]:
                return httpx.Response(502)
            await release.wait()  # hangs now
        return await answer(request)

    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        validator = optional_validator(http, tmp_path, clock)
        await validator.validate(ETH)  # Beta down: a partial rejection
        beta["down"] = False
        clock.now = 61
        result = await asyncio.wait_for(validator.validate(ETH), timeout=0.5)
        assert result.status is ValidationStatus.ALREADY_REJECTED  # nobody waited for Beta
        assert len(validator._rechecks) == 1
        release.set()
        await until(lambda: not validator._rechecks)  # the re-check ends once Beta answers


async def test_a_token_found_by_a_background_recheck_is_confirmed_next_time(
    tmp_path: Path,
) -> None:
    requests: list[str] = []
    state = {"beta": "down"}
    quiet = slow_network({}, {}, requests)
    token = slow_network({}, {"beta.rpc": "0x6080"}, requests)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "beta.rpc" and state["beta"] == "down":
            return httpx.Response(502)
        return await (token if state["beta"] == "token" else quiet)(request)

    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        validator = optional_validator(http, tmp_path, clock)
        told: list[tuple[Address, str]] = []
        validator.on_late_token = lambda address, network: told.append((address, network))
        await validator.validate(ETH)
        state["beta"] = "token"  # deployed on Beta meanwhile
        clock.now = 61
        await validator.validate(ETH)  # starts the re-check, answers at once
        await until(lambda: told)
        assert told == [(ETH, "Beta")]  # the pending watch hears at once
        requests.clear()
        confirmed = await validator.validate(ETH)  # the next check of the page
        assert (confirmed.status, confirmed.network) == (ValidationStatus.CONFIRMED, "Beta")
        assert requests == []  # from what the re-check found, nobody asked again


async def test_an_answer_in_the_same_step_as_a_token_is_not_late() -> None:
    clock = FakeClock()
    networks = (NETWORKS[0], EvmNetwork("Beta", "https://beta.rpc", optional=True))
    evm_validator = EvmContractValidator(
        httpx.AsyncClient(), networks, timeout=5, tuning=FakeTuning(), clock=clock
    )
    heard: list[str] = []
    evm_validator.on_late = lambda address, name, verdict: heard.append(name)
    done: asyncio.Future[ChainVerdict] = asyncio.get_running_loop().create_future()
    done.set_result(ChainVerdict(AddressKind.NO_CODE))
    evm_validator._finished("Beta", ETH.value, done, late=False)
    assert heard == []
    evm_validator._finished("Beta", ETH.value, done)
    assert heard == ["Beta"]
