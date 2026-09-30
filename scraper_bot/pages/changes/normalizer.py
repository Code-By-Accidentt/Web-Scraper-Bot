"""Turning rendered HTML into a compact, comparable list of nodes."""

from __future__ import annotations

import re
import sys
import threading
import unicodedata
from collections.abc import Iterable
from itertools import islice
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lxml import etree
from lxml import html as lxml_html

from scraper_bot.models.pages import Node, Signature

# Subtrees with nothing worth comparing. A canvas gets its size from JS, and that differs
# from render to render.
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

# We keep this many attributes per element, the first ones in document order. A script can
# set thousands on one element, and lxml takes quadratic time to parse and read that many.
MAX_ATTRIBUTES = 200
_NAME = r"[^\s\"'<>/=]"
# A start tag with more attributes than that, in the name="value" form Chromium writes.
_ATTRIBUTE_FLOOD_RE = re.compile(
    rf'(<[A-Za-z]{_NAME}*(?:\s+{_NAME}+="[^"]*"){{{MAX_ATTRIBUTES}}})(?:\s+{_NAME}+="[^"]*")+'
)

_WS_RE = re.compile(r"\s+")
# Candidates for characters that take no room on screen (Unicode's "format" characters: soft
# hyphens, zero-width spaces and joiners, direction marks, the BOM, ...). It lists those below
# U+10000 and takes every character above, so the search stays fast; _invisible() decides.
_INVISIBLE_RE = re.compile(
    r"[\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u180e\u200b-\u200f"
    r"\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\U00010000-\U0010ffff]"
)
# A <wbr> marks where a long word may wrap. It's no break in the text, but lxml would even
# take the text after it for the <wbr>'s own.
_WBR_RE = re.compile(r"</?wbr\b[^>]*>", re.IGNORECASE)
# Character references that may stand for an invisible character: every numeric one, and the
# named ones that do.
_REFERENCE_RE = re.compile(
    r"&(?:#([0-9]{1,7})|#[xX]([0-9a-fA-F]{1,6})|(shy|zwn?j|lrm|rlm|NoBreak|ZeroWidthSpace|"
    r"Negative(?:VeryThin|Thin|Medium|Thick)Space|af|ApplyFunction|it|InvisibleTimes|ic|"
    r"InvisibleComma));"
)
# Pages get parsed in worker threads, and an lxml parser can't be shared between threads.
_PARSERS = threading.local()


def _parser() -> lxml_html.HTMLParser:
    """Return this thread's HTML parser, creating it on first use."""
    parser = getattr(_PARSERS, "parser", None)
    if parser is None:
        # Without huge_tree, libxml2 quietly drops everything after a text node over 10 MB.
        parser = lxml_html.HTMLParser(
            encoding="utf-8", remove_comments=True, remove_pis=True, huge_tree=True
        )
        _PARSERS.parser = parser
    return parser


def clean_text(text: str) -> str:
    """Return ``text`` without invisible characters and with whitespace collapsed."""
    text = _INVISIBLE_RE.sub(_visible_character, text.replace("\xa0", " "))
    return _WS_RE.sub(" ", text).strip()


def unbroken(text: str) -> str:
    """Return raw markup without what may split a word in it unseen, to find addresses in.

    That is ``<wbr>`` tags and the characters :func:`clean_text` drops as invisible, also
    written as character references (``&shy;``). A page may put them inside a long address.
    """
    text = _WBR_RE.sub("", text)
    if "&" in text:
        text = _REFERENCE_RE.sub(_visible_reference, text)
    return _INVISIBLE_RE.sub(_visible_character, text)


def _invisible(character: str) -> bool:
    """True for a character that takes no room on screen: a Unicode "format" character."""
    return unicodedata.category(character) == "Cf"


def _visible_character(match: re.Match[str]) -> str:
    """Return the character found, or "" when it is invisible."""
    character = match.group(0)
    return "" if _invisible(character) else character


def _visible_reference(match: re.Match[str]) -> str:
    """Return a character reference as it is, or "" when it stands for an invisible character."""
    decimal, hexadecimal, name = match.groups()
    if name is not None:
        return ""
    code = int(decimal) if decimal is not None else int(hexadecimal, 16)
    if code <= sys.maxunicode and _invisible(chr(code)):
        return ""
    return match.group(0)


def parse_document(html: str) -> lxml_html.HtmlElement | None:
    """Return the document root, or None when the HTML is empty or cannot be parsed.

    Start tags with more than :data:`MAX_ATTRIBUTES` attributes lose the rest first, and
    ``<wbr>`` tags go, so the text around one stays one text.
    """
    text = html.replace("\x00", "").strip()
    if not text:
        return None
    text = _WBR_RE.sub("", _ATTRIBUTE_FLOOD_RE.sub(r"\1", text))
    try:
        return lxml_html.document_fromstring(text.encode("utf-8"), parser=_parser())
    except (etree.ParserError, etree.XMLSyntaxError, ValueError, TypeError):
        return None


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
        root = parse_document(html)
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

    def _node(self, element: lxml_html.HtmlElement) -> Node | None:
        """Return the node for one element, or None when it has no attributes and no own text.

        Only the first :data:`MAX_ATTRIBUTES` attributes count.
        """
        attrib = element.attrib
        if len(attrib) > MAX_ATTRIBUTES:
            # Read them lazily, since lxml finds each value by walking from the first attribute.
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

    @staticmethod
    def _own_text(element: lxml_html.HtmlElement) -> str:
        """Return the cleaned text that sits directly in the element, not in its children."""
        pieces = [element.text or ""]
        pieces.extend(child.tail or "" for child in element)
        return clean_text(" ".join(pieces))

    @classmethod
    def _clean_attribute(cls, key: str, value: str) -> str:
        """Return a cleaned attribute value: class names sorted, URLs without tracking."""
        value = clean_text(value)
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
        except ValueError:  # "https://[YOUR_HOST]/x?y" isn't a real URL, just text to compare
            return url
        kept = [
            (name, val)
            for name, val in parse_qsl(parts.query, keep_blank_values=True)
            if not (name.lower().startswith("utm_") or name.lower() in TRACKING_PARAMS)
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
