"""Recognising answers that mean the site is refusing us rather than being down.

Besides the verdict "blocked or not", the detector names the protection behind a block
when the answer gives it away (Cloudflare, Vercel, AWS WAF, DataDome, ...) and spots
captcha widgets that a person would have to solve on a page that otherwise loads fine.
"""

from __future__ import annotations

import re

from scraper_bot.models import BlockSignal
from scraper_bot.pages.fetcher import FetchResult

_BLOCK_STATUSES = frozenset({401, 403, 407, 429, 503})
# Phrases typical for WAF / anti-bot pages, searched in the first part of a block answer
# to explain it when the protection itself is not recognised.
_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "access denied",
    "checking your browser",
    "verify you are human",
    "too many requests",
    "ddos-guard",
    "bot verification",
    "request blocked",
    "captcha",
    "cf-chl",
    "vercel security checkpoint",
)
# A 2xx answer is a challenge only when its <title> begins with one of these: a real page
# may well mention a captcha ("CAPTCHA ($CAPTCHA) | fair launch"), it does not open with that.
_CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "access denied",
    "access to this page has been denied",
    "checking your browser",
    "verify you are human",
    "too many requests",
    "ddos-guard",
    "bot verification",
    "request blocked",
    "request rejected",
    "pardon our interruption",
    "vercel security checkpoint",
)
# ...or has a part (between separators such as "|") that is nothing but a challenge phrase:
# "Security Captcha", "site | Access Denied". "CAPTCHA ($CAPTCHA) | fair launch" is not.
# A word alone ("Verification | Launchpad", "Challenge | Meme Arena") is not enough.
_CHALLENGE_PART_RE = re.compile(
    r"(?:security|human|bot|browser) (?:check|captcha|challenge|verification)"
    r"|captcha (?:check|challenge|verification)"
    r"|(?:h|re)captcha(?: challenge)?"
    r"|(?:please )?verify (?:that )?you(?:'re| are) (?:a )?human\.?"
    r"|are you (?:a )?(?:human|robot)\??"
    r"|access denied|attention required!?|just a moment\.*|one more step|ddos protection"
)
_TITLE_SEPARATORS_RE = re.compile(r"\s+[|\-–—:·]\s+|\s*[|·]\s*")
# Protections and how their answers give them away: headers ("name" is present, or
# "name=value" where the value starts so) or phrases in the first part of the page. The
# first match names the block; the bare CDNs come last, as they front normal pages too.
_PROTECTIONS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        # The interstitial's own markers: Cloudflare puts /cdn-cgi/challenge-platform
        # scripts and cf-chl-widget ids (Turnstile) on normal pages too.
        "Cloudflare challenge",
        ("cf-mitigated=challenge",),
        ("_cf_chl_opt", "orchestrate/chl_page", "orchestrate/managed", "challenge-error-text"),
    ),
    ("Cloudflare Turnstile", (), ("challenges.cloudflare.com/turnstile", "cf-turnstile")),
    ("Vercel Security Checkpoint", ("x-vercel-mitigated",), ("vercel security checkpoint",)),
    ("AWS WAF", ("x-amzn-waf-action",), ("awswaf", "aws-waf-token")),
    ("DataDome", ("x-datadome", "server=datadome"), ("captcha-delivery.com", "datadome")),
    ("DDoS-Guard", ("server=ddos-guard",), ("ddos-guard",)),
    ("PerimeterX", (), ("px-captcha", "perimeterx")),
    ("Imperva", ("x-iinfo",), ("_incapsula_resource", "incapsula incident")),
    ("Sucuri", ("x-sucuri-id", "server=sucuri"), ("sucuri website firewall",)),
    ("hCaptcha", (), ("hcaptcha.com", "h-captcha")),
    ("reCAPTCHA", (), ("google.com/recaptcha", "recaptcha.net", "g-recaptcha")),
    ("Akamai", ("server=akamaighost",), ()),
    ("Cloudflare", ("cf-ray", "server=cloudflare"), ("| cloudflare</title>", "cf-error-details")),
)
# Captcha widgets a person has to tick or solve, as rendered frames. The invisible kinds
# (reCAPTCHA v3 and invisible v2: size=invisible; invisible hCaptcha: checkbox-invisible)
# only watch in the background and are left out.
# Each with a substring that must be there first, and bounded runs: a hostile page of
# endless attribute soup costs a scan, not a quadratic search.
_CAPTCHA_WIDGETS = (
    (
        "reCAPTCHA",
        "/anchor?",
        re.compile(
            r"/recaptcha/(?:api2|enterprise)/anchor\?[^\"'<>]{0,2000}?\bsize=(?:normal|compact)"
        ),
    ),
    (
        "hCaptcha",
        "#frame=checkbox",
        re.compile(r"hcaptcha\.com/[^\"'<>#]{0,500}#frame=checkbox(?![\w-])"),
    ),
    (
        "Cloudflare Turnstile",
        "/turnstile/",
        re.compile(r"challenges\.cloudflare\.com/[^\"'<>]{0,500}?/turnstile/"),
    ),
    (
        "Cloudflare Turnstile",
        "cf-turnstile",
        re.compile(r"<div\b[^>]{0,500}?\bclass=[\"'][^\"'>]{0,500}?\bcf-turnstile\b"),
    ),
)
_CONNECTION_ERRORS = (
    "err_connection_reset",
    "err_connection_refused",
    "err_connection_closed",
    "err_empty_response",
    "err_http2_protocol_error",
    "err_ssl_protocol_error",
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HEAD = 4000  # characters searched for a title or a challenge phrase
_PAGE = 16_000  # characters searched for signs of a protection


class BlockDetector:
    """Turns a :class:`FetchResult` into a :class:`BlockSignal` when it smells like a block."""

    def inspect(self, result: FetchResult) -> BlockSignal | None:
        headers = {key.lower(): value.lower() for key, value in result.headers.items()}
        # Cloudflare, Vercel and AWS WAF mark the answers of their firewalls, even a 2xx one.
        mitigated = (
            headers.get("cf-mitigated") == "challenge"
            or headers.get("x-vercel-mitigated") in {"challenge", "deny"}
            or headers.get("x-amzn-waf-action") in {"captcha", "challenge", "block"}
        )
        if result.status in _BLOCK_STATUSES:
            return self._signal(f"HTTP {result.status}", result, headers, result.retry_after)
        if result.error is not None:
            lowered = result.error.lower()
            if any(code in lowered for code in _CONNECTION_ERRORS):
                return BlockSignal(result.error[:120])
            return None
        if mitigated or self._challenge_title(result.html):
            return self._signal(f"challenge page with HTTP {result.status or 200}", result, headers)
        return None

    @staticmethod
    def captcha(html: str) -> str | None:
        """Name of a captcha widget a person would have to solve on this page, if any."""
        if "captcha" not in html and "turnstile" not in html:
            return None  # the usual page: skip the searches below
        return next(
            (
                name
                for name, sign, pattern in _CAPTCHA_WIDGETS
                if sign in html and pattern.search(html)
            ),
            None,
        )

    def _signal(
        self,
        answer: str,
        result: FetchResult,
        headers: dict[str, str],
        retry_after: float | None = None,
    ) -> BlockSignal:
        page = result.html[:_PAGE].lower()
        protection = self._protection(headers, page)
        if protection is None:
            marker = self._marker(page)
            if marker is not None:
                answer += f" ({marker})"
        return BlockSignal(answer, retry_after, protection)

    @staticmethod
    def _protection(headers: dict[str, str], page: str) -> str | None:
        for name, header_signs, page_signs in _PROTECTIONS:
            for sign in header_signs:
                header, _, prefix = sign.partition("=")
                value = headers.get(header)
                if value is not None and value.startswith(prefix):
                    return name
            if any(sign in page for sign in page_signs):
                return name
        return None

    @staticmethod
    def _marker(page: str) -> str | None:
        head = page[:_HEAD]
        return next((marker for marker in _CHALLENGE_MARKERS if marker in head), None)

    @staticmethod
    def _challenge_title(html: str) -> bool:
        title = _TITLE_RE.search(html[:_HEAD])
        if title is None:
            return False
        text = " ".join(title.group(1).split()).lower()
        if text.startswith(_CHALLENGE_TITLES):
            return True
        return any(
            _CHALLENGE_PART_RE.fullmatch(part.strip())
            for part in _TITLE_SEPARATORS_RE.split(text)
            if part.strip()
        )
