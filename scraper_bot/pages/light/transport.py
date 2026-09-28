"""How the light mode asks a page or an API: a plain HTTP client, or fetch() in the browser."""

from __future__ import annotations

from typing import Protocol

import httpx

from scraper_bot.chains.rpc.client import parse_retry_after
from scraper_bot.pages.fetching.result import FetchResult

BODY_LIMIT = 3 * 1024 * 1024  # bytes of one answer read; the rest is left out
TIMEOUT = 5.0  # seconds one light request may take


class LightTransport(Protocol):
    """Anything that can GET a URL of a site for the light mode."""

    async def get(
        self, url: str, *, origin: str, etag: str | None = None, modified: str | None = None
    ) -> FetchResult:
        """Return the answer to a GET of ``url``, a page of the site at ``origin``.

        ``etag`` and ``modified`` make it conditional: an unchanged answer is a 304 without
        a body. Errors come back in the result.
        """
        ...


class BrowserFetch(Protocol):
    """The part of the browser the light mode uses: fetch() from a tab on the site."""

    async def tab_fetch(self, url: str, *, origin: str, timeout: float = 10.0) -> FetchResult:
        """Fetch ``url`` from a tab kept on ``origin``; errors come back in the result."""
        ...


class HttpTransport:
    """Plain GET requests through a shared httpx client, conditional where the site allows."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        """Send every request through ``http``, which keeps the connections alive."""
        self._http = http

    async def get(
        self, url: str, *, origin: str, etag: str | None = None, modified: str | None = None
    ) -> FetchResult:
        """Return the answer to a GET of ``url``, at most ``BODY_LIMIT`` bytes of its body.

        A 304 comes back with an empty body. ``origin`` is not needed for a plain request.
        """
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        try:
            async with self._http.stream(
                "GET", url, headers=headers, timeout=TIMEOUT, follow_redirects=True
            ) as response:
                body = bytearray()
                if response.status_code != 304:
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) >= BODY_LIMIT:
                            break
                answered = {name.lower(): value for name, value in response.headers.items()}
                return FetchResult(
                    url,
                    response.status_code,
                    _text(bytes(body[:BODY_LIMIT]), response.encoding),
                    headers=answered,
                    retry_after=parse_retry_after(answered.get("retry-after")),
                )
        except httpx.HTTPError as exc:
            return FetchResult(url, None, error=f"{type(exc).__name__}: {exc}"[:200])


class BrowserTransport:
    """GET requests made by the browser itself: fetch() from a tab kept on the site."""

    def __init__(self, browser: BrowserFetch) -> None:
        """Send every request through ``browser``'s kept tabs."""
        self._browser = browser

    async def get(
        self, url: str, *, origin: str, etag: str | None = None, modified: str | None = None
    ) -> FetchResult:
        """Return the answer to a fetch() of ``url`` from the tab kept on ``origin``.

        The browser's own cache makes the request conditional, so ``etag`` and ``modified``
        are not needed; an unchanged answer comes back whole.
        """
        return await self._browser.tab_fetch(url, origin=origin, timeout=TIMEOUT * 2)


def _text(body: bytes, encoding: str | None) -> str:
    """Decode a body with its charset, or as UTF-8 when the charset is unknown."""
    try:
        return body.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")
