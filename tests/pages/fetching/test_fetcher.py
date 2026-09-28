"""Offline tests for :mod:`scraper_bot.pages.fetching`.

The HTTP prober runs over an ``httpx.MockTransport``; the browser-free helpers of
:class:`PlaywrightFetcher` are driven with duck-typed fakes. The real Chromium is
covered by ``test_fetcher_browser.py`` behind an opt-in flag.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from playwright import async_api as pw

from scraper_bot.pages.fetching.chromium import (
    _PER_RESPONSE_LIMIT,
    _SCANNED_CONTENT_TYPES,
    _SCANNED_RESOURCE_TYPES,
    PlaywrightFetcher,
)
from scraper_bot.pages.fetching.prober import HttpProber, ProbeResult
from scraper_bot.pages.fetching.result import FetchResult
from tests.conftest import make_settings

URL = "https://example.com/app"


# ── result types ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (None, "ConnectError: name not resolved", False),
        (404, None, False),
        (410, None, False),
        (200, None, True),
        (403, None, True),
        (405, None, True),
        (500, None, True),
    ],
)
def test_probe_result_worth_rendering(
    status: int | None, error: str | None, expected: bool
) -> None:
    assert ProbeResult(URL, status, error=error).worth_rendering is expected


@pytest.mark.parametrize(
    ("status", "error", "ok", "reason"),
    [
        (200, None, True, "HTTP 200"),
        (299, None, True, "HTTP 299"),
        (300, None, False, "HTTP 300"),
        (404, None, False, "HTTP 404"),
        (None, None, True, "HTTP None"),
        (None, "timeout after 30s", False, "timeout after 30s"),
        (200, "net::ERR_ABORTED", False, "net::ERR_ABORTED"),
    ],
)
def test_fetch_result_ok_and_reason(
    status: int | None, error: str | None, ok: bool, reason: str
) -> None:
    result = FetchResult(URL, status, "<html></html>", error=error)
    assert result.ok is ok
    assert result.reason == reason


# ── HttpProber ──────────────────────────────────────────────────────────────
class Recorder:
    """MockTransport handler that records requests and answers per (method, path)."""

    def __init__(self, answers: dict[tuple[str, str], httpx.Response | Exception]) -> None:
        self.answers = answers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers[(request.method, request.url.path)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def methods(self) -> list[str]:
        return [request.method for request in self.requests]


async def probe(recorder: Recorder, url: str = URL) -> ProbeResult:
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as http:
        return await HttpProber(http).probe(url)


async def test_prober_trusts_a_head_200_without_a_get() -> None:
    recorder = Recorder({("HEAD", "/app"): httpx.Response(200)})
    result = await probe(recorder)
    assert (result.url, result.status, result.error) == (URL, 200, None)
    assert recorder.methods == ["HEAD"]


async def test_prober_retries_a_rejected_head_with_get() -> None:
    recorder = Recorder(
        {("HEAD", "/app"): httpx.Response(405), ("GET", "/app"): httpx.Response(200)}
    )
    result = await probe(recorder)
    assert result.status == 200
    assert recorder.methods == ["HEAD", "GET"]


async def test_prober_keeps_404_when_get_agrees() -> None:
    recorder = Recorder(
        {("HEAD", "/app"): httpx.Response(404), ("GET", "/app"): httpx.Response(404)}
    )
    result = await probe(recorder)
    assert result.status == 404
    assert result.worth_rendering is False
    assert recorder.methods == ["HEAD", "GET"]


async def test_prober_uses_the_get_status_when_head_lies() -> None:
    recorder = Recorder(
        {("HEAD", "/app"): httpx.Response(404), ("GET", "/app"): httpx.Response(200)}
    )
    result = await probe(recorder)
    assert result.status == 200
    assert result.worth_rendering is True


@pytest.mark.parametrize("status", [200, 403, 500])
def test_prober_statuses_that_need_no_get(status: int) -> None:
    assert status not in HttpProber._RETRY_WITH_GET


async def test_prober_reports_connection_errors() -> None:
    recorder = Recorder({("HEAD", "/app"): httpx.ConnectError("[Errno 11001] getaddrinfo failed")})
    result = await probe(recorder)
    assert result.status is None
    assert result.error is not None
    assert result.error.startswith("ConnectError: ")
    assert "getaddrinfo failed" in result.error
    assert result.worth_rendering is False
    assert recorder.methods == ["HEAD"]


async def test_prober_follows_redirects() -> None:
    recorder = Recorder(
        {
            ("HEAD", "/app"): httpx.Response(302, headers={"location": "/final"}),
            ("HEAD", "/final"): httpx.Response(200),
        }
    )
    result = await probe(recorder)
    assert result.status == 200
    assert [request.url.path for request in recorder.requests] == ["/app", "/final"]


async def test_prober_error_message_is_capped() -> None:
    recorder = Recorder({("HEAD", "/app"): httpx.ReadTimeout("x" * 500)})
    result = await probe(recorder)
    assert result.error is not None
    assert len(result.error) == 200


# ── PlaywrightFetcher helpers (no browser) ──────────────────────────────────
class TargetClosedError(pw.Error):
    """Same name as Playwright's own class, which is what ``_browser_gone`` keys on."""


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TargetClosedError("Target page, context or browser has been closed"), True),
        (pw.Error("Target page, context or browser has been closed"), True),
        (pw.Error("Connection closed while reading from the driver"), True),
        (pw.Error("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://x.io"), False),
        (pw.Error("Page.goto: Timeout 30000ms exceeded."), False),
    ],
)
def test_browser_gone(exc: pw.Error, expected: bool) -> None:
    assert PlaywrightFetcher._browser_gone(exc) is expected


