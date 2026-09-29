"""Reading from a rendered page what it says about a coming launch.

A page may show a countdown ("Launch in 00:05:30", "2d 3h 4m"), a launch time with its time
zone ("Sep 30, 15:00 UTC"), a countdown target in its scripts (``new Date("…Z")``, a unix time
under a name like ``launchTime``) or in an attribute (``data-countdown-at="…Z"``), or a phrase
such as "CA: soon" or "presale is live". This module only lists them; the auto-turbo confirms
a countdown across renders and plans.
"""

from __future__ import annotations

import bisect
import functools
import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lxml import html as lxml_html

from scraper_bot.models.launch import Countdown, LaunchClues, LaunchTime, Phrase, TimeSource
from scraper_bot.pages.changes.normalizer import DROP_TAGS, parse_document

log = logging.getLogger(__name__)

HORIZON = 24 * 3600.0  # launch times further ahead than this are left alone
_MAX_TEXT = 400_000  # characters of visible text searched
_MAX_SCRIPTS = 512 * 1024  # characters of inline scripts searched
_MAX_FOUND = 20  # most countdowns we list per render, and most launch times per source
_MAX_SHOWN = 80  # about how long a countdown or a time gets with its launch words (chars)
_BEFORE, _AFTER = 120, 60  # characters around a countdown or a time searched for launch words
_CLOSE = 40  # characters right before it where a word of another kind ("round") vetoes
_NEAR = 32  # characters right before it searched for "in", a number, a date or "tomorrow"
_NAME_BEFORE = 80  # characters before a new Date(...) searched for a launch name
_NOT_SHOWN = frozenset({"head", "title", *DROP_TAGS})  # subtrees without visible text

_LAUNCH_WORDS_RE = re.compile(
    r"\b(?:launch\w*|tge|token generation|trading|mint(?:s|ing)?|pre-?sales?|sales?|listings?|"
    r"go(?:es|ing)? live|live in|opens?(?!\s+interest)|opening|starts?|starting|begins?|"
    r"contract address|ca|airdrop|claim(?:ing)?|deploy\w*)\b",
    re.IGNORECASE,
)
# Launch words that lift a veto in front of them ("Presale live now! Launch in 02:03:04").
_STRONG_WORDS_RE = re.compile(
    r"\b(?:launch\w*|tge|token generation|trading|mint(?:s|ing)?|listings?|contract address|ca|"
    r"deploy\w*)\b",
    re.IGNORECASE,
)
_OTHER_WORDS_RE = re.compile(
    r"\b(?:rounds?|epochs?|games?|match(?:es)?|raffles?|spins?|rewards?|auctions?|bids?|"
    r"refresh\w*|updat\w*|resets?|expir\w*|ends?|ending|clos(?:e|es|ing)|server|current|local|"
    r"funding|interest|live\s+now|now\s+live|(?:pre-?)?sales?\s+(?:is\s+)?live(?!\s+in\b)|"
    r"ongoing|in\s+progress)\b",
    re.IGNORECASE,
)
_AGO_RE = re.compile(r"\s*ago\b", re.IGNORECASE)
# "ET" and "PT" only in capitals, since "et" and "pt" are words in other languages.
_ZONE_NAMES = r"utc|gmt|bst|cet|cest|eet|eest|msk|est|edt|pst|pdt|sgt|hkt|kst|jst|(?-i:ET|PT)"
_CLOCK_AFTER_RE = re.compile(
    rf"\s*\(?\s*(?:(?:am|pm|z|{_ZONE_NAMES})\b|a\.m\.|p\.m\.)", re.IGNORECASE
)

