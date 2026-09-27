"""Parsing command text into its parts."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

_DELAY_RE = re.compile(r"^(\d+)([mh])$", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass(frozen=True, slots=True)
class TurboRequest:
    url: str
    minutes: float | None
    start_at: float | None


def parse_turbo_request(tokens: Sequence[str], now: float) -> TurboRequest:
    """``<url> [minutes] [at HH:MM | in 10m]``; raises ValueError with an explanation."""
    url, rest = tokens[0], list(tokens[1:])
    minutes: float | None = None
    start_at: float | None = None
    index = 0
    while index < len(rest):
        token = rest[index].lower()
        if token.isdigit():
            minutes = float(token)
            index += 1
        elif token == "at" and index + 1 < len(rest):
            match = _CLOCK_RE.match(rest[index + 1])
            if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
                raise ValueError("the start time must look like HH:MM (24h clock)")
            start_at = _next_occurrence(int(match.group(1)), int(match.group(2)), now)
            index += 2
        elif token == "in" and index + 1 < len(rest):
            match = _DELAY_RE.match(rest[index + 1])
            if not match:
                raise ValueError("the delay must look like 10m or 2h")
            unit = 60 if match.group(2).lower() == "m" else 3600
            start_at = now + int(match.group(1)) * unit
            index += 2
        else:
            raise ValueError(f"unexpected argument {rest[index]!r}")
    return TurboRequest(url, minutes, start_at)


def _next_occurrence(hour: int, minute: int, now: float) -> float:
    candidate = datetime.fromtimestamp(now).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    argument: str


def parse_command(text: str, bot_username: str | None = None) -> ParsedCommand | None:
    """``"/add@MyBot https://x.io"`` -> ``ParsedCommand("add", "https://x.io")``.

    A command addressed to another bot (``/pause@OtherBot`` in a shared group) is not
    ours: ``None``, once ``bot_username`` (our own) is known.
    """
    parts = text.strip().split(None, 1)
    if not parts or not parts[0].startswith("/"):
        return None
    name, _, mention = parts[0][1:].partition("@")
    if mention and bot_username and mention.lower() != bot_username.lower():
        return None
    if not name:
        return None
    return ParsedCommand(name.lower(), parts[1].strip() if len(parts) > 1 else "")