def test_explain_launch_error_points_to_playwright_install() -> None:
    exc = pw.Error(
        "BrowserType.launch: Executable doesn't exist at C:\\ms-playwright\\chrome.exe\n"
        "╔═══════╗\n║ Looks like Playwright was just installed or updated. ║"
    )
    text = PlaywrightFetcher._explain_launch_error(exc)
    assert text.startswith("browser could not start: BrowserType.launch: Executable doesn't exist")
    assert "playwright install chromium" in text
    assert "BROWSER_CHANNEL=chrome" in text  # Ubuntu 20.04 has no Playwright Chromium
    assert "╔" not in text  # only the first line survives


def test_explain_launch_error_for_other_failures() -> None:
    text = PlaywrightFetcher._explain_launch_error(pw.Error("out of memory"))
    assert text == "browser could not start: out of memory"
    assert "playwright install" not in text


def test_brief_keeps_first_line_and_200_chars() -> None:
    assert PlaywrightFetcher._brief(pw.Error("first line\nsecond line")) == "first line"
    assert PlaywrightFetcher._brief(pw.Error("  padded  \n")) == "padded"
    assert PlaywrightFetcher._brief(pw.Error("x" * 500)) == "x" * 200
    assert PlaywrightFetcher._brief(ValueError()) == "ValueError"
    assert PlaywrightFetcher._brief(ValueError("   \n  ")) == "ValueError"


async def test_is_connected_is_false_before_start(tmp_path: Path) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path))
    assert fetcher.is_connected is False


# ── deep scan with duck-typed Playwright objects ────────────────────────────
@dataclass
class FakeFrameRef:
    url: str


@dataclass
class FakeRequest:
    resource_type: str
    url: str = URL
    headers: dict[str, str] = field(default_factory=dict)
    frame_url: str | None = URL  # None: a service worker's request, which has no frame

    @property
    def frame(self) -> FakeFrameRef:
        if self.frame_url is None:
            raise pw.Error("Service Worker requests do not have an associated frame.")
        return FakeFrameRef(self.frame_url)


