"""Cheap HTTP check that a sub-page exists before it is rendered."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The answer to one probe: the HTTP status, or the error that stopped it."""

    url: str
    status: int | None
    error: str | None = None

    @property
    def worth_rendering(self) -> bool:
        """False only when the page certainly does not exist.

        Network failures and 404/410 answers are conclusive. Anything else (403 from a bot
        shield, 405 for HEAD, 5xx) is left to the browser.
        """
        if self.error is not None:
            return False
        return self.status not in {404, 410}


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
            return ProbeResult(url, None, error=f"{type(exc).__name__}: {exc}"[:200])
