"""Helpers for the HTML markup of Telegram messages.

Every message uses HTML parse mode, so dynamic values go through :func:`escape` and
things people copy (commands, addresses, chat ids) through :func:`code`.
"""

from __future__ import annotations

import html
import re

TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)\b[^<>]*>")  # any opening or closing tag
_LINK_RE = re.compile(r'<a href="([^"]*)">([^<]*)</a>')  # what link() writes


def escape(value: object) -> str:
    """Return ``value`` as text that is safe inside an HTML message."""
    return html.escape(str(value), quote=False)


def code(value: object) -> str:
    """Wrap ``value`` in inline monospace, which Telegram copies with one tap."""
    return f"<code>{escape(value)}</code>"


def pre(value: object) -> str:
    """Wrap ``value`` in a monospace block."""
    return f"<pre>{escape(value)}</pre>"


def link(text: object, url: str) -> str:
    """Return a clickable link; previews are off on every message, so links stay compact."""
    return f'<a href="{html.escape(url, quote=True)}">{escape(text)}</a>'


def strip_markup(text: str) -> str:
    """Return the plain-text form of an HTML message: tags removed, entities decoded.

    A link keeps its address as ``label (url)``, so the plain fallback still leads there.
    """
    return html.unescape(TAG_RE.sub("", _LINK_RE.sub(_plain_link, text)))


def _plain_link(match: re.Match[str]) -> str:
    """Return a link as ``label (url)``, or only the url when that is the label anyway."""
    url, label = match.group(1), match.group(2)
    return url if html.unescape(label) == html.unescape(url) else f"{label} ({url})"