@dataclass
class FakeResponse:
    resource_type: str
    content_type: str | None = None
    payload: bytes = b""
    body_error: str | None = None
    body_calls: int = 0
    url: str = URL
    length: int | None = None  # the Content-Length header

    @property
    def request(self) -> FakeRequest:
        return FakeRequest(self.resource_type)

    @property
    def headers(self) -> dict[str, str]:
        headers = {} if self.content_type is None else {"content-type": self.content_type}
        if self.length is not None:
            headers["content-length"] = str(self.length)
        return headers

    async def body(self) -> bytes:
        self.body_calls += 1
        if self.body_error is not None:
            raise pw.Error(self.body_error)
        return self.payload


@dataclass
class FakeFrame:
    markup: str = ""
    error: str | None = None

    async def content(self) -> str:
        if self.error is not None:
            raise pw.Error(self.error)
        return self.markup


@dataclass
class FakePage:
    main_frame: FakeFrame
    children: list[FakeFrame] = field(default_factory=list)
    shadow: object = ""
    evaluated: list[str] = field(default_factory=list)

    @property
    def frames(self) -> list[FakeFrame]:
        return [self.main_frame, *self.children]

    async def evaluate(self, script: str) -> object:
        self.evaluated.append(script)
        return self.shadow


@pytest.mark.parametrize(
    ("resource_type", "content_type", "expected"),
    [
        ("script", "application/javascript; charset=utf-8", True),
        ("script", "text/ecmascript", True),
        ("xhr", "application/json", True),
        ("fetch", "Application/JSON", True),
        ("document", "text/html; charset=utf-8", True),
        ("other", "application/xml", True),
        ("script", "application/octet-stream", False),
        ("script", "", False),
        ("script", None, False),
        ("image", "image/svg+xml", False),
        ("stylesheet", "text/css", False),
        ("font", "font/woff2", False),
    ],
)
def test_worth_scanning(resource_type: str, content_type: str | None, expected: bool) -> None:
    response = FakeResponse(resource_type, content_type)
    assert PlaywrightFetcher._worth_scanning(response) is expected  # type: ignore[arg-type]


def test_scanned_constants_are_consistent() -> None:
    assert {"document", "script", "xhr", "fetch"} <= _SCANNED_RESOURCE_TYPES
    assert "json" in _SCANNED_CONTENT_TYPES and "javascript" in _SCANNED_CONTENT_TYPES
    assert _PER_RESPONSE_LIMIT == 3 * 1024 * 1024


async def test_deep_scan_collects_frames_shadow_roots_and_bodies(tmp_path: Path) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path))
    page = FakePage(
        main_frame=FakeFrame("<html>MAIN</html>"),
        children=[
            FakeFrame("<p>frame one</p>"),
            FakeFrame(error="Frame was detached"),
            FakeFrame("<p>frame two</p>"),
        ],
        shadow="<p>shadow markup</p>",
    )
    kept = FakeResponse("script", "application/javascript", b"window.CONFIG = 1;")
    json_answer = FakeResponse("xhr", "application/json", b'{"launch": true}')
    image = FakeResponse("image", "image/png", b"PNG")
    redirect = FakeResponse("script", "text/javascript", body_error="Response body is unavailable")
    huge = FakeResponse("script", "application/javascript", b"x" * (_PER_RESPONSE_LIMIT + 1))
    empty = FakeResponse("fetch", "application/json", b"")

    responses = [kept, json_answer, image, redirect, huge, empty]
    dom, bodies = await fetcher._deep_scan(page, responses)  # type: ignore[arg-type]
    texts = dom + bodies
    assert dom == ("<p>frame one</p>", "<p>frame two</p>", "<p>shadow markup</p>")

    assert texts == (
        "<p>frame one</p>",
        "<p>frame two</p>",
        "<p>shadow markup</p>",
        "window.CONFIG = 1;",
        '{"launch": true}',
    )
    assert "MAIN" not in "".join(texts)
    assert image.body_calls == 0  # not worth scanning: never fetched
    assert redirect.body_calls == 1 and huge.body_calls == 1
    assert len(page.evaluated) == 1 and "shadowRoot" in page.evaluated[0]


