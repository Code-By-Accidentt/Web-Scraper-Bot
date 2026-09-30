"""Rendering pages in one shared Chromium."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from playwright import async_api as pw

from scraper_bot.chains.rpc.client import parse_retry_after
from scraper_bot.pages.fetching.observer import (
    BEFORE_CHARS,
    EARLY_BINDING,
    EARLY_CHARS,
    EARLY_ITEMS,
    OBSERVER_JS,
)
from scraper_bot.pages.fetching.request_filter import is_unwanted
from scraper_bot.pages.fetching.result import (
    BrowserUnavailable,
    EarlyListener,
    EarlyText,
    FetchResult,
)
from scraper_bot.settings.env import Settings
from scraper_bot.settings.live import Tuning
from scraper_bot.storage.files import atomic_write_text

log = logging.getLogger(__name__)

_T = TypeVar("_T")

_SCANNED_RESOURCE_TYPES = frozenset({"document", "script", "xhr", "fetch", "other"})
_SCANNED_CONTENT_TYPES = ("javascript", "ecmascript", "json", "text/", "xml")
_PER_RESPONSE_LIMIT = 3 * 1024 * 1024
# A launch error with one of these words means the sandbox failed ("No usable sandbox!").
_SANDBOX_TROUBLE = ("sandbox", "namespace", "zygote", "setuid")
# Once the driver is gone, every call raises this. It's a plain Exception, not a
# Playwright error.
_DRIVER_GONE = "connection closed while reading from the driver"
_CLOSE_TIMEOUT = 5.0  # seconds a tab, the browser or the driver gets to close
RECYCLE_AFTER = 3600.0  # seconds before we swap the Playwright driver for a fresh one
RECYCLE_RENDERS = 10_000  # or this many renders, whichever comes first
_RECYCLE_WAIT = 30.0  # seconds a scheduled restart waits for the renders in flight
# With automation hidden, the browser and the HTTP clients tell sites the reader's language.
LOCALE = "en-US"
ACCEPT_LANGUAGE = "en-US,en;q=0.9"
_BLINK_FEATURES = "--disable-blink-features="
_AUTOMATION_FEATURE = "AutomationControlled"  # turned off so navigator.webdriver reads false
STATE_FILE = "browser_state.json"  # cookies and local storage that survive a browser restart
_STATE_EVERY = 300.0  # seconds between saves of the browser state
_STATE_READ_TIMEOUT = 2.0  # seconds the browser gets to hand over its state
_STATE_MAX_CHARS = 2 * 1024 * 1024  # a bigger state is saved with its cookies only
_MAX_API_CALLS = 100  # JSON API calls noted per render
# Light mode's fetch() from a tab. We keep a tab ("anchor") open on each site's origin, so
# its requests carry the browser's cookies, headers and TLS. The tab shows an empty document
# we serve ourselves at this path, so opening it asks the site nothing.
_ANCHOR_PATH = "/robots.txt"
_MAX_ANCHORS = 8
_TAB_FETCH_LIMIT = 3 * 1024 * 1024  # characters of an answer we pass back to Python
# Cookies go only to the origin itself, as a page's own fetch() sends them: an API on another
# host that allows any origin (Access-Control-Allow-Origin: *) refuses a request with them.
_TAB_FETCH_JS = """
async ([url, limit]) => {
  const response = await fetch(url, {cache: "no-cache", credentials: "same-origin"});
  const text = await response.text();
  const headers = {};
  response.headers.forEach((value, name) => { headers[name] = value; });
  return [response.status, headers, text.length > limit ? text.slice(0, limit) : text];
}
"""

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


async def _within(seconds: float, call: Awaitable[_T]) -> _T:
    """Await one call to a page and return its result; TimeoutError after ``seconds``."""
    async with asyncio.timeout(seconds):
        return await call


async def _quietly(close: Callable[[], Awaitable[object]]) -> None:
    """Run a close or stop call, giving up after ``_CLOSE_TIMEOUT`` seconds; errors are ignored.

    Whatever does not close in time is left to the driver, which ends with the bot.
    """
    with contextlib.suppress(Exception):
        async with asyncio.timeout(_CLOSE_TIMEOUT):
            await close()


async def _serve_blank(route: pw.Route) -> None:
    """Answer a kept tab's own navigation with an empty page, without asking the site."""
    await route.fulfill(status=200, content_type="text/html", body="")