# 1d 02:03:04, 02:03:04:05, 02:03:04, 05:30
_COLON_RE = re.compile(
    r"(?<![\d:.,/-])(?:(\d{1,3})\s*(?:d|days?)\s*,?\s*)?"
    r"(\d{1,3})\s*:\s*(\d{2})(?:\s*:\s*(\d{2}))?(?:\s*:\s*(\d{2}))?(?![\d:])",
    re.IGNORECASE,
)
# One number with its unit: "02 Days", "3h", "20 min", "5s"
_PAIR_RE = re.compile(
    r"(?<![\w.])(?P<value>\d{1,3})\s*"
    r"(?P<unit>days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)
# One unit label with its number after it: "Days 02", "Hours: 03"
_LABEL_PAIR_RE = re.compile(
    r"(?<![\w.])(?P<unit>days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)\s*:?\s*"
    r"(?P<value>\d{1,3})(?!\w|\.\d)",
    re.IGNORECASE,
)
_PAIR_GAP_RE = re.compile(r"[\s:,|]*")
_NUMBER_BEFORE_RE = re.compile(r"(?<![\w.])\d{1,3}[\s:,|]*$")
_IN_RE = re.compile(r"\bin\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"d": 86_400, "h": 3_600, "m": 60, "s": 1}

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_ZONES = {  # hours from UTC of the zone names launch pages use
    "utc": 0,
    "gmt": 0,
    "z": 0,
    "bst": 1,
    "cet": 1,
    "cest": 2,
    "eet": 2,
    "eest": 3,
    "msk": 3,
    "est": -5,
    "edt": -4,
    "et": -5,
    "pst": -8,
    "pdt": -7,
    "pt": -8,
    "sgt": 8,
    "hkt": 8,
    "kst": 9,
    "jst": 9,
}
# Pages use these all year round to mean their region's time, with daylight saving.
_REGIONS = {
    "est": "America/New_York",
    "et": "America/New_York",
    "pst": "America/Los_Angeles",
    "pt": "America/Los_Angeles",
    "cet": "Europe/Berlin",
}
_OFFSET_RE = re.compile(r"[+-]\d{2}:?\d{2}")
_MONTH = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_CLOCK = r"(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?"
_ZONE = rf"\(?\s*({_ZONE_NAMES})\b(?:\s*([+\-−]\s*\d{{1,2}}(?::?\d{{2}})?))?\s*\)?"
_SEP = r"\s*,?\s*(?:at|@|-|–|—|\||·)?\s*"
_ISO = r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})"
_ISO_RE = re.compile(_ISO)
_ISO_TEXT_RE = re.compile(rf"(?<![\w.]){_ISO}(?!\w)")
# 2026-09-30 15:00 (UTC), 2026/09/30 3:00 PM UTC+8, 09-30 15:00 UTC (month first)
_NUMERIC_DATE_RE = re.compile(
    r"(?<![\w.:/-])(?:(\d{4})([-/])(\d{1,2})\2(\d{1,2})|(\d{1,2})-(\d{1,2}))"
    rf"(?:T|{_SEP}){_CLOCK}\s*{_ZONE}",
    re.IGNORECASE,
)
_DAY_MONTH_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}(?:,?\s+(\d{{4}}))?{_SEP}{_CLOCK}\s*{_ZONE}",
    re.IGNORECASE,
)
_MONTH_DAY_RE = re.compile(
    rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?{_SEP}{_CLOCK}\s*{_ZONE}",
    re.IGNORECASE,
)
_DOTTED_RE = re.compile(
    rf"(?<![\d.])(\d{{1,2}})\.(\d{{1,2}})(?:\.(\d{{2}}|\d{{4}}))?{_SEP}{_CLOCK}\s*{_ZONE}",
    re.IGNORECASE,
)
_CLOCK_ONLY_RE = re.compile(rf"(?<![\d:.])\b{_CLOCK}\s*{_ZONE}", re.IGNORECASE)
# A date right before a clock, maybe with the start of a range in between
# ("2026-09-30 ", "Oct 01 | ", "Nov 17, 07:30 to "). The clock is then a time of that day.
_DATE_BEFORE_RE = re.compile(
    rf"(?:\d{{1,4}}[-/.]\d{{1,2}}(?:[-/.]\d{{1,4}})?|\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?"
    rf"(?:,?\s+\d{{4}})?|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}(?:,?\s+\d{{4}})?)"
    rf"(?:T|{_SEP})(?:\d{{1,2}}:\d{{2}}(?::\d{{2}})?\s*(?:am|pm)?\s*(?:-|–|—|~|to)\s*)?$",
    re.IGNORECASE,
)
_TOMORROW_BEFORE_RE = re.compile(r"\btomorrow\b[\s,]*(?:at|@|from|-|–|—)?\s*$", re.IGNORECASE)
_TOMORROW_AFTER_RE = re.compile(r"\s*[,(]?\s*tomorrow\b", re.IGNORECASE)

