"""Small helpers for showing times and durations to people."""

from __future__ import annotations

from datetime import datetime


def format_clock(timestamp: float, *, date: bool = True) -> str:
    """Local time as ``2026-09-23 15:04`` or, with ``date=False``, ``15:04``."""
    pattern = "%Y-%m-%d %H:%M" if date else "%H:%M"
    return datetime.fromtimestamp(timestamp).strftime(pattern)


def format_duration(seconds: float) -> str:
    """``5s``, ``1m 5s``, ``1h 2m`` or ``1d 1h``; negative values read as ``0s``."""
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