def _note_api_call(calls: list[str], response: pw.Response) -> None:
    """Add the URL of a GET fetch/XHR call that answered JSON to ``calls`` (up to a limit)."""
    request = response.request
    if len(calls) >= _MAX_API_CALLS or request.resource_type not in {"fetch", "xhr"}:
        return
    if request.method != "GET" or response.status != 200:
        return
    if "json" in response.headers.get("content-type", "").lower():
        calls.append(response.url)


def _with_automation_hidden(args: list[str]) -> list[str]:
    """Return ``args`` with the AutomationControlled blink feature disabled.

    It joins a ``--disable-blink-features`` switch already in ``args`` (Chromium reads only
    the last one), so ``navigator.webdriver`` reads false as in a browser a person runs.
    """
    for index, arg in enumerate(args):
        if arg.startswith(_BLINK_FEATURES):
            if _AUTOMATION_FEATURE not in arg[len(_BLINK_FEATURES) :].split(","):
                args[index] = f"{arg},{_AUTOMATION_FEATURE}"
            return args
    return [*args, _BLINK_FEATURES + _AUTOMATION_FEATURE]


class _EarlyTap:
    """Passes the pieces a loading page reports to its fetch's listener, a bounded number."""

    def __init__(self, listener: EarlyListener) -> None:
        """Pass at most ``EARLY_ITEMS`` pieces to ``listener``."""
        self._listener = listener
        self._left = EARLY_ITEMS

    def take(self, item: EarlyText) -> None:
        """Pass one piece on, unless the page has used up its share."""
        if self._left <= 0:
            return
        self._left -= 1
        try:
            self._listener(item)
        except Exception:
            log.exception("handling text seen while a page loaded failed")


class _OwnTabs:
    """Tells the tabs the fetcher opened from the ones a page opened, and closes the latter.

    Playwright announces a new tab of the fetcher's own before ``new_page()`` returns it, so
    a tab born while opens are in flight is closed only once none of them turned out to be it.
    """

    def __init__(self) -> None:
        """Start with no tabs."""
        self._own: set[pw.Page] = set()
        self._opening: set[object] = set()  # a token per new_page() call in flight
        self._unclaimed: dict[pw.Page, set[object]] = {}  # new tabs and the opens they wait for
        self._closing: set[asyncio.Task[None]] = set()

    async def open(self, context: pw.BrowserContext) -> pw.Page:
        """Open a tab of the fetcher's own in ``context`` and return it."""
        token = object()
        self._opening.add(token)
        try:
            page = await context.new_page()
            self._own.add(page)
            self._unclaimed.pop(page, None)
            return page
        finally:
            self._opening.discard(token)
            for other, waiting in list(self._unclaimed.items()):
                waiting.discard(token)
                if not waiting:
                    del self._unclaimed[other]
                    self._close(other)

    def forget(self, page: pw.Page) -> None:
        """Stop counting ``page`` as a tab of the fetcher's own; it is being closed."""
        self._own.discard(page)

    def on_page(self, page: pw.Page) -> None:
        """Handle a tab the context reports: a popup is closed, the fetcher's own tab kept."""
        if page in self._own:
            return
        if self._opening:
            self._unclaimed[page] = set(self._opening)
        else:
            self._close(page)

    def _close(self, page: pw.Page) -> None:
        """Close, in the background, a tab a page opened (window.open, a link to a new window)."""
        log.debug("closing a tab a page opened: %s", page.url)
        task = asyncio.ensure_future(_quietly(page.close))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)