_SCRIPT_KEY_RE = re.compile(
    r"launch|start|tge|count_?down|target|deadline|release|open|live|mint|sale|trading|"
    r"listing|unlock",
    re.IGNORECASE,
)
# A unix time, or an ISO time with its zone, given to a name: "launchTime: 1790690400". We
# find the value first and read its name back from there, since trying a name at every word
# is much slower.
_GIVEN_NUMBER_RE = re.compile(r"[:=]\s*[\"']?(\d{13}|\d{10})(?![\d.])")
_GIVEN_ISO_RE = re.compile(
    r"[:=]\s*[\"'](\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2}))[\"']"
)
_NEW_DATE_RE = re.compile(
    r"new\s+Date\(\s*(?:[\"'`]([^\"'`]{8,40})[\"'`]|(\d{13}))\s*\)|"
    r"Date\.UTC\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})"
    r"(?:\s*,\s*(\d{1,2}))?(?:\s*,\s*(\d{1,2}))?(?:\s*,\s*(\d{1,2}))?"
)
# The name a value is given to ("countDownDate =", '"startAt":'). We match it on the reversed
# scripts, where it comes right after the ":" or "=" and can be read in place.
_GIVEN_TO_RE = re.compile(r"\s*[:=]\s*[\"']?([\w$]{0,40}[A-Za-z_$])(?![\w$])")
# Attribute names that hold a countdown target, and words that name another moment.
_ATTRIBUTE_KEY_RE = re.compile(
    r"count[-_]?down|launch|start|target|deadline|release|open|live|mint|sale|trading|listing|"
    r"unlock|tge"
)
_ATTRIBUTE_OTHER_RE = re.compile(r"(?:^|[-_:.])(?:end|expir|updat|creat)")
_TIME_LENGTHS = range(10, 41)  # lengths of an attribute value that may be a time (chars)
_UNIX_RE = re.compile(r"\d{13}|\d{10}")

_PHRASES = (
    re.compile(
        r"\b(?:ca|contract(?:\s+address)?)\s*[:\-–—]?\s*(?:is\s+)?(?:coming\s+soon|soon|tba|tbd|"
        r"to\s+be\s+announced|will\s+be\s+(?:announced|revealed|posted|shared))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bcontract\s+address\s+(?:is\s+)?(?:coming|drops?|dropping|reveal(?:ed|s|ing)?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\blaunch(?:ing|es)?\s+(?:today|tonight)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:launch(?:ing|ed)?\s+(?:now|live)|now\s+launching|launch\s+is\s+live)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:pre-?sale|public\s+sale|token\s+sale|mint(?:ing)?|trading|tge|claim(?:ing)?)\s+"
        r"(?:is\s+)?(?:now\s+)?(?:live|open(?:ed)?)\b",
        re.IGNORECASE,
    ),
)
_LABEL_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_WS_RE = re.compile(r"\s+")
# Zero-width spaces and joiners, direction marks, the word joiner and the BOM.
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u2060\ufeff]")
_NUMBERS_RE = re.compile(r"\d+")
_COLON_GAP_RE = re.compile(r"(?<=\d) ?: ?(?=\d)")


def read_launch(html: str, read_at: float) -> LaunchClues:
    """Return what a rendered page says about a coming launch, as read at ``read_at``.

    Only launch times in the next ``HORIZON`` seconds count; a countdown is listed whatever
    it shows, the auto-turbo judges it.
    """
    root = parse_document(html)
    if root is None:
        return LaunchClues(read_at)
    scripts = _inline_scripts(root)
    in_attributes = _attribute_times(root, read_at)
    text = _visible_text(root)
    phrases = _phrases(text)
    return LaunchClues(
        read_at,
        countdowns=tuple(_countdowns(text, launch_page=bool(phrases))),
        times=(*_text_times(text, read_at), *_script_times(scripts, read_at), *in_attributes),
        phrases=phrases,
        text_size=len(text),
    )


def _inline_scripts(root: lxml_html.HtmlElement) -> str:
    """Return the page's inline scripts in document order, ``_MAX_SCRIPTS`` characters at most.

    The smallest are kept first: a countdown's script is small, and one huge inlined bundle
    or data blob would crowd out every other.
    """
    bodies = [
        element.text
        for element in root.iter("script")
        if element.get("src") is None and element.text
    ]
    room = _MAX_SCRIPTS
    kept: dict[int, str] = {}
    for index in sorted(range(len(bodies)), key=lambda index: len(bodies[index])):
        if room <= 0:
            break
        kept[index] = bodies[index][:room]
        room -= len(kept[index])
    return "\n".join(kept[index] for index in sorted(kept))


