"""Formatting and delivering Telegram alerts."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from scraper_bot.models import Address, ChangeReport, ConfirmedContract
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

    # ── turbo mode ──────────────────────────────────────────────────────────
    async def turbo_started(
        self, site_url: str, pages: int, interval: float, minutes: float, stop_after: int
    ) -> None:
        ending = (
            f"after {stop_after} confirmed contract(s) or in {minutes:.0f} min"
            if stop_after
            else f"in {minutes:.0f} min"
        )
        await self._send(
            f"🚀 Turbo mode on {site_url}\n"
            f"Rendering {pages} page(s) every {interval:.0f}s, stops {ending}."
        )

    async def unverified_address(
        self, address: Address, page_url: str, recheck_interval: float
    ) -> None:
        await self._send(
            f"⚠️ Unverified address on {page_url}\n"
            f"Chain: {address.chain.value}\n"
            f"{address.value}\n"
            f"Not a contract on-chain yet; re-checking every {recheck_interval:.0f}s."
        )

    async def address_confirmed(self, contract: ConfirmedContract, page_url: str) -> None:
        await self._send(
            f"✅ Confirmed: {contract.address.value} is a contract on {contract.network}\n"
            f"Page: {page_url}"
        )

    async def turbo_finished(
        self,
        site_url: str,
        reason: str,
        contracts: Sequence[ConfirmedContract],
        seconds: float,
        renders: int,
    ) -> None:
        found = "\n".join(f"• {c.address.value} ({c.network})" for c in contracts)
        await self._send(
            f"🎯 Turbo finished on {site_url}: {reason}\n"
            f"Duration {seconds:.0f}s, {renders} renders, {len(contracts)} confirmed contract(s)."
            + (f"\n{found}" if found else "")
        )

    # ── blocking ────────────────────────────────────────────────────────────
    async def site_blocked(
        self, site_url: str, page_url: str, reason: str, next_delay: float
    ) -> None:
        await self._send(
            f"🚫 Possible IP block on {site_url}\n"
            f"Page: {page_url}\n"
            f"Reason: {reason}\n"
            f"Slowing down: next check in {next_delay:.0f}s, the interval keeps doubling "
            f"while this lasts and turbo mode (if any) was stopped. "
            f"If it persists, the site may have banned this IP address."
        )

    async def site_recovered(self, site_url: str) -> None:
        await self._send(f"✅ {site_url} answers normally again; normal interval restored.")

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
