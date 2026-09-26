"""A small asynchronous JSON-RPC 2.0 client used by every chain validator and helper.

Only quick failures are retried (HTTP 429 / 5xx, a refused or reset connection, an
HTTP/2 protocol error, a rate-limit error inside a 200 answer), and only within the
call's timeout, which is a hard deadline. A timeout is not retried: the budget is spent.
After an HTTP 429 every client of the same HTTP pool leaves the host alone for its
``Retry-After`` (capped), so a burst does not turn into a storm.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# JSON-RPC error codes and phrases public nodes use for rate limits inside an HTTP 200.
_RATE_LIMIT_CODES = frozenset({-32005, -32029, 429})
_RATE_LIMIT_PHRASES = ("rate limit", "too many requests", "limit exceeded")
BATCH_LIMIT = 50  # calls per HTTP request; public nodes refuse much larger batches
SINGLE_CALLS_FOR = 600.0  # seconds a node that refused a batch gets single calls
# Answers to a batch request that mean "no batches here", not "this call is wrong".
_BATCH_REFUSED_STATUSES = frozenset({400, 405, 413, 415, 422, 501})
# 429 pauses per host, shared by every client of one HTTP pool (see JsonRpcClient).
_COOLDOWNS: weakref.WeakKeyDictionary[httpx.AsyncClient, dict[str, float]] = (
    weakref.WeakKeyDictionary()
)


class RpcError(Exception):
    """Transport failure, non-200 answer or a JSON-RPC error object."""


class RpcCallError(RpcError):
    """The node answered, but with a JSON-RPC error object (e.g. ``execution reverted``)."""

    def __init__(self, text: str, code: int | None, message: str) -> None:
        super().__init__(text)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    delays: tuple[float, ...] = (0.25, 1.0)  # pauses before the 2nd and 3rd attempt
    # longest retry pause taken from a Retry-After; the 429 cooldown below is still
    # waited out when the call's budget allows it
    max_retry_after: float = 3.0
    cooldown_cap: float = 30.0  # longest pause for a host after an HTTP 429


NO_RETRY = RetryPolicy(delays=(), cooldown_cap=0.0)

Sleep = Callable[[float], Awaitable[None]]


class JsonRpcClient:
    """Sends JSON-RPC requests (single or batched) over a shared :class:`httpx.AsyncClient`."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        timeout: float,
        *,
        retry: RetryPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._url = url
        self._timeout = timeout
        self._retry = retry or RetryPolicy()
        self._clock = clock
        self._sleep = sleep
        self._host = urlsplit(url).hostname or url
        # Every client of one HTTP pool (validators, enrichment, pending watch, warm-up)
        # honours the pause a host asked for with an HTTP 429.
        self._cooldowns = _COOLDOWNS.setdefault(http, {})
        self._single_until = 0.0  # a node that refused a batch gets single calls till then

    @property
    def host(self) -> str:
        return self._host

    @property
    def batches(self) -> bool:
        """False while the node is sent single calls (it refused a batch lately)."""
        return self._clock() >= self._single_until

    async def call(self, method: str, params: list[Any]) -> Any:
        """Return the ``result`` of ``method`` or raise :class:`RpcError`."""
        return await self._call(method, params, self._clock() + self._timeout)

    async def call_batch(
        self, calls: Sequence[tuple[str, list[Any]]], *, same_block: bool = False
    ) -> list[Any]:
        """Results in order; a failed element comes back as an :class:`RpcError` instance.

        Batches above :data:`BATCH_LIMIT` are split. A node that does not take batches
        (it refuses them or answers with anything but a list) gets the calls one by one,
        within the same time budget. One batch is answered by one node; single calls
        behind a load balancer may reach several, so with ``same_block`` they all ask
        about the current block number instead of "latest" (a node that lacks the block
        answers with an error, not with an empty result).
        """
        deadline = self._clock() + self._timeout
        chunks = [calls[start : start + BATCH_LIMIT] for start in range(0, len(calls), BATCH_LIMIT)]
        answers = await asyncio.gather(
            *(self._batch(chunk, deadline, same_block) for chunk in chunks)
        )
        return [result for chunk in answers for result in chunk]

    async def _call(self, method: str, params: list[Any], deadline: float) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        data = await self._post(payload, method, deadline)
        if not isinstance(data, dict):
            raise RpcError(f"{method} via {self._host}: unexpected payload")
        return self._result(data, method)

    async def _batch(
        self, calls: Sequence[tuple[str, list[Any]]], deadline: float, same_block: bool
    ) -> list[Any]:
        if not self.batches:
            return await self._one_by_one(calls, deadline, same_block)
        payload = [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(calls)
        ]
        try:
            data = await self._post(payload, "batch", deadline)
        except _BatchRefused as exc:
            self._no_batches(str(exc))
            return await self._one_by_one(calls, deadline, same_block)
        if not isinstance(data, list):
            self._no_batches("the answer was not a list")
            return await self._one_by_one(calls, deadline, same_block)
        by_id = {item.get("id"): item for item in data if isinstance(item, dict)}
        results: list[Any] = []
        for index, (method, _) in enumerate(calls):
            item = by_id.get(index)
            if item is None:
                results.append(RpcError(f"{method} via {self._host}: missing from the batch"))
                continue
            try:
                results.append(self._result(item, method))
            except RpcError as exc:
                results.append(exc)
        return results

    def _no_batches(self, reason: str) -> None:
        if self.batches:
            log.info("%s refused a batch (%s); single calls for a while", self._host, reason)
        self._single_until = self._clock() + SINGLE_CALLS_FOR

    async def _one_by_one(
        self, calls: Sequence[tuple[str, list[Any]]], deadline: float, same_block: bool = False
    ) -> list[Any]:
        if same_block:
            try:
                block = await self._call("eth_blockNumber", [], deadline)
            except RpcError as exc:
                return [exc] * len(calls)
            calls = [(m, [block if p == "latest" else p for p in params]) for m, params in calls]
        return list(await asyncio.gather(*(self._single(m, p, deadline) for m, p in calls)))

    async def _single(self, method: str, params: list[Any], deadline: float) -> Any:
        try:
            return await self._call(method, params, deadline)
        except RpcError as exc:
            return exc

    async def _post(self, payload: Any, method: str, deadline: float) -> Any:
        """The decoded answer body; quick failures are retried, all before ``deadline``.

        The deadline is hard: httpx applies its timeout per phase and per read, so a node
        that drips its answer would otherwise hold the call far longer.
        """
        left = deadline - self._clock()
        if left <= 0:
            raise RpcError(f"{method} via {self._host}: no time left")
        try:
            async with asyncio.timeout(left):
                return await self._attempts(payload, method, deadline)
        except TimeoutError as exc:
            raise RpcError(f"{method} via {self._host}: timed out after {left:.1f}s") from exc

    async def _attempts(self, payload: Any, method: str, deadline: float) -> Any:
        attempt = 0
        while True:
            pause = self._cooldown_left()
            if pause > 0:
                if self._clock() + pause >= deadline:
                    raise RpcError(f"{method} via {self._host}: rate limited, cooling down")
                await self._sleep(pause)
            try:
                return await self._attempt(payload, method, deadline)
            except _Retryable as failure:
                if attempt >= len(self._retry.delays):
                    raise failure.error from failure.error.__cause__
                delay = self._retry.delays[attempt] * random.uniform(0.8, 1.2)
                if failure.retry_after:
                    delay = max(delay, min(failure.retry_after, self._retry.max_retry_after))
                delay = max(delay, self._cooldown_left())
                if self._clock() + delay >= deadline:
                    raise failure.error from failure.error.__cause__
            attempt += 1
            await self._sleep(delay)

    def _cooldown_left(self) -> float:
        return self._cooldowns.get(self._host, 0.0) - self._clock()

    async def _attempt(self, payload: Any, method: str, deadline: float) -> Any:
        """One request: the decoded body, :class:`_Retryable` or a final :class:`RpcError`."""
        timeout = max(deadline - self._clock(), 0.1)
        try:
            response = await self._http.post(self._url, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:  # the budget is spent: no retry
            raise self._transport_error(method, exc) from exc
        except httpx.TransportError as exc:  # refused, reset, HTTP/2 protocol error
            raise _Retryable(self._transport_error(method, exc)) from exc
        except httpx.HTTPError as exc:
            raise self._transport_error(method, exc) from exc
        except RuntimeError as exc:  # the client was closed during shutdown
            raise RpcError(f"{method} via {self._host}: {exc}") from exc
        status = response.status_code
        if status in _RETRY_STATUSES:
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            if status == 429 and self._retry.cooldown_cap > 0:
                pause = min(retry_after or 1.0, self._retry.cooldown_cap)
                until = self._clock() + pause
                self._cooldowns[self._host] = max(self._cooldowns.get(self._host, 0.0), until)
            raise _Retryable(RpcError(f"{method} via {self._host}: HTTP {status}"), retry_after)
        if status != 200:
            if isinstance(payload, list) and status in _BATCH_REFUSED_STATUSES:
                raise _BatchRefused(f"{method} via {self._host}: HTTP {status}")
            raise RpcError(f"{method} via {self._host}: HTTP {status}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RpcError(f"{method} via {self._host}: invalid JSON") from exc
        if _is_rate_limited(data):
            raise _Retryable(RpcError(f"{method} via {self._host}: rate limited"))
        return data

    def _transport_error(self, method: str, exc: Exception) -> RpcError:
        error = RpcError(f"{method} via {self._host}: {type(exc).__name__}: {exc}")
        error.__cause__ = exc
        return error

    def _result(self, data: dict[str, Any], method: str) -> Any:
        error = data.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            message = str(error.get("message", error)) if isinstance(error, dict) else str(error)
            raise RpcCallError(
                f"{method} via {self._host}: {message}",
                code if isinstance(code, int) else None,
                message,
            )
        return data.get("result")


class _BatchRefused(RpcError):
    """The node answered a batch request with a client error: it takes single calls only."""


class _Retryable(Exception):
    """A failure worth another attempt; carries the error to raise if none is left."""

    def __init__(self, error: RpcError, retry_after: float | None = None) -> None:
        super().__init__(str(error))
        self.error = error
        self.retry_after = retry_after


def _is_rate_limited(data: Any) -> bool:
    """A rate-limit error in the answer, or in any element of a batch answer."""
    if isinstance(data, list):
        return any(isinstance(item, dict) and _is_rate_limit(item.get("error")) for item in data)
    return isinstance(data, dict) and _is_rate_limit(data.get("error"))


def _is_rate_limit(error: Any) -> bool:
    if not isinstance(error, dict):
        return False
    if error.get("code") in _RATE_LIMIT_CODES:
        return True
    message = str(error.get("message", "")).lower()
    return any(phrase in message for phrase in _RATE_LIMIT_PHRASES)


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header; HTTP-date forms are ignored."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
