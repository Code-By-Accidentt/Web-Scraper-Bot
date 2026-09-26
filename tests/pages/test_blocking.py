import pytest

from scraper_bot.pages.blocking import BlockDetector
from scraper_bot.pages.fetcher import FetchResult, _parse_retry_after

URL = "https://example.com"
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>Checking…</body></html>"
NORMAL = "<html><head><title>Launchpad</title></head><body><p>Welcome</p></body></html>"
LOGIN = (
    "<html><head><title>Sign in</title></head>"
    "<body><div class='g-recaptcha'>captcha</div></body></html>"
)


def inspect(**kwargs):
    return BlockDetector().inspect(FetchResult(URL, **kwargs))


def test_block_statuses_are_signals_with_details() -> None:
    signal = inspect(status=403, html=CHALLENGE)
    assert signal is not None
    assert signal.reason == "HTTP 403 (just a moment)"
    assert inspect(status=503, html="").reason == "HTTP 503"
    limited = inspect(status=429, html="", retry_after=30.0)
    assert limited.reason == "HTTP 429" and limited.retry_after == 30.0


def test_normal_and_missing_pages_are_not_signals() -> None:
    assert inspect(status=200, html=NORMAL) is None
    assert inspect(status=404, html="<title>Not found</title>") is None
    assert inspect(status=None, html="", error="timeout after 30s") is None


def test_challenge_with_200_is_recognised_by_title_or_header_only() -> None:
    challenged = inspect(status=200, html="<title>Attention Required! | Cloudflare</title>")
    assert challenged is not None and "attention required" in challenged.reason
    header = inspect(status=200, html=NORMAL, headers={"CF-Mitigated": "challenge"})
    assert header is not None and "cf-mitigated" in header.reason
    assert inspect(status=200, html=LOGIN) is None  # "captcha" only in the body


VERCEL = (
    "<html><head><title>Vercel Security Checkpoint</title></head>"
    "<body><p>We're verifying your browser</p></body></html>"
)


def test_vercel_checkpoint_is_recognised_by_title_or_header() -> None:
    page = inspect(status=200, html=VERCEL)
    assert page is not None
    assert page.reason == "challenge page (vercel security checkpoint) with HTTP 200"
    limited = inspect(status=429, html=VERCEL, headers={"x-vercel-mitigated": "challenge"})
    assert limited is not None and limited.reason == "HTTP 429 (vercel security checkpoint)"
    header = inspect(status=200, html=NORMAL, headers={"X-Vercel-Mitigated": "Challenge"})
    assert header is not None and header.reason == (
        "challenge page (x-vercel-mitigated) with HTTP 200"
    )
    denied = inspect(status=403, html="", headers={"x-vercel-mitigated": "deny"})
    assert denied is not None and denied.reason == "HTTP 403 (challenge)"
    assert inspect(status=200, html=NORMAL, headers={"x-vercel-id": "fra1::abc"}) is None


def test_connection_resets_are_signals() -> None:
    signal = inspect(status=None, html="", error="Page.goto: net::ERR_CONNECTION_RESET at x")
    assert signal is not None and "ERR_CONNECTION_RESET" in signal.reason
    assert inspect(status=None, html="", error="Page.goto: net::ERR_NAME_NOT_RESOLVED") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30", 30.0),
        (" 5 ", 5.0),
        ("-1", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        (None, None),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert _parse_retry_after(value) == expected
