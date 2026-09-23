"""Formatting and delivering Telegram alerts (HTML markup, see :mod:`markup`)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from scraper_bot.enrichment import TokenEnricher, TokenInfo, format_amount, format_usd, token_links
from scraper_bot.markup import code, escape, link, pre
from scraper_bot.models import Address, ChangeReport, ConfirmedContract
from scraper_bot.telegram_client import TelegramClient
from scraper_bot.timefmt import format_duration
from scraper_bot.urls import url_to_filename

log = logging.getLogger(__name__)


class Notifier:
    """Everything the bot says on its own initiative goes through here."""

    def __init__(
        self,
        telegram: TelegramClient,
        chat_ids: Sequence[int],
        *,
        max_examples: int | Callable[[], int] = 5,
        problem_cooldown: float = 600.0,
        enricher: TokenEnricher | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._telegram = telegram
        self._chat_ids = tuple(chat_ids)
        self._max_examples = max_examples if callable(max_examples) else (lambda: max_examples)
        self._problem_cooldown = problem_cooldown
        self._enricher = enricher
        self._clock = clock
        self._problems: dict[str, float] = {}
        self._background: set[asyncio.Task[bool]] = set()

    # ── lifecycle and failures ──────────────────────────────────────────────
    async def started(self, version: str, total_sites: int, running: int) -> None:
        await self._send(
            f"🤖 Bot started (v{escape(version)}): {running}/{total_sites} site(s) running. "
            f"Send {code('/status')} for details."
        )

    async def problem(self, key: str, title: str, detail: str, hint: str | None = None) -> bool:
        """Report a failure at most once per cooldown per ``key``; False when suppressed."""
        now = self._clock()
        last = self._problems.get(key)
        if last is not None and now - last < self._problem_cooldown:
            return False
        self._problems[key] = now
        text = f"🛑 {escape(title)}\n{pre(str(detail)[:1500])}"
        if hint:
            text += f"\n{escape(hint)}"
        await self._send(text)
        return True

    def report_later(self, key: str, title: str, detail: str, hint: str | None = None) -> None:
        """Schedule :meth:`problem` from synchronous code, e.g. a task done-callback."""
        task = asyncio.ensure_future(self.problem(key, title, detail, hint))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ── monitoring ──────────────────────────────────────────────────────────
    async def page_discovered(self, page_url: str) -> None:
        await self._send(f"🔍 Sub-page found: {escape(page_url)}\nMonitoring started.")

    async def page_changed(self, report: ChangeReport) -> None:
        await self._send(self.format_report(report, self._max_examples()))

    async def page_timing_out(
        self, page_url: str, streak: int, reason: str, timeout: float
    ) -> None:
        await self._send(
            f"🐢 {escape(page_url)} has not rendered {streak} times in a row ({escape(reason)}).\n"
            f"Load timeout for this page raised to {timeout:.0f}s; "
            f"you will hear when it renders again."
        )

    async def page_renders_again(self, page_url: str, timeout: float) -> None:
        await self._send(
            f"✅ {escape(page_url)} renders again; load timeout back to {timeout:.0f}s."
        )

    async def contract_found(self, contract: ConfirmedContract, page_url: str) -> None:
        await self._send(
            f"🔔 Contract found on {escape(page_url)}\n"
            f"Network: {escape(contract.network)}\n"
            f"{code(contract.address.value)}" + await self._token_details(contract)
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
        listed = "\n".join(f"• {escape(page)}" for page in pages)
        await self._send(
            f"⏸️ Monitoring paused: {count} confirmed contracts found on {escape(page_url)}\n"
            f"Website: {escape(site_url)}\n"
            f"Pages:\n{listed}\n"
            f"Send {code(f'/resume {site_url}')} to continue."
        )

    # ── unverified addresses (turbo and /unverified mode) ───────────────────
    async def unverified_address(
        self,
        address: Address,
        page_url: str,
        recheck_seconds: float,
        watch_seconds: float | None = None,
    ) -> None:
        window = f" for {format_duration(watch_seconds)}" if watch_seconds else ""
        await self._send(
            f"⚠️ Unverified address on {escape(page_url)}\n"
            f"Chain: {escape(address.chain.value)}\n"
            f"{code(address.value)}\n"
            f"Not a contract on-chain yet; re-checking every "
            f"{format_duration(recheck_seconds)}{window}."
        )

    async def address_confirmed(self, contract: ConfirmedContract, page_url: str) -> None:
        await self._send(
            f"✅ Confirmed: {code(contract.address.value)} is a contract on "
            f"{escape(contract.network)}\nPage: {escape(page_url)}"
            + await self._token_details(contract)
        )

    async def unverified_digest(self, site_url: str, count: int, watch_seconds: float) -> None:
        await self._send(
            f"📨 {count} more unverified address(es) appeared on {escape(site_url)} in the last "
            f"hour (alert cap reached). They are watched for {format_duration(watch_seconds)}; "
            f"any that becomes a contract will be announced."
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
            f"🚀 Turbo mode on {escape(site_url)}\n"
            f"Rendering {pages} page(s) every {interval:.0f}s, stops {ending}.\n"
            f"Stop early with {code(f'/turbo stop {site_url}')}"
        )

    async def turbo_finished(
        self,
        site_url: str,
        reason: str,
        contracts: Sequence[ConfirmedContract],
        seconds: float,
        renders: int,
    ) -> None:
        found = "\n".join(f"• {code(c.address.value)} ({escape(c.network)})" for c in contracts)
        await self._send(
            f"🎯 Turbo finished on {escape(site_url)}: {escape(reason)}\n"
            f"Duration {seconds:.0f}s, {renders} renders, {len(contracts)} confirmed contract(s)."
            + (f"\n{found}" if found else "")
        )

    # ── blocking ────────────────────────────────────────────────────────────
    async def site_blocked(
        self, site_url: str, page_url: str, reason: str, next_delay: float
    ) -> None:
        await self._send(
            f"🚫 Possible IP block on {escape(site_url)}\n"
            f"Page: {escape(page_url)}\n"
            f"Reason: {code(reason)}\n"
            f"Slowing down: next check in {next_delay:.0f}s, the interval keeps doubling "
            f"while this lasts and turbo mode (if any) was stopped. "
            f"If it persists, the site may have banned this IP address."
        )

    async def site_recovered(self, site_url: str) -> None:
        await self._send(f"✅ {escape(site_url)} answers normally again; normal interval restored.")

    # ── formatting ──────────────────────────────────────────────────────────
    @staticmethod
    def format_report(report: ChangeReport, max_examples: int) -> str:
        counts = report.counts
        lines = [
            f"‼️ Changes on {escape(report.url)}",
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
                quoted = [f"{marker} {item}" for item in items[:max_examples]]
                if len(items) > max_examples:
                    quoted.append(f"... and {len(items) - max_examples} more")
                lines.append(f"\n{title}:\n{pre(chr(10).join(quoted))}")
        lines.append(f"\n➡️ {escape(report.url)}")
        return "\n".join(lines)

    async def _token_details(self, contract: ConfirmedContract) -> str:
        """Extra lines: what the token is, where it trades, where to look it up."""
        info = TokenInfo()
        lines: list[str] = []
        if self._enricher is not None:
            try:
                info = await self._enricher.enrich(contract)
                lines = describe_token(info, self._clock())
            except Exception:  # details must never hold back the alert
                log.warning("token details failed for %s", contract.address.value, exc_info=True)
        links = token_links(contract, info.coin_type)
        if links:
            lines.append("Links: " + " · ".join(link(label, url) for label, url in links))
        return "".join(f"\n{line}" for line in lines)

    async def _send(self, text: str) -> None:
        if not self._chat_ids:
            log.warning("no TELEGRAM_CHAT_IDS configured; dropping alert: %s", text.splitlines()[0])
            return
        await self._telegram.broadcast(self._chat_ids, text)


def describe_token(info: TokenInfo, now: float) -> list[str]:
    """Lines for a token alert; unknown parts are left out."""
    lines: list[str] = []
    if info.coin_type:
        lines.append(f"Coin type: {code(info.coin_type)}")
    identity: list[str] = []
    if info.name and info.symbol:
        identity.append(f"{escape(info.name)} ({escape(info.symbol)})")
    elif info.name or info.symbol:
        identity.append(escape(info.name or info.symbol))
    if info.decimals is not None:
        identity.append(f"{info.decimals} decimals")
    if info.supply is not None:
        identity.append(f"supply {format_amount(info.supply)}")
    if identity:
        lines.append("Token: " + " · ".join(identity))
    if info.pair_url:
        # Plain text on purpose: the pair page URL holds the pair's own address, and an
        # alert must name exactly one token.
        market = [escape(f"{info.dex or 'DEX'} {info.pair or ''}".strip())]
        if info.price_usd is not None:
            market.append(f"price {format_usd(info.price_usd)}")
        if info.liquidity_usd is not None:
            market.append(f"liquidity {format_usd(info.liquidity_usd)}")
        if info.fdv_usd is not None:
            market.append(f"FDV {format_usd(info.fdv_usd)}")
        if info.pair_created_at is not None:
            market.append(f"listed {format_duration(now - info.pair_created_at)} ago")
        lines.append("Trading on " + " · ".join(market))
    elif info.market_checked:
        lines.append("No DEX pair listed yet.")
    return lines