def _visible_text(root: lxml_html.HtmlElement) -> str:
    """Return the text a visitor sees, in reading order, cut to ``_MAX_TEXT`` characters.

    The head (with the title) and the subtrees in ``DROP_TAGS`` leave the tree; then a number
    only an aria-label holds is written into it (``_read_labels``).
    """
    for element in list(root.iter(*_NOT_SHOWN)):
        if element.getparent() is not None:
            element.drop_tree()
    _read_labels(root)
    text = " ".join(root.itertext())[:_MAX_TEXT]
    return _WS_RE.sub(" ", _INVISIBLE_RE.sub("", text.replace("\xa0", " "))).strip()


def _read_labels(root: lxml_html.HtmlElement) -> None:
    """Write the numbers only an aria-label holds into the tree, as their element's text.

    Custom elements such as ``<number-flow-react>`` draw their digits where the page's text
    does not reach and keep the number in their own aria-label or their wrapper's. It is read
    from an element without any text that is or holds a custom element (a tag with a hyphen);
    one digit gets a leading zero, as a countdown shows it ("06").
    """
    filled: set[lxml_html.HtmlElement] = set()
    for element in root.iterfind(".//*[@aria-label]"):
        label = (element.get("aria-label") or "").strip()
        if not _LABEL_NUMBER_RE.fullmatch(label) or not _holds_custom(element):
            continue
        if "".join(element.itertext()).strip() or filled.intersection(element.iterancestors()):
            continue
        element.text = label.zfill(2)
        filled.add(element)


def _holds_custom(element: lxml_html.HtmlElement) -> bool:
    """True when ``element`` is or holds a custom element: one whose tag has a hyphen."""
    return any(isinstance(node.tag, str) and "-" in node.tag for node in element.iter())


def _phrases(text: str) -> tuple[Phrase, ...]:
    """Return the launch phrases in the visible text, each once, in reading order."""
    found = sorted(
        (match.start(), match.group(0)) for rule in _PHRASES for match in rule.finditer(text)
    )
    phrases: dict[str, Phrase] = {}
    for _, shown in found:
        key = _WS_RE.sub(" ", shown.lower())
        phrases.setdefault(key, Phrase(key, shown))
    return tuple(phrases.values())


class _Spans:
    """Parts of the text already read as something, which never overlap."""

    def __init__(self) -> None:
        """Start with no part taken."""
        self._starts: list[int] = []
        self._ends: list[int] = []

    def overlaps(self, start: int, end: int) -> bool:
        """True when ``text[start:end]`` shares a character with a part taken before."""
        index = bisect.bisect_right(self._starts, start)
        if index and self._ends[index - 1] > start:
            return True
        return index < len(self._starts) and self._starts[index] < end

    def add(self, start: int, end: int) -> None:
        """Take ``text[start:end]``, which overlaps nothing taken before."""
        index = bisect.bisect_right(self._starts, start)
        self._starts.insert(index, start)
        self._ends.insert(index, end)


def _countdowns(text: str, *, launch_page: bool) -> list[Countdown]:
    """Return the launch countdowns in the page's visible text, in reading order.

    A countdown counts with launch words around it, or anywhere on a page that shows a launch
    phrase, but never right after a word of another kind (``_vetoed``). Every countdown found
    numbers the keys of the others of its shape, whether it counts or not.
    """
    found: list[tuple[int, int, float, float]] = []  # start, end, remaining, step
    taken = _Spans()
    for match in _COLON_RE.finditer(text):
        start, end = match.span()
        dated = _DATE_BEFORE_RE.search(text, max(start - _NEAR, 0), start)
        if dated or _CLOCK_AFTER_RE.match(text, end):
            continue  # a time of day ("15:00 UTC", "3:30 pm", "Sep 30, 17:00"), not time left
        remaining = _colon_seconds(match)
        if remaining is not None:
            found.append((start, end, remaining, 1.0))
            taken.add(start, end)
    found += _unit_countdowns(text, taken)
    found.sort()
    shapes: Counter[str] = Counter()
    countdowns = []
    for start, end, remaining, step in found:
        shape = _NUMBERS_RE.sub("#", text[start:end].lower())
        key = f"{shape}#{shapes[shape]}"
        shapes[shape] += 1
        words = _launch_words(text, start, end)
        if _vetoed(text, start) or (words is None and not launch_page):
            continue
        countdowns.append(Countdown(key, remaining, step, _shown(text, words, start, end)))
        if len(countdowns) >= _MAX_FOUND:
            break
    return countdowns


