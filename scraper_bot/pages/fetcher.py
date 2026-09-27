"""Page fetching: one shared headless browser plus a cheap HTTP prober."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from playwright import async_api as pw

from scraper_bot.chains.rpc import parse_retry_after
from scraper_bot.config import Settings
from scraper_bot.preferences import Tuning

log = logging.getLogger(__name__)


class BrowserUnavailable(RuntimeError):
    """The browser could not be started (or restarted); the message says what to do."""


# Deep scan: response types whose bodies may hide addresses (bundles, API answers, raw HTML).
_SCANNED_RESOURCE_TYPES = frozenset({"document", "script", "xhr", "fetch", "other"})
_SCANNED_CONTENT_TYPES = ("javascript", "ecmascript", "json", "text/", "xml")
_PER_RESPONSE_LIMIT = 3 * 1024 * 1024
# How a launch that failed because of the sandbox says so ("Chromium sandboxing failed!",
# "No usable sandbox!", "Running as root without --no-sandbox", namespace errors).
_SANDBOX_TROUBLE = ("sandbox", "namespace", "zygote", "setuid")

# Requests a page makes that the bot never needs. Next.js prefetches the pages its links
# point to (harvestco.ai: ten per load), and analytics beacons report every render to the
# site owner as a visit. Neither changes what the page shows. A prefetch is an ?_rsc= fetch
# with a prefetch header, or any ?_rsc= fetch of another page: links with prefetch={true}
# send no header (heaven.xyz). The page's own ?_rsc= fetch (router.refresh() revealing a
# CA) passes. Vercel's own checkpoint (/.well-known/vercel/) is always let through.
_ANALYTICS_HOSTS = (
    "google-analytics.com",
    "analytics.google.com",
    "googletagmanager.com",
    "vitals.vercel-insights.com",
    "va.vercel-scripts.com",
    "cloudflareinsights.com",
    "cdn.segment.com",
    "api.segment.io",
    "api-js.mixpanel.com",
    "api.mixpanel.com",
    "api-eu.mixpanel.com",
    "cdn.mxpnl.com",
    "static.hotjar.com",
    "script.hotjar.com",
    "clarity.ms",
    "i.posthog.com",
    "app.posthog.com",
    "us.posthog.com",
    "eu.posthog.com",
    "ingest.sentry.io",
    "ingest.us.sentry.io",
    "ingest.de.sentry.io",
    "stats.g.doubleclick.net",
)
_ANALYTICS_PATHS = ("/_vercel/insights/", "/_vercel/speed-insights/", "/cdn-cgi/rum")
_ALWAYS_ALLOWED = "/.well-known/vercel/"

# Collects the markup of every (open) shadow root on the page, nested ones included.
_SHADOW_ROOTS_JS = """
() => {
  const parts = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        parts.push(el.shadowRoot.innerHTML);
        walk(el.shadowRoot);
      }
    }
  };
  walk(document);
  return parts.join('\\n');
}
"""


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


class PlaywrightFetcher:
    """Renders pages in one shared Chromium.

    A tab per fetch; the browser lives for the whole process and is relaunched if it
    crashes; at most ``max_concurrent_pages`` renders run at once. With ``deep_scan``
    the result also carries iframe and shadow-root markup and the script/JSON bodies
    the page loaded, so addresses hidden there cost no extra request.
    """

    def __init__(self, settings: Settings, tuning: Tuning | None = None) -> None:
        self._settings = settings
        self._tuning: Tuning = tuning if tuning is not None else settings
        self._playwright: pw.Playwright | None = None
        self._browser: pw.Browser | None = None
        self._context: pw.BrowserContext | None = None
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_pages)
        self._launch_lock = asyncio.Lock()
        self._blocked = frozenset(settings.blocked_resource_types)
        self._args = list(settings.browser_args)
        # Chromium's own sandbox, unless BROWSER_ARGS switch it off or it cannot start here
        # (as root, or without user namespaces in a container): then the bot runs without it.
        self._sandbox = "--no-sandbox" not in self._args
        self.sandbox_error: str | None = None  # why the sandbox was given up, if it was
        self._launched = False
        # Told when a relaunch (after a crash) had to give the sandbox up; the start is
        # reported by the application.
        self.on_sandbox_lost: Callable[[str], None] | None = None

    @property
    def is_connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    @property
    def sandboxed(self) -> bool:
        return self._sandbox

    async def start(self) -> None:
        self._playwright = await pw.async_playwright().start()
        try:
            await self._launch()
        except pw.Error as exc:
            raise BrowserUnavailable(self._explain_launch_error(exc)) from exc

    async def stop(self) -> None:
        await self._close_browser()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def fetch(self, url: str, *, timeout: float | None = None) -> FetchResult:
        async with self._semaphore:
            for attempt in (1, 2):
                context = await self._ensure_context()
                try:
                    return await self._fetch_in(context, url, timeout)
                except pw.Error as exc:
                    if attempt == 1 and self._browser_gone(exc):
                        log.warning("browser connection lost (%s); relaunching", self._brief(exc))
                        await self._discard(context)
                        continue
                    return FetchResult(url, None, error=self._brief(exc))
            return FetchResult(url, None, error="browser unavailable")

    async def _fetch_in(
        self, context: pw.BrowserContext, url: str, timeout: float | None
    ) -> FetchResult:
        tuning = self._tuning
        load_timeout = timeout or self._settings.page_load_timeout
        deep_scan = tuning.deep_scan
        page = await context.new_page()
        responses: list[pw.Response] = []
        if deep_scan:
            # Playwright wraps the handler; that breaks on a builtin like responses.append.
            page.on("response", lambda response: responses.append(response))
        started_at = time.time()
        try:
            try:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=load_timeout * 1000
                )
            except pw.TimeoutError:
                return FetchResult(
                    url,
                    None,
                    error=f"timeout after {load_timeout:.0f}s",
                    timed_out=True,
                    started_at=started_at,
                )
            except pw.Error as exc:
                if self._browser_gone(exc):
                    raise
                return FetchResult(url, None, error=self._brief(exc))
            if tuning.network_idle_timeout > 0:
                # A page that keeps polling never goes idle; the settle delay covers that case.
                with contextlib.suppress(pw.TimeoutError):
                    await page.wait_for_load_state(
                        "networkidle", timeout=tuning.network_idle_timeout * 1000
                    )
            if tuning.settle_delay > 0:
                await asyncio.sleep(tuning.settle_delay)
            html = await page.content()
            dom, bodies = await self._deep_scan(page, responses) if deep_scan else ((), ())
            status = response.status if response is not None else None
            headers = dict(response.headers) if response is not None else {}
            return FetchResult(
                url,
                status,
                html,
                extra_texts=dom + bodies,
                dom_texts=dom,
                headers=headers,
                retry_after=parse_retry_after(headers.get("retry-after")),
                started_at=started_at,
            )
        finally:
            with contextlib.suppress(pw.Error):
                await page.close()

    async def _deep_scan(
        self, page: pw.Page, responses: list[pw.Response]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(markup of child frames and shadow roots, scripts/JSON the page loaded)."""
        budget = self._settings.deep_scan_max_bytes
        texts: list[str] = []

        def take(text: str) -> None:
            nonlocal budget
            if budget <= 0 or not text:
                return
            clipped = text[:budget]
            texts.append(clipped)
            budget -= len(clipped)

        for frame in page.frames:
            if frame is page.main_frame:
                continue
            with contextlib.suppress(pw.Error):
                take(await frame.content())
        with contextlib.suppress(pw.Error):
            take(str(await page.evaluate(_SHADOW_ROOTS_JS) or ""))
        dom = len(texts)
        # In URL order, not arrival order: every render then fills the budget with the same
        # bodies, and an address the first render skipped does not turn up later as "new".
        for response in sorted(responses, key=lambda item: item.url):
            if budget <= 0:
                break
            if not self._worth_scanning(response):
                continue
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > _PER_RESPONSE_LIMIT:
                continue  # not fetched from the browser only to be thrown away
            try:
                body = await response.body()
            except pw.Error:
                continue  # redirects, aborted or already discarded bodies
            if len(body) > _PER_RESPONSE_LIMIT:
                continue
            take(body.decode("utf-8", errors="replace"))
        return tuple(texts[:dom]), tuple(texts[dom:])

    @staticmethod
    def _worth_scanning(response: pw.Response) -> bool:
        if response.request.resource_type not in _SCANNED_RESOURCE_TYPES:
            return False
        content_type = response.headers.get("content-type", "").lower()
        return any(marker in content_type for marker in _SCANNED_CONTENT_TYPES)

    async def _ensure_context(self) -> pw.BrowserContext:
        if self._context is not None and self.is_connected:
            return self._context
        async with self._launch_lock:
            if self._context is None or not self.is_connected:
                await self._close_browser()
                try:
                    await self._launch()
                except pw.Error as exc:
                    raise BrowserUnavailable(self._explain_launch_error(exc)) from exc
            assert self._context is not None
            return self._context

    @classmethod
    def _explain_launch_error(cls, exc: pw.Error) -> str:
        brief = cls._brief(exc)
        if "executable doesn't exist" in brief.lower() or "browsertype.launch" in brief.lower():
            return (
                f"browser could not start: {brief}. "
                "Install it with: playwright install chromium (then restart the bot). Where "
                "Playwright has no Chromium build (e.g. Ubuntu 20.04), install Google Chrome "
                "and set BROWSER_CHANNEL=chrome"
            )
        return f"browser could not start: {brief}"

    async def _launch(self) -> None:
        assert self._playwright is not None, "start() must be called first"
        cfg = self._settings
        channel = cfg.browser_channel if cfg.browser_channel not in {"", "bundled"} else None
        chromium = self._playwright.chromium
        try:
            browser = await chromium.launch(
                headless=cfg.headless,
                channel=channel,
                args=self._args,
                chromium_sandbox=self._sandbox,
            )
        except pw.Error as exc:
            if not self._sandbox or not self._sandbox_trouble(exc):
                raise
            # The sandbox refuses root and needs user namespaces; without it the browser
            # still works, isolated only by the bot's own user account.
            browser = await chromium.launch(
                headless=cfg.headless, channel=channel, args=self._args, chromium_sandbox=False
            )
            self._sandbox = False
            self.sandbox_error = self._brief(exc)
            log.warning(
                "Chromium could not start with its sandbox (%s); running without it",
                self.sandbox_error,
            )
            if self._launched and self.on_sandbox_lost is not None:
                self.on_sandbox_lost(self.sandbox_error)
        try:
            context = await browser.new_context(
                viewport={"width": 1366, "height": 900},
                ignore_https_errors=True,
            )
            # Every request passes the filter: blocked resource types, prefetches, beacons.
            await context.route("**/*", self._route)
        except BaseException:
            with contextlib.suppress(Exception):
                await browser.close()  # the next attempt launches a new one
            raise
        self._browser, self._context = browser, context
        self._launched = True
        log.info(
            "browser started (%s, headless=%s, sandbox=%s, args=%s)",
            channel or "chromium",
            cfg.headless,
            "on" if self._sandbox else "off",
            " ".join(self._args) or "none",
        )

    @staticmethod
    def _sandbox_trouble(exc: pw.Error) -> bool:
        """True when a launch failed because of the sandbox (not a timeout or a crash)."""
        text = str(exc).lower()
        return any(sign in text for sign in _SANDBOX_TROUBLE)

    async def _discard(self, context: pw.BrowserContext) -> None:
        """Close the browser a failed tab used, unless another tab relaunched it already."""
        async with self._launch_lock:
            if self._context is context:
                await self._close_browser()

    async def _close_browser(self) -> None:
        browser, self._browser, self._context = self._browser, None, None
        if browser is not None:
            with contextlib.suppress(pw.Error):
                await browser.close()

    async def _route(self, route: pw.Route) -> None:
        if self._unwanted(route.request):
            await route.abort()
        else:
            await route.continue_()

    def _unwanted(self, request: pw.Request) -> bool:
        """Blocked resource types, Next.js prefetches and analytics beacons."""
        url = request.url
        if _ALWAYS_ALLOWED in url:
            return False  # the checkpoint's own images and fonts included
        if request.resource_type in self._blocked:
            return True
        headers = request.headers
        if headers.get("next-router-prefetch") == "1" or "next-router-segment-prefetch" in headers:
            return True
        if "prefetch" in headers.get("purpose", "") or "prefetch" in headers.get("sec-purpose", ""):
            return True
        parts = urlsplit(url)
        if "_rsc=" in parts.query and not _same_page(parts.path, request):
            return True
        host = (parts.hostname or "").lower()
        if any(host == known or host.endswith("." + known) for known in _ANALYTICS_HOSTS):
            return True
        return parts.path.startswith(_ANALYTICS_PATHS)

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


def _same_page(path: str, request: pw.Request) -> bool:
    """True when ``path`` is the page the request comes from, or that page is unknown."""
    try:
        page = urlsplit(request.frame.url).path
    except pw.Error:  # a service worker's request has no frame
        return True
    return path.rstrip("/") == page.rstrip("/")


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