class PlaywrightFetcher:
    """Renders pages in one shared Chromium, a tab per fetch, ``max_concurrent_pages`` at once.

    Playwright and its browser start again after a crash, and between renders after an hour
    or 10 000 renders, because Playwright keeps a little memory for every call it makes.
    With ``deep_scan`` the result also carries frame and shadow-root markup and the script and
    JSON bodies the page loaded, so addresses hidden there cost no extra request.
    With ``hide_automation`` the browser reads like one a person runs (no "Headless" in its
    User-Agent, ``navigator.webdriver`` false, one language), and its cookies outlive restarts
    in ``data/browser_state.json``, so a passed site check is not asked again.
    """

    def __init__(
        self,
        settings: Settings,
        tuning: Tuning | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        recycle_after: float = RECYCLE_AFTER,
        recycle_renders: int = RECYCLE_RENDERS,
    ) -> None:
        """Set up the fetcher; the browser itself starts in :meth:`start`.

        Playwright is started afresh once it has run ``recycle_after`` seconds or served
        ``recycle_renders`` renders, whichever comes first.
        """
        self._settings = settings
        self._tuning: Tuning = tuning if tuning is not None else settings
        self._clock = clock
        self._recycle_after = recycle_after
        self._recycle_renders = recycle_renders
        self._playwright: pw.Playwright | None = None
        self._browser: pw.Browser | None = None
        self._context: pw.BrowserContext | None = None
        self._tabs = _OwnTabs()
        self._taps: dict[pw.Page, _EarlyTap] = {}  # tabs whose fetch listens while they load
        self._anchors: OrderedDict[str, pw.Page] = OrderedDict()  # origin -> its kept tab
        self._anchor_locks: dict[str, asyncio.Lock] = {}  # origin -> held while its tab opens
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_pages)
        self._launch_lock = asyncio.Lock()
        self._blocked = frozenset(settings.blocked_resource_types)
        self._hide = settings.hide_automation
        self._args = list(settings.browser_args)
        if self._hide:
            self._args = _with_automation_hidden(self._args)
        self._state_path = settings.data_dir / STATE_FILE
        self._state_saved = 0.0  # when the browser state was last saved, on ``clock``
        self._saving: asyncio.Task[None] | None = None
        self.user_agent: str | None = None  # User-Agent the browser sends, None if left as is
        # Gets the User-Agent after each launch that sets one, so HTTP clients can send the same.
        self.on_user_agent: Callable[[str], None] | None = None
        self._sandbox = "--no-sandbox" not in self._args
        self.sandbox_error: str | None = None  # why we dropped the sandbox, None if we didn't
        self._launched = False
        self._stopped = False
        # Called when a relaunch drops the sandbox (the app reports the first start itself).
        self.on_sandbox_lost: Callable[[str], None] | None = None
        self._up_since = 0.0  # when the running driver started, on ``clock``
        self._renders = 0  # renders since then
        self._rendering = 0  # renders in flight
        self._idle = asyncio.Event()  # set while no render is in flight
        self._idle.set()
        self._ready = asyncio.Event()  # cleared while Playwright restarts on schedule
        self._ready.set()

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
        self._stopped = False
        async with self._launch_lock:
            await self._bring_up()

    async def stop(self) -> None:
        """Save the browser state, close the browser and stop Playwright for good.

        A browser or driver that is gone already, or slow to close, is no error.
        """
        self._stopped = True
        if self._saving is not None:
            await asyncio.wait({self._saving})
        await self._save_state()
        await self._close_browser()
        await self._stop_driver()

    async def fetch(
        self, url: str, *, timeout: float | None = None, on_text: EarlyListener | None = None
    ) -> FetchResult:
        """Render ``url`` in a new tab and return the result.

        ``on_text`` hears the address-like text the page shows while it still loads. Page
        errors come back inside the result. If the browser or the Playwright driver died,
        they start again and the fetch is tried once more; :class:`BrowserUnavailable`
        means they could not start.
        """
        async with self._semaphore:
            await self._recycle_if_due()
            self._rendering += 1
            self._idle.clear()
            try:
                return await self._render(url, timeout, on_text)
            finally:
                self._rendering -= 1
                self._renders += 1
                if not self._rendering:
                    self._idle.set()
                self._save_state_when_due()

    async def _render(
        self, url: str, timeout: float | None, on_text: EarlyListener | None
    ) -> FetchResult:
        """Fetch ``url``; if the browser or the driver turns out gone, start it and retry once."""
        for attempt in (1, 2):
            context = await self._ensure_context()
            try:
                return await self._fetch_in(context, url, timeout, on_text)
            except Exception as exc:
                driver = self._driver_gone(exc)
                if not driver and not isinstance(exc, pw.Error):
                    raise
                if attempt == 2 or not (driver or self._browser_gone(exc)):
                    return FetchResult(url, None, error=self._brief(exc))
                what = "Playwright driver" if driver else "browser connection"
                log.warning("%s lost (%s); starting it again", what, self._brief(exc))
                await self._discard(context, driver=driver)
        return FetchResult(url, None, error="browser unavailable")

    async def _fetch_in(
        self,
        context: pw.BrowserContext,
        url: str,
        timeout: float | None,
        on_text: EarlyListener | None = None,
    ) -> FetchResult:
        """Open ``url`` in a new tab of ``context``, let it settle and read it.

        A timeout or page error comes back in the result, a page that stops answering too;
        an error meaning the browser or the driver is gone is raised, so :meth:`fetch` can
        start them again. The new tab holds no earlier document, so all it reports to
        ``on_text`` is ``url`` loading.
        """
        tuning = self._tuning
        load_timeout = timeout or self._settings.page_load_timeout
        deep_scan = tuning.deep_scan
        try:
            page = await _within(load_timeout, self._tabs.open(context))
        except TimeoutError:
            error = f"browser did not open a tab within {load_timeout:.0f}s"
            return FetchResult(url, None, error=error, timed_out=True)
        if on_text is not None:
            self._taps[page] = _EarlyTap(on_text)
        responses: list[pw.Response] = []
        if deep_scan:
            # Playwright wraps the handler, and a builtin like responses.append breaks that.
            page.on("response", lambda response: responses.append(response))
        api_calls: list[str] = []
        page.on("response", lambda response: _note_api_call(api_calls, response))
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
                # A page that keeps polling never goes idle. The settle delay covers that case.
                with contextlib.suppress(pw.TimeoutError):
                    await page.wait_for_load_state(
                        "networkidle", timeout=tuning.network_idle_timeout * 1000
                    )
            if tuning.settle_delay > 0:
                await asyncio.sleep(tuning.settle_delay)
            try:
                html = await _within(load_timeout, page.content())
                dom, bodies = (
                    await self._deep_scan(page, responses, timeout=load_timeout)
                    if deep_scan
                    else ((), ())
                )
            except TimeoutError:
                # A script stuck in an endless loop keeps the page from answering.
                return FetchResult(
                    url,
                    None,
                    error=f"page did not respond within {load_timeout:.0f}s",
                    timed_out=True,
                    started_at=started_at,
                )
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
                api_calls=tuple(api_calls),
            )
        finally:
            self._taps.pop(page, None)
            self._tabs.forget(page)
            await _quietly(page.close)

    async def tab_fetch(self, url: str, *, origin: str, timeout: float = 10.0) -> FetchResult:
        """Fetch ``url`` with fetch() from a tab kept on ``origin``, as the browser would.

        The request carries the browser's cookies, headers and TLS, so a site that refuses a
        plain HTTP client but lets the browser in answers it. The tab stays on the origin for
        the next call; only a timeout or a closed tab gives it up. Errors come back in the
        result.
        """
        await self._ready.wait()
        started_at = time.time()
        try:
            # Not under the timeout: a browser start cut short would leave its process behind.
            context = await self._ensure_context()
        except Exception as exc:
            return FetchResult(url, None, error=self._brief(exc), started_at=started_at)
        page: pw.Page | None = None
        try:
            async with asyncio.timeout(timeout):
                page = await self._anchor(context, origin)
                status, headers, text = await page.evaluate(_TAB_FETCH_JS, [url, _TAB_FETCH_LIMIT])
        except TimeoutError:
            if page is not None:
                await self._drop_anchor(origin, page)
            error = f"no answer within {timeout:.0f}s"
            return FetchResult(url, None, error=error, timed_out=True, started_at=started_at)
        except Exception as exc:  # a fetch that failed, a closed tab, a browser that is gone
            if page is not None and (page.is_closed() or self._browser_gone(exc)):
                await self._drop_anchor(origin, page)
            return FetchResult(url, None, error=self._brief(exc), started_at=started_at)
        answered = {str(name).lower(): str(value) for name, value in dict(headers).items()}
        return FetchResult(
            url,
            int(status),
            str(text),
            headers=answered,
            retry_after=parse_retry_after(answered.get("retry-after")),
            started_at=started_at,
        )

    async def _anchor(self, context: pw.BrowserContext, origin: str) -> pw.Page:
        """Return the tab kept on ``origin``, opening one in ``context`` if there is none.

        Only calls for the same origin wait while its tab opens, so one tab serves them all.
        Beyond ``_MAX_ANCHORS`` origins the one used longest ago gives its tab up.
        """
        async with self._anchor_locks.setdefault(origin, asyncio.Lock()):
            page = self._anchors.pop(origin, None)
            if page is not None and not page.is_closed():
                self._anchors[origin] = page  # now the one used last
                return page
            if page is not None:
                self._tabs.forget(page)  # it closed on its own (a crash)
            page = await self._open_anchor(context, origin)
            self._anchors[origin] = page
        while len(self._anchors) > _MAX_ANCHORS:
            _, oldest = self._anchors.popitem(last=False)
            self._tabs.forget(oldest)
            await _quietly(oldest.close)
        return page

    async def _open_anchor(self, context: pw.BrowserContext, origin: str) -> pw.Page:
        """Open a tab on ``origin`` that shows an empty document we serve ourselves.

        The site is asked nothing, so no redirect takes the tab to another host, no app of
        the site loads in it, and a slow site can't hold it up.
        """
        url = origin + _ANCHOR_PATH
        page = await self._tabs.open(context)
        try:
            await page.route(lambda requested: requested == url, _serve_blank)
            await page.goto(url, wait_until="domcontentloaded")
        except BaseException:
            self._tabs.forget(page)
            await _quietly(page.close)
            raise
        return page

    async def _drop_anchor(self, origin: str, page: pw.Page) -> None:
        """Close ``page``, the tab kept on ``origin``; the next call opens a fresh one.

        A tab that is no longer the one kept there (replaced meanwhile) is left alone.
        """
        if self._anchors.get(origin) is not page:
            return
        del self._anchors[origin]
        self._tabs.forget(page)
        await _quietly(page.close)

    def _on_early(self, source: dict[str, Any], *args: object) -> None:
        """Take a piece of text a loading tab reports and pass it to that tab's fetch.

        Pieces from a tab no fetch listens to, and calls not shaped like the observer's
        (a page could call the binding itself), are dropped.
        """
        tap = self._taps.get(source.get("page"))  # type: ignore[arg-type]
        if tap is None or len(args) != 3:
            return
        kind, text, before = args
        if kind not in ("text", "attr") or not isinstance(text, str) or not isinstance(before, str):
            return
        tap.take(EarlyText(kind, text[:EARLY_CHARS], before[-BEFORE_CHARS:], time.time()))

    async def _deep_scan(
        self, page: pw.Page, responses: list[pw.Response], *, timeout: float | None = None
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return the markup of child frames and shadow roots, and the bodies the page loaded.

        Bodies are the script, JSON and other text responses; both share the
        ``deep_scan_max_bytes`` budget, frames and shadow roots first. Reading a frame or the
        shadow roots raises TimeoutError after ``timeout`` seconds (default
        ``PAGE_LOAD_TIMEOUT``); the bodies share that much time, and the rest is left out.
        """
        seconds = timeout or self._settings.page_load_timeout
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
                take(await _within(seconds, frame.content()))
        with contextlib.suppress(pw.Error):
            take(str(await _within(seconds, page.evaluate(_SHADOW_ROOTS_JS)) or ""))
        dom = len(texts)
        # Sort by URL, not by arrival, so every render fills the budget with the same bodies
        # and an address we skipped once doesn't show up later as "new".
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
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
        """Return the shared browser context, starting the browser (and the driver) if gone.

        A failed start raises :class:`BrowserUnavailable`.
        """
        if self._context is not None and self.is_connected:
            return self._context
        async with self._launch_lock:
            if self._context is None or not self.is_connected:
                await self._close_browser()
                await self._bring_up()
            assert self._context is not None
            return self._context

    async def _bring_up(self) -> None:
        """Launch the browser, first starting the Playwright driver when none runs.

        A driver found dead on the way is replaced once. Raise :class:`BrowserUnavailable`
        when either cannot start, or once the fetcher was stopped.
        """
        if self._stopped:
            raise BrowserUnavailable("the browser was stopped")
        for attempt in (1, 2):
            await self._start_driver()
            try:
                await self._launch()
                return
            except Exception as exc:
                if self._driver_gone(exc):
                    await self._stop_driver()
                    if attempt == 1:
                        continue
                    brief = self._brief(exc)
                    raise BrowserUnavailable(f"browser could not start: {brief}") from exc
                if isinstance(exc, pw.Error):
                    raise BrowserUnavailable(self._explain_launch_error(exc)) from exc
                raise

    async def _start_driver(self) -> None:
        """Start the Playwright driver unless one runs; its age and render count start at zero.

        Raise :class:`BrowserUnavailable` when it cannot start.
        """
        if self._playwright is not None:
            return
        try:
            self._playwright = await pw.async_playwright().start()
        except Exception as exc:
            raise BrowserUnavailable(f"Playwright could not start: {self._brief(exc)}") from exc
        self._up_since, self._renders = self._clock(), 0

    async def _stop_driver(self) -> None:
        """Stop the Playwright driver, if there is one, and forget it; a dead one is no error."""
        playwright, self._playwright = self._playwright, None
        if playwright is not None:
            await _quietly(playwright.stop)

    def _recycle_due(self) -> bool:
        """True once the driver has run ``recycle_after`` seconds or ``recycle_renders`` renders."""
        if self._playwright is None or self._stopped:
            return False
        return (
            self._renders >= self._recycle_renders
            or self._clock() - self._up_since >= self._recycle_after
        )

    async def _recycle_if_due(self) -> None:
        """Start Playwright and the browser afresh when they have served long enough.

        The restart waits for the renders in flight (``_RECYCLE_WAIT`` seconds at most), and
        renders that come meanwhile wait for the new browser; this frees what Playwright
        keeps for every call it made.
        """
        await self._ready.wait()
        if not self._recycle_due():
            return
        self._ready.clear()
        try:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_RECYCLE_WAIT):
                    await self._idle.wait()
            log.info(
                "restarting Playwright and the browser after %d renders in %.0f min (frees memory)",
                self._renders,
                (self._clock() - self._up_since) / 60,
            )
            async with self._launch_lock:
                if self._saving is not None:
                    await asyncio.wait({self._saving})  # a save in the background ends first
                await self._save_state()
                await self._close_browser()
                await self._stop_driver()
                await self._bring_up()
        finally:
            self._ready.set()

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

        The context refuses downloads, closes any tab a page opens, and runs the observer that
        reports addresses while a page loads. If the sandbox keeps Chromium from starting (as
        root, or without user namespaces), launch again without it and remember why; the
        browser is then isolated only by the bot's own user account. With automation hidden,
        ``on_user_agent`` hears the User-Agent the context sends.
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
            user_agent = await self._plain_user_agent(browser) if self._hide else None
            context = await self._open_context(browser, user_agent)
            context.on("page", self._tabs.on_page)
            await context.route("**/*", self._route)
            # The binding goes first, because the observer calls it from the first node parsed.
            await context.expose_binding(EARLY_BINDING, self._on_early)
            await context.add_init_script(OBSERVER_JS)
        except BaseException:
            await _quietly(browser.close)
            raise
        self._browser, self._context = browser, context
        self._launched = True
        self._state_saved = self._clock()
        log.info(
            "browser started (%s, headless=%s, sandbox=%s, args=%s)",
            channel or "chromium",
            cfg.headless,
            "on" if self._sandbox else "off",
            " ".join(self._args) or "none",
        )
        if user_agent is not None:
            self.user_agent = user_agent
            if self.on_user_agent is not None:
                self.on_user_agent(user_agent)

    @staticmethod
    async def _plain_user_agent(browser: pw.Browser) -> str | None:
        """Return the browser's own User-Agent with "HeadlessChrome" read as "Chrome".

        It is read from a throwaway tab, so the version and platform are the browser's
        own. None if it cannot be read; the browser then keeps its own.
        """
        try:
            page = await _within(_CLOSE_TIMEOUT, browser.new_page())
            try:
                agent = await _within(_CLOSE_TIMEOUT, page.evaluate("navigator.userAgent"))
            finally:
                await _quietly(page.context.close)
        except (pw.Error, TimeoutError) as exc:
            log.warning(
                "could not read the browser's User-Agent: %s", PlaywrightFetcher._brief(exc)
            )
            return None
        return str(agent).replace("HeadlessChrome/", "Chrome/")

    async def _open_context(self, browser: pw.Browser, user_agent: str | None) -> pw.BrowserContext:
        """Open the shared context; with automation hidden it also restores the saved state.

        It then speaks ``LOCALE`` with the given User-Agent. A saved state that cannot be
        used is moved aside, and the context starts without it.
        """
        options: dict[str, Any] = {
            "viewport": {"width": 1366, "height": 900},
            "ignore_https_errors": True,
            "accept_downloads": False,
        }
        if not self._hide:
            return await browser.new_context(**options)
        options["locale"] = LOCALE
        options["extra_http_headers"] = {"Accept-Language": ACCEPT_LANGUAGE}
        if user_agent is not None:
            options["user_agent"] = user_agent
        state = self._saved_state()
        if state is not None:
            try:
                return await browser.new_context(storage_state=state, **options)
            except pw.Error as exc:
                self._set_state_aside(self._brief(exc))
        return await browser.new_context(**options)

    def _saved_state(self) -> dict[str, Any] | None:
        """Return the browser state saved by an earlier browser, or None when there is none.

        A file that does not hold a state is moved aside.
        """
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            self._set_state_aside(str(exc))
            return None
        if not isinstance(state, dict) or not isinstance(state.get("cookies", []), list):
            self._set_state_aside("not a browser state")
            return None
        return state

    def _set_state_aside(self, reason: str) -> None:
        """Move an unusable state file aside (``.bad``), so the next save starts afresh."""
        log.warning("%s is unusable (%s); the browser starts without it", self._state_path, reason)
        with contextlib.suppress(OSError):
            self._state_path.replace(self._state_path.with_name(STATE_FILE + ".bad"))

    def _save_state_when_due(self) -> None:
        """Start saving the browser state in the background once ``_STATE_EVERY`` has passed."""
        if not self._hide or self._stopped or self._context is None:
            return
        if self._saving is not None and not self._saving.done():
            return
        if self._clock() - self._state_saved < _STATE_EVERY:
            return
        self._state_saved = self._clock()
        self._saving = asyncio.ensure_future(self._save_state())

    async def _save_state(self) -> None:
        """Save the context's cookies and local storage to ``data/browser_state.json``.

        A state over ``_STATE_MAX_CHARS`` is saved with its cookies only. A browser that
        cannot hand its state over (closing, gone) leaves the last save in place.
        """
        context = self._context
        if not self._hide or context is None:
            return
        try:
            state = await _within(_STATE_READ_TIMEOUT, context.storage_state())
        except Exception as exc:
            log.debug("browser state not saved: %s", self._brief(exc))
            return
        text = json.dumps(state)
        if len(text) > _STATE_MAX_CHARS:
            text = json.dumps({"cookies": state.get("cookies", []), "origins": []})
        try:
            await asyncio.to_thread(atomic_write_text, self._state_path, text)
        except OSError as exc:
            log.warning("could not save the browser state to %s: %s", self._state_path, exc)

    @staticmethod
    def _sandbox_trouble(exc: pw.Error) -> bool:
        """True when a launch failed because of the sandbox (not a timeout or a crash)."""
        text = str(exc).lower()
        return any(sign in text for sign in _SANDBOX_TROUBLE)

    async def _discard(self, context: pw.BrowserContext, *, driver: bool = False) -> None:
        """Close the browser a failed tab used, unless another tab replaced it already.

        With ``driver`` the Playwright driver is stopped as well, so a fresh one starts next.
        """
        async with self._launch_lock:
            if self._context is context:
                await self._close_browser()
                if driver:
                    await self._stop_driver()

    async def _close_browser(self) -> None:
        """Close the browser, if there is one, and forget it and its context; errors are ignored.

        The kept tabs go with it; the next light fetch of their origins opens new ones.
        """
        browser, self._browser, self._context = self._browser, None, None
        for page in self._anchors.values():
            self._tabs.forget(page)
        self._anchors.clear()
        if browser is not None:
            await _quietly(browser.close)

    async def _route(self, route: pw.Route) -> None:
        """Abort a request the bot does not need; let any other through."""
        if is_unwanted(route.request, self._blocked):
            await route.abort()
        else:
            await route.continue_()

    @staticmethod
    def _driver_gone(exc: BaseException) -> bool:
        """True when the error means the Playwright driver process itself is gone."""
        return _DRIVER_GONE in str(exc).lower()

    @staticmethod
    def _browser_gone(exc: BaseException) -> bool:
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
