"""Telegram HTML formatting helpers.

Every message the bot sends uses Telegram's HTML parse mode, so every dynamic
value (URLs, page text, error messages) must pass through :func:`escape`, and
things people copy (commands, addresses, chat ids) are wrapped in :func:`code`.
"""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"</?(?:code|pre|b|i|u)>|<a href=\"[^\"]*\">|</a>")


def escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def code(value: object) -> str:
    """Inline monospace: copied with one tap in Telegram."""
    return f"<code>{escape(value)}</code>"


def pre(value: object) -> str:
    """Monospace block for multi-line quotes."""
    return f"<pre>{escape(value)}</pre>"


def bold(value: object) -> str:
    return f"<b>{escape(value)}</b>"


def link(text: object, url: str) -> str:
    """Clickable text; previews are disabled on every message, so links stay compact."""
    return f'<a href="{html.escape(url, quote=True)}">{escape(text)}</a>'


def strip_markup(text: str) -> str:
    """Plain-text form of an HTML message: tags removed, entities decoded."""
    return html.unescape(_TAG_RE.sub("", text))
