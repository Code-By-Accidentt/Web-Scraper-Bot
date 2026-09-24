"""Telegram HTML helpers.

Every message uses HTML parse mode, so dynamic values go through :func:`escape` and
things people copy (commands, addresses, chat ids) through :func:`code`.
"""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"</?(?:code|pre)>|<a href=\"[^\"]*\">|</a>")


def escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def code(value: object) -> str:
    """Inline monospace: copied with one tap in Telegram."""
    return f"<code>{escape(value)}</code>"


def pre(value: object) -> str:
    return f"<pre>{escape(value)}</pre>"


def link(text: object, url: str) -> str:
    """Clickable text; previews are disabled on every message, so links stay compact."""
    return f'<a href="{html.escape(url, quote=True)}">{escape(text)}</a>'


def strip_markup(text: str) -> str:
    """Plain-text form of an HTML message: tags removed, entities decoded."""
    return html.unescape(_TAG_RE.sub("", text))
