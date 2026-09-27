"""Cheap HTTP check that a sub-page exists before it is rendered."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class ProbeResult:
    url: str
    status: int | None
    error: str | None = None

    @property
    def worth_rendering(self) -> bool:
        """False only when the page certainly does not exist.

        DNS or connection failures and 404/410 answers are conclusive. Anything
        else (403 from a bot shield, 405 for HEAD, 5xx) is left to the browser.
        """
        if self.error is not None:
            return False
        return self.status not in {404, 410}


class HttpProber:
    """Cheap existence check for derived sub-URLs before spending a browser tab."""

    _RETRY_WITH_GET = frozenset({404, 405, 410, 501})
    _TIMEOUT = 10.0

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def probe(self, url: str) -> ProbeResult:
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
