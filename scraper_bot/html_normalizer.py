"""Turning rendered HTML into a compact, comparable list of nodes."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lxml import etree
from lxml import html as lxml_html

from scraper_bot.models import Node, Signature

# Subtrees that never carry user-visible content worth comparing. ``canvas`` is a
# drawing surface whose width/height are set by JavaScript after load, so it flaps.
DROP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "meta",
        "link",
        "base",
        "iframe",
        "object",
        "embed",
    }
)
# Attributes whose values are URLs and may carry cache-busting or tracking parameters.
URL_ATTRIBUTES = frozenset({"href", "src", "action", "poster", "data-src", "data-href"})
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "yclid",
        "msclkid",
        "ref",
        "_t",
        "t",
        "ts",
        "v",
        "_",
        "cb",
        "cache",
        "nocache",
        "timestamp",
        "rnd",
        "random",
    }
)

_WS_RE = re.compile(r"\s+")
_INVISIBLE_RE = re.compile(r"[​-‏⁠﻿]")
_PARSER = lxml_html.HTMLParser(encoding="utf-8", remove_comments=True, remove_pis=True)


class HtmlNormalizer:
    """Builds a :data:`Signature` (ordered nodes) out of raw HTML.

    Each element becomes one :class:`Node` with its own text (not the text of
    its descendants), so a change in a leaf never inflates the diff through every
    ancestor. Elements without attributes and own text carry no information and
    are skipped.
    """

    def __init__(self, ignored_attributes: Iterable[str] = ()) -> None:
        self._ignored = frozenset(name.lower() for name in ignored_attributes)

    def signature(self, html: str) -> Signature:
        root = self._parse(html)
        if root is None:
            return ()
        for element in list(root.iter(*DROP_TAGS)):
            if element.getparent() is not None:
                element.drop_tree()
        nodes: list[Node] = []
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue  # comments, processing instructions
            node = self._node(element)
            if node is not None:
                nodes.append(node)
        return tuple(nodes)

    # ── internals ───────────────────────────────────────────────────────────
    @staticmethod
    def _parse(html: str) -> lxml_html.HtmlElement | None:
        text = html.replace("\x00", "").strip()
        if not text:
            return None
        try:
            return lxml_html.document_fromstring(text.encode("utf-8"), parser=_PARSER)
        except (etree.ParserError, etree.XMLSyntaxError, ValueError, TypeError):
            return None

    def _node(self, element: lxml_html.HtmlElement) -> Node | None:
        attrs: list[tuple[str, str]] = []
        for name, value in element.attrib.items():
            key = name.lower()
            if key in self._ignored:
                continue
            attrs.append((key, self._clean_attribute(key, value)))
        attrs.sort()
        text = self._own_text(element)
        if not attrs and not text:
            return None
        return Node(element.tag.lower(), tuple(attrs), text)

    @classmethod
    def _own_text(cls, element: lxml_html.HtmlElement) -> str:
        pieces = [element.text or ""]
        pieces.extend(child.tail or "" for child in element)
        return cls._clean_text(" ".join(pieces))

    @staticmethod
    def _clean_text(text: str) -> str:
        text = _INVISIBLE_RE.sub("", text.replace("\xa0", " "))
        return _WS_RE.sub(" ", text).strip()

    @classmethod
    def _clean_attribute(cls, key: str, value: str) -> str:
        value = cls._clean_text(value)
        if key == "class":
            return " ".join(sorted(value.split()))
        if key in URL_ATTRIBUTES:
            return cls._strip_tracking(value)
        return value

    @staticmethod
    def _strip_tracking(url: str) -> str:
        if "?" not in url:
            return url
        parts = urlsplit(url)
        kept = [
            (name, val)
            for name, val in parse_qsl(parts.query, keep_blank_values=True)
            if not (name.lower().startswith("utm_") or name.lower() in TRACKING_PARAMS)
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
