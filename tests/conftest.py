"""Shared fakes and helpers for the test-suite."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scraper_bot.chains.addresses.extractor import AddressExtractor, drop_coin_packages
from scraper_bot.chains.rpc.health import FailingEndpoint
from scraper_bot.models.addresses import (
    Address,
    ConfirmedContract,
    ValidationResult,
    ValidationStatus,
)
from scraper_bot.models.pages import BlockSignal, ChangeReport, Node, PageCheckOutcome, PageStatus
from scraper_bot.models.sightings import Observation, Sighting, SightingSource, Timeline
from scraper_bot.models.sites import SiteState
from scraper_bot.monitoring.site_monitor import MonitorStats
from scraper_bot.monitoring.turbo.session import TurboStats
from scraper_bot.pages.fetching.prober import ProbeResult
from scraper_bot.pages.fetching.result import EarlyListener, EarlyText, FetchResult
from scraper_bot.settings.env import Settings
from scraper_bot.telegram.client import SentMessage


def extract(extractor: AddressExtractor, text: str) -> list[Address]:
    """Unique addresses of ``text`` in order of appearance, Sui coins without their packages."""
    unique = list(dict.fromkeys(address for address, _ in extractor.locate(text)))
    return drop_coin_packages(unique, lambda address: address)


@dataclass
class FakeTuning:
    """Mutable stand-in for Preferences with test-friendly (tiny) timings."""

    poll_interval_min: float = 0.01
    poll_interval_max: float = 0.02
    confirm_delay: float = 2.0
    network_idle_timeout: float = 0.0
    light_interval: float = 1.0
    light_turbo_interval: float = 0.3
    render_interval: float = 30.0
    settle_delay: float = 0.0
    render_failure_threshold: int = 5
    require_erc20: bool = True
    skip_stable_copies: bool = True
    contract_pause_threshold: int = 5
    rejected_ttl: float = 0.0
    deep_scan: bool = True
    unverified_recheck: float = 0.0
    unverified_watch: float = 3600.0
    unverified_max_per_hour: int = 10
    max_change_examples: int = 5
    enrich_timeout: float = 3.0
    flap_window: float = 0.0
    flap_threshold: int = 2
    turbo_interval: float = 0.01
    turbo_default_minutes: float = 30.0
    turbo_max_minutes: float = 180.0
    turbo_stop_after: int = 1
    turbo_max_sites: int = 1
    turbo_max_pages: int = 4
    turbo_recheck_interval: float = 0.0
    block_max_backoff: float = 600.0
    auto_turbo: bool = True
    auto_turbo_before: float = 600.0
    auto_turbo_after: float = 3600.0
    auto_turbo_max_sites: int = 3


MINIMAL_ENV = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_IDS": "1,2"}


def make_settings(tmp_path: Path, **overrides: str) -> Settings:
    env = {**MINIMAL_ENV, "DATA_DIR": str(tmp_path / "data"), **overrides}
    return Settings.from_env(env, dotenv_path=None)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


def node(tag: str, text: str = "", **attrs: str) -> Node:
    """Build a Node with attributes given as keywords (``class_`` becomes ``class``)."""
    fixed = {("class" if key == "class_" else key): value for key, value in attrs.items()}
    return Node(tag, tuple(sorted(fixed.items())), text)


class FakeFetcher:
    """Returns scripted results per URL; the last scripted result repeats forever."""

    is_connected = True  # so it can stand in for the browser behind /status

    def __init__(self) -> None:
        self.scripts: dict[str, list[FetchResult]] = defaultdict(list)
        self.calls: list[str] = []
        self.timeouts: list[float | None] = []  # the ``timeout`` passed to each fetch
        self.early: dict[str, list[EarlyText]] = {}  # reported by the next fetch that listens

    def script(self, url: str, *results: FetchResult) -> None:
        self.scripts[url].extend(results)

    def html(
        self,
        url: str,
        *pages: str,
        status: int = 200,
        extra: tuple[str, ...] = (),
        headers: dict[str, str] | None = None,
    ) -> None:
        self.script(
            url,
            *(
                FetchResult(url, status, page, extra_texts=extra, headers=headers or {})
                for page in pages
            ),
        )

    async def fetch(
        self, url: str, *, timeout: float | None = None, on_text: EarlyListener | None = None
    ) -> FetchResult:
        self.calls.append(url)
        self.timeouts.append(timeout)
        if on_text is not None and url in self.early:
            for item in self.early.pop(url):
                on_text(item)
            await asyncio.sleep(0)  # the early checks start while the page still loads
        queue = self.scripts[url]
        if not queue:
            return FetchResult(url, None, error="no scripted response")
        return queue.pop(0) if len(queue) > 1 else queue[0]


class FakeValidator:
    """Scripted verdicts: a list per address, the last entry repeats forever."""

    def __init__(self) -> None:
        self.verdicts: dict[Address, list[ValidationResult]] = {}
        self.calls: list[list[Address]] = []
        self.negative_cache_calls: list[bool] = []
        self.options: list[tuple[tuple[str, ...] | None, bool]] = []  # (networks, urgent)

    def confirm(self, address: Address, network: str = "Ethereum") -> None:
        self.verdicts[address] = [ValidationResult(address, ValidationStatus.CONFIRMED, network)]

    def reject_then_confirm(
        self, address: Address, network: str = "Ethereum", rejections: int = 1
    ) -> None:
        self.verdicts[address] = [ValidationResult(address, ValidationStatus.REJECTED)] * (
            rejections
        ) + [ValidationResult(address, ValidationStatus.CONFIRMED, network)]

    def known(self, address: Address) -> None:
        self.verdicts[address] = [ValidationResult(address, ValidationStatus.ALREADY_KNOWN)]

    async def validate(
        self,
        address: Address,
        *,
        negative_cache: bool = True,
        networks: Collection[str] | None = None,
        urgent: bool = False,
    ) -> ValidationResult:
        results = await self.validate_many(
            [address], negative_cache=negative_cache, networks=networks, urgent=urgent
        )
        return results[0]

    async def validate_many(
        self,
        addresses: Iterable[Address],
        *,
        negative_cache: bool = True,
        follow_pairs: bool = True,
        networks: Collection[str] | None = None,
        urgent: bool = False,
    ) -> list[ValidationResult]:
        batch = list(addresses)
        self.calls.append(batch)
        self.negative_cache_calls.append(negative_cache)
        self.options.append((None if networks is None else tuple(networks), urgent))
        results: list[ValidationResult] = []
        for address in batch:
            queue = self.verdicts.get(address)
            if not queue:
                results.append(ValidationResult(address, ValidationStatus.REJECTED))
            else:
                results.append(queue.pop(0) if len(queue) > 1 else queue[0])
        return results


class FakeProber:
    def __init__(self, statuses: dict[str, int | None] | None = None) -> None:
        self.statuses = statuses or {}
        self.calls: list[str] = []
        self.peeks: list[str] = []
        self.peek_status = 200  # what a light GET of any page answers

    async def probe(self, url: str) -> ProbeResult:
        self.calls.append(url)
        status = self.statuses.get(url, 200)
        if status is None:
            return ProbeResult(url, None, error="ConnectError: name not resolved", unreachable=True)
        return ProbeResult(url, status)

    async def peek(self, url: str) -> FetchResult:
        self.peeks.append(url)
        return FetchResult(url, self.peek_status, "<html><title>page</title></html>")


class FakeNotifier:
    def __init__(self) -> None:
        self.discovered: list[str] = []
        self.changes: list[ChangeReport] = []
        self.timing_out: list[tuple[str, int, str, float]] = []
        self.renders_again: list[tuple[str, float]] = []
        self.contracts: list[tuple[ConfirmedContract, str]] = []
        self.contract_details: list[tuple[Sighting | None, Timeline | None]] = []
        self.batches: list[tuple[str, str, tuple[ConfirmedContract, ...]]] = []
        self.paused: list[tuple[str, str, int]] = []
        self.first_check_pauses: list[bool] = []  # ``first_check`` of each auto-pause
        self.stuck: list[tuple[tuple[FailingEndpoint, ...], float]] = []
        self.turbo_started_calls: list[tuple[str, int, float, float, int]] = []
        self.unverified: list[tuple[Address, str]] = []
        self.confirmed: list[tuple[ConfirmedContract, str]] = []
        self.turbo_finished_calls: list[tuple[str, str, int]] = []
        self.blocked: list[tuple[str, str, str, float]] = []  # reason: signal.describe()
        self.block_signals: list[BlockSignal] = []
        self.recovered: list[str] = []
        self.blocked_for: list[float | None] = []
        self.captchas: list[tuple[str, str]] = []
        self.digests: list[tuple[str, int]] = []
        self.problems: list[tuple[str, str, str, str | None]] = []
        self.started_calls: list[tuple[str, int, int]] = []
        self.late_batches: list[tuple[str, tuple[ConfirmedContract, ...]]] = []
        # (site, what, target, start, end, moved_from) of each auto-turbo notice
        self.auto_planned: list[tuple[str, str, float | None, float, float, float | None]] = []
        self.auto_full: list[tuple[str, str]] = []
        self.auto_finished: list[tuple[str, str, int, float | None]] = []

    async def page_discovered(self, page_url: str) -> None:
        self.discovered.append(page_url)

    async def page_changed(self, report: ChangeReport) -> None:
        self.changes.append(report)

    async def page_timing_out(
        self, page_url: str, streak: int, reason: str, timeout: float
    ) -> None:
        self.timing_out.append((page_url, streak, reason, timeout))

    async def page_renders_again(self, page_url: str, timeout: float) -> None:
        self.renders_again.append((page_url, timeout))

    async def captcha_shown(self, page_url: str, widget: str) -> None:
        self.captchas.append((page_url, widget))

    async def contract_found(
        self,
        contract: ConfirmedContract,
        page_url: str,
        *,
        sighting: Sighting | None = None,
        timeline: Timeline | None = None,
    ) -> None:
        self.contracts.append((contract, page_url))
        self.contract_details.append((sighting, timeline))

    async def contracts_batch(
        self, site_url: str, page_url: str, contracts: Sequence[ConfirmedContract]
    ) -> None:
        self.batches.append((site_url, page_url, tuple(contracts)))

    async def site_auto_paused(
        self,
        site_url: str,
        page_url: str,
        count: int,
        pages: Sequence[str],
        first_check: bool = False,
    ) -> None:
        self.paused.append((site_url, page_url, count))
        self.first_check_pauses.append(first_check)

    async def turbo_started(
        self, site_url: str, pages: int, interval: float, minutes: float, stop_after: int
    ) -> None:
        self.turbo_started_calls.append((site_url, pages, interval, minutes, stop_after))

    async def unverified_address(
        self,
        address: Address,
        page_url: str,
        recheck_seconds: float,
        watch_seconds: float | None = None,
    ) -> None:
        self.unverified.append((address, page_url))

    async def unverified_digest(self, site_url: str, count: int, watch_seconds: float) -> None:
        self.digests.append((site_url, count))

    async def address_confirmed(self, contract: ConfirmedContract, page_url: str) -> None:
        self.confirmed.append((contract, page_url))

    async def turbo_finished(
        self,
        site_url: str,
        reason: str,
        contracts: Sequence[ConfirmedContract],
        seconds: float,
        renders: int,
    ) -> None:
        self.turbo_finished_calls.append((site_url, reason, len(contracts)))

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
        self.auto_planned.append((site_url, what, target, start, end, moved_from))

    async def auto_turbo_full(
        self, site_url: str, page_url: str, what: str, target: float | None, windows: int
    ) -> None:
        self.auto_full.append((site_url, what))

    async def auto_turbo_finished(
        self,
        site_url: str,
        reason: str,
        contracts: Sequence[ConfirmedContract],
        seconds: float | None,
    ) -> None:
        self.auto_finished.append((site_url, reason, len(contracts), seconds))

    async def site_blocked(
        self, site_url: str, page_url: str, signal: BlockSignal, next_delay: float
    ) -> None:
        self.blocked.append((site_url, page_url, signal.describe(), next_delay))
        self.block_signals.append(signal)

    async def site_recovered(self, site_url: str, blocked_for: float | None = None) -> None:
        self.recovered.append(site_url)
        self.blocked_for.append(blocked_for)

    async def problem(self, key: str, title: str, detail: str, hint: str | None = None) -> bool:
        self.problems.append((key, title, detail, hint))
        return True

    def report_later(self, key: str, title: str, detail: str, hint: str | None = None) -> None:
        self.problems.append((key, title, detail, hint))

    def rpc_endpoints_stuck(self, endpoints: Sequence[FailingEndpoint], retry_delay: float) -> None:
        self.stuck.append((tuple(endpoints), retry_delay))

    async def started(self, version: str, total_sites: int, running: int) -> None:
        self.started_calls.append((version, total_sites, running))

    async def late_contracts_batch(
        self, site_url: str, contracts: Sequence[ConfirmedContract]
    ) -> None:
        self.late_batches.append((site_url, tuple(contracts)))


class FakeChecker:
    """Scripted stand-in for PageChecker.

    Like the real one it reports a page's addresses to ``on_observation`` first: the
    scripted ``tokens`` as CONFIRMED, the outcome's other candidates as REJECTED, all seen
    in page text.
    """

    def __init__(self) -> None:
        self.outcomes: dict[str, PageCheckOutcome] = {}
        self.tokens: dict[str, tuple[ConfirmedContract, ...]] = {}
        self.calls: list[str] = []
        self.timeouts: list[float | None] = []  # the ``load_timeout`` passed to each check

    async def check(
        self,
        page_url: str,
        *,
        load_timeout: float | None = None,
        on_observation: Callable[[Observation], Awaitable[None]] | None = None,
    ) -> PageCheckOutcome:
        self.calls.append(page_url)
        self.timeouts.append(load_timeout)
        outcome = self.outcomes.get(page_url, PageCheckOutcome(page_url, PageStatus.UNCHANGED))
        confirmed = {contract.address: contract for contract in self.tokens.get(page_url, ())}
        addresses = list(dict.fromkeys([*outcome.candidates, *confirmed]))
        if on_observation is not None and addresses:
            results = tuple(
                ValidationResult(a, ValidationStatus.CONFIRMED, confirmed[a].network)
                if a in confirmed
                else ValidationResult(a, ValidationStatus.REJECTED)
                for a in addresses
            )
            sightings = tuple(Sighting(a, SightingSource.TEXT) for a in addresses)
            await on_observation(Observation(page_url, time.time(), sightings, results))
        return outcome


class FakeTelegram:
    """Records what the bot sends; an edit replaces the recorded text of its message."""

    username: str | None = None  # the bot's own name; unknown, as when getMe failed

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.documents: list[tuple[int, str, bytes, str]] = []
        self.updates: list[list[dict[str, Any]] | None] = []
        self.offsets: list[int | None] = []
        self.edits: list[tuple[int, str]] = []
        self.profile_calls = 0

    async def send_message(self, chat_id: int, text: str) -> bool:
        self.messages.append((chat_id, text))
        return True

    async def send_tracked(self, chat_id: int, text: str) -> SentMessage | None:
        self.messages.append((chat_id, text))
        return SentMessage(chat_id, len(self.messages) - 1)

    async def edit_message_text(self, message: SentMessage, text: str) -> bool:
        self.edits.append((message.chat_id, text))
        self.messages[message.message_id] = (message.chat_id, text)
        return True

    async def broadcast(
        self,
        chat_ids: Iterable[int],
        text: str,
        *,
        patient: bool = False,
        wait: float | None = None,
    ) -> bool:
        for chat_id in chat_ids:
            await self.send_message(chat_id, text)
        return bool(chat_ids)

    async def get_me(self) -> dict[str, Any] | None:
        self.profile_calls += 1
        return None

    def former_id(self, chat_id: int) -> int | None:
        return None

    async def broadcast_document(
        self,
        chat_ids: Iterable[int],
        filename: str,
        content: bytes,
        caption: str = "",
        *,
        patient: bool = False,
    ) -> None:
        for chat_id in chat_ids:
            self.documents.append((chat_id, filename, content, caption))

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]] | None:
        self.offsets.append(offset)
        if self.updates:
            return self.updates.pop(0)
        await asyncio.sleep(0.01)
        return []


class StubMonitor:
    """Minimal monitor for manager tests: runs until stopped, optionally crashes once."""

    def __init__(self, site: SiteState, persist: Any, on_auto_pause: Any, *, crash: bool = False):
        self.site = site
        self.persist = persist
        self.on_auto_pause = on_auto_pause
        self.crash = crash
        self.started = False
        self.stats = MonitorStats()
        self._stop = asyncio.Event()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        self.started = True
        if self.crash:
            raise RuntimeError("boom")
        await self._stop.wait()


class StubTurboSession:
    """Stand-in for TurboSession: records its arguments and runs until stopped."""

    def __init__(self, url: str, pages: list[str], duration: float, limits=None) -> None:
        self.url = url
        self.pages = list(pages)
        self.duration = duration
        self.limits = limits
        self.stats = TurboStats()
        self.pending_count = 0
        self.stop_reason: str | None = None
        self._stop = asyncio.Event()

    @property
    def seconds_left(self) -> float:
        return self.duration

    def request_stop(self) -> None:
        self.stop_reason = "stopped manually"
        self._stop.set()

    async def run(self) -> TurboStats:
        self.stats.started_at = 0.0
        await self._stop.wait()
        self.stats.stop_reason = self.stop_reason
        return self.stats
