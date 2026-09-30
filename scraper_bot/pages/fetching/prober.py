"""Cheap HTTP checks: that a sub-page exists before it is rendered, or that a site still refuses."""

from __future__ import annotations

import socket
from dataclasses import dataclass

import httpx

from scraper_bot.chains.rpc.client import parse_retry_after
from scraper_bot.pages.fetching.result import FetchResult

_PEEK_BYTES = 64 * 1024  # enough for a challenge page's title and the signs of its protection


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The answer to one probe: the HTTP status, or the error that stopped it."""

    url: str
    status: int | None
    error: str | None = None
    unreachable: bool = False  # the host name did not resolve, or the host refused to connect

    @property
    def worth_rendering(self) -> bool:
        """False only when the page certainly does not exist.

        A 404 or 410 answer, a host name that does not resolve and a refused connection are
        conclusive. Anything else (403 from a bot shield, 405 for HEAD, 5xx, a timeout, a
        certificate the browser accepts anyway) is left to the browser.
        """
        if self.error is not None:
            return not self.unreachable
        return self.status not in {404, 410}

    @property
    def gone(self) -> bool:
        """True when the server answered that the page does not exist (404 or 410)."""
        return self.error is None and self.status in {404, 410}


class HttpProber:
    """Checks cheaply that a derived sub-URL exists before a browser tab is spent on it."""

    _RETRY_WITH_GET = frozenset({404, 405, 410, 501})
    _TIMEOUT = 10.0

    def __init__(self, http: httpx.AsyncClient) -> None:
        """Send every probe through the shared ``http`` client."""
        self._http = http

    async def probe(self, url: str) -> ProbeResult:
        """Ask for ``url`` with HEAD and return the answer.

        Some servers get HEAD wrong, so a 404, 405, 410 or 501 is asked again with a GET whose
        body is never read. Network errors come back in the result.
        """
        try:
            response = await self._http.head(url, timeout=self._TIMEOUT, follow_redirects=True)
            status = response.status_code
            if status in self._RETRY_WITH_GET:
                async with self._http.stream(
                    "GET", url, timeout=self._TIMEOUT, follow_redirects=True
                ) as streamed:
                    status = streamed.status_code
            return ProbeResult(url, status)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"[:200]
            return ProbeResult(url, None, error=error, unreachable=_unreachable(exc))

    async def peek(self, url: str) -> FetchResult:
        """Ask for ``url`` with one GET and return its status, headers and first 64 KB.

        That is enough to tell a refusal or a challenge page from the page itself, without
        a render. Network errors come back in the result.
        """
        try:
            async with self._http.stream(
                "GET", url, timeout=self._TIMEOUT, follow_redirects=True
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) >= _PEEK_BYTES:
                        break
                headers = dict(response.headers)
                try:
                    text = bytes(body[:_PEEK_BYTES]).decode(
                        response.encoding or "utf-8", errors="replace"
                    )
                except LookupError:  # a charset Python does not know
                    text = bytes(body[:_PEEK_BYTES]).decode("utf-8", errors="replace")
                retry_after = parse_retry_after(headers.get("retry-after"))
                return FetchResult(
                    url, response.status_code, text, headers=headers, retry_after=retry_after
                )
        except httpx.HTTPError as exc:
            return FetchResult(url, None, error=f"{type(exc).__name__}: {exc}"[:200])


def _unreachable(error: BaseException) -> bool:
    """True when ``error`` comes from a host name that does not resolve or a refused connection.

    httpx wraps the socket's own error, so we follow the causes down to it. When the host
    has several addresses, every attempt must have been refused.
    """
    if isinstance(error, socket.gaierror | ConnectionRefusedError):
        return True
    if isinstance(error, BaseExceptionGroup):
        return all(_unreachable(attempt) for attempt in error.exceptions)
    cause = error.__cause__ or error.__context__
    return cause is not None and _unreachable(cause)
