"""Formats and delivers the alerts and notices the bot sends on its own (Telegram HTML)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Sequence
from typing import Any
from uuid import uuid4

from scraper_bot.chains.enrichment import TokenEnricher
from scraper_bot.chains.rpc.health import FailingEndpoint
from scraper_bot.models.addresses import Address, Chain, ConfirmedContract
from scraper_bot.models.pages import BlockSignal, ChangeReport
from scraper_bot.models.sightings import Sighting, Timeline
from scraper_bot.pages.urls import url_to_filename
from scraper_bot.settings.defaults import ChangedDefault
from scraper_bot.storage.outbox import Outbox, OutboxEntry
from scraper_bot.telegram.client import SentMessage, TelegramClient
from scraper_bot.telegram.markup import code, escape, pre
from scraper_bot.telegram.timefmt import format_duration, format_moment, format_utc_offset
from scraper_bot.telegram.views.token import (
    NO_PAIR_YET,
    describe_token,
    found_line,
    link_lines,
    timeline_line,
)

log = logging.getLogger(__name__)

_NOTICE_WAIT = 2.0  # how long a caller waits for its notice to go out before moving on (s)
_REPORT_WAIT = 10.0  # same for a problem report, since its caller wants to know it went out
_ALERTS_OUT_WAIT = 300.0  # how long a follow-up notice waits for the alerts ahead of it (s)


class Notifier:
    """Sends everything the bot says on its own initiative.

    Token alerts go out at once and get their details edited in later; with an outbox they
    and the token documents are kept on disk until every chat got them, so a restart sends
    what a stop cut off. Any other notice holds up its caller for a few seconds at most,
    however many alerts wait ahead of it. Notices that follow alerts (a turbo summary, an
    auto-pause) wait until those alerts have left, so a summary never arrives before them.
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
        The ``outbox`` attribute, set by build_runtime, keeps alerts and documents until
        they are out.
        """
        self._telegram = telegram
        self._chat_ids = tuple(chat_ids)
        self._max_examples = max_examples if callable(max_examples) else (lambda: max_examples)
        self._problem_cooldown = problem_cooldown
        self._enricher = enricher
        self._clock = clock
        self._problems: dict[str, float] = {}
        self._background: set[asyncio.Future[Any]] = set()
        # Token alerts and documents that haven't gone out yet, so a summary can wait for them.
        self._unsent: set[asyncio.Future[Any]] = set()
        self._outbox_failing = False  # a failed save was reported; quiet until a save works
        self.outbox: Outbox | None = None

    async def started(self, version: str, total_sites: int, running: int) -> None:
        """Announce that the bot started and how many sites are running."""
        await self._notify(
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
        return await self._send(text)

    async def problem(self, key: str, title: str, detail: str, hint: str | None = None) -> bool:
        """Report a failure; return True when a chat received it.

        The same ``key`` is reported at most once per cooldown; a report that no chat
        received does not count.
        """
        if not self._problem_due(key):
            return False
        return await self._report(key, _problem_text(title, detail, hint))

    def report_later(self, key: str, title: str, detail: str, hint: str | None = None) -> None:
        """Like :meth:`problem`, for synchronous code such as a task done-callback.

        The message is sent in the background.
        """
        if self._problem_due(key):
            self._spawn(self._report(key, _problem_text(title, detail, hint)))

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

    async def _report(self, key: str, text: str) -> bool:
        """Send a problem report; one that no chat received does not start the cooldown.

        The caller waits ``_REPORT_WAIT`` seconds at most; a report still queued goes on.
        """
        started = self._problems.get(key)
        sent = await self._queue(text, _REPORT_WAIT)
        if not sent and self._chat_ids and self._problems.get(key) == started:
            del self._problems[key]
        return sent

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait up to ``timeout`` seconds for what is still being delivered, e.g. at shutdown.

        What has not gone out by then is cancelled; token alerts and documents stay in the
        outbox and go out at the next start.
        """
        pending = set(self._background)
        if not pending:
            return
        _, late = await asyncio.wait(pending, timeout=timeout)
        for task in late:
            task.cancel()
        if late:
            await asyncio.wait(late)

    def resend_unsent(self) -> None:
        """Send what an earlier run left in the outbox: alerts and documents a stop cut off.

        Chats no longer configured are skipped, and a damaged outbox is reported.
        """
        if self.outbox is None:
            return
        left = self.outbox.left_over()
        if self.outbox.broken is not None:
            problem, moved_to = self.outbox.broken
            where = (
                f"The damaged file was moved to: {moved_to}"
                if moved_to is not None
                else "It could not be moved aside, so no alert is kept on disk until it is gone."
            )
            self.report_later(
                "outbox-damaged",
                "The list of unsent alerts (outbox.json) was damaged",
                f"{problem}\n{where}",
                hint="The alerts it held are not sent again; the damaged file lists them.",
            )
        resent = 0
        for entry in left:
            for chat_id in [chat for chat in entry.chats if chat not in self._chat_ids]:
                self._delivered(entry, chat_id)  # taken out of TELEGRAM_CHAT_IDS in the meantime
            if not entry.chats:
                continue
            try:
                self._resend(entry)
            except (KeyError, TypeError, ValueError) as exc:
                log.error("an unsent %s in the outbox is unreadable (%s); dropped", entry.kind, exc)
                for chat_id in list(entry.chats):
                    self._delivered(entry, chat_id)
            else:
                resent += 1
        if resent:
            log.info("sending %d alert(s) and document(s) the last run left unsent", resent)

    async def page_discovered(self, page_url: str) -> None:
        """Announce a new sub-page that is monitored from now on."""
        await self._notify(f"🔍 Sub-page found: {escape(page_url)}\nMonitoring started.")

    async def page_changed(self, report: ChangeReport) -> None:
        """Send the change report of a page."""
        await self._notify(self.format_report(report, self._max_examples()))

    async def page_timing_out(
        self, page_url: str, streak: int, reason: str, timeout: float
    ) -> None:
        """Warn that a page keeps failing to render and now has a longer load timeout."""
        await self._notify(
            f"🐢 {escape(page_url)} has not rendered {streak} times in a row ({escape(reason)}).\n"
            f"Load timeout for this page raised to {timeout:.0f}s; "
            f"you will hear when it renders again."
        )

    async def page_renders_again(self, page_url: str, timeout: float) -> None:
        """Say that a page renders again and its load timeout is back to normal."""
        await self._notify(
            f"✅ {escape(page_url)} renders again; load timeout back to {timeout:.0f}s."
        )

    async def captcha_shown(self, page_url: str, widget: str) -> None:
        """Warn that a page shows a captcha widget, which the bot does not solve."""
        await self._notify(
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
        page check. The outbox keeps the alert until every chat got it.
        """
        head = [
            f"🔔 Contract found on {escape(page_url)}",
            f"Network: {escape(contract.network)}",
            code(contract.address.value),
        ]
        middle: list[str] = []
        found = found_line(contract, sighting)
        if found:
            middle.append(found)
        if timeline is not None:
            middle.append(timeline_line(timeline, self._clock()))
        self._spawn_alert(contract, head, middle)

    async def contracts_batch(
        self, site_url: str, page_url: str, contracts: Sequence[ConfirmedContract]
    ) -> None:
        """Send many addresses as one text document instead of a burst of messages.

        Like a token alert, it is delivered from a task the notifier owns and kept in the
        outbox: the tokens are already marked as announced, so nothing may lose the document.
        """
        lines = sorted(f"{c.address.value}\t{c.network}" for c in contracts)
        filename = f"contracts_{url_to_filename(site_url)}_{int(time.time())}.txt"
        caption = f"⚠️ {len(contracts)} confirmed contracts found on {page_url}"
        self._ship_document(filename, "\n".join(lines) + "\n", caption)

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
        await self._notify(
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
        self._spawn_alert(contract, head, [])

    async def unverified_digest(self, site_url: str, count: int, watch_seconds: float) -> None:
        """Say how many more unverified addresses appeared after the hourly alert cap."""
        await self._notify(
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
        await self._notify(
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

    async def auto_turbo_planned(
        self,
        site_url: str,
        page_url: str,
        what: str,
        target: float | None,
        start: float,
        end: float,
        *,
        moved_from: float | None = None,
    ) -> None:
        """Say that a site is checked faster around the launch time one of its pages shows.

        ``what`` is what the page showed, ``target`` the launch time (None for a phrase,
        which names no time) and ``moved_from`` the launch time it showed before a move.
        """
        now = self._clock()
        if target is None:
            head = f"⏰ {escape(what)} appeared on {escape(page_url)}."
        elif moved_from is not None:
            head = (
                f"⏰ The launch time for {escape(site_url)} changed from "
                f"{format_moment(moved_from, now)} to {_launch_at(target, now)}: "
                f"{escape(page_url)} shows a {escape(what)}."
            )
        else:
            head = (
                f"⏰ {escape(page_url)} shows a {escape(what)}: "
                f"launch at {_launch_at(target, now)}."
            )
        span = "now" if start <= now + 1 else f"from {format_moment(start, now)}"
        await self._notify(
            f"{head}\nAuto-turbo for {escape(site_url)}: checking faster {span} until "
            f"{format_moment(end, now)}, or until its first token.\n"
            f"Cancel with {code(f'/turbo stop {site_url}')}"
        )

    async def auto_turbo_full(
        self, site_url: str, page_url: str, what: str, target: float | None, windows: int
    ) -> None:
        """Say that a page shows a launch time, but auto-turbo already covers enough sites."""
        now = self._clock()
        if target is None:
            head = f"⏰ {escape(what)} appeared on {escape(page_url)}."
        else:
            head = (
                f"⏰ {escape(page_url)} shows a {escape(what)}: "
                f"launch at {_launch_at(target, now)}."
            )
        await self._notify(
            f"{head}\nAuto-turbo already covers {windows} site(s) (auto_turbo_max_sites), so "
            f"{escape(site_url)} keeps its usual pace; {code(f'/turbo {site_url}')} speeds it up "
            "by hand."
        )

    async def auto_turbo_finished(
        self,
        site_url: str,
        reason: str,
        contracts: Sequence[ConfirmedContract],
        seconds: float | None,
    ) -> None:
        """Send the auto-turbo summary once the token alerts it lists have gone out.

        ``seconds`` is how long the site was checked faster: None when a token ended the
        window before it began.
        """
        found = "\n".join(f"• {code(c.address.value)} ({escape(c.network)})" for c in contracts)
        if seconds is None:
            text = (
                f"🎯 Auto-turbo for {escape(site_url)} ended before its faster checks began: "
                f"{escape(reason)}"
            )
        else:
            text = (
                f"🎯 Auto-turbo finished on {escape(site_url)}: {escape(reason)}\n"
                f"Checked faster for {format_duration(seconds)}, "
                f"{len(contracts)} confirmed contract(s)."
            )
        self._spawn(self._after_alerts(text + (f"\n{found}" if found else "")))

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
        await self._notify("\n".join(lines))

    async def site_recovered(self, site_url: str, blocked_for: float | None = None) -> None:
        """Say that a site answers normally again and how long the block lasted, if known."""
        after = f" after {format_duration(blocked_for)}" if blocked_for is not None else ""
        await self._notify(
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
        middle: list[str],
        entry: OutboxEntry,
        out: asyncio.Future[None],
    ) -> None:
        """Send the token alert to its chats and edit the token's details into each copy.

        The sends are queued first and never wait for the details, which are looked up
        meanwhile; a chat gets its edit once both its own send and the details are ready.
        ``out`` is resolved once every send is over, which lets waiting notices go. Details
        never cost the alert: if they fail or add nothing, the first text stays.
        """
        text = "\n".join([*head, *middle, *link_lines(contract)])
        sends = [
            asyncio.ensure_future(self._send_alert(chat_id, text, entry))
            for chat_id in list(entry.chats)
        ]
        all_sent = asyncio.gather(*sends, return_exceptions=True)
        all_sent.add_done_callback(lambda _: _settle(out))
        details = None
        if self._enricher is not None:
            details = asyncio.ensure_future(self._with_details(contract, head, middle))
        try:
            await asyncio.gather(*(self._edit_in(send, details) for send in sends))
        finally:
            if details is not None:
                details.cancel()  # still running only when no chat got the alert
            _settle(out)

    async def _send_alert(self, chat_id: int, text: str, entry: OutboxEntry) -> SentMessage | None:
        """Send a token alert to one chat, then strike that chat off the alert's outbox entry."""
        message = await self._telegram.send_tracked(chat_id, text)
        self._delivered(entry, chat_id)
        return message

    async def _with_details(
        self, contract: ConfirmedContract, head: list[str], middle: list[str]
    ) -> str | None:
        """Return the alert text with the token's details, or None if they fail or add nothing."""
        assert self._enricher is not None
        try:
            info = await self._enricher.enrich(contract)
            details = describe_token(info, self._clock())
        except Exception:
            log.warning("token details failed for %s", contract.address.value, exc_info=True)
            return None
        if not [line for line in details if line != NO_PAIR_YET] and not info.coin_type:
            return None
        return "\n".join([*head, *details, *middle, *link_lines(contract, info.coin_type)])

    async def _edit_in(
        self,
        send: asyncio.Future[SentMessage | None],
        details: asyncio.Future[str | None] | None,
    ) -> None:
        """Edit the details into one chat's alert once its send and the details are done."""
        message = await send
        if message is None or details is None:
            return
        text = await details
        if text is not None:
            await self._telegram.edit_message_text(message, text)

    def _spawn_alert(
        self,
        contract: ConfirmedContract,
        head: list[str],
        middle: list[str],
        entry: OutboxEntry | None = None,
    ) -> None:
        """Start delivering a token alert in the background, kept in the outbox until it is out.

        ``entry`` is an alert an earlier run left unsent. Until its first send is over it
        counts as unsent, so later notices wait for it.
        """
        if entry is None:
            if not self._chat_ids:
                log.warning("no TELEGRAM_CHAT_IDS configured; dropping alert: %s", head[0])
                return
            payload = {
                "chain": contract.address.chain.value,
                "address": contract.address.value,
                "network": contract.network,
                "head": head,
                "middle": middle,
            }
            entry = self._keep("alert", payload)
        out: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._unsent.add(out)
        out.add_done_callback(self._unsent.discard)
        self._spawn(self._deliver_token(contract, head, middle, entry, out))

    def _ship_document(self, filename: str, content: str, caption: str) -> None:
        """Send a text file to every alert chat from a task the notifier owns.

        The file stays in the outbox until every chat got it, and the notices that follow
        it (an auto-pause) wait until it has left.
        """
        if not self._chat_ids:
            log.warning("no TELEGRAM_CHAT_IDS configured; dropping document %s", filename)
            return
        payload = {"filename": filename, "caption": caption, "content": content}
        self._spawn_document(self._keep("document", payload))

    def _spawn_document(self, entry: OutboxEntry) -> None:
        """Start delivering an outbox document; later notices wait until it has left."""
        task = self._spawn(self._deliver_document(entry))
        self._unsent.add(task)
        task.add_done_callback(self._unsent.discard)

    async def _deliver_document(self, entry: OutboxEntry) -> None:
        """Send an outbox document patiently to its chats, then strike them off the entry."""
        payload, chats = entry.payload, list(entry.chats)
        content = payload["content"].encode("utf-8")
        await self._telegram.broadcast_document(
            chats, payload["filename"], content, payload["caption"], patient=True
        )
        for chat_id in chats:
            self._delivered(entry, chat_id)

    def _resend(self, entry: OutboxEntry) -> None:
        """Deliver an alert or document an earlier run left in the outbox.

        Raise KeyError, TypeError or ValueError when the entry cannot be read.
        """
        payload = entry.payload
        if entry.kind == "document":
            if not all(isinstance(payload[key], str) for key in ("filename", "caption", "content")):
                raise TypeError("a document field is not text")
            self._spawn_document(entry)
            return
        if entry.kind != "alert":
            raise ValueError(f"unknown kind {entry.kind!r}")
        head, middle = payload["head"], payload["middle"]
        if not isinstance(head, list) or not isinstance(middle, list):
            raise TypeError("the alert lines are not lists")
        address = Address(Chain(payload["chain"]), str(payload["address"]))
        contract = ConfirmedContract(address, str(payload["network"]))
        self._spawn_alert(contract, [str(line) for line in head], [str(m) for m in middle], entry)

    def _keep(self, kind: str, payload: dict[str, Any]) -> OutboxEntry:
        """Put an alert or document for every alert chat into the outbox; return its entry."""
        entry = OutboxEntry(uuid4().hex, kind, list(self._chat_ids), payload)
        if self.outbox is not None:
            try:
                self.outbox.add(entry)
            except Exception as exc:  # any failure: the alert must go out all the same
                self._outbox_failed(exc)
            else:
                self._outbox_failing = False
        return entry

    def _delivered(self, entry: OutboxEntry, chat_id: int) -> None:
        """Strike a chat off an outbox entry once its send is over, sent or refused."""
        if self.outbox is None:
            return
        try:
            self.outbox.delivered(entry, chat_id)
        except Exception as exc:
            self._outbox_failed(exc)

    def _outbox_failed(self, exc: Exception) -> None:
        """Log that the outbox cannot be saved, and report it once until a save works again.

        The alerts still go out.
        """
        log.error("saving the outbox failed: %s", exc)
        if self._outbox_failing:
            return
        self._outbox_failing = True
        self.report_later(
            "outbox-save",
            "Saving unsent alerts (outbox.json) failed",
            str(exc),
            hint="Alerts still go out, but one cut off by a restart is not sent again. Check "
            "free disk space and whether another program keeps the file open.",
        )

    async def _notify(self, text: str) -> None:
        """Queue ``text`` for every alert chat and give it a moment to go out.

        A notice stuck behind a backlog of alerts stays queued, so the caller (usually a
        monitor's round) is held up for at most ``_NOTICE_WAIT`` seconds.
        """
        await self._queue(text, _NOTICE_WAIT)

    async def _queue(self, text: str, wait: float) -> bool:
        """Queue ``text`` for every alert chat; return True when one got it within ``wait`` s."""
        if not self._chat_ids:
            log.warning("no TELEGRAM_CHAT_IDS configured; dropping alert: %s", text.splitlines()[0])
            return False
        return await self._telegram.broadcast(self._chat_ids, text, wait=wait)

    async def _after_alerts(self, text: str, *, patient: bool = False) -> None:
        """Send ``text`` once the token alerts and documents under way have left.

        The alerts wait out flood limits themselves (about ten minutes at most); the time limit
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

    async def late_contracts_batch(
        self, site_url: str, contracts: Sequence[ConfirmedContract]
    ) -> None:
        """Send tokens that a site's pausing first check showed but confirmed late, as one file.

        The pause message promised that the page's tokens would not come one by one, so
        these are old news too. Like :meth:`contracts_batch`, the document leaves from a task
        the notifier owns and stays in the outbox until every chat got it.
        """
        filename = f"contracts_{url_to_filename(site_url)}_{int(self._clock())}_late.txt"
        lines = sorted(f"{c.address.value}\t{c.network}" for c in contracts)
        caption = (
            f"📄 {len(contracts)} more contract(s) that were already on {site_url} at the "
            "first check (confirmed late)"
        )
        self._ship_document(filename, "\n".join(lines) + "\n", caption)


def _settle(future: asyncio.Future[None]) -> None:
    """Mark ``future`` as done, unless it already is."""
    if not future.done():
        future.set_result(None)


def _launch_at(timestamp: float, now: float) -> str:
    """Return a launch time like ``15:04 (UTC+02:00, in 5m 30s)``."""
    zone = format_utc_offset(timestamp)
    return f"{format_moment(timestamp, now)} ({zone}, in {format_duration(timestamp - now)})"


def _problem_text(title: str, detail: str, hint: str | None) -> str:
    """Return the text of a problem message; the detail is cut to 1500 characters."""
    text = f"🛑 {escape(title)}\n{pre(str(detail)[:1500])}"
    return f"{text}\n{escape(hint)}" if hint else text
