"""Integration test against the real browser.

Skipped unless ``SCRAPER_BOT_BROWSER_TESTS=1`` is set, because it needs the
Playwright Chromium (``playwright install chromium``) and takes a few seconds.
"""

from __future__ import annotations

import http.server
import os
import socketserver
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from scraper_bot.address_extractor import AddressExtractor
from scraper_bot.fetcher import PlaywrightFetcher
from tests.conftest import make_settings

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


@pytest.fixture
def site(tmp_path: Path) -> Iterator[str]:
    (tmp_path / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (tmp_path / "bundle.js").write_text(BUNDLE_JS, encoding="utf-8")
    (tmp_path / "frame.html").write_text(FRAME_HTML, encoding="utf-8")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tmp_path), **kwargs)

        def do_GET(self):
            if self.path == "/api":
                body = API_JSON.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
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
    return {a.value[-2:] for a in AddressExtractor().extract_many(texts)}


async def test_deep_scan_finds_addresses_outside_the_dom(site: str, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, SETTLE_DELAY="0.5", DEEP_SCAN="true")
    async with PlaywrightFetcher(settings) as fetcher:
        result = await fetcher.fetch(site)
    assert result.ok
    assert suffixes((result.html,)) == {"01", "02", "03", "04", "05", "06", "07"}
    assert suffixes(result.scan_texts) == {
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
    settings = make_settings(tmp_path, SETTLE_DELAY="0.5", DEEP_SCAN="false")
    async with PlaywrightFetcher(settings) as fetcher:
        result = await fetcher.fetch(site)
    assert result.extra_texts == ()
    assert result.scan_texts == (result.html,)


async def test_load_timeout_override_is_reported(site: str, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, DEEP_SCAN="false")
    async with PlaywrightFetcher(settings) as fetcher:
        result = await fetcher.fetch(site.replace("/index.html", "/slow"), timeout=1)
    assert result.timed_out and not result.ok
    assert result.error == "timeout after 1s"
