"""Receiving commands from Telegram."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from scraper_bot.telegram.client import TelegramClient
from scraper_bot.telegram.commands.handlers import BotCommands

log = logging.getLogger(__name__)


_PROFILE_RETRY = 60.0  # seconds between getMe calls while the bot's own name is unknown


class TelegramListener:
    """Long-polls ``getUpdates`` forever and feeds each update to :class:`BotCommands`."""

    def __init__(
        self,
        telegram: TelegramClient,
        commands: BotCommands,
        *,
        poll_timeout: int = 30,
        error_backoff: float = 5.0,
    ) -> None:
        self._telegram = telegram
        self._commands = commands
        self._poll_timeout = poll_timeout
        self._error_backoff = error_backoff

    async def run(self) -> None:
        log.info("Telegram listener started")
        offset: int | None = None
        profile_due = 0.0
        while True:
            if self._telegram.username is None and time.monotonic() >= profile_due:
                # Commands addressed to another bot are told apart by our own name; the
                # getMe at the start may have failed.
                profile_due = time.monotonic() + _PROFILE_RETRY
                with contextlib.suppress(Exception):
                    await self._telegram.get_me()
            updates = await self._telegram.get_updates(offset, self._poll_timeout)
            if updates is None:
                await asyncio.sleep(self._error_backoff)
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                try:
                    await self._commands.handle_update(update)
                except Exception:
                    log.exception("handling update %s failed", update_id)
