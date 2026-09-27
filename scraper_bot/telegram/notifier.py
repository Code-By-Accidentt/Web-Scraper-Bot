"""Formats and delivers the alerts and notices the bot sends on its own (Telegram HTML)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from scraper_bot.chains.enrichment import TokenEnricher
from scraper_bot.chains.rpc.health import FailingEndpoint
from scraper_bot.models.addresses import Address, ConfirmedContract
from scraper_bot.models.pages import BlockSignal, ChangeReport
from scraper_bot.models.sightings import Sighting, Timeline
from scraper_bot.pages.urls import url_to_filename
from scraper_bot.settings.defaults import ChangedDefault
from scraper_bot.telegram.client import SentMessage, TelegramClient
from scraper_bot.telegram.markup import code, escape, pre
from scraper_bot.telegram.timefmt import format_duration
from scraper_bot.telegram.views.token import (
    NO_PAIR_YET,
    describe_token,
    found_line,
    link_lines,
    timeline_line,
)

log = logging.getLogger(__name__)


class Notifier:
    """Sends everything the bot says on its own initiative.

    Token alerts go out at once and get their details edited in later. Notices that follow
    alerts (a turbo summary, an auto-pause) wait until those alerts have left, so a summary
    never arrives before the alerts it follows.
    """

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
        """Send to ``chat_ids``; the optional ``enricher`` adds token details to alerts.

        ``max_examples`` (examples per change report) is a number or a function that returns
        the live value. A problem is reported once per ``problem_cooldown`` seconds per key.
        """
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

    async def started(self, version: str, total_sites: int, running: int) -> None:
        """Announce that the bot started and how many sites are running."""
        await self._send(
            f"🤖 Bot started (v{escape(version)}): {running}/{total_sites} site(s) running. "
            f"Send {code('/status')} for details."
        )

    async def outdated_defaults(self, changes: Sequence[ChangedDefault]) -> bool:
        """List the .env lines that pin an old default; return True when a chat received it."""
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
        """Report a failure; return True when a chat received it.

        The same ``key`` is reported at most once per cooldown.
        """
        if not self._problem_due(key):
            return False
        return await self._send(_problem_text(title, detail, hint))

    def report_later(self, key: str, title: str, detail: str, hint: str | None = None) -> None:
        """Like :meth:`problem`, for synchronous code such as a task done-callback.

        The message is sent in the background.
        """
        if self._problem_due(key):
            self._spawn(self._send(_problem_text(title, detail, hint)))

    def rpc_endpoints_stuck(self, endpoints: Sequence[FailingEndpoint], retry_delay: float) -> None:
        """Report RPC endpoints that have not answered for minutes, once per outage.

        The validators call it when an endpoint starts failing for minutes, not again while
        the outage lasts, so a long outage is one message. Since it is said only once, it
        goes out patiently in the background.
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
        self._spawn(self._send(text, patient=True))

    def _problem_due(self, key: str) -> bool:
        """Return True and restart the cooldown, unless ``key`` was reported within it."""
        now = self._clock()
        last = self._problems.get(key)
        if last is not None and now - last < self._problem_cooldown:
            return False
        self._problems[key] = now
        return True

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait up to ``timeout`` seconds for alerts still being delivered, e.g. at shutdown."""
        pending = set(self._background)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def page_discovered(self, page_url: str) -> None:
        """Announce a new sub-page that is monitored from now on."""
        await self._send(f"🔍 Sub-page found: {escape(page_url)}\nMonitoring started.")

    async def page_changed(self, report: ChangeReport) -> None:
        """Send the change report of a page."""
        await self._send(self.format_report(report, self._max_examples()))

    async def page_timing_out(
        self, page_url: str, streak: int, reason: str, timeout: float
    ) -> None:
        """Warn that a page keeps failing to render and now has a longer load timeout."""
        await self._send(
            f"🐢 {escape(page_url)} has not rendered {streak} times in a row ({escape(reason)}).\n"
            f"Load timeout for this page raised to {timeout:.0f}s; "
            f"you will hear when it renders again."
        )

    async def page_renders_again(self, page_url: str, timeout: float) -> None:
        """Say that a page renders again and its load timeout is back to normal."""
        await self._send(
            f"✅ {escape(page_url)} renders again; load timeout back to {timeout:.0f}s."
        )

    async def captcha_shown(self, page_url: str, widget: str) -> None:
        """Warn that a page shows a captcha widget, which the bot does not solve."""
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

        Delivery runs in a task the notifier owns, so a cancelled round cannot lose an alert
        whose token is already in the known cache, and a rate limit does not hold up the
        page check.
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

        Like a token alert, it is delivered from a task the notifier owns: the tokens are
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
        """Say that a site was paused because a page showed ``count`` tokens at once.

        ``first_check`` means it was the page's first round since its monitor (re)started.
        The message goes out from its own task after the document with the tokens, so a
        flood limit does not hold up the caller (the site's monitor, already paused).
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
        self._spawn(self._after_alerts(text, patient=True))

    async def unverified_address(
        self,
        address: Address,
        page_url: str,
        recheck_seconds: float,
        watch_seconds: float | None = None,
    ) -> None:
        """Announce an address that is not a confirmed token yet and is re-checked on-chain.

        ``watch_seconds``, when given, is how long it stays watched.
        """
        window = f" for {format_duration(watch_seconds)}" if watch_seconds else ""
        await self._send(
            f"⚠️ Unverified address on {escape(page_url)}\n"
            f"Chain: {escape(address.chain.value)}\n"
            f"{code(address.value)}\n"
            f"Not a confirmed token yet; re-checking every "
            f"{format_duration(recheck_seconds)}{window}."
        )

    async def address_confirmed(self, contract: ConfirmedContract, page_url: str) -> None:
        """Announce that an unverified address became a token; details follow as an edit."""
        head = [
            f"✅ Confirmed: {code(contract.address.value)} is a token on "
            f"{escape(contract.network)}",
            f"Page: {escape(page_url)}",
        ]
        self._spawn_alert(contract, head, None, None)

    async def unverified_digest(self, site_url: str, count: int, watch_seconds: float) -> None:
        """Say how many more unverified addresses appeared after the hourly alert cap."""
        await self._send(
            f"📨 {count} more unverified address(es) appeared on {escape(site_url)} in the last "
            f"hour (alert cap reached). They are watched for {format_duration(watch_seconds)}; "
            f"any that becomes a contract will be announced."
        )

    async def turbo_started(
        self, site_url: str, pages: int, interval: float, minutes: float, stop_after: int
    ) -> None:
        """Announce a turbo session: how many pages, how often, and when it stops."""
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
        """Send the turbo summary once the token alerts it lists have gone out."""
        found = "\n".join(f"• {code(c.address.value)} ({escape(c.network)})" for c in contracts)
        text = (
            f"🎯 Turbo finished on {escape(site_url)}: {escape(reason)}\n"
            f"Duration {seconds:.0f}s, {renders} renders, {len(contracts)} confirmed contract(s)."
            + (f"\n{found}" if found else "")
        )
        self._spawn(self._after_alerts(text))

    async def site_blocked(
        self, site_url: str, page_url: str, signal: BlockSignal, next_delay: float
    ) -> None:
        """Warn that a site seems to block the bot and that its checks slow down."""
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
        """Say that a site answers normally again and how long the block lasted, if known."""
        after = f" after {format_duration(blocked_for)}" if blocked_for is not None else ""
        await self._send(
            f"✅ {escape(site_url)} answers normally again{after}; normal interval restored."
        )

    @staticmethod
    def format_report(report: ChangeReport, max_examples: int) -> str:
        """Return a change report as text, with at most ``max_examples`` lines per section."""
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

    async def _deliver_token(
        self,
        contract: ConfirmedContract,
        head: list[str],
        sighting: Sighting | None,
        timeline: Timeline | None,
        out: asyncio.Future[None],
    ) -> None:
        """Send the token alert first, then edit the token's details into it.

        ``out`` is resolved once the send is over, which lets waiting notices go. Details
        never cost the alert: if they fail or add nothing, the first text stays.
        """
        middle: list[str] = []
        found = found_line(contract, sighting)
        if found:
            middle.append(found)
        if timeline is not None:
            middle.append(timeline_line(timeline, self._clock()))
        try:
            sent = await self._send_tracked("\n".join([*head, *middle, *link_lines(contract)]))
        finally:
            if not out.done():
                out.set_result(None)
        if not sent or self._enricher is None:
            return
        try:
            info = await self._enricher.enrich(contract)
            details = describe_token(info, self._clock())
        except Exception:
            log.warning("token details failed for %s", contract.address.value, exc_info=True)
            return
        if not [line for line in details if line != NO_PAIR_YET] and not info.coin_type:
            return
        text = "\n".join([*head, *details, *middle, *link_lines(contract, info.coin_type)])
        await asyncio.gather(*(self._telegram.edit_message_text(m, text) for m in sent))

    async def _send_tracked(self, text: str) -> list[SentMessage]:
        """Send ``text`` patiently to every alert chat; return the messages that went out."""
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
        """Start delivering a token alert in the background.

        Until its first send is over it counts as unsent, so later notices wait for it.
        """
        out: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._unsent.add(out)
        out.add_done_callback(self._unsent.discard)
        self._spawn(self._deliver_token(contract, head, sighting, timeline, out))

    async def _after_alerts(self, text: str, *, patient: bool = False) -> None:
        """Send ``text`` once the token alerts and documents under way have left.

        The alerts wait out flood limits themselves (a few minutes at most); the time limit
        here only keeps a stuck delivery from holding this message forever.
        """
        if self._unsent:
            await asyncio.wait(set(self._unsent), timeout=_ALERTS_OUT_WAIT)
        await self._send(text, patient=patient)

    def _spawn(self, work: Coroutine[Any, Any, Any]) -> asyncio.Future[Any]:
        """Run ``work`` as a background task the notifier keeps track of; return the task."""
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Future[Any]) -> None:
        """Forget a finished background task and log its error, if it failed."""
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("delivering an alert failed", exc_info=task.exception())

    async def _send(self, text: str, *, patient: bool = False) -> bool:
        """Send ``text`` to every alert chat; return True when at least one received it."""
        if not self._chat_ids:
            log.warning("no TELEGRAM_CHAT_IDS configured; dropping alert: %s", text.splitlines()[0])
            return False
        return await self._telegram.broadcast(self._chat_ids, text, patient=patient)


_ALERTS_OUT_WAIT = 300.0  # seconds a follow-up notice waits at most for the alerts before it


def _problem_text(title: str, detail: str, hint: str | None) -> str:
    """Return the text of a problem message; the detail is cut to 1500 characters."""
    text = f"🛑 {escape(title)}\n{pre(str(detail)[:1500])}"
    return f"{text}\n{escape(hint)}" if hint else text