def _colon_seconds(match: re.Match[str]) -> float | None:
    """Return the seconds a colon countdown shows, or None when its numbers cannot be one."""
    days, *parts = match.groups()
    numbers = [int(part) for part in parts if part is not None]
    if days is not None:
        if len(numbers) != 3:
            return None
        numbers = [int(days), *numbers]
    if len(numbers) == 4:
        d, h, m, s = numbers
        if h >= 24 or m >= 60 or s >= 60:
            return None
        return float(((d * 24 + h) * 60 + m) * 60 + s)
    if len(numbers) == 3:
        h, m, s = numbers
        if m >= 60 or s >= 60:
            return None
        return float((h * 60 + m) * 60 + s)
    m, s = numbers
    if s >= 60:
        return None
    return float(m * 60 + s)


def _unit_countdowns(text: str, taken: _Spans) -> list[tuple[int, int, float, float]]:
    """Return countdowns written with units: "2d 3h 4m", "03 Hours 04 Minutes", "Days 02 Hours 03".

    Two or more numbers with falling units in a row make one; a single one only after "in"
    ("in 5m"). A row reads its labels before its numbers only when it starts with a label and
    no number stands right before it, so "02 Days 03 Hours" stays number first. A duration
    followed by "ago" is time passed.
    """
    found = []
    for rule in (_LABEL_PAIR_RE, _PAIR_RE):
        labels_first = rule is _LABEL_PAIR_RE
        for row in _unit_rows(text, rule, taken):
            start, end = row[0][0], row[-1][1]
            if _AGO_RE.match(text, end):
                continue
            lead = max(start - _NEAR, 0)
            if labels_first and (len(row) < 2 or _NUMBER_BEFORE_RE.search(text, lead, start)):
                continue
            if len(row) == 1 and not _IN_RE.search(text, lead, start):
                continue
            remaining = sum(value * _UNIT_SECONDS[unit] for _, _, value, unit in row)
            found.append((start, end, float(remaining), float(_UNIT_SECONDS[row[-1][3]])))
            if labels_first:
                taken.add(start, end)
    return found


def _unit_rows(
    text: str, rule: re.Pattern[str], taken: _Spans
) -> list[list[tuple[int, int, int, str]]]:
    """Return the numbers with units that ``rule`` finds, in rows of falling units.

    Each is (start, end, value, unit letter); one that overlaps a part taken is skipped.
    """
    rows: list[list[tuple[int, int, int, str]]] = []
    for match in rule.finditer(text):
        start, end = match.span()
        if taken.overlaps(start, end):
            continue
        unit = match["unit"][0].lower()
        pair = (start, end, int(match["value"]), unit)
        last = rows[-1][-1] if rows else None
        if (
            last is not None
            and _PAIR_GAP_RE.fullmatch(text, last[1], start) is not None
            and _UNIT_SECONDS[unit] < _UNIT_SECONDS[last[3]]
        ):
            rows[-1].append(pair)
        else:
            rows.append([pair])
    return rows


def _vetoed(text: str, start: int) -> bool:
    """True when a word of another kind stands right before ``text[start:]``.

    "Next round in", "Presale ends in", "Funding / Countdown" or "Sale live now" make what
    follows no launch; a strong launch word after that word lifts the veto ("Presale live
    now! Launch in 02:03:04").
    """
    others = list(_OTHER_WORDS_RE.finditer(text, max(start - _CLOSE, 0), start))
    return bool(others) and _STRONG_WORDS_RE.search(text, others[-1].end(), start) is None