async def test_deep_scan_ignores_an_empty_shadow_result(tmp_path: Path) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path))
    page = FakePage(main_frame=FakeFrame("<html/>"), shadow=None)
    assert await fetcher._deep_scan(page, []) == ((), ())  # type: ignore[arg-type]


async def test_deep_scan_honours_the_byte_budget(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, DEEP_SCAN_MAX_MB="0.001")
    assert settings.deep_scan_max_bytes == 1048
    fetcher = PlaywrightFetcher(settings)
    page = FakePage(
        main_frame=FakeFrame("<html/>"),
        children=[FakeFrame("f" * 600)],
        shadow="s" * 600,
    )
    response = FakeResponse("script", "application/javascript", b"never read")

    dom, bodies = await fetcher._deep_scan(page, [response])  # type: ignore[arg-type]
    texts = dom + bodies

    assert texts == ("f" * 600, "s" * 448)
    assert sum(len(text) for text in texts) == settings.deep_scan_max_bytes
    assert response.body_calls == 0  # budget exhausted before the responses


async def test_deep_scan_reads_bodies_in_url_order_whatever_the_arrival(tmp_path: Path) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path, DEEP_SCAN_MAX_MB="0.001"))
    page = FakePage(main_frame=FakeFrame("<html/>"))
    late = FakeResponse("script", "text/javascript", b"b" * 1048, url="https://x.io/a.js")
    early = FakeResponse("script", "text/javascript", b"z" * 1048, url="https://x.io/z.js")
    # z.js arrived first this time; the budget still goes to a.js, as on every render
    assert await fetcher._deep_scan(page, [early, late]) == ((), ("b" * 1048,))  # type: ignore[arg-type]


async def test_deep_scan_clips_a_body_to_the_remaining_budget(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, DEEP_SCAN_MAX_MB="0.001")
    fetcher = PlaywrightFetcher(settings)
    page = FakePage(main_frame=FakeFrame("<html/>"), children=[FakeFrame("f" * 1000)])
    first = FakeResponse("script", "application/javascript", b"a" * 100)
    second = FakeResponse("script", "application/javascript", b"b" * 100)

    dom, bodies = await fetcher._deep_scan(page, [first, second])  # type: ignore[arg-type]
    texts = dom + bodies

    assert texts == ("f" * 1000, "a" * 48)
    assert second.body_calls == 0


# ── request routing ─────────────────────────────────────────────────────────
@dataclass
class FakeRoute:
    resource_type: str
    url: str = URL
    headers: dict[str, str] = field(default_factory=dict)
    frame_url: str | None = "https://x.io/docs"  # the page the request comes from
    aborted: bool = False
    continued: bool = False

    @property
    def request(self) -> FakeRequest:
        return FakeRequest(self.resource_type, self.url, self.headers, self.frame_url)

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


async def test_route_aborts_blocked_resource_types(tmp_path: Path) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path))  # defaults: image, media, font
    outcomes: dict[str, str] = {}
    for resource_type in ("image", "media", "font", "document", "script", "xhr", "stylesheet"):
        route = FakeRoute(resource_type)
        await fetcher._route(route)  # type: ignore[arg-type]
        assert route.aborted != route.continued  # exactly one decision
        outcomes[resource_type] = "abort" if route.aborted else "continue"
    assert outcomes == {
        "image": "abort",
        "media": "abort",
        "font": "abort",
        "document": "continue",
        "script": "continue",
        "xhr": "continue",
        "stylesheet": "continue",
    }


async def test_route_follows_the_configured_block_list(tmp_path: Path) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path, BLOCKED_RESOURCE_TYPES="stylesheet"))
    image, stylesheet = FakeRoute("image"), FakeRoute("stylesheet")
    await fetcher._route(image)  # type: ignore[arg-type]
    await fetcher._route(stylesheet)  # type: ignore[arg-type]
    assert image.continued and not image.aborted
    assert stylesheet.aborted and not stylesheet.continued


