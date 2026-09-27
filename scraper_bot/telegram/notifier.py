"""Formatting and delivering Telegram alerts (HTML markup, see :mod:`markup`)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from scraper_bot.chains.contract_validators import FailingEndpoint
from scraper_bot.chains.enrichment import TokenEnricher, TokenInfo
from scraper_bot.chains.networks import token_links
from scraper_bot.config import ChangedDefault
from scraper_bot.models import (
    Address,
    BlockSignal,
    ChangeReport,
    ConfirmedContract,
    Sighting,
    Timeline,
)
from scraper_bot.pages.urls import url_to_filename
from scraper_bot.telegram.client import SentMessage, TelegramClient
from scraper_bot.telegram.markup import code, escape, link, pre
from scraper_bot.telegram.timefmt import format_duration, format_precise, format_utc_offset

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
        self._background: set[asyncio.Future[Any]] = set()
        # Token alerts and documents that have not left yet: a summary must not overtake them.
        self._unsent: set[asyncio.Future[Any]] = set()

    # ── lifecycle and failures ──────────────────────────────────────────────
    async def started(self, version: str, total_sites: int, running: int) -> None:
        await self._send(
            f"🤖 Bot started (v{escape(version)}): {running}/{total_sites} site(s) running. "
            f"Send {code('/status')} for details."
        )

    async def outdated_defaults(self, changes: Sequence[ChangedDefault]) -> bool:
        """Name the .env lines that pin an old default; True when some chat received it."""
        listed = "\n".join(
            f"• {code(f'{c.key}={c.previous}')} → the default is now {code(c.current)}"
            for c in changes
        )
        text = (
            f"⚙️ .env still sets defaults of an earlier release:\n{listed}\n"
            "Delete these lines to use the new defaults (or keep them on purpose), "
            "then restart the bot."
        )
        if not self._chat_ids:
            return False
        sent = await asyncio.gather(*(self._telegram.send_message(c, text) for c in self._chat_ids))
        return any(sent)

    async def problem(self, key: str, title: str, detail: str, hint: str | None = None) -> bool:
        """Report a failure at most once per cooldown per ``key``; True when a chat got it."""
        if not self._problem_due(key):
            return False
        return await self._send(_problem_text(title, detail, hint))

    def report_later(self, key: str, title: str, detail: str, hint: str | None = None) -> None:
        """:meth:`problem` for synchronous code (a task done-callback); sent in the background."""
        if self._problem_due(key):
            self._spawn(self._send(_problem_text(title, detail, hint)))

    def rpc_endpoints_stuck(self, endpoints: Sequence[FailingEndpoint], retry_delay: float) -> None:
        """RPC endpoints that have not answered for minutes: one 🛑 per outage.

        The validators call it when an endpoint joins the ones failing for minutes, not
        while an outage lasts, so a long Arc outage is one message, not one per cooldown.
        """
        required = any(not endpoint.optional for endpoint in endpoints)
        detail = "\n".join(
            f"{endpoint.name} for {format_duration(endpoint.seconds)}: {endpoint.error}"
            for endpoint in endpoints
        )
        hints = []
        if required:
            hints.append(f"Addresses are re-checked every {retry_delay:.0f} s meanwhile.")
        optional = [endpoint.name for endpoint in endpoints if endpoint.optional]
        if optional:
            what = "is an optional network" if len(optional) == 1 else "are optional networks"
            hints.append(
                f"{' and '.join(optional)} {what}: the other networks decide meanwhile, and "
                "addresses are checked there again once it answers."
            )
        hints.append(
            "Replace the endpoint in .env (an empty EVM URL skips that network) and restart."
        )
        text = _problem_text("On-chain checks keep failing", detail, " ".join(hints))
        self._spawn(self._send(text, patient=True))  # said once per outage: it must arrive

    def _problem_due(self, key: str) -> bool:
        """True (and the cooldown starts) unless ``key`` was reported within the cooldown."""
        now = self._clock()
        last = self._problems.get(key)
        if last is not None and now - last < self._problem_cooldown:
            return False
        self._problems[key] = now
        return True

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait (a bounded time) for alerts still being delivered, e.g. before shutting down."""
        pending = set(self._background)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

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

    async def captcha_shown(self, page_url: str, widget: str) -> None:
        await self._send(
            f"🧩 Captcha on {escape(page_url)}: the page shows a {escape(widget)} widget.\n"
            "The bot keeps watching the page, but it does not solve captchas: whatever "
            "appears only after solving one stays out of its sight. You hear about each "
            "widget once per page."
        )

    async def contract_found(
        self,
        contract: ConfirmedContract,
        page_url: str,
        *,
        sighting: Sighting | None = None,
        timeline: Timeline | None = None,
    ) -> None:
        """Announce a token right away; its details follow as an edit of the same message.

        Delivery runs in a task the notifier owns: a cancelled round must not lose an alert
        whose token is already in the known cache, and a Telegram rate limit must not hold
        up the page check.
        """
        head = [
            f"🔔 Contract found on {escape(page_url)}",
            f"Network: {escape(contract.network)}",
            code(contract.address.value),
        ]
        self._spawn_alert(contract, head, sighting, timeline)

    async def contracts_batch(
        self, site_url: str, page_url: str, contracts: Sequence[ConfirmedContract]
    ) -> None:
        """Send many addresses as one text document instead of a burst of messages.

        Like a token alert it is delivered from a task the notifier owns: the tokens are
        already marked as announced, so a cancelled round must not lose the document.
        """
        lines = sorted(f"{c.address.value}\t{c.network}" for c in contracts)
        filename = f"contracts_{url_to_filename(site_url)}_{int(time.time())}.txt"
        caption = f"⚠️ {len(contracts)} confirmed contracts found on {page_url}"
        if not self._chat_ids:
            log.warning("no TELEGRAM_CHAT_IDS configured; dropping document %s", filename)
            return
        content = ("\n".join(lines) + "\n").encode("utf-8")
        task = self._spawn(
            self._telegram.broadcast_document(
                self._chat_ids, filename, content, caption, patient=True
            )
        )
        self._unsent.add(task)  # the auto-pause message waits for it
        task.add_done_callback(self._unsent.discard)

    async def site_auto_paused(
        self,
        site_url: str,
        page_url: str,
        count: int,
        pages: Sequence[str],
        first_check: bool = False,
    ) -> None:
        """``first_check``: the page's first round since its monitor (re)started.

        Sent from a task of its own after the document with the tokens: the caller (the
        site's monitor, already paused) is not held up by a Telegram flood limit.
        """
        listed = "\n".join(f"• {escape(page)}" for page in pages)
        why = (
            "This was the first check of the page since monitoring started or resumed, so "
            f"these are most likely tokens the page already listed (for example on a newly "
            f"supported network), not {count} launches.\n"
            if first_check
            else ""
        )
        text = (
            f"⏸️ Monitoring paused: {count} confirmed contracts found on {escape(page_url)}\n"
            f"{why}"
            f"Website: {escape(site_url)}\n"
            f"Pages:\n{listed}\n"
            f"Send {code(f'/resume {site_url}')} to continue; these tokens will not be "
            "announced again."
        )
        self._spawn(self._after_alerts(text, patient=True))  # the document goes first

    # ── unverified addresses (/unverified mode) ─────────────────────────────
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
            f"Not a confirmed token yet; re-checking every "
            f"{format_duration(recheck_seconds)}{window}."
        )

    async def address_confirmed(self, contract: ConfirmedContract, page_url: str) -> None:
        head = [
            f"✅ Confirmed: {code(contract.address.value)} is a token on "
            f"{escape(contract.network)}",
            f"Page: {escape(page_url)}",
        ]
        self._spawn_alert(contract, head, None, None)

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
        text = (
            f"🎯 Turbo finished on {escape(site_url)}: {escape(reason)}\n"
            f"Duration {seconds:.0f}s, {renders} renders, {len(contracts)} confirmed contract(s)."
            + (f"\n{found}" if found else "")
        )
        self._spawn(self._after_alerts(text))  # the tokens listed here are announced first

    # ── blocking ────────────────────────────────────────────────────────────
    async def site_blocked(
        self, site_url: str, page_url: str, signal: BlockSignal, next_delay: float
    ) -> None:
        lines = [
            f"🚫 Possible IP block on {escape(site_url)}",
            f"Page: {escape(page_url)}",
            f"Reason: {code(signal.reason)}",
        ]
        if signal.protection is not None:
            lines.append(f"Protection: {escape(signal.protection)}")
        lines.append(
            f"Slowing down: next check in {next_delay:.0f}s, the interval keeps doubling "
            "while this lasts; a turbo session on this site ends when its own render sees "
            "the block. The bot does not solve captchas or browser checks, it waits until "
            "the site lets it through; you get a message when it answers normally again. "
            "If it persists, the site may have banned this IP address."
        )
        await self._send("\n".join(lines))

    async def site_recovered(self, site_url: str, blocked_for: float | None = None) -> None:
        after = f" after {format_duration(blocked_for)}" if blocked_for is not None else ""
        await self._send(
            f"✅ {escape(site_url)} answers normally again{after}; normal interval restored."
        )

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

    # ── token alerts ────────────────────────────────────────────────────────
    async def _deliver_token(
        self,
        contract: ConfirmedContract,
        head: list[str],
        sighting: Sighting | None,
        timeline: Timeline | None,
        out: asyncio.Future[None],
    ) -> None:
        """Send the alert, then edit the token's details into it (never before sending)."""
        middle: list[str] = []
        found = _found_line(contract, sighting)
        if found:
            middle.append(found)
        if timeline is not None:
            middle.append(_timeline_line(timeline, self._clock()))
        try:
            sent = await self._send_tracked("\n".join([*head, *middle, *_links(contract)]))
        finally:
            if not out.done():
                out.set_result(None)
        if not sent or self._enricher is None:
            return
        try:
            info = await self._enricher.enrich(contract)
            details = describe_token(info, self._clock())
        except Exception:  # details must never cost the alert
            log.warning("token details failed for %s", contract.address.value, exc_info=True)
            return
        if not [line for line in details if line != NO_PAIR_YET] and not info.coin_type:
            return  # nothing worth an edit
        text = "\n".join([*head, *details, *middle, *_links(contract, info.coin_type)])
        await asyncio.gather(*(self._telegram.edit_message_text(m, text) for m in sent))

    async def _send_tracked(self, text: str) -> list[SentMessage]:
        if not self._chat_ids:
            log.warning("no TELEGRAM_CHAT_IDS configured; dropping alert: %s", text.splitlines()[0])
            return []
        sent = await asyncio.gather(
            *(self._telegram.send_tracked(c, text, patient=True) for c in self._chat_ids)
        )
        return [message for message in sent if message is not None]

    def _spawn_alert(
        self,
        contract: ConfirmedContract,
        head: list[str],
        sighting: Sighting | None,
        timeline: Timeline | None,
    ) -> None:
        out: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._unsent.add(out)
        out.add_done_callback(self._unsent.discard)
        self._spawn(self._deliver_token(contract, head, sighting, timeline, out))

    async def _after_alerts(self, text: str, *, patient: bool = False) -> None:
        """Send ``text`` once the token alerts and documents under way have left.

        They wait out flood limits themselves (a few minutes at most); the bound only keeps
        a stuck delivery from holding this message forever.
        """
        if self._unsent:
            await asyncio.wait(set(self._unsent), timeout=_ALERTS_OUT_WAIT)
        await self._send(text, patient=patient)

    def _spawn(self, work: Coroutine[Any, Any, Any]) -> asyncio.Future[Any]:
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Future[Any]) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("delivering an alert failed", exc_info=task.exception())

    async def _send(self, text: str, *, patient: bool = False) -> bool:
        """Send to every alert chat; True when at least one of them received it."""
        if not self._chat_ids:
            log.warning("no TELEGRAM_CHAT_IDS configured; dropping alert: %s", text.splitlines()[0])
            return False
        return await self._telegram.broadcast(self._chat_ids, text, patient=patient)


