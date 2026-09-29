"""Small helpers for showing times and durations to people."""

from __future__ import annotations

from datetime import datetime


def format_clock(timestamp: float, *, date: bool = True) -> str:
    """Return local time as ``2026-09-23 15:04``, or as ``15:04`` with ``date=False``."""
    pattern = "%Y-%m-%d %H:%M" if date else "%H:%M"
    return datetime.fromtimestamp(timestamp).strftime(pattern)


def format_moment(timestamp: float, now: float) -> str:
    """Return a moment on this machine's clock, like ``15:04``, or ``15:04:05`` off the minute.

    The date comes first when the moment is not on the day of ``now``.
    """
    moment = datetime.fromtimestamp(timestamp)
    text = f"{moment:%H:%M}" if moment.second == 0 else f"{moment:%H:%M:%S}"
    if moment.date() != datetime.fromtimestamp(now).date():
        return f"{moment:%Y-%m-%d} {text}"
    return text


def format_precise(timestamp: float) -> str:
    """Return local time with hundredths of a second, like ``12:34:56.15``."""
    moment = datetime.fromtimestamp(timestamp)
    return f"{moment:%H:%M:%S}.{moment.microsecond // 10_000:02d}"


def format_utc_offset(timestamp: float) -> str:
    """Return the local zone at ``timestamp`` as an offset, like ``UTC+02:00``."""
    try:
        moment = datetime.fromtimestamp(timestamp).astimezone()
    except (OSError, OverflowError, ValueError):  # Windows can't handle some early timestamps
        moment = datetime.now().astimezone()
    offset = moment.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{rest:02d}"


def format_schedule(timestamp: float, now: float) -> str:
    """Return a planned time like ``2026-09-28 15:30 (UTC+03:00, in 3h 12m)``.

    The zone is the one of the machine the bot runs on. It is shown so that a server in
    UTC cannot start a session planned in local time hours off without anyone noticing.
    """
    zone = format_utc_offset(timestamp)
    return f"{format_clock(timestamp)} ({zone}, in {format_duration(timestamp - now)})"


def format_duration(seconds: float) -> str:
    """Return a short duration like ``5s``, ``1m 5s``, ``1h 2m`` or ``1d 1h``.

    Negative values give ``0s``.
    """
    seconds = max(int(seconds), 0)
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
