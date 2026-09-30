"""Tests for the composition root: logging, exit codes, heartbeat and the wired runtime.

Everything runs offline: HTTP goes through ``httpx.MockTransport``, Telegram is the
``FakeTelegram`` from conftest and the browser is a scripted ``FakeFetcher``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import signal
import sys
import time
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import httpx
import pytest

from scraper_bot import __version__
from scraper_bot.app import application, cli, notices
from scraper_bot.app import heartbeat as heartbeat_module
from scraper_bot.app import runtime as runtime_module
from scraper_bot.app.application import Application
from scraper_bot.app.cli import configure_logging, main
from scraper_bot.app.heartbeat import Heartbeat
from scraper_bot.app.runtime import HEARTBEAT_FILE, Runtime, _load_address_cache, build_runtime
from scraper_bot.pages.fetching.result import BrowserUnavailable
from scraper_bot.settings.defaults import ChangedDefault
from scraper_bot.settings.env import ConfigError, Settings
from scraper_bot.settings.live import Preferences
from scraper_bot.storage.legacy import LegacyStateImporter
from scraper_bot.telegram.client import TelegramAuthError
from scraper_bot.telegram.commands.help import HELP_TEXT
from scraper_bot.telegram.commands.parsing import ParsedCommand
from scraper_bot.telegram.markup import strip_markup
from scraper_bot.telegram.notifier import Notifier
from tests.conftest import FakeFetcher, FakeTelegram, make_settings

SITE = "https://example.com"
PAGE = "<html><head><title>Example</title></head><body><h1>Hello</h1><p>static</p></body></html>"
EVM_A = "0x1111111111111111111111111111111111111101"
EVM_B = "0x1111111111111111111111111111111111111102"


# ── configure_logging ───────────────────────────────────────────────────────
@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Undo what ``configure_logging`` (``basicConfig(force=True)``) did to the root logger."""
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    noisy = {
        name: logging.getLogger(name).level
        for name in ("httpx", "httpcore", "asyncio", "hpack", "h2")
    }
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in saved_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(saved_level)
    for name, level in noisy.items():
        logging.getLogger(name).setLevel(level)


def test_configure_logging_with_a_log_file(tmp_path: Path, restore_logging: None) -> None:
    log_file = tmp_path / "logs" / "bot.log"
    settings = make_settings(tmp_path, LOG_FILE=str(log_file), LOG_LEVEL="debug")
    assert settings.log_level == "DEBUG"

    configure_logging(settings)

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1
    handler = file_handlers[0]
    assert Path(handler.baseFilename).resolve() == log_file.resolve()
    assert handler.maxBytes == 5_000_000 and handler.backupCount == 3
    assert any(isinstance(h, logging.StreamHandler) and h is not handler for h in root.handlers)
    # hpack would log the request path of every HTTP/2 call: the bot token is in it
    for name in ("httpx", "httpcore", "asyncio", "hpack", "h2"):
        assert logging.getLogger(name).level == logging.WARNING

    logging.getLogger("scraper_bot.test_app").debug("hello from the test")
    handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "DEBUG    scraper_bot.test_app: hello from the test" in text
    with pytest.raises(ConfigError, match="LOG_FILE"):  # exit code 2, not a traceback
        configure_logging(make_settings(tmp_path, LOG_FILE=str(log_file / "inside-a-file.log")))


def test_configure_logging_without_a_log_file(tmp_path: Path, restore_logging: None) -> None:
    settings = make_settings(tmp_path, LOG_LEVEL="warning")
    assert settings.log_file is None

    configure_logging(settings)

    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert not any(isinstance(h, RotatingFileHandler) for h in root.handlers)
    assert len(root.handlers) == 1
    assert not (tmp_path / "data").exists()  # nothing was written anywhere


# ── main() exit codes ───────────────────────────────────────────────────────
@pytest.fixture
def quiet_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """``main()`` without touching ``.env``, the data directory or the logging setup."""
    settings = make_settings(tmp_path)
    monkeypatch.setattr(
        cli.Settings, "from_env", classmethod(lambda cls, *args, **kwargs: settings)
    )
    monkeypatch.setattr(cli, "configure_logging", lambda settings: None)
    return settings


def run_raising(exc: BaseException | None) -> Any:
    async def run(self: Application) -> None:
        if exc is not None:
            raise exc

    return run


