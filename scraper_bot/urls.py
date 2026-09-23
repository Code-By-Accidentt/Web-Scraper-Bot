"""URL normalisation, sub-URL derivation and filesystem-safe names."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

_HOST_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class InvalidUrlError(ValueError):
    """The text cannot be interpreted as an http(s) URL."""


def normalize_url(raw: str) -> str:
    """Return a canonical form of ``raw`` or raise :class:`InvalidUrlError`.

    A missing scheme defaults to ``https``; scheme and host are lower-cased; the
    fragment is dropped and a bare ``/`` path is removed so that
    ``Example.com`` and ``https://example.com/`` map to the same key.
    """
    text = raw.strip()
    if not text or any(ch.isspace() for ch in text):
        raise InvalidUrlError(f"{raw!r} is not a URL (expected e.g. https://example.com)")
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise InvalidUrlError(f"{raw!r} is not a URL (expected e.g. https://example.com)") from exc
    if parts.scheme not in {"http", "https"}:
        raise InvalidUrlError(f"{raw!r}: only http and https URLs can be monitored")
    host = (parts.hostname or "").lower()
    labels = host.split(".")
    if not host or not all(_HOST_LABEL_RE.match(label) for label in labels):
        raise InvalidUrlError(f"{raw!r} has an invalid host name")
    if len(labels) < 2 and host != "localhost":
        raise InvalidUrlError(f"{raw!r} has an invalid host name")
    netloc = f"{host}:{port}" if port else host
    path = parts.path if parts.path not in {"", "/"} else ""
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def registrable_domain(url: str) -> str:
    """Host of ``url`` without a leading ``www.``."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class SubUrlDeriver:
    """Expands a root URL into the well-known sub-pages worth watching."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self._patterns = tuple(patterns)

    def derive(self, root_url: str) -> list[str]:
        """Sub-pages for ``root_url`` in pattern order, without the root itself."""
        domain = registrable_domain(root_url)
        seen: set[str] = {root_url}
        result: list[str] = []
        for pattern in self._patterns:
            try:
                candidate = normalize_url(pattern.format(domain=domain))
            except (InvalidUrlError, KeyError, IndexError, ValueError):
                continue
            if candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
        return result

    def pages(self, root_url: str) -> list[str]:
        """Root URL first, then its derived sub-pages."""
        return [root_url, *self.derive(root_url)]


def url_to_filename(url: str) -> str:
    """Readable, collision-free file stem for ``url``."""
    parts = urlsplit(url)
    readable = _UNSAFE_RE.sub("_", f"{parts.netloc}{parts.path}").strip("_")[:80] or "page"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"
