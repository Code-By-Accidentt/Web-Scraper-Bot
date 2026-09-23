"""Small helpers for showing durations to people."""

from __future__ import annotations


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
