"""Rendering pages in one shared Chromium."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from playwright import async_api as pw

from scraper_bot.chains.rpc.client import parse_retry_after
from scraper_bot.pages.fetching.request_filter import is_unwanted
from scraper_bot.pages.fetching.result import BrowserUnavailable, FetchResult
from scraper_bot.settings.env import Settings
from scraper_bot.settings.live import Tuning

log = logging.getLogger(__name__)

_SCANNED_RESOURCE_TYPES = frozenset({"document", "script", "xhr", "fetch", "other"})
_SCANNED_CONTENT_TYPES = ("javascript", "ecmascript", "json", "text/", "xml")
_PER_RESPONSE_LIMIT = 3 * 1024 * 1024
# Words in a launch error that mean the sandbox failed ("No usable sandbox!", namespaces).
_SANDBOX_TROUBLE = ("sandbox", "namespace", "zygote", "setuid")

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


class PlaywrightFetcher:
    """Renders pages in one shared Chromium, one tab per fetch.

    The browser lives as long as the process and is relaunched after a crash; at most
    ``max_concurrent_pages`` pages render at once. With ``deep_scan`` the result also
    carries frame and shadow-root markup and the script and JSON bodies the page loaded,
    so addresses hidden there cost no extra request.
    """

    def __init__(self, settings: Settings, tuning: Tuning | None = None) -> None:
        """Set up the fetcher; the browser itself starts in :meth:`start`."""
        self._settings = settings
        self._tuning: Tuning = tuning if tuning is not None else settings
        self._playwright: pw.Playwright | None = None
        self._browser: pw.Browser | None = None
        self._context: pw.BrowserContext | None = None
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_pages)
        self._launch_lock = asyncio.Lock()
        self._blocked = frozenset(settings.blocked_resource_types)
        self._args = list(settings.browser_args)
        self._sandbox = "--no-sandbox" not in self._args
        self.sandbox_error: str | None = None  # why the sandbox was dropped; None if it was not
        self._launched = False
        # Called when a relaunch drops the sandbox; the first start is reported by the app.
        self.on_sandbox_lost: Callable[[str], None] | None = None

    @property
    def is_connected(self) -> bool:
        """True when the browser is running and connected."""
        return self._browser is not None and self._browser.is_connected()

    @property
    def sandboxed(self) -> bool:
        """True while Chromium runs with its own sandbox."""
        return self._sandbox

    async def start(self) -> None:
        """Start Playwright and launch the browser.

        Raise :class:`BrowserUnavailable`, with a hint on what to install, if it cannot start.
        """
        self._playwright = await pw.async_playwright().start()
        try:
            await self._launch()
        except pw.Error as exc:
            raise BrowserUnavailable(self._explain_launch_error(exc)) from exc

    async def stop(self) -> None:
        """Close the browser and stop Playwright."""
        await self._close_browser()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def fetch(self, url: str, *, timeout: float | None = None) -> FetchResult:
        """Render ``url`` in a new tab and return the result.

        Page errors come back inside the result. If the browser died, it is relaunched and
        the fetch tried once more; :class:`BrowserUnavailable` means it could not start.
        """
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
        """Open ``url`` in a new tab of ``context``, let it settle and read it.

        A timeout or page error comes back in the result; an error meaning the browser is
        gone is raised, so :meth:`fetch` can relaunch it.
        """
        tuning = self._tuning
        load_timeout = timeout or self._settings.page_load_timeout
        deep_scan = tuning.deep_scan
        page = await context.new_page()
        responses: list[pw.Response] = []
        if deep_scan:
            # Playwright wraps the handler; a builtin like responses.append breaks it.
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
                # A page that keeps polling never goes idle; the settle delay covers it.
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
        """Return the markup of child frames and shadow roots, and the bodies the page loaded.

        Bodies are the script, JSON and other text responses. Both share the
        ``deep_scan_max_bytes`` budget, frames and shadow roots first.
        """
        budget = self._settings.deep_scan_max_bytes
        texts: list[str] = []

        def take(text: str) -> None:
            """Keep ``text``, cut to what is left of the budget."""
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
        # URL order, not arrival order: every render fills the budget with the same bodies,
        # so an address skipped once does not turn up later as "new".
        for response in sorted(responses, key=lambda item: item.url):
            if budget <= 0:
                break
            if not self._worth_scanning(response):
                continue
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > _PER_RESPONSE_LIMIT:
                continue
            try:
                body = await response.body()
            except pw.Error:
                continue
            if len(body) > _PER_RESPONSE_LIMIT:
                continue
            take(body.decode("utf-8", errors="replace"))
        return tuple(texts[:dom]), tuple(texts[dom:])

    @staticmethod
    def _worth_scanning(response: pw.Response) -> bool:
        """True for a document, script or data response with a text-like content type."""
        if response.request.resource_type not in _SCANNED_RESOURCE_TYPES:
            return False
        content_type = response.headers.get("content-type", "").lower()
        return any(marker in content_type for marker in _SCANNED_CONTENT_TYPES)

    async def _ensure_context(self) -> pw.BrowserContext:
        """Return the shared browser context, launching the browser first if it is gone.

        A failed launch raises :class:`BrowserUnavailable`.
        """
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
        """Return a message for a failed launch, with install hints when Chromium is missing."""
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
        """Launch Chromium and open the shared context; every request goes through the filter.

        If the sandbox keeps Chromium from starting (as root, or without user namespaces),
        launch again without it and remember why; the browser is then isolated only by the
        bot's own user account.
        """
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
            await context.route("**/*", self._route)
        except BaseException:
            with contextlib.suppress(Exception):
                await browser.close()
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
        """Close the browser, if there is one, and forget it and its context."""
        browser, self._browser, self._context = self._browser, None, None
        if browser is not None:
            with contextlib.suppress(pw.Error):
                await browser.close()

    async def _route(self, route: pw.Route) -> None:
        """Abort a request the bot does not need; let any other through."""
        if is_unwanted(route.request, self._blocked):
            await route.abort()
        else:
            await route.continue_()

    @staticmethod
    def _browser_gone(exc: pw.Error) -> bool:
        """True when the error means the browser or the connection to it is gone."""
        message = str(exc).lower()
        return (
            type(exc).__name__ == "TargetClosedError"
            or "has been closed" in message
            or ("connection closed" in message)
        )

    @staticmethod
    def _brief(exc: BaseException) -> str:
        """Return the first line of an error, cut to 200 characters (its type name if blank)."""
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return first_line[:200]
