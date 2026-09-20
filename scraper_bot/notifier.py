"""Formatting and delivering Telegram alerts."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from scraper_bot.models import ChangeReport, ConfirmedContract
from scraper_bot.telegram_client import TelegramClient
from scraper_bot.urls import url_to_filename

log = logging.getLogger(__name__)


class Notifier:
    """Everything the bot says on its own initiative goes through here."""

    def __init__(
        self, telegram: TelegramClient, chat_ids: Sequence[int], *, max_examples: int = 5
    ) -> None:
        self._telegram = telegram
        self._chat_ids = tuple(chat_ids)
        self._max_examples = max_examples

    async def page_discovered(self, page_url: str) -> None:
        await self._send(f"🔍 Sub-page found: {page_url}\nMonitoring started.")

    async def page_changed(self, report: ChangeReport) -> None:
        await self._send(self.format_report(report, self._max_examples))

    async def contract_found(self, contract: ConfirmedContract, page_url: str) -> None:
        await self._send(
            f"🔔 Contract found on {page_url}\n"
            f"Network: {contract.network}\n"
            f"{contract.address.value}"
        )

    async def contracts_batch(
        self, site_url: str, page_url: str, contracts: Sequence[ConfirmedContract]
    ) -> None:
        """Send many addresses as one text document instead of a burst of messages."""
        lines = sorted(f"{c.address.value}\t{c.network}" for c in contracts)
        filename = f"contracts_{url_to_filename(site_url)}_{int(time.time())}.txt"
        caption = f"⚠️ {len(contracts)} confirmed contracts found on {page_url}"
        if not self._chat_ids:
            log.warning("no TELEGRAM_CHAT_IDS configured; dropping document %s", filename)
            return
        content = ("\n".join(lines) + "\n").encode("utf-8")
        await self._telegram.broadcast_document(self._chat_ids, filename, content, caption)

    async def site_auto_paused(
        self, site_url: str, page_url: str, count: int, pages: Sequence[str]
    ) -> None:
        listed = "\n".join(f"• {page}" for page in pages)
        await self._send(
            f"⏸️ Monitoring paused: {count} confirmed contracts found on {page_url}\n"
            f"Website: {site_url}\n"
            f"Pages:\n{listed}\n"
            f"Send /resume {site_url} to continue."
        )

    @staticmethod
    def format_report(report: ChangeReport, max_examples: int) -> str:
        counts = report.counts
        lines = [
            f"‼️ Changes on {report.url}",
            f"• Removed: {counts.removed}",
            f"• Added: {counts.added}",
            f"• Changed attributes: {counts.modified}",
        ]
        if max_examples > 0:
            sections = (
                ("Removed", "-", [node.describe() for node in report.removed]),
                ("Added", "+", [node.describe() for node in report.added]),
                ("Changed", "~", [old.describe_attr_change(new) for old, new in report.modified]),
            )
            for title, marker, items in sections:
                if not items:
                    continue
                lines.append(f"\n{title}:")
                lines.extend(f"{marker} {item}" for item in items[:max_examples])
                if len(items) > max_examples:
                    lines.append(f"... and {len(items) - max_examples} more")
        lines.append(f"\n➡️ {report.url}")
        return "\n".join(lines)

    async def _send(self, text: str) -> None:
        if not self._chat_ids:
            log.warning("no TELEGRAM_CHAT_IDS configured; dropping alert: %s", text.splitlines()[0])
            return
        await self._telegram.broadcast(self._chat_ids, text)
