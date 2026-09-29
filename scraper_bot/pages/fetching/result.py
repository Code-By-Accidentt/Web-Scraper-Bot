"""What a page fetch returns, and what a page fetcher looks like."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class BrowserUnavailable(RuntimeError):
    """The browser could not be started (or restarted); the message says what to do."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    """One fetched page: its status, headers and HTML, or the error that stopped it."""

    url: str
    status: int | None
    html: str = ""
    error: str | None = None
    extra_texts: tuple[str, ...] = ()  # frames, shadow roots, script/JSON bodies (deep scan)
    dom_texts: tuple[str, ...] = ()  # the frame and shadow-root part of extra_texts
    headers: dict[str, str] = field(default_factory=dict)  # of the main document
    retry_after: float | None = None  # seconds, from a Retry-After header
    timed_out: bool = False  # the document never arrived within the load timeout
    # Epoch seconds when loading began, after any wait for a free tab.
    started_at: float | None = None
    # URLs of the page's GET fetch/XHR calls that answered JSON, one per call, in order.
    api_calls: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when there was no error and the status, if known, is 2xx."""
        return self.error is None and (self.status is None or 200 <= self.status < 300)

    @property
    def reason(self) -> str:
        """The error text, or ``HTTP <status>`` when the answer itself was the problem."""
        return self.error or f"HTTP {self.status}"


@dataclass(frozen=True, slots=True)
class EarlyText:
    """A piece of a page that may hold an address, seen while the page was still loading.

    ``kind`` is "text" for visible text, with the text in front of it in ``before``, or
    "attr" for an attribute value, with the attribute's name in ``before``.
    """

    kind: str
    text: str
    before: str
    seen_at: float  # epoch seconds


EarlyListener = Callable[[EarlyText], None]


class PageFetcher(Protocol):
    """Anything that can turn a URL into rendered HTML."""

    async def fetch(
        self, url: str, *, timeout: float | None = None, on_text: EarlyListener | None = None
    ) -> FetchResult:
        """Render ``url``; ``timeout`` overrides the load timeout (seconds) for this fetch.

        ``on_text`` hears, while the page loads, each piece of it that may hold an address.
        """
        ...
