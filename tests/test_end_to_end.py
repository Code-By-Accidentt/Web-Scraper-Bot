"""End-to-end scenario: the wired bot driven through Telegram commands on fakes.

Real: Preferences, Notifier with TokenEnricher, ContractValidator and the chain
validators, PageChecker, SiteMonitor, MonitorManager, TurboManager/TurboSession,
UnverifiedWatcher, StatusReporter, BotCommands and every store under ``data/``.
Fake: the browser (FakeFetcher), Telegram (FakeTelegram) and all HTTP traffic
(httpx.MockTransport answering JSON-RPC, DexScreener and the sub-page probes).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from scraper_bot.app import Runtime, build_runtime
from scraper_bot.chains.enrichment import TokenEnricher
from scraper_bot.chains.evm import ERC20_DECIMALS, ERC20_NAME, ERC20_SYMBOL, ERC20_TOTAL_SUPPLY
from scraper_bot.config import Settings
from scraper_bot.pages.fetcher import FetchResult
from scraper_bot.preferences import Preferences
from scraper_bot.telegram.commands import parse_command
from scraper_bot.telegram.notifier import Notifier
from tests.conftest import FakeFetcher, FakeTelegram, make_settings

ROOT = "https://example.com"
APP = "https://app.example.com"
CHAT = 1
TOKEN_A = "0x" + "aa" * 20
TOKEN_B = "0x" + "bb" * 20
TOKEN_C = "0x" + "cc" * 20
LAUNCH = "0x" + "dd" * 20
WALLET = "0x" + "ee" * 20
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>Checking</body></html>"
ENV = {
    "TELEGRAM_CHAT_IDS": str(CHAT),
    "ETH_RPC_URL": "https://alpha.rpc",
    "BASE_RPC_URL": "",
    "ARB_RPC_URL": "",
    "OPT_RPC_URL": "",
    "BSC_RPC_URL": "",
    "FANTOM_RPC_URL": "",
    "SONIC_RPC_URL": "",
    "AVAX_RPC_URL": "",
    "SOLANA_RPC_URL": "https://sol.rpc",
    "SUI_RPC_URL": "https://sui.rpc",
    "SUB_URL_PATTERNS": "https://app.{domain}",
    "CONFIRM_DELAY": "0",
    "TURBO_RECHECK_INTERVAL": "1",
    "TURBO_STOP_AFTER": "1",
    "ENRICH_TIMEOUT": "3",
}


def page(*lines: str) -> str:
    body = "".join(f"<p>{line}</p>" for line in lines)
    return f"<html><body><h1>Launchpad</h1>{body}</body></html>"


def abi_string(text: str) -> str:
    raw = text.encode()
    padded = raw + b"\x00" * (-len(raw) % 32)
    return "0x" + (32).to_bytes(32, "big").hex() + len(raw).to_bytes(32, "big").hex() + padded.hex()


def abi_uint(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


class FakeChain:
    """JSON-RPC nodes, DexScreener and the sub-page probes behind httpx.MockTransport."""

    def __init__(self) -> None:
        self.deployed: set[str] = set()  # lower-case addresses that have bytecode
        self.live_pages = {APP}

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "alpha.rpc":
            return self._evm(json.loads(request.content))
        if host in {"sol.rpc", "sui.rpc"}:
            return self._result({"value": None})
        if host == "api.dexscreener.com":
            return httpx.Response(200, json=[])
        return httpx.Response(200 if str(request.url) in self.live_pages else 404)

    def _evm(self, body: dict[str, Any]) -> httpx.Response:
        method, params = body["method"], body["params"]
        if method == "eth_getCode":
            deployed = params[0].lower() in self.deployed
            return self._result("0x6080604052" if deployed else "0x")
        if method == "eth_call":
            answers = {
                ERC20_NAME: abi_string("Launch Token"),
                ERC20_SYMBOL: abi_string("LNCH"),
                ERC20_DECIMALS: abi_uint(18),
                ERC20_TOTAL_SUPPLY: abi_uint(1_000 * 10**18),
            }
            return self._result(answers.get(params[0]["data"], "0x"))
        error = {"code": -32601, "message": f"unknown method {method}"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": error})

    @staticmethod
    def _result(value: Any) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": value})


class Bot:
    """One process lifetime of the bot: build, drive through commands, read alerts."""

    def __init__(self, settings: Settings, chain: FakeChain, fetcher: FakeFetcher) -> None:
        self.settings = settings
        self.telegram = FakeTelegram()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(chain.handler))
        prefs = Preferences(settings, settings.data_dir / "preferences.json")
        prefs.load()
        enricher = TokenEnricher(
            self.http,
            settings.evm_networks,
            settings.solana_rpc_url,
            timeout=settings.rpc_timeout,
            tuning=prefs,
            sui_rpc_url=settings.sui_rpc_url,
        )
        notifier = Notifier(
            self.telegram,  # type: ignore[arg-type]
            settings.chat_ids,
            max_examples=lambda: prefs.max_change_examples,
            enricher=enricher,
        )
        self.runtime: Runtime = build_runtime(
            settings,
            prefs,
            http=self.http,
            telegram=self.telegram,  # type: ignore[arg-type]
            notifier=notifier,
            fetcher=fetcher,
            legacy_dir=settings.data_dir,
            sleep=lambda _seconds: asyncio.sleep(0.01),
        )
        self._cursor = 0

    async def start(self) -> None:
        await self.runtime.start()

    async def stop(self) -> None:
        await self.runtime.shutdown()
        await self.http.aclose()

    async def command(self, text: str) -> str:
        parsed = parse_command(text)
        assert parsed is not None
        return await self.runtime.commands.execute(CHAT, parsed)

    async def alert(self, needle: str, timeout: float = 5.0) -> str:
        """The next alert containing ``needle``; alerts are consumed in order."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for index in range(self._cursor, len(self.telegram.messages)):
                _, text = self.telegram.messages[index]
                if needle in text:
                    self._cursor = index + 1
                    return text
            await asyncio.sleep(0.01)
        pending = [text for _, text in self.telegram.messages[self._cursor :]]
        raise AssertionError(f"no alert containing {needle!r}; unread alerts: {pending}")

    def alerted(self, needle: str) -> bool:
        return any(needle in text for _, text in self.telegram.messages)

    async def rounds(self, minimum: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            stats = self.runtime.manager.stats(ROOT)
            if stats is not None and stats.rounds >= minimum:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"the monitor did not complete {minimum} round(s)")