def test_main_returns_2_on_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def from_env(cls: type[Settings], *args: Any, **kwargs: Any) -> Settings:
        raise ConfigError("TELEGRAM_BOT_TOKEN is required")

    monkeypatch.setattr(cli.Settings, "from_env", classmethod(from_env))
    monkeypatch.setattr(cli, "configure_logging", lambda settings: None)
    monkeypatch.setattr(cli.Application, "run", run_raising(AssertionError("not reached")))

    assert main() == 2
    assert "Configuration error: TELEGRAM_BOT_TOKEN is required" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (TelegramAuthError("Telegram rejected TELEGRAM_BOT_TOKEN"), 2),
        (BrowserUnavailable("browser could not start"), 3),
        (RuntimeError("boom"), 1),
        (KeyboardInterrupt(), 0),
        (asyncio.CancelledError(), 0),  # SIGTERM
        (None, 0),
    ],
)
def test_main_exit_codes(
    monkeypatch: pytest.MonkeyPatch, quiet_main: Settings, exc: BaseException | None, code: int
) -> None:
    monkeypatch.setattr(cli.Application, "run", run_raising(exc))
    assert main() == code


@pytest.mark.skipif(sys.platform == "win32", reason="the Windows event loop takes no signals")
async def test_sigterm_shuts_the_bot_down_like_ctrl_c() -> None:
    started, cleaned = asyncio.Event(), []

    class Bot:
        async def run(self) -> None:
            started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                cleaned.append("alerts drained")

    serving = asyncio.create_task(cli._serve(Bot()))  # type: ignore[arg-type]
    await started.wait()
    signal.raise_signal(signal.SIGTERM)
    with pytest.raises(asyncio.CancelledError):
        await serving
    assert cleaned == ["alerts drained"]
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # a second SIGTERM kills


def test_main_logs_the_failure_reason(
    monkeypatch: pytest.MonkeyPatch, quiet_main: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="scraper_bot.app")
    monkeypatch.setattr(cli.Application, "run", run_raising(RuntimeError("kaboom")))
    assert main() == 1
    assert "fatal error" in caplog.text and "kaboom" in caplog.text
    assert f"scraper-bot {__version__} starting" in caplog.text

    caplog.clear()
    monkeypatch.setattr(cli.Application, "run", run_raising(KeyboardInterrupt()))
    assert main() == 0
    assert "interrupted; shutting down" in caplog.text


def test_main_warns_only_when_no_chat_ids_are_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="scraper_bot.app")
    configured = make_settings(tmp_path)
    silent = make_settings(tmp_path, TELEGRAM_CHAT_IDS="")
    assert silent.chat_ids == ()
    monkeypatch.setattr(cli, "configure_logging", lambda settings: None)
    monkeypatch.setattr(cli.Application, "run", run_raising(None))

    monkeypatch.setattr(
        cli.Settings, "from_env", classmethod(lambda cls, *args, **kwargs: configured)
    )
    assert main() == 0
    assert "TELEGRAM_CHAT_IDS is empty" not in caplog.text

    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls, *args, **kwargs: silent))
    assert main() == 0
    assert "TELEGRAM_CHAT_IDS is empty" in caplog.text
    assert not silent.data_dir.exists()  # Application.run was stubbed: nothing on disk


def test_main_warns_about_defaults_of_an_earlier_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="scraper_bot.app")
    outdated = dataclasses.replace(
        make_settings(tmp_path),
        outdated_defaults=(ChangedDefault("DEEP_SCAN", "true", "false"),),
    )
    monkeypatch.setattr(
        cli.Settings, "from_env", classmethod(lambda cls, *args, **kwargs: outdated)
    )
    monkeypatch.setattr(cli, "configure_logging", lambda settings: None)
    monkeypatch.setattr(cli.Application, "run", run_raising(None))
    assert main() == 0
    assert "DEEP_SCAN=true is the default of an earlier release" in caplog.text
    assert "the default is now false" in caplog.text


# ── Heartbeat ───────────────────────────────────────────────────────────────
async def cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_heartbeat_writes_the_epoch_seconds(tmp_path: Path) -> None:
    path = tmp_path / "hb" / "heartbeat"
    before = time.time()
    task = asyncio.create_task(Heartbeat(path, interval=0.01).run())
    try:
        for _ in range(200):
            if path.is_file() and path.read_text(encoding="utf-8").strip():
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("heartbeat file was never written")
    finally:
        await cancel(task)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    stamp = int(text.strip())
    assert before - 1 <= stamp <= time.time() + 1
    assert not list(path.parent.glob(".*.tmp"))  # atomic write left no temp file behind


