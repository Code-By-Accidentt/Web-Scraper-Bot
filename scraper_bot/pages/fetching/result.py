"""What a page fetch returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class BrowserUnavailable(RuntimeError):
    """The browser could not be started (or restarted); the message says what to do."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: int | None
    html: str = ""
    error: str | None = None
    extra_texts: tuple[str, ...] = ()  # frames, shadow roots, script/JSON bodies (deep scan)
    dom_texts: tuple[str, ...] = ()  # the frames and shadow roots among them: page content
    headers: dict[str, str] = field(default_factory=dict)  # of the main document
    retry_after: float | None = None  # seconds, from a Retry-After header
    timed_out: bool = False  # the document never arrived within the load timeout
    # when loading began (epoch seconds), after any wait for a free tab: the page shows
    # what the site served between this moment and the end of the render
    started_at: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (self.status is None or 200 <= self.status < 300)

    @property
    def reason(self) -> str:
        return self.error or f"HTTP {self.status}"


class PageFetcher(Protocol):
    """Anything that can turn a URL into rendered HTML.

    ``timeout`` overrides the configured load timeout for this one fetch (seconds).
    """

    async def fetch(self, url: str, *, timeout: float | None = None) -> FetchResult: ...