def _launch_words(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Return where the launch words that make ``text[start:end]`` count stand, or None.

    They are the nearest launch word before it, joined by the one right before that when it
    is another word ("Trading opens"); without any there, the first one after it ("00:05:30
    until launch").
    """
    before = list(_LAUNCH_WORDS_RE.finditer(text, max(start - _BEFORE, 0), start))
    if not before:
        after = _LAUNCH_WORDS_RE.search(text, end, end + _AFTER)
        return after.span() if after is not None else None
    nearest, first = before[-1], before[-1].start()
    if len(before) > 1:
        other = before[-2]
        if text[other.end() : first].isspace() and other[0].lower() != nearest[0].lower():
            first = other.start()
    return first, nearest.end()


def _shown(text: str, words: tuple[int, int] | None, start: int, end: int) -> str:
    """Return ``text[start:end]`` as a message shows it: with its launch words ``words``.

    The text between them is cut so that the whole stays about ``_MAX_SHOWN`` characters
    long; digits lose the spaces around their colons ("Launch in 00:05:30").
    """
    shown = text[start:end]
    if words is not None:
        lead = text[words[0] : words[1]]
        room = _MAX_SHOWN - len(lead) - len(shown)
        if words[1] <= start:
            shown = lead + _cut(text[words[1] : start], room, keep_end=True) + shown
        else:
            shown = shown + _cut(text[end : words[0]], room, keep_end=False) + lead
    return _COLON_GAP_RE.sub(":", shown)


def _cut(between: str, room: int, *, keep_end: bool) -> str:
    """Return the text between a countdown or time and its launch words in ``room`` characters.

    Whole words are kept, the ones next to the countdown or time: the last ones of a text
    before it (``keep_end``) or the first ones of a text after it.
    """
    if len(between) <= room:
        return between
    words = between.split()
    kept: list[str] = []
    budget = room - 4  # the ellipsis with its spaces
    for word in reversed(words) if keep_end else words:
        if len(word) + 1 > budget:
            break
        kept.append(word)
        budget -= len(word) + 1
    if keep_end:
        return " … " + "".join(f"{word} " for word in reversed(kept))
    return "".join(f" {word}" for word in kept) + " … "


def _text_times(text: str, now: float) -> list[LaunchTime]:
    """Return the launch times the visible text states with a time zone, in reading order.

    Only times in the next ``HORIZON`` seconds with launch words around them count, never one
    right after a word of another kind. A date a rule matches but cannot read (a 31 September)
    keeps its clock from being read as a clock alone.
    """
    spans: list[tuple[int, int, float | None]] = []
    taken = _Spans()
    for rule, parse in (
        (_ISO_TEXT_RE, _iso_text_time),
        (_NUMERIC_DATE_RE, _numeric_time),
        (_DAY_MONTH_RE, _day_month_time),
        (_MONTH_DAY_RE, _month_day_time),
        (_DOTTED_RE, _dotted_time),
        (_CLOCK_ONLY_RE, _clock_only_time),
    ):
        for match in rule.finditer(text):
            start, end = match.start(), match.start() + len(match[0].rstrip())
            if not taken.overlaps(start, end):
                taken.add(start, end)
                spans.append((start, end, parse(match, now)))
    spans.sort(key=lambda span: span[0])
    shapes: Counter[str] = Counter()
    times = []
    for start, end, at in spans:
        if at is None:
            continue
        shape = _NUMBERS_RE.sub("#", text[start:end].lower())
        key = f"{shape}#{shapes[shape]}"
        shapes[shape] += 1
        words = _launch_words(text, start, end)
        if not now < at <= now + HORIZON or words is None or _vetoed(text, start):
            continue
        times.append(LaunchTime(key, at, _shown(text, words, start, end), TimeSource.TEXT))
        if len(times) >= _MAX_FOUND:
            break
    return times


def _iso_text_time(match: re.Match[str], _now: float) -> float | None:
    """Return the time of "2026-09-30T15:00:00Z" or "2026-09-30T18:00+03:00" in epoch seconds."""
    return _iso(match.group(0))


def _numeric_time(match: re.Match[str], now: float) -> float | None:
    """Return the time of "2026-09-30 15:00 (UTC)" or "09-30 15:00 UTC" in epoch seconds.

    The date is year, month, day, with "-" or "/" between; without its year it is month
    first ("09-30") in the nearest year.
    """
    year, _, month, day, short_month, short_day, hour, minute, half, zone, shift = match.groups()
    month, day = (month, day) if year is not None else (short_month, short_day)
    return _dated(now, year, int(month), int(day), hour, minute, half, zone, shift)


def _day_month_time(match: re.Match[str], now: float) -> float | None:
    """Return the time of "30 Sep 2026, 15:00 UTC" in epoch seconds."""
    day, month, year, hour, minute, half, zone, shift = match.groups()
    return _dated(now, year, _MONTHS[month.lower()[:3]], int(day), hour, minute, half, zone, shift)


def _month_day_time(match: re.Match[str], now: float) -> float | None:
    """Return the time of "Sep 30, 2026 at 3:00 PM UTC" in epoch seconds."""
    month, day, year, hour, minute, half, zone, shift = match.groups()
    return _dated(now, year, _MONTHS[month.lower()[:3]], int(day), hour, minute, half, zone, shift)


def _dotted_time(match: re.Match[str], now: float) -> float | None:
    """Return the time of "30.09.2026 15:00 UTC" (day first) in epoch seconds."""
    day, month, year, hour, minute, half, zone, shift = match.groups()
    if year is not None and len(year) == 2:
        year = f"20{year}"
    return _dated(now, year, int(month), int(day), hour, minute, half, zone, shift)


def _clock_only_time(match: re.Match[str], now: float) -> float | None:
    """Return the moment the clock of "at 15:00 UTC" shows later today, in epoch seconds.

    It is tomorrow's when "tomorrow" stands next to it; a clock already passed today is a
    launch that happened, not one to come. A clock with seconds ("15:04:05 UTC") is a live
    clock, and one right after a date belongs to that date.
    """
    text, (start, end) = match.string, match.span()
    hour, minute, half, zone, shift = match.groups()
    if (minute is None and half is None) or match.group(0).count(":") > 1:
        return None  # "5 UTC" is not a clock at all
    lead = max(start - _NEAR, 0)
    if _DATE_BEFORE_RE.search(text, lead, start):
        return None
    clock, local = _hour(hour, half), _zone(zone, shift)
    if clock is None or local is None:
        return None
    tomorrow = _TOMORROW_BEFORE_RE.search(text, lead, start) or _TOMORROW_AFTER_RE.match(text, end)
    day = datetime.fromtimestamp(now, local).date() + timedelta(days=1 if tomorrow else 0)
    at = _epoch(day.year, day.month, day.day, clock, int(minute or 0), local)
    return at if at is not None and at > now else None


def _dated(
    now: float,
    year: str | None,
    month: int,
    day: int,
    hour: str,
    minute: str | None,
    half: str | None,
    zone: str,
    shift: str | None,
) -> float | None:
    """Return the epoch seconds of a written date and clock; a missing year is the nearest."""
    if minute is None and half is None:
        return None
    clock, local = _hour(hour, half), _zone(zone, shift)
    if clock is None or local is None:
        return None
    if year is not None:
        return _epoch(int(year), month, day, clock, int(minute or 0), local)
    this_year = datetime.fromtimestamp(now, UTC).year
    for candidate in (this_year, this_year + 1):
        at = _epoch(candidate, month, day, clock, int(minute or 0), local)
        if at is not None and at > now - HORIZON:
            return at
    return None


def _hour(hour: str, half: str | None) -> int | None:
    """Return the hour of a clock on 24 hours, reading "3 pm" as 15; None when impossible."""
    value = int(hour)
    if half is None:
        return value if value < 24 else None
    if not 1 <= value <= 12:
        return None
    pm = half.lower().startswith("p")
    return (value % 12) + (12 if pm else 0)


def _zone(name: str, shift: str | None) -> tzinfo | None:
    """Return the time zone of a zone name ("UTC", "EST") with a shift ("+3", "-04:30").

    An offset alone ("+03:00") is one too. EST, ET, PST, PT and CET follow their region's
    daylight saving time, since pages write "EST" in summer as well, unless this machine has
    no time zone data. None when it is no zone.
    """
    name = name.lower()
    if shift is None and name in _REGIONS:
        region = _region(_REGIONS[name])
        if region is not None:
            return region
    if name in _ZONES:
        minutes = _ZONES[name] * 60
    elif _OFFSET_RE.fullmatch(name):
        minutes, shift = 0, name
    else:
        return None
    if shift:
        shift = shift.replace("−", "-").replace(" ", "")
        sign = -1 if shift.startswith("-") else 1
        digits = shift[1:].replace(":", "")
        hours, extra = (digits[:-2], digits[-2:]) if len(digits) > 2 else (digits, "0")
        if int(hours) > 14 or int(extra) >= 60:
            return None
        minutes += sign * (int(hours) * 60 + int(extra))
    return timezone(timedelta(minutes=minutes))


@functools.cache
def _region(key: str) -> tzinfo | None:
    """Return the time zone of a region ("America/New_York"), or None without time zone data."""
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        log.warning("no time zone data for %s: its zone names keep a fixed offset", key)
        return None


def _epoch(year: int, month: int, day: int, hour: int, minute: int, zone: tzinfo) -> float | None:
    """Return the epoch seconds of a date and clock in ``zone``, or None for no such date."""
    try:
        return datetime(year, month, day, hour, minute, tzinfo=zone).timestamp()
    except (ValueError, OverflowError):
        return None


def _script_times(scripts: str, now: float) -> list[LaunchTime]:
    """Return the countdown targets the inline scripts hold for the next ``HORIZON`` seconds.

    A value counts under a name like ``launchTime`` or ``countdownDate``, or in a
    ``new Date(...)`` / ``Date.UTC(...)`` with such a name right before it; each is shown by
    that name, or by the name the date is given to. A date without its zone depends on the
    viewer's clock and is left to the rendered countdown.
    """
    backwards = scripts[::-1]
    found: list[tuple[int, float, str]] = []  # place, launch time, name
    for rule, read in ((_GIVEN_NUMBER_RE, _unix), (_GIVEN_ISO_RE, _iso)):
        for match in rule.finditer(scripts):
            name = _given_to(backwards, match.start() + 1)
            if name is None or not _SCRIPT_KEY_RE.search(name):
                continue
            at = read(match[1])
            if at is not None:
                found.append((match.start(), at, name))
    for match in _NEW_DATE_RE.finditer(scripts):
        start = match.start()
        if not _SCRIPT_KEY_RE.search(scripts, max(start - _NAME_BEFORE, 0), start):
            continue
        written, millis, *utc = match.groups()
        if written is not None:
            at = _iso(written.strip())
        elif millis is not None:
            at = int(millis) / 1000
        else:
            year, month, day, hour, minute, second = (int(part or 0) for part in utc)
            stamp = _epoch(year, month + 1, day, hour, minute, UTC)
            at = stamp + second if stamp is not None else None
        if at is not None:
            call = "new Date(...)" if match[0].startswith("new") else "Date.UTC(...)"
            found.append((start, at, _given_to(backwards, start) or call))
    return _listed(found, now, TimeSource.SCRIPT)


def _given_to(backwards: str, stop: int) -> str | None:
    """Return the name a value is given to: the ``name`` of ``name: value`` or ``name = value``.

    ``backwards`` is the scripts reversed and ``stop`` where the text before the value ends in
    the scripts; None when no name stands right there.
    """
    match = _GIVEN_TO_RE.match(backwards, len(backwards) - stop)
    return match[1][::-1] if match is not None else None


def _attribute_times(root: lxml_html.HtmlElement, now: float) -> list[LaunchTime]:
    """Return the countdown targets element attributes hold for the next ``HORIZON`` seconds.

    An attribute counts when its name carries a launch word and no word of another moment
    ("data-countdown-at", not "data-sale-end") and its value is an ISO time with its zone or
    a unix time in seconds or milliseconds; it is shown by its name.
    """
    found: list[tuple[int, float, str]] = []  # place, launch time, name
    for place, element in enumerate(root.iter()):
        for name, value in element.items():
            if len(value) not in _TIME_LENGTHS or not value[:1].isdigit():
                continue
            key = name.lower()
            if not _ATTRIBUTE_KEY_RE.search(key) or _ATTRIBUTE_OTHER_RE.search(key):
                continue
            at = _unix(value) if _UNIX_RE.fullmatch(value) else _iso(value)
            if at is not None:
                found.append((place, at, key))
    return _listed(found, now, TimeSource.ATTRIBUTE)


def _listed(
    found: list[tuple[int, float, str]], now: float, source: TimeSource
) -> list[LaunchTime]:
    """Return the targets found (place, launch time, name) that fall in the next ``HORIZON``.

    Each is keyed by its source, its name and its place among the others of that name.
    """
    names: Counter[str] = Counter()
    times = []
    for _, at, name in sorted(found):
        shape = f"{source.value}:{_NUMBERS_RE.sub('#', name)}"
        key = f"{shape}#{names[shape]}"
        names[shape] += 1
        if now < at <= now + HORIZON:
            times.append(LaunchTime(key, at, name, source))
            if len(times) >= _MAX_FOUND:
                break
    return times


def _unix(value: str) -> float:
    """Return the epoch seconds of a unix time in seconds (10 digits) or milliseconds (13)."""
    return int(value) / (1000 if len(value) == 13 else 1)


def _iso(value: str) -> float | None:
    """Return the epoch seconds of an ISO time with its zone, or None for any other text."""
    match = _ISO_RE.fullmatch(value)
    if match is None:
        return None
    year, month, day, hour, minute, second, zone = match.groups()
    local = _zone(zone, None)
    if local is None:
        return None
    at = _epoch(int(year), int(month), int(day), int(hour), int(minute), local)
    return at + int(second or 0) if at is not None else None
