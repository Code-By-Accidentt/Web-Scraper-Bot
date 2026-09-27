"""Requests a rendered page makes that the bot never needs.

Next.js prefetches the pages its links point to (ten per load on some sites), and analytics
beacons report every render to the site owner as a visit. Neither changes what the page shows.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from playwright import async_api as pw

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


def _same_page(path: str, request: pw.Request) -> bool:
    """True when ``path`` is the page the request comes from, or that page is unknown."""
    try:
        page = urlsplit(request.frame.url).path
    except pw.Error:  # a service worker's request has no frame
        return True
    return path.rstrip("/") == page.rstrip("/")


def is_unwanted(request: pw.Request, blocked: frozenset[str]) -> bool:
    """Return True for blocked resource types, Next.js prefetches and analytics beacons.

    Any ``?_rsc=`` fetch of another page is a prefetch, header or not: links with
    ``prefetch={true}`` send none. The page's own ``?_rsc=`` fetch must pass, as
    ``router.refresh()`` may be what reveals a CA. Vercel's checkpoint always passes.
    """
    url = request.url
    if _ALWAYS_ALLOWED in url:
        return False  # before the type check, so the checkpoint's images and fonts load too
    if request.resource_type in blocked:
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