@pytest.mark.parametrize(
    ("resource_type", "url", "headers", "blocked"),
    [
        # Next.js prefetches the pages its links point to
        ("fetch", "https://x.io/docs?_rsc=1ld0r", {"rsc": "1", "next-router-prefetch": "1"}, True),
        ("fetch", "https://x.io/docs", {"next-router-prefetch": "1"}, True),
        ("fetch", "https://x.io/docs?_rsc=1", {"next-router-segment-prefetch": "/_tree"}, True),
        ("document", "https://x.io/docs", {"sec-purpose": "prefetch;prerender"}, True),
        ("document", "https://x.io/docs", {"purpose": "prefetch"}, True),
        # prefetch={true} sends no header: an RSC fetch of another page is a prefetch too
        ("fetch", "https://x.io/ideas?_rsc=rxx9e", {"rsc": "1"}, True),
        # the page's own RSC fetch: router.refresh() revealing a CA
        ("fetch", "https://x.io/docs/?_rsc=1ld0r", {"rsc": "1"}, False),
        ("fetch", "https://x.io/docs", {"rsc": "1"}, False),
        # analytics beacons
        ("script", "https://www.googletagmanager.com/gtag/js?id=G-1", {}, True),
        ("ping", "https://region1.google-analytics.com/g/collect?v=2", {}, True),
        ("script", "https://static.cloudflareinsights.com/beacon.min.js", {}, True),
        ("fetch", "https://x.io/_vercel/insights/event", {}, True),
        ("script", "https://x.io/_vercel/speed-insights/script.js", {}, True),
        ("fetch", "https://o1.ingest.us.sentry.io/api/1/envelope/", {}, True),
        ("fetch", "https://o1.ingest.de.sentry.io/api/1/envelope/", {}, True),
        ("fetch", "https://api-eu.mixpanel.com/track", {}, True),
        ("fetch", "https://eu.posthog.com/e/", {}, True),
        ("ping", "https://stats.g.doubleclick.net/g/collect", {}, True),
        ("fetch", "https://x.io/cdn-cgi/rum?", {}, True),
        # what the page needs, and Vercel's own checkpoint
        ("document", "https://x.io/", {}, False),
        ("script", "https://x.io/_next/static/chunks/main.js", {}, False),
        ("fetch", "https://x.io/api/token", {}, False),
        ("script", "https://notgoogle-analytics.com.example/x.js", {}, False),
        ("fetch", "https://x.io/.well-known/vercel/security/request-challenge", {}, False),
        ("fetch", "https://x.io/.well-known/vercel/x?_rsc=1", {}, False),
        ("image", "https://x.io/.well-known/vercel/security/static/logo.png", {}, False),
        ("font", "https://x.io/.well-known/vercel/security/static/inter.woff2", {}, False),
    ],
)
async def test_route_drops_prefetches_and_beacons(
    tmp_path: Path, resource_type: str, url: str, headers: dict[str, str], blocked: bool
) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path))
    route = FakeRoute(resource_type, url, headers)
    await fetcher._route(route)  # type: ignore[arg-type]
    assert (route.aborted, route.continued) == (blocked, not blocked)


class FakeChromium:
    def __init__(self, sandbox_error: str | None = None) -> None:
        self.launches: list[dict[str, object]] = []
        self.routes: list[str] = []
        self.listeners: list[str] = []  # the context events the fetcher listens to
        self.context_options: dict[str, object] = {}
        self.sandbox_error = sandbox_error  # what a launch with the sandbox fails with
        self.closed = 0

    async def launch(self, **kwargs: object) -> FakeChromium:
        self.launches.append(kwargs)
        if kwargs.get("chromium_sandbox") and self.sandbox_error is not None:
            raise pw.Error(self.sandbox_error)
        return self

    async def close(self) -> None:
        self.closed += 1

    async def new_context(self, **kwargs: object) -> FakeContext:
        self.context_options = kwargs
        return FakeContext(self)

    async def new_page(self) -> FakeTab:  # the throwaway tab that reads the User-Agent
        return FakeTab()


