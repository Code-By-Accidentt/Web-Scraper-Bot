"""Notices the chat gets once: Chromium without its sandbox, and old defaults left in .env."""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from scraper_bot.settings.defaults import ChangedDefault
from scraper_bot.storage.files import atomic_write_text
from scraper_bot.telegram.notifier import Notifier

OUTDATED_NOTICE_FILE = "outdated_defaults.txt"  # the old .env values we last told the chat about
SANDBOX_NOTICE_FILE = "no_sandbox_notice.txt"  # kept while the chat knows the sandbox is off
SANDBOX_HINT = (
    "Pages are then isolated only by the bot's own user account. The sandbox does not start "
    "as root or where user namespaces are off (containers, AppArmor on Ubuntu 23.10+): run "
    "the bot as its own non-root user; Google Chrome (BROWSER_CHANNEL=chrome) brings a "
    "setuid sandbox helper that may work there."
)


class SandboxState(Protocol):
    """What the sandbox notice needs to know about the browser."""

    @property
    def sandboxed(self) -> bool:
        """True when Chromium runs with its sandbox."""
        ...

    @property
    def sandbox_error(self) -> str | None:
        """Why the sandbox had to be given up, or None if it was not."""
        ...


async def tell_about_sandbox(notifier: Notifier, browser: SandboxState, marker: Path) -> None:
    """Tell the chat once that Chromium had to start without its sandbox.

    The ``marker`` file records that the chat was told, so restarts stay quiet. A start
    with the sandbox deletes it, so a later failure is reported again.
    """
    if browser.sandboxed:
        with contextlib.suppress(OSError):
            marker.unlink(missing_ok=True)
        return
    if browser.sandbox_error is None or marker.is_file():
        return  # no sandbox_error means BROWSER_ARGS turned it off on purpose
    if await notifier.problem(
        "sandbox", "Chromium runs without its sandbox", browser.sandbox_error, hint=SANDBOX_HINT
    ):
        with contextlib.suppress(OSError):
            atomic_write_text(marker, browser.sandbox_error + "\n")


def outdated_notice_due(path: Path, changes: Sequence[ChangedDefault]) -> bool:
    """Return True when ``changes`` differ from the old values last reported in the chat.

    The log warns on every start, but the chat hears about one set of old values only
    once, not after every restart. With no changes the file is deleted and this is False.
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
    """Record ``changes`` as reported; call it once the message went out."""
    with contextlib.suppress(OSError):
        atomic_write_text(path, _listed(changes))


def _listed(changes: Sequence[ChangedDefault]) -> str:
    """Return the changes as sorted ``KEY=old value`` lines, as the notice file keeps them."""
    return "\n".join(sorted(f"{c.key}={c.previous}" for c in changes)) + "\n"