async def test_heartbeat_keeps_running_when_the_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="scraper_bot.app")
    attempts: list[str] = []

    def failing_write(path: Path, text: str) -> None:
        attempts.append(text)
        raise OSError("disk full")

    monkeypatch.setattr(heartbeat_module, "atomic_write_text", failing_write)
    path = tmp_path / "heartbeat"
    reports: list[tuple[str, ...]] = []
    task = asyncio.create_task(
        Heartbeat(path, interval=0.001, report=lambda *args: reports.append(args)).run()
    )
    try:
        for _ in range(200):
            if len(attempts) >= 3:
                break
            await asyncio.sleep(0.005)
    finally:
        await cancel(task)
    assert len(attempts) >= 3
    assert not path.exists()
    assert "heartbeat write failed: disk full" in caplog.text
    # The chat hears it once: without a heartbeat the supervisor restarts the bot every 5 min.
    [(key, _title, detail, hint)] = reports
    assert (key, detail) == ("heartbeat", "disk full") and str(tmp_path) in hint


# ── Application._check_token ────────────────────────────────────────────────
class ProfileTelegram:
    def __init__(self, profile: dict[str, Any] | None) -> None:
        self.profile = profile

    async def get_me(self) -> dict[str, Any] | None:
        return self.profile


async def test_check_token_warns_when_telegram_is_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="scraper_bot.app")
    await Application._check_token(ProfileTelegram(None))  # type: ignore[arg-type]
    assert "could not verify the Telegram token now; continuing anyway" in caplog.text
    assert caplog.records[-1].levelno == logging.WARNING


async def test_check_token_logs_the_bot_identity(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="scraper_bot.app")
    profile = {"username": "scraper_bot", "id": 424242, "is_bot": True}
    await Application._check_token(ProfileTelegram(profile))  # type: ignore[arg-type]
    assert "Telegram bot @scraper_bot (id 424242)" in caplog.text
    assert caplog.records[-1].levelno == logging.INFO


# ── _load_address_cache ─────────────────────────────────────────────────────
def prefs_for(settings: Settings) -> Preferences:
    prefs = Preferences(settings, settings.data_dir / "preferences.json")
    prefs.load()
    return prefs


def test_load_address_cache_imports_legacy_wallets_on_first_run(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "found_wallets.txt").write_text(f"{EVM_A}\n\nnot an address\n", encoding="utf-8")
    known_path = settings.data_dir / "known_contracts.txt"
    assert not known_path.exists()

    cache = _load_address_cache(settings, prefs_for(settings), LegacyStateImporter(legacy_dir))

    assert cache.known_count == 1 and cache.rejected_count == 0
    assert EVM_A in known_path.read_text(encoding="utf-8")
    assert "legacy" in known_path.read_text(encoding="utf-8")


def test_load_address_cache_skips_the_import_once_the_cache_exists(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "found_wallets.txt").write_text(f"{EVM_A}\n", encoding="utf-8")
    settings.data_dir.mkdir(parents=True)
    known_path = settings.data_dir / "known_contracts.txt"
    known_path.write_text("", encoding="utf-8")  # an existing (even empty) cache wins

    cache = _load_address_cache(settings, prefs_for(settings), LegacyStateImporter(legacy_dir))

    assert cache.known_count == 0
    assert known_path.read_text(encoding="utf-8") == ""


