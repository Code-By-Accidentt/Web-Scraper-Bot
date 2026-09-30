"""Recognising answers that mean the site is refusing us rather than being down.

Besides the verdict "blocked or not", the detector names the protection behind a block
when the answer gives it away (Cloudflare, Vercel, AWS WAF, DataDome, ...) and spots
captcha widgets that a person would have to solve on a page that otherwise loads fine.
"""

from __future__ import annotations

import re

from scraper_bot.models.pages import BlockSignal
from scraper_bot.pages.fetching.result import FetchResult

_BLOCK_STATUSES = frozenset({401, 403, 407, 429, 503})
# Phrases added to a block's reason when we can't tell which protection it is. On a 503
# they also tell a refusal from a server that is down.
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
# Title starts that mark a 2xx page as a challenge. Only the start counts, since a real page
# may mention a captcha further on.
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
# A whole title part that's a challenge phrase ("site | Access Denied"). A bare word isn't
# enough, since "Verification | Launchpad" and "Challenge | Meme Arena" are real pages.
_CHALLENGE_PART_RE = re.compile(
    r"(?:security|human|bot|browser) (?:check|captcha|challenge|verification)"
    r"|captcha (?:check|challenge|verification)"
    r"|(?:h|re)captcha(?: challenge)?"
    r"|(?:please )?verify (?:that )?you(?:'re| are) (?:a )?human\.?"
    r"|are you (?:a )?(?:human|robot)\??"
    r"|access denied|attention required!?|just a moment\.*|one more step|ddos protection"
)
_TITLE_SEPARATORS_RE = re.compile(r"\s+[|\-–—:·]\s+|\s*[|·]\s*")
# Each entry is (name, header signs, page signs), and a header sign is "header" or
# "header=value prefix". The first match wins, and the plain CDNs come last because they
# sit in front of normal pages too.
_PROTECTIONS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        # Only marks of the interstitial itself. Challenge-platform scripts and cf-chl-widget
        # ids show up on normal pages too, so they're not here.
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
_PLAIN_CDNS = frozenset({"Akamai", "Cloudflare"})
# Challenge phrases a server's own error page carries too: Cloudflare puts its widget ids
# (cf-chl-widget-...) on normal pages. They don't make a 503 a refusal.
_COMMON_MARKERS = frozenset({"cf-chl"})
# Widgets a person has to solve, as (name, substring checked first, pattern). Invisible
# ones (size=invisible, checkbox-invisible) are left out. The length caps in the patterns
# keep hostile HTML cheap to scan.
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
_REFUSED_CONNECTION = "err_connection_refused"  # nobody listens: the server is down
_CONNECTION_ERRORS = (
    "err_connection_reset",
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
        """Return a block signal when the answer looks like a refusal, or None.

        Block statuses, connection resets and challenge pages count; firewall headers from
        Cloudflare, Vercel and AWS WAF mark a challenge even on a 2xx answer. A 503 without
        any sign of a protection and a refused connection are an outage signal: the server
        is down (a deploy, maintenance). Other fetch errors return None.
        """
        headers = {key.lower(): value.lower() for key, value in result.headers.items()}
        mitigated = (
            headers.get("cf-mitigated") == "challenge"
            or headers.get("x-vercel-mitigated") in {"challenge", "deny"}
            or headers.get("x-amzn-waf-action") in {"captcha", "challenge", "block"}
        )
        if result.status in _BLOCK_STATUSES:
            return self._signal(f"HTTP {result.status}", result, headers, result.retry_after)
        if result.error is not None:
            lowered = result.error.lower()
            if _REFUSED_CONNECTION in lowered:
                return BlockSignal(result.error[:120], outage=True)
            if any(code in lowered for code in _CONNECTION_ERRORS):
                return BlockSignal(result.error[:120])
            return None
        if mitigated or self._challenge_title(result.html):
            return self._signal(f"challenge page with HTTP {result.status or 200}", result, headers)
        return None

    @staticmethod
    def captcha(html: str) -> str | None:
        """Return the name of a captcha widget a person would have to solve here, or None."""
        if "captcha" not in html and "turnstile" not in html:
            return None
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
        """Build the signal for ``answer``, naming the protection when the answer shows it.

        Without a known protection, a challenge phrase from the page is added to the reason.
        A 503 that shows no protection beyond a plain CDN, no challenge title and no
        challenge phrase is an outage signal.
        """
        page = result.html[:_PAGE].lower()
        protection = self._protection(headers, page)
        if protection is None:
            marker = self._marker(page)
            if marker is not None:
                answer += f" ({marker})"
        outage = result.status == 503 and not self._guarded(result.html, page, protection)
        return BlockSignal(answer, retry_after, protection, outage)

    def _guarded(self, html: str, page: str, protection: str | None) -> bool:
        """True when a refusal shows more than a plain CDN in front of the server.

        That is a protection we know by name, a challenge title, or a challenge phrase near
        the top of the page.
        """
        if protection is not None and protection not in _PLAIN_CDNS:
            return True
        head = page[:_HEAD]
        phrases = (marker for marker in _CHALLENGE_MARKERS if marker not in _COMMON_MARKERS)
        return self._challenge_title(html) or any(marker in head for marker in phrases)

    @staticmethod
    def _protection(headers: dict[str, str], page: str) -> str | None:
        """Return the first protection whose header or page signs match, or None."""
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
        """Return the first challenge phrase near the top of the page, or None."""
        head = page[:_HEAD]
        return next((marker for marker in _CHALLENGE_MARKERS if marker in head), None)

    @staticmethod
    def _challenge_title(html: str) -> bool:
        """True when the page's title starts with a challenge phrase or has a part that is one."""
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
