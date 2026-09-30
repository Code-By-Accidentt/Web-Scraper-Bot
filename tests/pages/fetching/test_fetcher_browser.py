"""Integration test against the real browser.

Skipped unless ``SCRAPER_BOT_BROWSER_TESTS=1`` is set, because it needs the
Playwright Chromium (``playwright install chromium``) and takes a few seconds. Where there is
none (Ubuntu 20.04), ``BROWSER_CHANNEL=chrome`` in the environment runs it on Google Chrome.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import os
import socketserver
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
import pytest

from scraper_bot.chains.addresses.extractor import AddressExtractor
from scraper_bot.pages.fetching.chromium import PlaywrightFetcher
from scraper_bot.pages.fetching.result import EarlyText
from scraper_bot.settings.env import Settings
from tests.conftest import extract, make_settings

pytestmark = pytest.mark.skipif(
    os.getenv("SCRAPER_BOT_BROWSER_TESTS") != "1",
    reason="set SCRAPER_BOT_BROWSER_TESTS=1 to run browser integration tests",
)

ADDR = "0x11111111111111111111111111111111111111{:02d}"
INDEX_HTML = f"""<!doctype html><html><head><meta charset="utf-8"><title>Coverage</title>
<meta name="description" content="{ADDR.format(1)}">
<!-- {ADDR.format(2)} -->
<script src="/bundle.js"></script></head><body>
<p>visible {ADDR.format(3)}</p>
<a href="https://etherscan.io/token/{ADDR.format(4)}">link</a>
<div data-contract="{ADDR.format(5)}"></div>
<script type="application/json">{{"contract": "{ADDR.format(6)}"}}</script>
<div id="js"></div><div id="host"></div>
<iframe src="/frame.html"></iframe>
<script>
  document.getElementById("js").textContent = "{ADDR.format(7)}";
  fetch("/api").then(r => r.json()).then(j => {{ window.__api = j; }});
