"""A tiny asynchronous JSON-RPC 2.0 client used by every chain validator and helper."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx


class RpcError(Exception):
    """Transport failure, non-200 answer or a JSON-RPC error object."""


class RpcCallError(RpcError):
    """The node answered, but with a JSON-RPC error object (e.g. ``execution reverted``)."""

    def __init__(self, text: str, code: int | None, message: str) -> None:
        super().__init__(text)
        self.code = code
        self.message = message


class JsonRpcClient:
    """Sends single JSON-RPC requests over a shared :class:`httpx.AsyncClient`."""

    def __init__(self, http: httpx.AsyncClient, url: str, timeout: float) -> None:
        self._http = http
        self._url = url
        self._timeout = timeout
        self._host = urlsplit(url).hostname or url

    async def call(self, method: str, params: list[Any]) -> Any:
        """Return the ``result`` of ``method`` or raise :class:`RpcError`."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            response = await self._http.post(self._url, json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise RpcError(f"{method} via {self._host}: {type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            raise RpcError(f"{method} via {self._host}: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RpcError(f"{method} via {self._host}: invalid JSON") from exc
        if not isinstance(data, dict):
            raise RpcError(f"{method} via {self._host}: unexpected payload")
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
