"""Page fetching: one shared headless browser plus a cheap HTTP prober."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx
from playwright import async_api as pw

from scraper_bot.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: int | None
    html: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (self.status is None or 200 <= self.status < 300)

    @property
    def reason(self) -> str:
        return self.error or f"HTTP {self.status}"


class PageFetcher(Protocol):
    """Anything that can turn a URL into rendered HTML."""

    async def fetch(self, url: str) -> FetchResult: ...


class PlaywrightFetcher:
    """Renders pages in one shared Chromium.

    A tab is opened per fetch and closed afterwards; the browser itself lives for
    the whole process and is relaunched transparently if it crashes. Concurrency
    is bounded by ``max_concurrent_pages``.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: pw.Playwright | None = None
        self._browser: pw.Browser | None = None
        self._context: pw.BrowserContext | None = None
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_pages)
        self._launch_lock = asyncio.Lock()
        self._blocked = frozenset(settings.blocked_resource_types)

    async def __aenter__(self) -> PlaywrightFetcher:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def start(self) -> None:
        self._playwright = await pw.async_playwright().start()
        await self._launch()

    async def stop(self) -> None:
        await self._close_browser()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def fetch(self, url: str) -> FetchResult:
        async with self._semaphore:
            for attempt in (1, 2):
                context = await self._ensure_context()
                try:
                    return await self._fetch_in(context, url)
                except pw.Error as exc:
                    if attempt == 1 and self._browser_gone(exc):
                        log.warning("browser connection lost (%s); relaunching", self._brief(exc))
                        await self._close_browser()
                        continue
                    return FetchResult(url, None, error=self._brief(exc))
            return FetchResult(url, None, error="browser unavailable")

    # ── internals ───────────────────────────────────────────────────────────
    async def _fetch_in(self, context: pw.BrowserContext, url: str) -> FetchResult:
        cfg = self._settings
        page = await context.new_page()
        try:
            try:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=cfg.page_load_timeout * 1000
                )
            except pw.TimeoutError:
                return FetchResult(url, None, error=f"timeout after {cfg.page_load_timeout:.0f}s")
            except pw.Error as exc:
                if self._browser_gone(exc):
                    raise
                return FetchResult(url, None, error=self._brief(exc))
            if cfg.network_idle_timeout > 0:
                # A page that keeps polling never goes idle; the settle delay covers that case.
                with contextlib.suppress(pw.TimeoutError):
                    await page.wait_for_load_state(
                        "networkidle", timeout=cfg.network_idle_timeout * 1000
                    )
            if cfg.settle_delay > 0:
                await asyncio.sleep(cfg.settle_delay)
            html = await page.content()
            status = response.status if response is not None else None
            return FetchResult(url, status, html)
        finally:
            with contextlib.suppress(pw.Error):
                await page.close()

    async def _ensure_context(self) -> pw.BrowserContext:
        if self._context is not None and self._browser is not None and self._browser.is_connected():
            return self._context
        async with self._launch_lock:
            if self._context is None or self._browser is None or not self._browser.is_connected():
                await self._close_browser()
                await self._launch()
            assert self._context is not None
            return self._context

    async def _launch(self) -> None:
        assert self._playwright is not None, "start() must be called first"
        cfg = self._settings
        channel = cfg.browser_channel if cfg.browser_channel not in {"", "bundled"} else None
        self._browser = await self._playwright.chromium.launch(
            headless=cfg.headless, channel=channel
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=True,
        )
        if self._blocked:
            await self._context.route("**/*", self._route)
        log.info("browser started (%s, headless=%s)", channel or "chromium", cfg.headless)

    async def _close_browser(self) -> None:
        browser, self._browser, self._context = self._browser, None, None
        if browser is not None:
            with contextlib.suppress(pw.Error):
                await browser.close()

    async def _route(self, route: pw.Route) -> None:
        if route.request.resource_type in self._blocked:
            await route.abort()
        else:
            await route.continue_()

    @staticmethod
    def _browser_gone(exc: pw.Error) -> bool:
        message = str(exc).lower()
        return (
            type(exc).__name__ == "TargetClosedError"
            or "has been closed" in message
            or ("connection closed" in message)
        )

    @staticmethod
    def _brief(exc: BaseException) -> str:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return first_line[:200]


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

    def __init__(self, http: httpx.AsyncClient, timeout: float = 10.0) -> None:
        self._http = http
        self._timeout = timeout

    async def probe(self, url: str) -> ProbeResult:
        try:
            response = await self._http.head(url, timeout=self._timeout, follow_redirects=True)
            status = response.status_code
            if status in self._RETRY_WITH_GET:
                async with self._http.stream(
                    "GET", url, timeout=self._timeout, follow_redirects=True
                ) as streamed:
                    status = streamed.status_code
            return ProbeResult(url, status)
        except httpx.HTTPError as exc:
            return ProbeResult(url, None, error=f"{type(exc).__name__}: {exc}"[:200])
