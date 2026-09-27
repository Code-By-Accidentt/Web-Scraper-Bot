"""Turning rendered HTML into a compact, comparable list of nodes."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from itertools import islice
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lxml import etree
from lxml import html as lxml_html

from scraper_bot.models.pages import Node, Signature

# Subtrees with nothing worth comparing; a canvas's size is set by JS and differs per render.
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

# Attributes kept per element, the first in document order. A script can set thousands on
# one element, and lxml needs quadratic time to parse and to read that many.
MAX_ATTRIBUTES = 200
_NAME = r"[^\s\"'<>/=]"
# A start tag with more attributes than that, written the way Chromium writes them.
_ATTRIBUTE_FLOOD_RE = re.compile(
    rf'(<[A-Za-z]{_NAME}*(?:\s+{_NAME}+="[^"]*"){{{MAX_ATTRIBUTES}}})(?:\s+{_NAME}+="[^"]*")+'
)

_WS_RE = re.compile(r"\s+")
# Zero-width spaces and joiners, direction marks, the word joiner and the BOM.
_INVISIBLE_RE = re.compile(r"[​-‏⁠﻿]")
# Pages are parsed in worker threads; an lxml parser must not be shared between threads.
_PARSERS = threading.local()


def _parser() -> lxml_html.HTMLParser:
    """Return this thread's HTML parser, creating it on first use."""
    parser = getattr(_PARSERS, "parser", None)
    if parser is None:
        # huge_tree: without it libxml2 silently drops everything after a text node over 10 MB.
        parser = lxml_html.HTMLParser(
            encoding="utf-8", remove_comments=True, remove_pis=True, huge_tree=True
        )
        _PARSERS.parser = parser
    return parser


class HtmlNormalizer:
    """Builds a :data:`Signature` (the page's nodes in order) out of raw HTML.

    Each node holds an element's *own* text, not its descendants', so a leaf change never
    inflates the diff through every ancestor. Elements with neither attributes nor own text
    are skipped.
    """

    def __init__(self, ignored_attributes: Iterable[str] = ()) -> None:
        """Leave the attributes named in ``ignored_attributes`` out of every node."""
        self._ignored = frozenset(name.lower() for name in ignored_attributes)

    def signature(self, html: str) -> Signature:
        """Return the page's nodes in document order, or an empty tuple for unparsable HTML.

        The subtrees in :data:`DROP_TAGS` (scripts, styles, frames, ...) are dropped first.
        """
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

    @staticmethod
    def _parse(html: str) -> lxml_html.HtmlElement | None:
        """Return the document root, or None when the HTML is empty or cannot be parsed.

        Start tags with more than :data:`MAX_ATTRIBUTES` attributes lose the rest first.
        """
        text = html.replace("\x00", "").strip()
        if not text:
            return None
        text = _ATTRIBUTE_FLOOD_RE.sub(r"\1", text)
        try:
            return lxml_html.document_fromstring(text.encode("utf-8"), parser=_parser())
        except (etree.ParserError, etree.XMLSyntaxError, ValueError, TypeError):
            return None

    def _node(self, element: lxml_html.HtmlElement) -> Node | None:
        """Return the node for one element, or None when it has no attributes and no own text.

        Only the first :data:`MAX_ATTRIBUTES` attributes count.
        """
        attrib = element.attrib
        if len(attrib) > MAX_ATTRIBUTES:
            # Read lazily: lxml looks every value up from the first attribute on.
            items = list(islice(attrib.iteritems(), MAX_ATTRIBUTES))
        else:
            items = attrib.items()
        attrs: list[tuple[str, str]] = []
        for name, value in items:
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
        """Return the cleaned text that sits directly in the element, not in its children."""
        pieces = [element.text or ""]
        pieces.extend(child.tail or "" for child in element)
        return cls._clean_text(" ".join(pieces))

    @staticmethod
    def _clean_text(text: str) -> str:
        """Return ``text`` without invisible characters and with whitespace collapsed."""
        text = _INVISIBLE_RE.sub("", text.replace("\xa0", " "))
        return _WS_RE.sub(" ", text).strip()

    @classmethod
    def _clean_attribute(cls, key: str, value: str) -> str:
        """Return a cleaned attribute value: class names sorted, URLs without tracking."""
        value = cls._clean_text(value)
        if key == "class":
            return " ".join(sorted(value.split()))
        if key in URL_ATTRIBUTES:
            return cls._strip_tracking(value)
        return value

    @staticmethod
    def _strip_tracking(url: str) -> str:
        """Return ``url`` without ``utm_*`` and the other tracking or cache-busting parameters."""
        if "?" not in url:
            return url
        try:
            parts = urlsplit(url)
        except ValueError:  # "https://[YOUR_HOST]/x?y": not a URL, just text to compare
            return url
        kept = [
            (name, val)
            for name, val in parse_qsl(parts.query, keep_blank_values=True)
            if not (name.lower().startswith("utm_") or name.lower() in TRACKING_PARAMS)
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
