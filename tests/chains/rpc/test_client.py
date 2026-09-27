import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from scraper_bot.chains.rpc.client import (
    BATCH_LIMIT,
    NO_RETRY,
    JsonRpcClient,
    RetryPolicy,
    RpcCallError,
    RpcError,
    parse_retry_after,
)

# The path carries an API key, as the URLs of public node providers do.
URL = "https://node.example/v2/SECRET-API-KEY"
Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> JsonRpcClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JsonRpcClient(http, URL, timeout=5, retry=NO_RETRY)


class Clock:
    """A fake clock whose sleeps only move time forward (and are recorded)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def retrying(handler: Handler, clock: Clock, timeout: float = 5) -> JsonRpcClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JsonRpcClient(
        http, URL, timeout=timeout, retry=RetryPolicy(), clock=clock, sleep=clock.sleep
    )


def scripted(*responses: httpx.Response | Exception) -> tuple[Handler, list[httpx.Request]]:
    """A node answering with ``responses`` in turn; the last one repeats."""
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    return handler, seen


OK = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})


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


# ── retries ─────────────────────────────────────────────────────────────────
async def test_quick_failures_are_retried_with_growing_pauses() -> None:
    clock = Clock()
    handler, seen = scripted(httpx.Response(502), httpx.Response(503), OK)
    assert await retrying(handler, clock).call("eth_chainId", []) == "0x1"
    assert len(seen) == 3
    first, second = clock.sleeps
    assert 0.2 <= first <= 0.3 and 0.8 <= second <= 1.2  # 0.25 s and 1 s, with jitter


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.RemoteProtocolError("GOAWAY"),  # an HTTP/2 connection the server dropped
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}}),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": "Rate limit"}}),
    ],
)
async def test_resets_and_rate_limits_inside_a_200_are_retried(failure: Any) -> None:
    handler, seen = scripted(failure, OK)
    assert await retrying(handler, Clock()).call("eth_chainId", []) == "0x1"
    assert len(seen) == 2


async def test_timeouts_and_other_answers_are_not_retried() -> None:
    for failure in (httpx.ReadTimeout("slow"), httpx.Response(404), httpx.Response(200, text="?")):
        handler, seen = scripted(failure, OK)
        with pytest.raises(RpcError):
            await retrying(handler, Clock()).call("eth_chainId", [])
        assert len(seen) == 1


async def test_retries_give_up_after_three_attempts() -> None:
    handler, seen = scripted(httpx.Response(500))
    with pytest.raises(RpcError, match="HTTP 500"):
        await retrying(handler, Clock()).call("eth_chainId", [])
    assert len(seen) == 3


async def test_a_429_honours_retry_after_and_cools_the_host_down() -> None:
    clock = Clock()
    handler, seen = scripted(httpx.Response(429, headers={"Retry-After": "2"}), OK)
    rpc = retrying(handler, clock)
    assert await rpc.call("eth_chainId", []) == "0x1"
    assert clock.sleeps[0] >= 2  # waited for Retry-After (not just 0.25 s)
    handler, _ = scripted(httpx.Response(429, headers={"Retry-After": "20"}))
    long_wait = retrying(handler, clock, timeout=5)
    with pytest.raises(RpcError, match=r"HTTP 429|cooling down"):
        await long_wait.call("eth_chainId", [])  # 20 s do not fit into a 5 s call
    # The host stays in its cool-down: the next call gives up without a request.
    handler, seen = scripted(OK)
    long_wait._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RpcError, match="cooling down"):
        await long_wait.call("eth_chainId", [])
    assert seen == []


async def test_a_closed_client_is_an_rpc_error() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(scripted(OK)[0]))
    await http.aclose()
    with pytest.raises(RpcError, match=r"eth_chainId via node\.example"):
        await JsonRpcClient(http, URL, timeout=5).call("eth_chainId", [])


# ── batches ─────────────────────────────────────────────────────────────────
async def test_batch_answers_are_matched_by_id_and_errors_stay_per_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert [item["id"] for item in body] == [0, 1, 2]
        return httpx.Response(
            200,
            json=[
                {"jsonrpc": "2.0", "id": 2, "error": {"code": 3, "message": "execution reverted"}},
                {"jsonrpc": "2.0", "id": 0, "result": "0xa"},
                # id 1 is missing from the answer
            ],
        )

    first, second, third = await client(handler).call_batch(
        [("eth_getCode", ["0x1"]), ("eth_getCode", ["0x2"]), ("eth_call", [])]
    )
    assert first == "0xa"
    assert isinstance(second, RpcError) and "missing from the batch" in str(second)
    assert isinstance(third, RpcCallError) and third.code == 3


async def test_a_node_without_batches_gets_the_calls_one_by_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": None, "error": "no batches"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": body["params"][0]})

    assert await client(handler).call_batch([("echo", ["a"]), ("echo", ["b"])]) == ["a", "b"]


async def test_large_batches_are_split() -> None:
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sizes.append(len(body))
        return httpx.Response(
            200, json=[{"jsonrpc": "2.0", "id": i["id"], "result": i["params"][0]} for i in body]
        )

    calls = [("echo", [n]) for n in range(BATCH_LIMIT + 3)]
    assert await client(handler).call_batch(calls) == list(range(BATCH_LIMIT + 3))
    assert sorted(sizes) == [3, BATCH_LIMIT]


async def test_a_refused_batch_falls_back_to_single_calls() -> None:
    kinds: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kinds.append("batch" if isinstance(body, list) else "single")
        if isinstance(body, list):
            return httpx.Response(400, json={"error": "batch requests are not supported"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": body["params"][0]})

    rpc = client(handler)
    assert await rpc.call_batch([("echo", ["a"]), ("echo", ["b"])]) == ["a", "b"]
    assert await rpc.call_batch([("echo", ["c"])]) == ["c"]
    assert kinds == ["batch", "single", "single", "single"]  # asked once, remembered


async def test_a_rate_limit_inside_a_batch_answer_is_retried() -> None:
    limited = httpx.Response(
        200, json=[{"jsonrpc": "2.0", "id": 0, "error": {"code": -32005, "message": "limit"}}]
    )
    fine = httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 0, "result": "0x1"}])
    handler, seen = scripted(limited, fine)
    assert await retrying(handler, Clock()).call_batch([("eth_chainId", [])]) == ["0x1"]
    assert len(seen) == 2


async def test_the_one_by_one_fallback_shares_the_batch_budget() -> None:
    clock = Clock()
    seen: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now += 1.0  # the batch answer takes the whole 1 s budget
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": None, "error": "no batches"})

    results = await retrying(handler, clock, timeout=1).call_batch([("a", []), ("b", [])])
    assert all(isinstance(result, RpcError) for result in results)
    assert len(seen) == 1  # no fresh budget for the single calls


async def test_the_timeout_is_a_hard_deadline() -> None:
    import asyncio

    async def dripping(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)  # httpx's per-read timeout would let a slow drip run on
        return OK

    http = httpx.AsyncClient(transport=httpx.MockTransport(dripping))
    rpc = JsonRpcClient(http, URL, timeout=0.1, retry=NO_RETRY)
    with pytest.raises(RpcError, match="timed out"):
        await rpc.call("eth_chainId", [])


async def test_a_429_pauses_every_client_of_the_same_pool() -> None:
    clock = Clock()
    handler, seen = scripted(httpx.Response(429, headers={"Retry-After": "20"}), OK)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def make() -> JsonRpcClient:
        return JsonRpcClient(
            http, URL, timeout=5, retry=RetryPolicy(), clock=clock, sleep=clock.sleep
        )

    first, second = make(), make()
    with pytest.raises(RpcError):
        await first.call("eth_chainId", [])
    assert len(seen) == 1  # no retry sleeps into a pause longer than the budget
    with pytest.raises(RpcError, match="cooling down"):
        await second.call("eth_chainId", [])  # another client, same host: no request
    assert len(seen) == 1
    other = JsonRpcClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), URL, timeout=5, clock=clock
    )
    assert await other.call("eth_chainId", []) == "0x1"  # another pool is not affected


async def test_a_node_gets_batches_again_after_a_while() -> None:
    clock = Clock()
    kinds: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kinds.append("batch" if isinstance(body, list) else "single")
        if isinstance(body, list) and len(kinds) == 1:
            return httpx.Response(413)  # a proxy with a size limit, this once
        if isinstance(body, list):
            return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 0, "result": "0x1"}])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})

    rpc = retrying(handler, clock)
    await rpc.call_batch([("eth_chainId", [])])
    assert not rpc.batches
    clock.now += 601
    assert rpc.batches
    await rpc.call_batch([("eth_chainId", [])])
    assert kinds == ["batch", "single", "batch"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120", 120.0),
        (" 5 ", 5.0),
        ("0", 0.0),
        ("1.5", 1.5),
        ("-1", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected


async def test_a_contracts_own_revert_message_is_not_a_rate_limit() -> None:
    reverted = httpx.Response(
        200,
        json=[
            {"jsonrpc": "2.0", "id": 0, "result": "0x" + "00" * 31 + "12"},
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": 3, "message": "execution reverted: Max wallet limit exceeded"},
            },
        ],
    )
    handler, seen = scripted(reverted)
    answers = await retrying(handler, Clock()).call_batch([("a", []), ("b", [])])
    assert answers[0] == "0x" + "00" * 31 + "12"
    assert isinstance(answers[1], RpcCallError) and len(seen) == 1  # answered, not retried


async def test_a_batch_refused_with_one_error_object_falls_back_to_single_calls() -> None:
    kinds: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if isinstance(body, list):
            kinds.append("batch")
            error = {"code": -32600, "message": "batch size limit exceeded"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": None, "error": error})
        kinds.append("single")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": body["params"][0]})

    rpc = retrying(handler, Clock())
    assert await rpc.call_batch([("echo", ["a"]), ("echo", ["b"])]) == ["a", "b"]
    assert kinds == ["batch", "single", "single"] and not rpc.batches


async def test_a_malformed_url_is_an_rpc_error() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(scripted(OK)[0]))
    rpc = JsonRpcClient(http, "https://rpc.example:8545x", timeout=5, retry=NO_RETRY)
    with pytest.raises(RpcError, match="InvalidURL"):
        await rpc.call("eth_chainId", [])
