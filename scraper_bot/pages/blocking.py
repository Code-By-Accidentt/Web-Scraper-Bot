"""Recognising answers that mean the site is refusing us rather than being down."""

from __future__ import annotations

import re

from scraper_bot.models import BlockSignal
from scraper_bot.pages.fetcher import FetchResult

_BLOCK_STATUSES = frozenset({401, 403, 407, 429, 503})
# Phrases typical for WAF / anti-bot pages. For a 2xx answer only the <title> is
# consulted, so a login form mentioning "captcha" in its body is not a false alarm.
_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "access denied",
    "checking your browser",
    "verify you are human",
    "too many requests",
    "ddos-guard",
    "bot verification",
    "request blocked",
    "captcha",
    "cf-chl",
)
_CONNECTION_ERRORS = (
    "err_connection_reset",
    "err_connection_refused",
    "err_connection_closed",
    "err_empty_response",
    "err_http2_protocol_error",
    "err_ssl_protocol_error",
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class BlockDetector:
    """Turns a :class:`FetchResult` into a :class:`BlockSignal` when it smells like a block."""

    def inspect(self, result: FetchResult) -> BlockSignal | None:
        headers = {key.lower(): value for key, value in result.headers.items()}
        challenged = headers.get("cf-mitigated", "").lower() == "challenge"
        if result.status in _BLOCK_STATUSES:
            marker = self._marker(result.html, title_only=False)
            detail = f"HTTP {result.status}"
            if challenged or marker:
                detail += f" ({marker or 'challenge'})"
            return BlockSignal(detail, result.retry_after)
        if result.error is not None:
            lowered = result.error.lower()
            if any(code in lowered for code in _CONNECTION_ERRORS):
                return BlockSignal(result.error[:120])
            return None
        marker = self._marker(result.html, title_only=True)
        if challenged or marker:
            return BlockSignal(
                f"challenge page ({marker or 'cf-mitigated'}) with HTTP {result.status or 200}"
            )
        return None

    @staticmethod
    def _marker(html: str, *, title_only: bool) -> str | None:
        head = html[:4000].lower()
        title = _TITLE_RE.search(head)
        haystack = title.group(1) if title else ""
        if not title_only:
            haystack += " " + head
        return next((marker for marker in _CHALLENGE_MARKERS if marker in haystack), None)
