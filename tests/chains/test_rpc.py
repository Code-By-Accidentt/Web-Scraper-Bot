import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from scraper_bot.chains.rpc import JsonRpcClient, RpcCallError, RpcError

# The path carries an API key, as the URLs of public node providers do.
URL = "https://node.example/v2/SECRET-API-KEY"
Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> JsonRpcClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JsonRpcClient(http, URL, timeout=5)


def answer(**body: Any) -> Handler:
    """A node that answers every request with the given JSON-RPC body fields."""
    return lambda _request: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, **body})


async def test_result_is_returned_and_the_request_is_json_rpc_2() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert str(request.url) == URL
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})

    assert await client(handler).call("eth_getCode", ["0xabc", "latest"]) == "0x1"
    assert seen == [
        {"jsonrpc": "2.0", "id": 1, "method": "eth_getCode", "params": ["0xabc", "latest"]}
    ]
    assert await client(answer()).call("eth_getCode", []) is None  # no result field
    assert await client(answer(result=7, error=None)).call("x", []) == 7  # explicit null error


@pytest.mark.parametrize(
    ("handler", "detail"),
    [
        (lambda _r: httpx.Response(502, text="bad gateway"), "HTTP 502"),
        (lambda _r: httpx.Response(429, json={"error": "slow down"}), "HTTP 429"),
        (lambda _r: httpx.Response(200, text="<html>maintenance</html>"), "invalid JSON"),
        (lambda _r: httpx.Response(200, json=[1, 2]), "unexpected payload"),
        (lambda _r: httpx.Response(200, json="a string"), "unexpected payload"),
    ],
)
async def test_http_and_payload_problems_are_plain_rpc_errors(
    handler: Handler, detail: str
) -> None:
    with pytest.raises(RpcError, match=detail) as info:
        await client(handler).call("eth_call", [])
    assert not isinstance(info.value, RpcCallError)
    assert str(info.value) == f"eth_call via node.example: {detail}"


async def test_transport_failures_name_the_exception_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(RpcError) as info:
        await client(handler).call("eth_call", [])
    assert str(info.value) == "eth_call via node.example: ConnectTimeout: timed out"
    assert not isinstance(info.value, RpcCallError)
    assert isinstance(info.value.__cause__, httpx.ConnectTimeout)


async def test_json_rpc_error_objects_become_call_errors() -> None:
    reverted = answer(error={"code": 3, "message": "execution reverted", "data": "0x"})
    with pytest.raises(RpcCallError) as info:
        await client(reverted).call("eth_call", [])
    assert (info.value.code, info.value.message) == (3, "execution reverted")
    assert str(info.value) == "eth_call via node.example: execution reverted"
    assert isinstance(info.value, RpcError)

    with pytest.raises(RpcCallError) as info:
        await client(answer(error="node is syncing")).call("eth_call", [])
    assert (info.value.code, info.value.message) == (None, "node is syncing")
    assert str(info.value) == "eth_call via node.example: node is syncing"

    # Sui-style error codes are strings: they are not mistaken for numeric codes.
    with pytest.raises(RpcCallError) as info:
        await client(answer(error={"code": "notExists", "message": "gone"})).call("sui_x", [])
    assert (info.value.code, info.value.message) == (None, "gone")

    with pytest.raises(RpcCallError) as info:
        await client(answer(error={"code": -32000})).call("eth_call", [])
    assert info.value.code == -32000 and info.value.message == "{'code': -32000}"


async def test_error_messages_name_method_and_host_but_never_the_url() -> None:
    handlers: list[Handler] = [
        lambda _r: httpx.Response(500, text=f"error for {URL}"),
        lambda _r: httpx.Response(200, text="not json"),
        answer(error={"code": 3, "message": "reverted"}),
    ]

    def connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    handlers.append(connect_error)
    for handler in handlers:
        with pytest.raises(RpcError) as info:
            await client(handler).call("eth_call", [])
        text = str(info.value)
        assert text.startswith("eth_call via node.example: ")
        assert "SECRET-API-KEY" not in text and "/v2/" not in text and "https://" not in text
