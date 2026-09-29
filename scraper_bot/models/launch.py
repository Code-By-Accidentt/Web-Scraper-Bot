"""What a render says about a coming token launch: countdowns, launch times, phrases."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TimeSource(Enum):
    """Where a page states a launch time."""

    TEXT = "text"  # a date or a clock with its time zone in the visible text
    SCRIPT = "script"  # a countdown target in an inline script
    ATTRIBUTE = "attribute"  # a countdown target in an element attribute (data-countdown-at=...)


@dataclass(frozen=True, slots=True)
class Countdown:
    """One countdown a page shows as a launch countdown, and the time it has left."""

    key: str  # tells it apart from the page's other countdowns by shape and place among them
    remaining: float  # seconds
    step: float  # its smallest unit in seconds: 1 for 00:05:30, 60 for "3h 20m"
    text: str  # as shown, with the launch words in front of it: "Launch in 00:05:30"


@dataclass(frozen=True, slots=True)
class LaunchTime:
    """A launch time a page states: in its text, its scripts or an element attribute."""

    key: str
    at: float  # epoch seconds
    # As written, with its launch words ("Trading opens at 13:22 UTC"). For a script or an
    # attribute it's the name instead, like "launchTime" or "data-countdown-at".
    text: str
    source: TimeSource


@dataclass(frozen=True, slots=True)
class Phrase:
    """A launch phrase a page shows, such as "CA: soon" or "Presale is LIVE"."""

    key: str  # lower case, whitespace collapsed: "presale is live"
    text: str  # as the page writes it: "Presale is LIVE"


@dataclass(frozen=True, slots=True)
class LaunchClues:
    """Everything one render says about a coming launch, read at ``read_at`` (epoch seconds).

    ``text_size`` is how many characters of visible text the render had, which tells a
    half-loaded render from a complete one.
    """

    read_at: float
    countdowns: tuple[Countdown, ...] = ()
    times: tuple[LaunchTime, ...] = ()
    phrases: tuple[Phrase, ...] = ()
    text_size: int = 0
