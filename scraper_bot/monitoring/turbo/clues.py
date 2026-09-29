"""What the renders of a page say about a coming launch, turned into launch times."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TypeVar

from scraper_bot.models.launch import Countdown, LaunchClues, LaunchTime, TimeSource
from scraper_bot.pages.launch import HORIZON

log = logging.getLogger(__name__)

AGREE_SLACK = 5.0  # how much two readings of a countdown may disagree beyond its step (s)
REPEATS_WITHIN = 3600.0  # a countdown that starts over this soon after its end is a round timer
PHRASE_MEMORY = 24 * 3600.0  # we remember a page's phrase this long after we last saw it
HALF_LOADED = 0.5  # a render with less text than this share of the next one was still loading
_KEEP = 500  # how many countdown readings, and pages with their phrases, we keep (the newest)

K = TypeVar("K")
V = TypeVar("V")


@dataclass(frozen=True, slots=True)
class LaunchCandidate:
    """A launch time one page shows, which may be worth an auto-turbo window."""

    page_url: str
    source: str  # the page and the countdown, time or phrase on it
    what: str  # what the page showed, in words for the messages and the log
    target: float  # the launch time (epoch seconds), or for a phrase, when it appeared
    timed: bool  # False for a phrase, which names no time


@dataclass(slots=True)
class _Reading:
    """The value one countdown showed last, and when a render first showed that value."""

    remaining: float
    at: float  # epoch seconds

    @property
    def target(self) -> float:
        """The moment the countdown reaches zero, by this reading."""
        return self.at + self.remaining


class LaunchClueTracker:
    """Turns what the renders of each page say about a coming launch into launch times.

    A countdown counts once two renders agree on when it ends (within its step plus
    ``AGREE_SLACK``) and the later one shows less time left, and only when that end is less
    than a day away; one that starts over soon after its end (a game round) never counts
    again. A stated time counts at once. A phrase counts when it appears on a page that has
    not shown it for a day, unless the render before was still loading.
    """

    def __init__(self) -> None:
        """Start without any reading."""
        self._readings: dict[tuple[str, str, str], _Reading] = {}  # (site, page, countdown)
        self._targets: dict[tuple[str, str, str], float] = {}  # a countdown's last agreed end
        self._repeating: set[tuple[str, str, str]] = set()  # countdowns that started over
        self._phrases: dict[tuple[str, str], dict[str, float]] = {}  # page -> phrase -> seen
        self._sizes: dict[tuple[str, str], int] = {}  # page -> text size of its last render

    def read(self, site_url: str, page_url: str, clues: LaunchClues) -> list[LaunchCandidate]:
        """Take in one render of ``page_url`` and return the launch times it confirms.

        They come soonest first; a countdown's time is kept only when it is less than a day
        away.
        """
        found: list[LaunchCandidate] = []
        for countdown in clues.countdowns:
            target = self._confirmed(site_url, page_url, countdown, clues.read_at)
            if target is not None and target <= clues.read_at + HORIZON:
                what = f"countdown “{countdown.text}”"
                source = f"{page_url} {countdown.key}"
                found.append(LaunchCandidate(page_url, source, what, target, True))
        for stated in clues.times:
            source = f"{page_url} {stated.key}"
            found.append(LaunchCandidate(page_url, source, _describe(stated), stated.at, True))
        found += self._new_phrases(site_url, page_url, clues)
        return sorted(found, key=lambda candidate: candidate.target)

    def forget(self, site_url: str) -> None:
        """Drop everything read on the pages of ``site_url``."""
        for memory in (self._readings, self._targets, self._phrases, self._sizes):
            for key in [key for key in memory if key[0] == site_url]:
                del memory[key]
        self._repeating = {key for key in self._repeating if key[0] != site_url}

    def _confirmed(
        self, site_url: str, page_url: str, countdown: Countdown, read_at: float
    ) -> float | None:
        """Note one reading of a countdown; return its end once two readings agree on it."""
        key = (site_url, page_url, countdown.key)
        if key in self._repeating or countdown.remaining <= 0:
            return None
        last = self._readings.get(key)
        if last is not None and countdown.remaining == last.remaining:
            return None  # the first render that showed this value dates it best
        reading = _Reading(countdown.remaining, read_at)
        _put(self._readings, key, reading)
        if last is None or reading.remaining >= last.remaining:
            return None
        if abs(reading.target - last.target) > countdown.step + AGREE_SLACK:
            return None
        ended = self._targets.pop(key, None)
        _put(self._targets, key, reading.target)
        started_over = (
            ended is not None
            and ended <= read_at < ended + REPEATS_WITHIN
            and reading.target - ended > countdown.step + AGREE_SLACK
        )
        if started_over:
            self._repeating.add(key)
            log.info(
                "[%s] the countdown “%s” on %s started over; it plans no auto-turbo",
                site_url,
                countdown.text,
                page_url,
            )
            return None
        return reading.target

    def _new_phrases(
        self, site_url: str, page_url: str, clues: LaunchClues
    ) -> list[LaunchCandidate]:
        """Return the phrases that appeared on the page since it last showed them.

        The first render of a page since the start only shows which phrases it carries.
        """
        page, now = (site_url, page_url), clues.read_at
        before = self._sizes.pop(page, None)
        _put(self._sizes, page, clues.text_size)
        seen = self._phrases.pop(page, None)
        if seen is None:
            _put(self._phrases, page, {phrase.key: now for phrase in clues.phrases})
            return []
        seen = {key: at for key, at in seen.items() if now - at < PHRASE_MEMORY}
        new = [phrase for phrase in clues.phrases if phrase.key not in seen]
        seen.update({phrase.key: now for phrase in clues.phrases})
        _put(self._phrases, page, seen)
        if before is not None and before < clues.text_size * HALF_LOADED:
            return []  # the render before was still loading, so these phrases were there already
        return [
            LaunchCandidate(page_url, f"{page_url} “{phrase.key}”", f"“{phrase.text}”", now, False)
            for phrase in new
        ]


def _describe(stated: LaunchTime) -> str:
    """Return a stated launch time in words for the messages: what it is and how it was written."""
    if stated.source is TimeSource.SCRIPT:
        return f"countdown target “{stated.text}” in its scripts"
    if stated.source is TimeSource.ATTRIBUTE:
        return f"countdown target “{stated.text}” in its HTML"
    return f"launch time “{stated.text}”"


def _put(memory: dict[K, V], key: K, value: V) -> None:
    """Store ``value`` as the newest entry of ``memory``; the oldest go beyond ``_KEEP``."""
    memory.pop(key, None)
    memory[key] = value
    while len(memory) > _KEEP:
        del memory[next(iter(memory))]