HEADLESS_UA = "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/149.0.0.0 Safari/537.36"
HIDDEN = "--disable-blink-features=AutomationControlled"


class FakeTab:
    def __init__(self) -> None:
        self.context = self  # closing its own context closes the tab

    async def evaluate(self, _script: str) -> str:
        return HEADLESS_UA

    async def close(self) -> None:
        pass


class FakeContext:
    def __init__(self, browser: FakeChromium) -> None:
        self.browser = browser

    def on(self, event: str, _handler: object) -> None:
        self.browser.listeners.append(event)

    async def route(self, pattern: str, _handler: object) -> None:
        self.browser.routes.append(pattern)

    async def expose_binding(self, name: str, _callback: object) -> None:
        self.browser.listeners.append(f"binding:{name}")

    async def add_init_script(self, _script: str) -> None:
        self.browser.listeners.append("init script")


@dataclass
class FakePlaywright:
    chromium: FakeChromium = field(default_factory=FakeChromium)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ["--disable-3d-apis", "--disable-webgl", "--disable-webgl2", HIDDEN]),
        ("", [HIDDEN]),
        (
            "--lang=en-US '--window-size=1366,900'",
            ["--lang=en-US", "--window-size=1366,900", HIDDEN],
        ),
    ],
)
async def test_launch_passes_the_browser_args_and_always_installs_the_filter(
    tmp_path: Path, value: str | None, expected: list[str]
) -> None:
    overrides = {} if value is None else {"BROWSER_ARGS": value}
    fetcher = PlaywrightFetcher(make_settings(tmp_path, BLOCKED_RESOURCE_TYPES="", **overrides))
    playwright = FakePlaywright()
    fetcher._playwright = playwright  # type: ignore[assignment]
    await fetcher._launch()
    [launch] = playwright.chromium.launches
    assert launch["args"] == expected
    assert launch["chromium_sandbox"] is True and fetcher.sandboxed
    assert playwright.chromium.routes == ["**/*"]  # beacons are dropped even with no types
    assert playwright.chromium.context_options["accept_downloads"] is False
    # tabs a page opens get closed; the observer calls its binding from the first node
    assert playwright.chromium.listeners == ["page", "binding:__textSeen", "init script"]


async def test_hidden_automation_reads_like_a_person_and_keeps_the_saved_state(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, BROWSER_ARGS="--disable-blink-features=Foo")
    state = {"cookies": [{"name": "cf_clearance", "value": "x", "domain": ".x.io"}], "origins": []}
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "browser_state.json").write_text(json.dumps(state), encoding="utf-8")
    fetcher = PlaywrightFetcher(settings)
    heard: list[str] = []
    fetcher.on_user_agent = heard.append  # the HTTP clients learn it
    playwright = FakePlaywright()
    fetcher._playwright = playwright  # type: ignore[assignment]
    await fetcher._launch()
    assert playwright.chromium.launches[0]["args"] == [
        "--disable-blink-features=Foo,AutomationControlled"
    ]
    options = playwright.chromium.context_options
    assert options["user_agent"] == heard[0] == HEADLESS_UA.replace("Headless", "")
    assert options["locale"] == "en-US" and options["storage_state"] == state

    plain = PlaywrightFetcher(make_settings(tmp_path, HIDE_AUTOMATION="false"))
    plain._playwright = playwright = FakePlaywright()  # type: ignore[assignment]
    await plain._launch()
    assert HIDDEN not in playwright.chromium.launches[0]["args"]  # type: ignore[operator]
    assert "user_agent" not in playwright.chromium.context_options


async def test_without_a_usable_sandbox_the_browser_starts_without_it(tmp_path: Path) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path))
    playwright = FakePlaywright(FakeChromium(sandbox_error="No usable sandbox!\nmore"))
    fetcher._playwright = playwright  # type: ignore[assignment]
    await fetcher._launch()
    assert [launch["chromium_sandbox"] for launch in playwright.chromium.launches] == [
        True,
        False,
    ]
    assert not fetcher.sandboxed and fetcher.sandbox_error == "No usable sandbox!"
    await fetcher._launch()  # a relaunch remembers: no second try with the sandbox
    assert playwright.chromium.launches[-1]["chromium_sandbox"] is False


