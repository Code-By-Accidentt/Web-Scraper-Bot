import pytest

from scraper_bot.models.pages import BlockSignal
from scraper_bot.pages.fetching.blocking import BlockDetector
from scraper_bot.pages.fetching.result import FetchResult

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
    assert signal.reason == "HTTP 403 (just a moment)" and signal.protection is None
    assert inspect(status=503, html="").reason == "HTTP 503"
    limited = inspect(status=429, html="", retry_after=30.0)
    assert limited.reason == "HTTP 429" and limited.retry_after == 30.0


def test_normal_and_missing_pages_are_not_signals() -> None:
    assert inspect(status=200, html=NORMAL) is None
    assert inspect(status=404, html="<title>Not found</title>") is None
    assert inspect(status=None, html="", error="timeout after 30s") is None


def test_challenge_with_200_is_recognised_by_title_or_header_only() -> None:
    challenged = inspect(status=200, html="<title>Attention Required! | Cloudflare</title>")
    assert challenged is not None and challenged.protection == "Cloudflare"
    header = inspect(status=200, html=NORMAL, headers={"CF-Mitigated": "challenge"})
    assert header is not None and header.protection == "Cloudflare challenge"
    assert header.describe() == "Cloudflare challenge (challenge page with HTTP 200)"
    assert inspect(status=200, html=LOGIN) is None  # "captcha" only in the body


@pytest.mark.parametrize(
    "title",
    [
        "CAPTCHA ($CAPTCHA) | fair launch",
        "hCaptcha Labs",
        "Why we never ask you to verify you are human",
        "Launch: access denied to bots, not to you",
    ],
)
def test_a_title_that_only_mentions_a_challenge_is_a_normal_page(title: str) -> None:
    assert inspect(status=200, html=f"<html><head><title>{title}</title></head></html>") is None


def test_a_challenge_title_counts_from_its_first_words() -> None:
    html = "<html><head><title>\n  Access   Denied | example.com</title></head></html>"
    assert inspect(status=200, html=html) is not None
    blocked = "<title>Access to this page has been denied</title><div id='px-captcha'></div>"
    signal = inspect(status=200, html=blocked)
    assert signal is not None and signal.protection == "PerimeterX"


VERCEL = (
    "<html><head><title>Vercel Security Checkpoint</title></head>"
    "<body><p>We're verifying your browser</p></body></html>"
)


def test_vercel_checkpoint_is_recognised_by_title_or_header() -> None:
    page = inspect(status=200, html=VERCEL)
    assert page is not None
    assert page.reason == "challenge page with HTTP 200"
    assert page.protection == "Vercel Security Checkpoint"
    limited = inspect(status=429, html=VERCEL, headers={"x-vercel-mitigated": "challenge"})
    assert limited is not None and limited.describe() == "Vercel Security Checkpoint (HTTP 429)"
    header = inspect(status=200, html=NORMAL, headers={"X-Vercel-Mitigated": "Challenge"})
    assert header is not None and header.protection == "Vercel Security Checkpoint"
    denied = inspect(status=403, html="", headers={"x-vercel-mitigated": "deny"})
    assert denied is not None and denied.describe() == "Vercel Security Checkpoint (HTTP 403)"
    assert inspect(status=200, html=NORMAL, headers={"x-vercel-id": "fra1::abc"}) is None


CF_CHALLENGE = (
    "<html><head><title>Just a moment...</title></head><body>"
    "<script>window._cf_chl_opt={cvId:'3'};</script>"
    "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'></script>"
    "</body></html>"
)


@pytest.mark.parametrize(
    ("status", "html", "headers", "protection"),
    [
        (403, CF_CHALLENGE, {"server": "cloudflare", "cf-ray": "8a1b-FRA"}, "Cloudflare challenge"),
        (
            403,
            "<div class='cf-turnstile' data-sitekey='0x4AAA'></div>",
            {"server": "cloudflare"},
            "Cloudflare Turnstile",
        ),
        (405, "", {"x-amzn-waf-action": "captcha"}, "AWS WAF"),
        (
            403,
            "<script src='https://ct.captcha-delivery.com/c.js'></script>",
            {"x-datadome": "protected"},
            "DataDome",
        ),
        (403, "<title>DDoS-Guard</title>", {"server": "ddos-guard"}, "DDoS-Guard"),
        (403, "", {"x-iinfo": "10-1234"}, "Imperva"),
        (403, "<title>Sucuri WebSite Firewall - Access Denied</title>", {}, "Sucuri"),
        (403, "<script src='https://js.hcaptcha.com/1/api.js'></script>", {}, "hCaptcha"),
        (
            429,
            "<script src='https://www.google.com/recaptcha/api.js'></script>",
            {},
            "reCAPTCHA",
        ),
        (403, "<h1>Access Denied</h1>Reference #18", {"server": "AkamaiGHost"}, "Akamai"),
        (429, "", {"server": "cloudflare", "cf-ray": "8a1b-FRA"}, "Cloudflare"),
    ],
)
def test_a_block_names_the_protection_behind_it(
    status: int, html: str, headers: dict[str, str], protection: str
) -> None:
    signal = inspect(status=status, html=html, headers=headers)
    assert signal is not None and signal.protection == protection
    assert signal.describe() == f"{protection} ({signal.reason})"