NO_PAIR_YET = "No DEX pair listed yet."
_ALERTS_OUT_WAIT = 300.0  # longest wait of a follow-up notice for the alerts it follows
# Every RPC node keeps at least this much transaction history (seconds). The oldest
# transaction a public node still has may be days younger than the token itself.
_HISTORY_SURELY_KEPT = 3600.0


def _problem_text(title: str, detail: str, hint: str | None) -> str:
    text = f"🛑 {escape(title)}\n{pre(str(detail)[:1500])}"
    return f"{text}\n{escape(hint)}" if hint else text


def _short(value: float) -> str:
    if abs(value) >= 100:
        text = f"{value:.0f}"
    elif abs(value) >= 10:
        text = f"{value:.1f}"
    else:
        text = f"{value:.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_amount(value: float) -> str:
    """``1.23K``, ``12.4M``, ``5B``; plain numbers below a thousand."""
    for unit, threshold in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= threshold:
            return f"{_short(value / threshold)}{unit}"
    return _short(value)


def format_usd(value: float) -> str:
    if value >= 1000:
        return f"${format_amount(value)}"
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.0001:
        return f"${value:.4g}"
    return f"${value:.2g}"


def _found_line(contract: ConfirmedContract, sighting: Sighting | None) -> str | None:
    """Where on the page the address stood and what stood in front of it."""
    if sighting is None:
        return None
    via = sighting.address != contract.address  # the page showed the token's LP pair
    where = f"{'Found via an LP pair in' if via else 'Found in'} {sighting.source.value}"
    return f"{where}: «{escape(sighting.context)}»" if sighting.context else where