async def test_no_sandbox_in_the_browser_args_is_respected(tmp_path: Path) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path, BROWSER_ARGS="--no-sandbox"))
    playwright = FakePlaywright()
    fetcher._playwright = playwright  # type: ignore[assignment]
    await fetcher._launch()
    [launch] = playwright.chromium.launches
    assert launch["chromium_sandbox"] is False
    assert not fetcher.sandboxed and fetcher.sandbox_error is None  # nothing to report


async def test_a_tab_that_notices_a_crash_late_keeps_the_relaunched_browser(
    tmp_path: Path,
) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path))
    fetcher._playwright = FakePlaywright()  # type: ignore[assignment]
    await fetcher._launch()
    old = fetcher._context
    browser = fetcher._browser
    await fetcher._launch()  # another tab relaunched the browser meanwhile
    await fetcher._discard(old)  # type: ignore[arg-type]
    assert fetcher._browser is browser and browser.closed == 0  # type: ignore[union-attr]
    await fetcher._discard(fetcher._context)  # type: ignore[arg-type]
    assert fetcher._browser is None and browser.closed == 1  # type: ignore[union-attr]


async def test_a_service_worker_rsc_fetch_is_not_mistaken_for_a_prefetch(tmp_path: Path) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path))
    route = FakeRoute("fetch", "https://x.io/other?_rsc=1", {"rsc": "1"}, frame_url=None)
    await fetcher._route(route)  # type: ignore[arg-type]
    assert route.continued


async def test_deep_scan_does_not_fetch_a_body_announced_as_too_big(tmp_path: Path) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path))
    big = FakeResponse("script", "application/javascript", b"x", length=20 * 1024 * 1024)
    small = FakeResponse("script", "application/javascript", b"window.a=1", length=10)
    page = FakePage(main_frame=FakeFrame("<html/>"))
    _, bodies = await fetcher._deep_scan(page, [big, small])  # type: ignore[arg-type]
    assert big.body_calls == 0 and small.body_calls == 1
    assert bodies == ("window.a=1",)


async def test_a_launch_failure_unrelated_to_the_sandbox_is_not_retried_without_it(
    tmp_path: Path,
) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path))
    playwright = FakePlaywright(FakeChromium(sandbox_error="Timeout 180000ms exceeded"))
    fetcher._playwright = playwright  # type: ignore[assignment]
    with pytest.raises(pw.Error, match="Timeout"):
        await fetcher._launch()
    assert len(playwright.chromium.launches) == 1 and fetcher.sandboxed


async def test_a_relaunch_that_loses_the_sandbox_is_reported(tmp_path: Path) -> None:
    fetcher = PlaywrightFetcher(make_settings(tmp_path))
    chromium = FakeChromium()
    fetcher._playwright = FakePlaywright(chromium)  # type: ignore[assignment]
    lost: list[str] = []
    fetcher.on_sandbox_lost = lost.append
    await fetcher._launch()  # the start: the sandbox works
    chromium.sandbox_error = "Chromium sandboxing failed!"
    await fetcher._launch()  # after a crash it does not any more
    assert lost == ["Chromium sandboxing failed!"] and not fetcher.sandboxed


async def test_a_browser_whose_context_fails_is_closed(tmp_path: Path) -> None:
    class BrokenContext(FakeChromium):
        async def new_context(self, **_kwargs: object) -> FakeContext:
            raise pw.Error("Protocol error (Target.createBrowserContext)")

    fetcher = PlaywrightFetcher(make_settings(tmp_path))
    chromium = BrokenContext()
    fetcher._playwright = FakePlaywright(chromium)  # type: ignore[assignment]
    with pytest.raises(pw.Error):
        await fetcher._launch()
    assert chromium.closed == 1 and fetcher._browser is None
