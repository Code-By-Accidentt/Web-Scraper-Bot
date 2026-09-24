import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from scraper_bot.chains.rpc import JsonRpcClient, RpcError
from scraper_bot.chains.sui import (
    COIN_CHECK_BATCH,
    MAX_COIN_CANDIDATES,
    coin_metadata,
    coin_type_candidates,
    find_coin,
    object_kind,
    total_supply,
)

PKG = "0x" + "ab" * 32
COIN = f"{PKG}::usdc::USDC"
OBJECT = "0x" + "cd" * 32
WALLET = "0x" + "ef" * 32
Responder = Callable[[str, list[Any]], Any]


class Fail(Exception):
    """The node answers this request with an HTTP 500."""


class FakeSui:
    """One Sui node behind an httpx transport: routes methods to a callable, records calls."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.calls: list[tuple[str, list[Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append((body["method"], body["params"]))
        try:
            result = self.responder(body["method"], body["params"])
        except Fail:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def client(self) -> JsonRpcClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return JsonRpcClient(http, "https://sui.rpc", timeout=5)

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)


def package_node(
    structs: dict[str, list[str]],
    coins: dict[str, dict[str, Any]],
    failing: frozenset[str] = frozenset(),
) -> Responder:
    """A package with the given modules/structs; ``coins`` have metadata, ``failing`` 500."""

    def responder(method: str, params: list[Any]) -> Any:
        if method == "sui_getNormalizedMoveModulesByPackage":
            return {m: {"structs": {s: {} for s in names}} for m, names in structs.items()}
        if method == "suix_getCoinMetadata":
            if params[0] in failing:
                raise Fail()
            return coins.get(params[0])
        raise Fail()

    return responder


async def test_object_kind_tells_packages_objects_and_nothing_apart() -> None:
    kinds: dict[str, Any] = {
        PKG: {"data": {"objectId": PKG, "type": "package"}},
        OBJECT: {"data": {"objectId": OBJECT, "type": "0x2::coin::Coin<0x2::sui::SUI>"}},
        WALLET: {"error": {"code": "notExists", "object_id": WALLET}},
        COIN: None,
    }
    node = FakeSui(lambda _method, params: kinds[params[0]])
    client = node.client()
    assert await object_kind(client, PKG) == "package"
    assert await object_kind(client, OBJECT) == "object"
    assert await object_kind(client, WALLET) == "missing"
    assert await object_kind(client, COIN) == "missing"
    assert node.calls[0] == ("sui_getObject", [PKG, {"showType": True}])


async def test_coin_metadata_is_a_dict_or_nothing() -> None:
    answers = iter([{"name": "USDC", "decimals": 6}, None, "USDC", ["x"]])
    client = FakeSui(lambda _m, _p: next(answers)).client()
    assert await coin_metadata(client, COIN) == {"name": "USDC", "decimals": 6}
    assert await coin_metadata(client, COIN) is None
    assert await coin_metadata(client, COIN) is None
    assert await coin_metadata(client, COIN) is None


async def test_total_supply_tolerates_odd_answers() -> None:
    answers = iter(
        [{"value": "734000000"}, {"value": 12}, {"value": "lots"}, {"value": None}, None, "12"]
    )
    client = FakeSui(lambda _m, _p: next(answers)).client()
    assert await total_supply(client, COIN) == 734_000_000
    assert await total_supply(client, COIN) == 12
    assert await total_supply(client, COIN) is None  # not a number
    assert await total_supply(client, COIN) is None  # null value
    assert await total_supply(client, COIN) is None  # no such coin
    assert await total_supply(client, COIN) is None  # not an object at all


async def test_coin_type_candidates_put_witness_structs_first() -> None:
    modules: dict[str, Any] = {
        "lib": {"structs": {"Config": {}, "LIB": {}}},
        "meme": {"structs": {"Helper": {}, "MEME": {}, "Meme": {}}},
        "broken": "not a module",
        "empty": {"structs": []},
        "nothing": {},
    }
    client = FakeSui(lambda _m, _p: modules).client()
    assert await coin_type_candidates(client, PKG) == [
        f"{PKG}::lib::LIB",
        f"{PKG}::meme::MEME",
        f"{PKG}::meme::Meme",  # the witness comparison ignores case
        f"{PKG}::lib::Config",
        f"{PKG}::meme::Helper",
    ]
    assert await coin_type_candidates(FakeSui(lambda _m, _p: ["x"]).client(), PKG) == []
    assert await coin_type_candidates(FakeSui(lambda _m, _p: None).client(), PKG) == []


async def test_coin_type_candidates_are_capped_but_keep_the_witness() -> None:
    many: dict[str, Any] = {
        "lib": {"structs": {f"S{i}": {} for i in range(MAX_COIN_CANDIDATES + 20)}},
        "coin": {"structs": {"COIN": {}}},
    }
    candidates = await coin_type_candidates(FakeSui(lambda _m, _p: many).client(), PKG)
    assert len(candidates) == MAX_COIN_CANDIDATES
    assert candidates[0] == f"{PKG}::coin::COIN"
    assert len(set(candidates)) == MAX_COIN_CANDIDATES


async def test_find_coin_returns_the_first_coin_with_its_metadata() -> None:
    node = FakeSui(
        package_node({"helpers": ["Config"], "usdc": ["USDC"]}, {COIN: {"symbol": "USDC"}})
    )
    assert await find_coin(node.client(), PKG) == (COIN, {"symbol": "USDC"})
    assert node.count("sui_getNormalizedMoveModulesByPackage") == 1
    assert node.count("suix_getCoinMetadata") == 2  # one batch tries both structs


async def test_find_coin_gives_no_verdict_when_a_lookup_failed() -> None:
    structs = {"lib": ["A", "B"]}
    node = FakeSui(package_node(structs, {}, failing=frozenset({f"{PKG}::lib::A"})))
    with pytest.raises(RpcError, match=r"suix_getCoinMetadata via sui\.rpc: HTTP 500"):
        await find_coin(node.client(), PKG)
    # Nothing failed: a package without coins is simply not a coin.
    assert await find_coin(FakeSui(package_node(structs, {})).client(), PKG) is None
    assert await find_coin(FakeSui(package_node({}, {})).client(), PKG) is None


async def test_find_coin_keeps_searching_after_a_failed_batch() -> None:
    # No struct is named COIN, so the order is the declaration order: the coin sits in
    # the second batch and a lookup in the first batch fails.
    names = [f"S{i}" for i in range(COIN_CHECK_BATCH)] + ["MEME"]
    coin = f"{PKG}::coin::MEME"
    node = FakeSui(
        package_node(
            {"coin": names}, {coin: {"decimals": 9}}, failing=frozenset({f"{PKG}::coin::S3"})
        )
    )
    assert await find_coin(node.client(), PKG) == (coin, {"decimals": 9})
    assert node.count("suix_getCoinMetadata") == COIN_CHECK_BATCH + 1
