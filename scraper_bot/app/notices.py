"""One-time Telegram notices about the environment: the browser sandbox and outdated .env values."""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from scraper_bot.settings.defaults import ChangedDefault
from scraper_bot.storage.files import atomic_write_text
from scraper_bot.telegram.notifier import Notifier

OUTDATED_NOTICE_FILE = "outdated_defaults.txt"  # the old .env values last reported
SANDBOX_NOTICE_FILE = "no_sandbox_notice.txt"  # the chat heard that Chromium runs unsandboxed
SANDBOX_HINT = (
    "Pages are then isolated only by the bot's own user account. The sandbox does not start "
    "as root or where user namespaces are off (containers, AppArmor on Ubuntu 23.10+): run "
    "the bot as its own non-root user; Google Chrome (BROWSER_CHANNEL=chrome) brings a "
    "setuid sandbox helper that may work there."
)


class SandboxState(Protocol):
    @property
    def sandboxed(self) -> bool: ...

    @property
    def sandbox_error(self) -> str | None: ...


async def tell_about_sandbox(notifier: Notifier, browser: SandboxState, marker: Path) -> None:
    """One 🛑 when Chromium had to start without its sandbox, again only after it worked.

    ``marker`` notes that the chat heard it; a start with the sandbox removes it.
    """
    if browser.sandboxed:
        with contextlib.suppress(OSError):
            marker.unlink(missing_ok=True)
        return
    if browser.sandbox_error is None or marker.is_file():
        return  # switched off in BROWSER_ARGS on purpose, or already reported
    if await notifier.problem(
        "sandbox", "Chromium runs without its sandbox", browser.sandbox_error, hint=SANDBOX_HINT
    ):
        with contextlib.suppress(OSError):
            atomic_write_text(marker, browser.sandbox_error + "\n")


def outdated_notice_due(path: Path, changes: Sequence[ChangedDefault]) -> bool:
    """True when ``changes`` differ from the set last reported in Telegram.

    The log warns on every start; the chat hears about one set of old values once, not on
    every restart the supervisor makes (see :func:`remember_outdated_notice`).
    """
    if not changes:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return False
    try:
        return path.read_text(encoding="utf-8") != _listed(changes)
    except (OSError, ValueError):  # missing, or re-saved in another encoding
        return True


def remember_outdated_notice(path: Path, changes: Sequence[ChangedDefault]) -> None:
    """Note ``changes`` as reported, once the message went out."""
    with contextlib.suppress(OSError):
        atomic_write_text(path, _listed(changes))


def _listed(changes: Sequence[ChangedDefault]) -> str:
    return "\n".join(sorted(f"{c.key}={c.previous}" for c in changes)) + "\n"