def test_protection_headers_of_normal_answers_are_not_blocks() -> None:
    # DataDome and Cloudflare send their headers with every answer, not only with blocks.
    headers = {"x-datadome": "protected", "server": "cloudflare", "cf-ray": "8a1b-FRA"}
    assert inspect(status=200, html=NORMAL, headers=headers) is None


def test_an_unknown_protection_leaves_the_reason_alone() -> None:
    assert BlockSignal("HTTP 429").describe() == "HTTP 429"
    signal = inspect(status=403, html="<title>Forbidden</title>")
    assert signal is not None and signal.describe() == "HTTP 403"


def test_connection_resets_are_signals() -> None:
    signal = inspect(status=None, html="", error="Page.goto: net::ERR_CONNECTION_RESET at x")
    assert signal is not None and "ERR_CONNECTION_RESET" in signal.reason
    assert inspect(status=None, html="", error="Page.goto: net::ERR_NAME_NOT_RESOLVED") is None


RECAPTCHA_V2 = (
    '<div class="g-recaptcha"><iframe title="reCAPTCHA" '
    'src="https://www.google.com/recaptcha/api2/anchor?ar=1&amp;k=6Le&amp;co=aHR0&amp;hl=en'
    '&amp;v=abc&amp;size=normal&amp;cb=x"></iframe></div>'
)
RECAPTCHA_V3 = (
    '<div class="grecaptcha-badge"><iframe title="reCAPTCHA" '
    'src="https://www.google.com/recaptcha/api2/anchor?ar=1&amp;k=6Le&amp;size=invisible'
    '&amp;cb=x"></iframe></div>'
)
HCAPTCHA = (
    '<div class="h-captcha"><iframe src="https://newassets.hcaptcha.com/captcha/v1/abc/static/'
    'hcaptcha.html#frame=checkbox&amp;id=0x&amp;sitekey=10000000" '
    'title="Widget containing checkbox for hCaptcha security challenge"></iframe></div>'
)
HCAPTCHA_INVISIBLE = HCAPTCHA.replace("frame=checkbox&", "frame=checkbox-invisible&")
TURNSTILE = '<form><div class="cf-turnstile" data-sitekey="0x4AAAAAAA"></div></form>'


@pytest.mark.parametrize(
    ("html", "widget"),
    [
        (RECAPTCHA_V2, "reCAPTCHA"),
        (RECAPTCHA_V2.replace("api2", "enterprise").replace("normal", "compact"), "reCAPTCHA"),
        (RECAPTCHA_V3, None),
        (HCAPTCHA, "hCaptcha"),
        (HCAPTCHA_INVISIBLE, None),
        (TURNSTILE, "Cloudflare Turnstile"),
        (
            '<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/'
            'turnstile/if/ov2/av0/rcv0/0/abc/0x4AAA/auto/normal/"></iframe>',
            "Cloudflare Turnstile",
        ),
        (LOGIN, None),  # the class alone, no widget rendered in it
        (NORMAL, None),
        ("<p>We use reCAPTCHA and hCaptcha on the claim page.</p>", None),
    ],
)
def test_visible_captcha_widgets_are_named_and_invisible_ones_ignored(
    html: str, widget: str | None
) -> None:
    assert BlockDetector.captcha(html) == widget


@pytest.mark.parametrize(
    "title",
    [
        "Security Captcha",
        "hCaptcha challenge",
        "Please verify you are human",
        "pons.fun | Access Denied",
        "Human Verification - pons.fun",
    ],
)
def test_a_title_made_of_a_challenge_phrase_is_a_challenge(title: str) -> None:
    signal = inspect(status=200, html=f"<html><head><title>{title}</title></head></html>")
    assert signal is not None


@pytest.mark.parametrize(
    "title",
    [
        "Verify your email | Launchpad",
        "Captcha Coin | fair launch",
        "Security audit: passed",
        "Verification | Launchpad",
        "Check - Airdrop",
        "Challenge | Meme Arena",
        "MEME | Verification",
        "Captcha",
    ],
)
def test_a_title_that_only_uses_the_words_is_a_page(title: str) -> None:
    assert inspect(status=200, html=f"<html><head><title>{title}</title></head></html>") is None


def test_cloudflare_scripts_on_a_normal_page_do_not_make_a_challenge() -> None:
    origin_error = (
        "<html><head><title>Service unavailable</title>"
        "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script></head>"
        "<body><div id='cf-chl-widget-abc'></div></body></html>"
    )
    signal = inspect(status=503, html=origin_error, headers={"server": "cloudflare"})
    assert signal is not None and signal.protection == "Cloudflare"  # the CDN, no challenge


def test_the_widget_search_stays_fast_on_hostile_markup() -> None:
    import time

    soup = "<div " + "class=x " * 40_000 + ">cf-turnstile " * 3 + "/anchor?" + "a" * 50_000
    started = time.perf_counter()
    assert BlockDetector.captcha(soup) is None
    assert time.perf_counter() - started < 1.0