def _timeline_line(timeline: Timeline, sent_at: float) -> str:
    when = format_precise
    if timeline.deployed and timeline.on_page_since is not None and timeline.missing_at is not None:
        text = (
            f"on page since {when(timeline.on_page_since)} · deployed between "
            f"{when(timeline.missing_at)} and {when(timeline.found_at)}"
        )
    elif timeline.missing_at is not None:
        text = f"appeared between {when(timeline.missing_at)} and {when(timeline.found_at)}"
    elif timeline.on_page_since is not None:
        text = f"on page since {when(timeline.on_page_since)} · confirmed {when(timeline.found_at)}"
    else:
        text = f"found {when(timeline.found_at)} on the first check of the page"
    return f"⏱ {text} · sent {when(sent_at)} ({format_utc_offset(sent_at)})"


def _links(contract: ConfirmedContract, identifier: str | None = None) -> list[str]:
    links = token_links(contract, identifier)
    return ["Links: " + " · ".join(link(label, url) for label, url in links)] if links else []


def _risk_lines(info: TokenInfo) -> list[str]:
    """Solana: what the issuer can still do to holders (a rug needs one of these)."""
    lines: list[str] = []
    if info.mint_authority is not None and info.freeze_authority is not None:
        kept = [
            name
            for name, flag in (("mint", info.mint_authority), ("freeze", info.freeze_authority))
            if flag
        ]
        lines.append(
            f"⚠️ Authority kept: {', '.join(kept)}"
            if kept
            else "Authorities: mint and freeze revoked ✅"
        )
    if info.risky_extensions:
        lines.append("⚠️ Token-2022: " + ", ".join(escape(e) for e in info.risky_extensions))
    return lines


def _age_lines(info: TokenInfo, now: float) -> list[str]:
    if info.created_at is not None:
        age = max(now - info.created_at, 0.0)
        if age < _HISTORY_SURELY_KEPT:
            return [f"🆕 Created {format_duration(age)} ago"]
        return [f"Created more than {format_duration(age)} ago"]  # the node may forget
    if info.busy:
        return ["Active token: 1000+ transactions"]
    if info.deployed_within is not None:
        return [f"🆕 Deployed within the last {format_duration(info.deployed_within)}"]
    if info.deployed_before is not None:
        return [f"Deployed more than {format_duration(info.deployed_before)} ago"]
    return []


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
    lines.extend(_risk_lines(info))
    lines.extend(_age_lines(info, now))
    if info.also_on:
        lines.append("Also on: " + ", ".join(escape(name) for name in info.also_on))
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
        lines.append(NO_PAIR_YET)
    return lines
