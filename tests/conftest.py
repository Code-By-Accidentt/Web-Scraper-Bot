"""Shared fakes and helpers for the test-suite."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable, Sequence
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

    def html(self, url: str, *pages: str, status: int = 200) -> None:
        self.script(url, *(FetchResult(url, status, page) for page in pages))

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        queue = self.scripts[url]
        if not queue:
            return FetchResult(url, None, error="no scripted response")
        return queue.pop(0) if len(queue) > 1 else queue[0]


class FakeValidator:
    def __init__(self) -> None:
        self.verdicts: dict[Address, ValidationResult] = {}
        self.calls: list[list[Address]] = []

    def confirm(self, address: Address, network: str = "Ethereum") -> None:
        self.verdicts[address] = ValidationResult(address, ValidationStatus.CONFIRMED, network)

    async def validate_many(self, addresses: Iterable[Address]) -> list[ValidationResult]:
        batch = list(addresses)
        self.calls.append(batch)
        return [self.verdicts.get(a, ValidationResult(a, ValidationStatus.REJECTED)) for a in batch]


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