async def test_bot_lifecycle_through_telegram(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, **ENV)
    chain, fetcher = FakeChain(), FakeFetcher()
    fetcher.html(APP, page("App shell"))
    fetcher.html(ROOT, page("Coming soon"))
    state_file = settings.data_dir / "state.json"

    bot = Bot(settings, chain, fetcher)
    await bot.start()
    try:
        assert "0/0 site(s) running" in await bot.alert("🤖 Bot started")

        # A site is added: baseline snapshot, the live sub-page is discovered.
        assert "added to monitoring" in await bot.command(f"/add {ROOT}")
        await bot.alert(f"🔍 Sub-page found: {APP}")
        await bot.rounds(1)
        assert ROOT in json.loads(state_file.read_text("utf-8"))["sites"]

        # A real change is confirmed by the second render and reported with examples.
        fetcher.html(ROOT, page("Launch is live"))
        changes = await bot.alert("‼️ Changes on")
        assert "Coming soon" in changes and "Launch is live" in changes

        # Change alerts are muted, contracts are still announced with token details.
        assert "disabled" in await bot.command(f"/nonotifi {ROOT}")
        chain.deployed.add(TOKEN_A)
        fetcher.html(ROOT, page("Launch is live", f"Token: {TOKEN_A}"))
        found = await bot.alert("🔔 Contract found")
        assert "Launch Token (LNCH)" in found and "18 decimals" in found
        assert "No DEX pair listed yet" in found and "Etherscan" in found
        listing = await bot.command("/list")
        assert "[alerts: OFF]" in listing and "[RUNNING]" in listing

        # /set applies at once: two new contracts on one page ship a document and pause.
        assert "applies immediately" in await bot.command("/set contract_pause_threshold 2")
        chain.deployed.update({TOKEN_B, TOKEN_C})
        fetcher.html(ROOT, page("Launch is live", f"Token: {TOKEN_A}", TOKEN_B, TOKEN_C))
        paused = await bot.alert("⏸️ Monitoring paused")
        assert "2 confirmed contracts" in paused
        assert bot.telegram.documents and bot.telegram.documents[-1][1].startswith("contracts_")
        assert "[PAUSED]" in await bot.command("/list")
        assert json.loads(state_file.read_text("utf-8"))["sites"][ROOT]["paused"] is True

        # Resume, look at /status, then hunt a launch in turbo mode.
        assert "resumed" in await bot.command(f"/resume {ROOT}")
        await bot.rounds(1)
        status = await bot.command("/status")
        assert "3 known contract(s)" in status and "last round" in status
        assert (await bot.command(f"/turbo {ROOT} 1")).startswith("🚀 Turbo started")
        await bot.alert("🚀 Turbo mode on")
        fetcher.html(ROOT, page("Launch is live", TOKEN_A, TOKEN_B, TOKEN_C, f"Presale: {LAUNCH}"))
        await bot.alert("⚠️ Unverified address")
        chain.deployed.add(LAUNCH)  # the contract is deployed after its address was published
        finished = await bot.alert("🎯 Turbo finished", timeout=8.0)
        assert "1 confirmed contract(s)" in finished and LAUNCH in finished.lower()
        assert bot.alerted("✅ Confirmed") or bot.alerted("🔔 Contract found")

        # Unverified mode announces a plain wallet at once and remembers it.
        assert "Unverified mode ON" in await bot.command(f"/unverified {ROOT} on")
        fetcher.html(
            ROOT, page("Launch is live", TOKEN_A, TOKEN_B, TOKEN_C, LAUNCH, f"Treasury: {WALLET}")
        )
        unverified = await bot.alert("⚠️ Unverified address")
        assert WALLET in unverified.lower() and "re-checking every" in unverified
    finally:
        await bot.stop()

    # A restart restores sites, flags and the unverified memory; nothing is re-announced.
    again = Bot(settings, chain, fetcher)
    await again.start()
    try:
        assert "1/1 site(s) running" in await again.alert("🤖 Bot started")
        site = again.runtime.manager.sites()[0]
        assert site.unverified_enabled and not site.notifications_enabled
        assert any(WALLET in key.lower() for key in site.unverified)
        await again.rounds(2)
        assert not again.alerted("⚠️ Unverified address")
        assert not again.alerted("🔔 Contract found")

        # A challenge page slows the site down with one warning; recovery is announced.
        fetcher.html(ROOT, CHALLENGE, status=403)
        blocked = await again.alert("🚫 Possible IP block")
        assert "HTTP 403" in blocked
        assert "BLOCKED?" in await again.command("/status")
        fetcher.html(ROOT, page("Launch is live", TOKEN_A, TOKEN_B, TOKEN_C, LAUNCH, WALLET))
        await again.alert("answers normally again")

        # Five load timeouts in a row: one 🐢 alert and a stretched timeout, then recovery.
        stuck = FetchResult(ROOT, None, error="timeout after 20s", timed_out=True)
        healthy = FetchResult(ROOT, 200, page("Launch is live", TOKEN_A, TOKEN_B, TOKEN_C, LAUNCH))
        fetcher.script(ROOT, *[stuck] * 5, healthy)
        slow = await again.alert("🐢")
        assert "5 times in a row" in slow and "raised to 40s" in slow  # 2 x PAGE_LOAD_TIMEOUT
        assert "1 page(s) timing out" in await again.command("/status")
        await again.alert("renders again")
        assert 40.0 in fetcher.timeouts
    finally:
        await again.stop()

    final_state = json.loads(state_file.read_text("utf-8"))["sites"][ROOT]
    assert final_state["paused"] is False and final_state["unverified_enabled"] is True
    known = (settings.data_dir / "known_contracts.txt").read_text("utf-8").lower()
    assert all(address in known for address in (TOKEN_A, TOKEN_B, TOKEN_C, LAUNCH))
    assert WALLET not in known