</script></body></html>"""
BUNDLE_JS = f"""window.CONFIG = {{presale: "{ADDR.format(20)}"}};
document.addEventListener("DOMContentLoaded", () => {{
  const root = document.getElementById("host").attachShadow({{mode: "open"}});
  root.innerHTML = "<p>{ADDR.format(15)}</p>";
}});"""
FRAME_HTML = f"<!doctype html><html><body><p>{ADDR.format(16)}</p></body></html>"
API_JSON = f'{{"launch": "{ADDR.format(17)}"}}'
# An address in the HTML, another one a script shows 0.2 s later; polling keeps it from idling.
EARLY_HTML = f"""<p>static CA: {ADDR.format(30)}</p><p id="ca">soon</p><script>
setTimeout(() => {{ document.getElementById("ca").textContent = "CA: {ADDR.format(31)}"; }}, 200);
setInterval(() => fetch("/api?poll=" + Math.random()), 150);
</script>"""
HOSTILE = {
    # an endless loop once the document has loaded: the page stops answering
    "/spin": "<p>spin</p><script>setTimeout(() => { while (true) {} }, 50);</script>",
    # a popup and a link that opens a new window
    "/popups": "<p>popups</p><a id='x' href='/api?blank' target='_blank'>x</a><script>"
    "window.open('/api?popup'); document.getElementById('x').click();</script>",
}
ASKED: list[str] = []  # the paths the test site was asked for


@pytest.fixture
def site(tmp_path: Path) -> Iterator[str]:
    (tmp_path / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (tmp_path / "bundle.js").write_text(BUNDLE_JS, encoding="utf-8")
    (tmp_path / "frame.html").write_text(FRAME_HTML, encoding="utf-8")
    ASKED.clear()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tmp_path), **kwargs)

        def do_GET(self):
            ASKED.append(self.path)
            if self.path in ("/api", "/cors"):
                body = API_JSON.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if self.path == "/cors":  # an API that lets any site read it, without cookies
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/slow":
                time.sleep(3)  # longer than the 1 s load timeout the test asks for
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body>slow</body></html>")
                return
            if self.path in ("/cookie", "/whoami"):
                body = (
                    f"<html><body><p>ua: {self.headers.get('User-Agent')}</p>"
                    f"<p>cookie: {self.headers.get('Cookie')}</p><p id='wd'></p><script>"
                    "document.getElementById('wd').textContent = 'wd: ' + navigator.webdriver;"
                    "</script></body></html>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                if self.path == "/cookie":
                    self.send_header("Set-Cookie", "passed=1; Max-Age=3600; Path=/")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in HOSTILE or self.path == "/early":
                body = EARLY_HTML if self.path == "/early" else HOSTILE[self.path]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"<html><body>{body}</body></html>".encode())
                return
            super().do_GET()

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
    finally:
        server.shutdown()


def suffixes(texts: tuple[str, ...]) -> set[str]:
    extractor = AddressExtractor()
    return {a.value[-2:] for text in texts for a in extract(extractor, text)}


def settings_for(tmp_path: Path, **overrides: str) -> Settings:
    """Build test settings that use the browser named by BROWSER_CHANNEL, if the env sets one."""
    channel = os.getenv("BROWSER_CHANNEL")
    if channel:
        overrides.setdefault("BROWSER_CHANNEL", channel)
    return make_settings(tmp_path, **overrides)


@asynccontextmanager
async def browser(settings: Settings) -> AsyncIterator[PlaywrightFetcher]:
    fetcher = PlaywrightFetcher(settings)
    await fetcher.start()
    try:
        yield fetcher
    finally:
        await fetcher.stop()


async def test_deep_scan_finds_addresses_outside_the_dom(site: str, tmp_path: Path) -> None:
    settings = settings_for(tmp_path, SETTLE_DELAY="0.5", DEEP_SCAN="true")
    async with browser(settings) as fetcher:
        result = await fetcher.fetch(site)
    assert result.ok
    assert suffixes((result.html,)) == {"01", "02", "03", "04", "05", "06", "07"}
    assert suffixes((result.html, *result.extra_texts)) == {
        "01",
        "02",
        "03",
        "04",
        "05",
        "06",
        "07",
        "15",
        "16",
        "17",
        "20",
    }


async def test_deep_scan_can_be_disabled(site: str, tmp_path: Path) -> None:
    settings = settings_for(tmp_path, SETTLE_DELAY="0.5", DEEP_SCAN="false")
    async with browser(settings) as fetcher:
        before = time.time()
        result = await fetcher.fetch(site)
        after = time.time()
    assert result.extra_texts == () and result.dom_texts == ()
    assert result.started_at is not None and before <= result.started_at <= after


async def test_load_timeout_override_is_reported(site: str, tmp_path: Path) -> None:
    settings = settings_for(tmp_path, DEEP_SCAN="false")
    async with browser(settings) as fetcher:
        result = await fetcher.fetch(site.replace("/index.html", "/slow"), timeout=1)
    assert result.timed_out and not result.ok
    assert result.error == "timeout after 1s"


async def test_a_page_that_stops_answering_times_out(site: str, tmp_path: Path) -> None:
    settings = settings_for(tmp_path, DEEP_SCAN="true", SETTLE_DELAY="0")
    async with browser(settings) as fetcher:
        started = time.monotonic()
        result = await fetcher.fetch(site.replace("/index.html", "/spin"), timeout=2)
        took = time.monotonic() - started
        assert (await fetcher.fetch(site)).ok  # the tab is gone, the browser still renders
    assert result.timed_out and result.error == "page did not respond within 2s"
    assert took < 15


async def test_tabs_a_page_opens_are_closed(site: str, tmp_path: Path) -> None:
    settings = settings_for(tmp_path, DEEP_SCAN="false", SETTLE_DELAY="0.5")
    async with browser(settings) as fetcher:
        result = await fetcher.fetch(site.replace("/index.html", "/popups"))
        context = fetcher._context
        assert context is not None
        for _ in range(50):  # the popups close in the background
            if not context.pages:
                break
            await asyncio.sleep(0.1)
        assert result.ok and context.pages == []


async def test_a_dead_playwright_driver_is_replaced(site: str, tmp_path: Path) -> None:
    settings = settings_for(tmp_path, DEEP_SCAN="false")
    async with browser(settings) as fetcher:  # stop() at the end must not raise either
        assert (await fetcher.fetch(site)).ok
        for child in psutil.Process().children():  # the driver; Chromium runs under it
            with contextlib.suppress(psutil.NoSuchProcess):
                if child.name().lower().startswith("node"):
                    child.kill()
                    child.wait(10)
        result = await fetcher.fetch(site)
        assert result.ok and fetcher.is_connected


async def test_addresses_are_heard_while_the_page_still_loads(site: str, tmp_path: Path) -> None:
    settings = settings_for(tmp_path, DEEP_SCAN="false", NETWORK_IDLE_TIMEOUT="2")
    heard: list[tuple[float, EarlyText]] = []
    async with browser(settings) as fetcher:
        result = await fetcher.fetch(
            site.replace("/index.html", "/early"),
            on_text=lambda item: heard.append((time.monotonic(), item)),
        )
        done = time.monotonic()
    assert result.ok and ADDR.format(31) in result.html
    shown = [(at, item) for at, item in heard if ADDR.format(31) in item.text]
    assert any(ADDR.format(30) in item.text for _, item in heard)  # parsed from the HTML
    assert shown and shown[0][1].kind == "text"
    assert done - shown[0][0] > 1.0  # long before the page was read (it never goes idle)


async def test_the_browser_reads_like_a_person_and_keeps_cookies_across_restarts(
    site: str, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path, DEEP_SCAN="false")
    async with browser(settings) as fetcher:
        assert (await fetcher.fetch(site.replace("/index.html", "/cookie"))).ok
    async with browser(settings) as fetcher:  # a new browser, as after a restart
        page = (await fetcher.fetch(site.replace("/index.html", "/whoami"))).html
    assert "cookie: passed=1" in page and "wd: false" in page
    assert "ua: Mozilla/5.0" in page and "Headless" not in page


async def test_a_tab_kept_on_the_site_fetches_with_the_browser_cookies(
    site: str, tmp_path: Path
) -> None:
    origin = site.replace("/index.html", "")
    api_host = origin.replace("127.0.0.1", "localhost")  # another origin, as api.x.io is
    async with browser(settings_for(tmp_path, DEEP_SCAN="false")) as fetcher:
        assert (await fetcher.fetch(origin + "/cookie")).ok  # the site sets a cookie
        first = await fetcher.tab_fetch(origin + "/whoami", origin=origin)
        again = await fetcher.tab_fetch(origin + "/whoami", origin=origin)  # the same tab
        # The kept tab's page never came from the network, so Chromium can't tell it's on
        # this machine too, and asks before it reaches another local origin. We say yes.
        assert fetcher._context is not None
        await fetcher._context.grant_permissions(["local-network-access"])
        api = await fetcher.tab_fetch(api_host + "/cors", origin=origin)
    assert first.status == 200 and "cookie: passed=1" in first.html
    assert "Headless" not in first.html and again.status == 200
    assert api.status == 200 and ADDR.format(17) in api.html
    assert "/robots.txt" not in ASKED  # the kept tab itself asks the site nothing


async def test_playwright_starts_afresh_after_its_share_of_renders(
    site: str, tmp_path: Path
) -> None:
    settings = settings_for(
        tmp_path, DEEP_SCAN="false", NETWORK_IDLE_TIMEOUT="0", MAX_CONCURRENT_PAGES="3"
    )
    fetcher = PlaywrightFetcher(settings, recycle_renders=4)
    await fetcher.start()
    try:
        first = fetcher._playwright
        results = await asyncio.gather(*(fetcher.fetch(site) for _ in range(10)))
        assert all(result.ok for result in results)  # none fell into a restart
        assert fetcher._playwright is not first
    finally:
        await fetcher.stop()