def test_load_address_cache_imports_only_once(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    wallets = legacy_dir / "found_wallets.txt"
    wallets.write_text(f"{EVM_A}\n", encoding="utf-8")
    prefs = prefs_for(settings)
    legacy = LegacyStateImporter(legacy_dir)

    first = _load_address_cache(settings, prefs, legacy)
    wallets.write_text(f"{EVM_A}\n{EVM_B}\n", encoding="utf-8")
    second = _load_address_cache(settings, prefs, legacy)

    assert first.known_count == 1
    assert second.known_count == 1  # EVM_B is not picked up: the cache already existed


def test_load_address_cache_without_legacy_data(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    cache = _load_address_cache(settings, prefs_for(settings), LegacyStateImporter(tmp_path / "x"))
    assert cache.known_count == 0
    assert not (settings.data_dir / "known_contracts.txt").exists()


# ── build_runtime / Runtime ─────────────────────────────────────────────────
class OfflineTransport:
    """MockTransport handler: probes miss, RPC nodes see no code, DexScreener knows nothing."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x"})
        if "dexscreener" in request.url.host:
            return httpx.Response(200, json=[])
        return httpx.Response(404)  # HEAD probes and their GET follow-ups


class Wired:
    """A runtime built through ``build_runtime`` over the offline adapters."""

    def __init__(self, tmp_path: Path, http: httpx.AsyncClient, transport: OfflineTransport):
        self.settings = make_settings(tmp_path)
        self.prefs = prefs_for(self.settings)
        self.telegram = FakeTelegram()
        self.fetcher = FakeFetcher()
        self.fetcher.html(SITE, PAGE)
        self.transport = transport
        self.notifier = Notifier(self.telegram, self.settings.chat_ids)  # type: ignore[arg-type]
        self.runtime: Runtime = build_runtime(
            self.settings,
            self.prefs,
            http=http,
            telegram=self.telegram,  # type: ignore[arg-type]
            notifier=self.notifier,
            fetcher=self.fetcher,
            legacy_dir=tmp_path / "legacy",
            sleep=lambda seconds: asyncio.sleep(0.01),
        )

    def texts(self) -> list[str]:
        return [strip_markup(text) for _, text in self.telegram.messages]

    async def wait_for(self, condition: Any, what: str) -> None:
        for _ in range(500):
            if condition():
                return
            await asyncio.sleep(0.01)
        pytest.fail(f"timed out waiting for {what}")


@pytest.fixture
async def wired(tmp_path: Path) -> Any:
    transport = OfflineTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        yield Wired(tmp_path, http, transport)


async def test_build_runtime_wires_a_working_bot(wired: Wired) -> None:
    runtime, manager, telegram = wired.runtime, wired.runtime.manager, wired.telegram
    try:
        await runtime.start()
        assert [chat for chat, _ in telegram.messages] == [1, 2]
        started = wired.texts()[0]
        assert started.startswith(f"🤖 Bot started (v{__version__}): 0/0 site(s) running.")

        assert await runtime.commands.execute(1, ParsedCommand("help", "")) == HELP_TEXT

        reply = await runtime.commands.execute(1, ParsedCommand("add", "example.com"))
        assert strip_markup(reply) == f"✅ {SITE} added to monitoring."
        await wired.wait_for(lambda: manager.is_running(SITE), "the monitor to start")

        await wired.wait_for(
            lambda: (manager.stats(SITE) or None) is not None and manager.stats(SITE).rounds >= 2,  # type: ignore[union-attr]
            "two monitor rounds",
        )
        assert wired.fetcher.calls.count(SITE) >= 2
        assert wired.fetcher.calls == [SITE] * len(wired.fetcher.calls)  # sub-pages were dead

        probes = {(r.method, r.url.host) for r in wired.transport.requests}
        assert ("HEAD", "app.example.com") in probes and ("GET", "app.example.com") in probes
        assert all(r.method in {"HEAD", "GET"} for r in wired.transport.requests)

        status = await runtime.commands.execute(1, ParsedCommand("status", ""))
        assert "Browser: connected" in status
        assert "Sites: 1 total, 1 running, 0 paused" in status

        listed = strip_markup(await runtime.commands.execute(1, ParsedCommand("list", "")))
        assert f"• {SITE} [alerts: ON] [RUNNING]" in listed

        state = json.loads((wired.settings.data_dir / "state.json").read_text(encoding="utf-8"))
        assert list(state["sites"]) == [SITE]
        assert list((wired.settings.data_dir / "snapshots").glob("*.json"))
    finally:
        await runtime.shutdown()

    assert not manager.is_running(SITE)
    assert wired.texts() == [wired.texts()[0]] * 2  # a static page raises no alert


async def test_runtime_serve_can_be_cancelled(wired: Wired) -> None:
    runtime, manager, telegram = wired.runtime, wired.runtime.manager, wired.telegram
    telegram.updates = [
        [{"update_id": 7, "message": {"chat": {"id": 1}, "text": f"/add {SITE}"}}],
    ]

    task = asyncio.create_task(runtime.serve(), name="serve")
    await wired.wait_for(lambda: bool(telegram.offsets), "the listener to poll once")
    await wired.wait_for(lambda: manager.is_running(SITE), "the /add update to be handled")
    await wired.wait_for(
        lambda: (wired.settings.data_dir / HEARTBEAT_FILE).is_file(), "the heartbeat file"
    )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not manager.is_running(SITE)  # manager.shutdown() ran in serve()'s finally
    assert [site.url for site in manager.sites()] == [SITE]
    assert telegram.offsets[0] is None and 8 in telegram.offsets
    replies = wired.texts()
    assert replies[0].startswith("🤖 Bot started")
    assert f"✅ {SITE} added to monitoring." in replies
    assert runtime.turbo.active() == [] and runtime.turbo.scheduled() == []


async def test_a_stop_during_the_start_still_shuts_everything_down(wired: Wired) -> None:
    runtime = wired.runtime

    async def hanging_check() -> list[str]:
        await asyncio.Event().wait()  # SIGTERM arrives during the chain-id check
        return []

    runtime.warmer.wrong_chains = hanging_check  # type: ignore[method-assign]
    task = asyncio.create_task(runtime.serve())
    await wired.wait_for(lambda: bool(wired.telegram.messages), "the start message")
    assert runtime._pending_task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime._pending_task is None  # shutdown() ran: the watch stopped and saved
    assert (wired.settings.data_dir / runtime_module.PENDING_FILE).is_file()


async def test_a_hanging_shutdown_step_is_bounded_and_pending_is_saved_first(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "SHUTDOWN_STEP", 0.05)
    runtime = wired.runtime
    await runtime.start()

    saved_before: list[bool] = []

    async def hanging() -> None:
        saved_before.append((wired.settings.data_dir / runtime_module.PENDING_FILE).is_file())
        await asyncio.Event().wait()  # Telegram does not answer the turbo summary

    runtime.turbo.shutdown = hanging  # type: ignore[method-assign]
    await asyncio.wait_for(runtime.shutdown(), timeout=5)
    assert saved_before == [True]  # saved before the step that hung


def test_http2_falls_back_when_h2_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_settings(tmp_path)
    assert application._http2(settings) is True
    assert application._http2(make_settings(tmp_path, HTTP2="false")) is False
    monkeypatch.setitem(sys.modules, "h2", None)  # an update without pip install
    with caplog.at_level(logging.WARNING):
        assert application._http2(settings) is False
    assert "h2 package is missing" in caplog.text


def test_old_env_values_reach_the_chat_once_per_set(tmp_path: Path) -> None:
    marker = tmp_path / "outdated.txt"
    old = (ChangedDefault("DEEP_SCAN", "true", "false"),)
    assert notices.outdated_notice_due(marker, old) is True
    assert notices.outdated_notice_due(marker, old) is True  # not delivered: ask again
    notices.remember_outdated_notice(marker, old)
    assert notices.outdated_notice_due(marker, old) is False  # a restart: log only
    more = (*old, ChangedDefault("RPC_RETRY_DELAY", "600", "10"))
    assert notices.outdated_notice_due(marker, more) is True
    assert notices.outdated_notice_due(marker, ()) is False and not marker.exists()
    assert notices.outdated_notice_due(marker, old) is True  # fixed, then back again
    marker.write_bytes("DEEP_SCAN=true\n".encode("utf-16"))  # re-saved by Notepad
    assert notices.outdated_notice_due(marker, old) is True  # no crash


async def test_the_outdated_notice_counts_as_sent_only_when_a_chat_got_it() -> None:
    old = (ChangedDefault("DEEP_SCAN", "true", "false"),)
    telegram = FakeTelegram()
    assert await Notifier(telegram, (1,)).outdated_defaults(old) is True  # type: ignore[arg-type]
    assert await Notifier(telegram, ()).outdated_defaults(old) is False  # type: ignore[arg-type]


def test_new_networks_are_those_the_saved_verdicts_never_saw(tmp_path: Path) -> None:
    from scraper_bot.app.runtime import added_networks, remember_networks

    path = tmp_path / "evm_networks.txt"
    current = ("Ethereum", "Base", "Arc")
    assert added_networks(path, current, existing_data=False) == frozenset()  # fresh install
    assert added_networks(path, current, existing_data=True) == frozenset(current)  # from 2.1
    remember_networks(path, ("Ethereum", "Base"))
    assert added_networks(path, current, existing_data=True) == {"Arc"}
    remember_networks(path, current)
    assert added_networks(path, current, existing_data=True) == frozenset()
    assert added_networks(path, ("Ethereum",), existing_data=True) == frozenset()  # one less


async def test_an_upgrade_that_adds_networks_forgets_old_evm_verdicts(tmp_path: Path) -> None:
    from scraper_bot.models.addresses import Address, AddressKind, Chain, ValidationResult
    from scraper_bot.models.addresses import ValidationStatus as Status

    settings = make_settings(tmp_path)
    data = settings.data_dir
    data.mkdir(parents=True)
    wallet = Address(Chain.ETHEREUM, "0x" + "4d" * 20)
    mint = Address(Chain.SOLANA, "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump")
    # What a 2.1 run left behind: rejections made without Arc and Robinhood Chain.
    (data / "rejected_addresses.txt").write_text(
        f"Ethereum\t{wallet.value}\t{time.time():.0f}\nSolana\t{mint.value}\t{time.time():.0f}\n",
        encoding="utf-8",
    )
    transport = OfflineTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        first = Wired(tmp_path, http, transport)
        first.runtime.pending.offer(
            SITE, SITE, None, ValidationResult(wallet, Status.REJECTED, kind=AddressKind.NO_CODE)
        )
        first.runtime.pending.save()
        second = Wired(tmp_path, http, transport)  # the next start, with no networks file yet
        runtime = second.runtime
        assert not runtime.validator._cache.is_rejected(wallet)  # EVM: checked again
        assert runtime.validator._cache.is_rejected(mint)  # Solana is not affected
        try:
            await runtime.start()
            assert runtime.pending._entries[wallet].baseline  # old news if on a new network
            names = (data / "evm_networks.txt").read_text(encoding="utf-8").splitlines()
            assert sorted(names) == sorted(net.name for net in settings.evm_networks)
        finally:
            await runtime.shutdown()
        third = Wired(tmp_path, http, transport)  # a later start forgets nothing
        assert third.runtime.pending._new_networks == frozenset()


async def test_a_site_only_in_turbo_keeps_its_addresses_watched(tmp_path: Path) -> None:
    transport = OfflineTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        runtime = Wired(tmp_path, http, transport).runtime
        await runtime.start()
        try:
            await runtime.manager.add_site(SITE)
            await runtime.manager.pause_site(SITE)
            assert runtime.pending._is_paused(SITE)
            # /turbo works on a URL that is not monitored; the launch may come after it.
            assert not runtime.pending._is_paused("https://turbo-only.example")
        finally:
            await runtime.shutdown()


class SandboxState:
    def __init__(self, sandboxed: bool, error: str | None = None) -> None:
        self.sandboxed = sandboxed
        self.sandbox_error = error


async def test_the_chat_hears_once_that_chromium_runs_without_its_sandbox(tmp_path: Path) -> None:
    from scraper_bot.app.notices import tell_about_sandbox
    from tests.conftest import FakeNotifier

    notifier = FakeNotifier()
    marker = tmp_path / "no_sandbox_notice.txt"
    unsandboxed = SandboxState(False, "No usable sandbox!")
    await tell_about_sandbox(notifier, unsandboxed, marker)  # type: ignore[arg-type]
    await tell_about_sandbox(notifier, unsandboxed, marker)  # type: ignore[arg-type]
    [(key, title, detail, hint)] = notifier.problems
    assert (key, title, detail) == (
        "sandbox",
        "Chromium runs without its sandbox",
        unsandboxed.sandbox_error,
    )
    assert hint is not None and "non-root user" in hint
    await tell_about_sandbox(notifier, SandboxState(False), marker)  # type: ignore[arg-type]
    assert len(notifier.problems) == 1  # switched off on purpose in BROWSER_ARGS
    await tell_about_sandbox(notifier, SandboxState(True), marker)  # type: ignore[arg-type]
    assert not marker.exists()  # works again: a later failure is reported anew
    await tell_about_sandbox(notifier, unsandboxed, marker)  # type: ignore[arg-type]
    assert len(notifier.problems) == 2


async def test_the_sandbox_notice_is_remembered_only_when_a_chat_got_it(tmp_path: Path) -> None:
    from scraper_bot.app.notices import tell_about_sandbox
    from tests.conftest import FakeNotifier

    class Unreachable(FakeNotifier):
        async def problem(self, key: str, title: str, detail: str, hint: str | None = None) -> bool:
            await super().problem(key, title, detail, hint)
            return False  # Telegram down, or no alert chat configured yet

    marker = tmp_path / "no_sandbox_notice.txt"
    notifier = Unreachable()
    await tell_about_sandbox(notifier, SandboxState(False, "No usable sandbox!"), marker)  # type: ignore[arg-type]
    assert not marker.exists()  # the next start tries again


async def test_late_tokens_reach_the_pending_watch(tmp_path: Path) -> None:
    transport = OfflineTransport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        runtime = Wired(tmp_path, http, transport).runtime
        assert runtime.validator.on_late_token == runtime.pending.hurry
        assert runtime.pending._stale_absences == frozenset()  # a fresh install
