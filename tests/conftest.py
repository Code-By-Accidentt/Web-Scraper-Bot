"""Shared fakes and helpers for the test-suite."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scraper_bot.config import Settings
from scraper_bot.fetcher import FetchResult, ProbeResult
from scraper_bot.models import (
    Address,
    ChangeReport,
    ConfirmedContract,
    Node,
    PageCheckOutcome,
    PageStatus,
    SiteState,
    ValidationResult,
    ValidationStatus,
)
from scraper_bot.site_monitor import MonitorStats
from scraper_bot.turbo import TurboStats


@dataclass
class FakeTuning:
    """Mutable stand-in for Preferences with test-friendly (tiny) timings."""

    poll_interval_min: float = 0.01
    poll_interval_max: float = 0.02
    confirm_delay: float = 2.0
    network_idle_timeout: float = 0.0
    settle_delay: float = 0.0
    require_erc20: bool = True
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

    def __init__(self) -> None:
        self.scripts: dict[str, list[FetchResult]] = defaultdict(list)
        self.calls: list[str] = []

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

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
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

    async def validate(self, address: Address, *, negative_cache: bool = True) -> ValidationResult:
        return (await self.validate_many([address], negative_cache=negative_cache))[0]

    async def validate_many(
        self, addresses: Iterable[Address], *, negative_cache: bool = True
    ) -> list[ValidationResult]:
        batch = list(addresses)
        self.calls.append(batch)
        self.negative_cache_calls.append(negative_cache)
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

    async def probe(self, url: str) -> ProbeResult:
        self.calls.append(url)
        status = self.statuses.get(url, 200)
        if status is None:
            return ProbeResult(url, None, error="ConnectError: name not resolved")
        return ProbeResult(url, status)


class FakeNotifier:
    def __init__(self) -> None:
        self.discovered: list[str] = []
        self.changes: list[ChangeReport] = []
        self.contracts: list[tuple[ConfirmedContract, str]] = []
        self.batches: list[tuple[str, str, tuple[ConfirmedContract, ...]]] = []
        self.paused: list[tuple[str, str, int]] = []
        self.turbo_started_calls: list[tuple[str, int, float, float, int]] = []
        self.unverified: list[tuple[Address, str]] = []
        self.confirmed: list[tuple[ConfirmedContract, str]] = []
        self.turbo_finished_calls: list[tuple[str, str, int]] = []
        self.blocked: list[tuple[str, str, str, float]] = []
        self.recovered: list[str] = []
        self.digests: list[tuple[str, int]] = []
        self.problems: list[tuple[str, str, str, str | None]] = []
        self.started_calls: list[tuple[str, int, int]] = []

    async def page_discovered(self, page_url: str) -> None:
        self.discovered.append(page_url)

    async def page_changed(self, report: ChangeReport) -> None:
        self.changes.append(report)

    async def contract_found(self, contract: ConfirmedContract, page_url: str) -> None:
        self.contracts.append((contract, page_url))

    async def contracts_batch(
        self, site_url: str, page_url: str, contracts: Sequence[ConfirmedContract]
    ) -> None:
        self.batches.append((site_url, page_url, tuple(contracts)))

    async def site_auto_paused(
        self, site_url: str, page_url: str, count: int, pages: Sequence[str]
    ) -> None:
        self.paused.append((site_url, page_url, count))

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

    async def site_blocked(
        self, site_url: str, page_url: str, reason: str, next_delay: float
    ) -> None:
        self.blocked.append((site_url, page_url, reason, next_delay))

    async def site_recovered(self, site_url: str) -> None:
        self.recovered.append(site_url)

    async def problem(self, key: str, title: str, detail: str, hint: str | None = None) -> bool:
        self.problems.append((key, title, detail, hint))
        return True

    def report_later(self, key: str, title: str, detail: str, hint: str | None = None) -> None:
        self.problems.append((key, title, detail, hint))

    async def started(self, version: str, total_sites: int, running: int) -> None:
        self.started_calls.append((version, total_sites, running))


class FakeChecker:
    """Scripted stand-in for PageChecker."""

    def __init__(self) -> None:
        self.outcomes: dict[str, PageCheckOutcome] = {}
        self.calls: list[str] = []

    async def check(self, page_url: str) -> PageCheckOutcome:
        self.calls.append(page_url)
        return self.outcomes.get(page_url, PageCheckOutcome(page_url, PageStatus.UNCHANGED))


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.documents: list[tuple[int, str, bytes, str]] = []
        self.updates: list[list[dict[str, Any]] | None] = []
        self.offsets: list[int | None] = []

    async def send_message(self, chat_id: int, text: str) -> bool:
        self.messages.append((chat_id, text))
        return True

    async def broadcast(self, chat_ids: Iterable[int], text: str) -> None:
        for chat_id in chat_ids:
            await self.send_message(chat_id, text)

    async def send_document(
        self, chat_id: int, filename: str, content: bytes, caption: str = ""
    ) -> bool:
        self.documents.append((chat_id, filename, content, caption))
        return True

    async def broadcast_document(
        self, chat_ids: Iterable[int], filename: str, content: bytes, caption: str = ""
    ) -> None:
        for chat_id in chat_ids:
            await self.send_document(chat_id, filename, content, caption)

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
