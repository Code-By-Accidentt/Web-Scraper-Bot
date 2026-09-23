import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from scraper_bot.commands import (
    SETTINGS_USAGE,
    BotCommands,
    TelegramListener,
    TurboRequest,
    parse_command,
    parse_turbo_request,
)
from scraper_bot.markup import strip_markup
from scraper_bot.monitor_manager import MonitorManager
from scraper_bot.preferences import Preferences
from scraper_bot.storage import SnapshotStore, StateStore
from scraper_bot.turbo import TurboManager
from scraper_bot.urls import SubUrlDeriver
from tests.conftest import (
    FakeProber,
    FakeTelegram,
    FakeTuning,
    StubMonitor,
    StubTurboSession,
    make_settings,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/add https://x.io", ("add", "https://x.io")),
        ("/ADD@MyBot  x.io ", ("add", "x.io")),
        ("/list", ("list", "")),
        ("  /help  ", ("help", "")),
        ("hello", None),
        ("/", None),
        ("", None),
    ],
)
def test_parse_command(text: str, expected: tuple[str, str] | None) -> None:
    parsed = parse_command(text)
    assert ((parsed.name, parsed.argument) if parsed else None) == expected


def make(
    tmp_path: Path, allowed: tuple[int, ...] = ()
) -> tuple[MonitorManager, FakeTelegram, BotCommands]:
    manager = MonitorManager(
        state_store=StateStore(tmp_path / "state.json"),
        snapshot_store=SnapshotStore(tmp_path / "snaps"),
        deriver=SubUrlDeriver(("https://app.{domain}",)),
        monitor_factory=StubMonitor,
        restart_delay=0,
    )
    telegram = FakeTelegram()
    commands = BotCommands(manager, telegram, allowed_chat_ids=allowed, alert_chat_ids=(1,))
    return manager, telegram, commands


def update(text: str, chat_id: int = 1, update_id: int = 1) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


async def send(commands: BotCommands, telegram: FakeTelegram, text: str, chat_id: int = 1) -> str:
    await commands.handle_update(update(text, chat_id))
    return strip_markup(telegram.messages[-1][1]) if telegram.messages else ""


async def test_start_and_help(tmp_path: Path) -> None:
    _, telegram, commands = make(tmp_path)
    reply = await send(commands, telegram, "/start", chat_id=77)
    assert "chat id is 77" in reply
    assert "/add <url>" in reply
    assert telegram.messages[-1][0] == 77
    assert "/remove <url>" in await send(commands, telegram, "/help")


async def test_full_site_lifecycle_through_commands(tmp_path: Path) -> None:
    manager, telegram, commands = make(tmp_path)
    await manager.start()
    try:
        assert await send(commands, telegram, "/add Example.com/") == (
            "✅ https://example.com added to monitoring."
        )
        assert manager.is_running("https://example.com")
        assert "already monitored" in await send(commands, telegram, "/add example.com")
        assert "https://example.com [alerts: ON] [RUNNING]" in await send(
            commands, telegram, "/list"
        )

        assert (await send(commands, telegram, "/pause example.com")).startswith(
            "⏸️ https://example.com paused."
        )
        assert "[PAUSED]" in await send(commands, telegram, "/list")
        assert "already paused" in await send(commands, telegram, "/pause example.com")
        assert (
            await send(commands, telegram, "/resume example.com")
            == "▶️ https://example.com resumed."
        )
        assert "is not paused" in await send(commands, telegram, "/resume example.com")

        assert "disabled" in await send(commands, telegram, "/nonotifi example.com")
        assert "[alerts: OFF]" in await send(commands, telegram, "/list")
        assert "enabled" in await send(commands, telegram, "/notify example.com")
        assert "[alerts: ON]" in await send(commands, telegram, "/list")

        assert await send(commands, telegram, "/remove example.com") == (
            "✅ https://example.com removed from monitoring."
        )
        assert "is not monitored" in await send(commands, telegram, "/remove example.com")
        assert "No site is monitored" in await send(commands, telegram, "/list")
    finally:
        await manager.shutdown()


async def test_usage_hints_and_errors(tmp_path: Path) -> None:
    _, telegram, commands = make(tmp_path)
    assert await send(commands, telegram, "/add") == "Usage: /add <url>"
    assert (await send(commands, telegram, "/add not a url")).startswith("❌")
    assert "Unknown command /bogus" in await send(commands, telegram, "/bogus")
    assert "is not monitored" in await send(commands, telegram, "/pause nothing.io")


async def test_unauthorised_chats_and_plain_text_are_ignored(tmp_path: Path) -> None:
    _, telegram, commands = make(tmp_path, allowed=(42,))
    await commands.handle_update(update("/help", chat_id=1))
    await commands.handle_update(update("just chatting", chat_id=42))
    await commands.handle_update({"update_id": 3, "edited_message": {"chat": {"id": 42}}})
    assert telegram.messages == []
    assert "Available commands" in await send(commands, telegram, "/help", chat_id=42)


async def test_listener_dispatches_updates_and_advances_offset(tmp_path: Path) -> None:
    _, telegram, commands = make(tmp_path)
    telegram.updates = [[update("/help", update_id=10), update("/list", update_id=11)], None]
    listener = TelegramListener(telegram, commands, poll_timeout=1, error_backoff=0.01)
    task = asyncio.create_task(listener.run())
    for _ in range(200):
        if len(telegram.messages) >= 2 and len(telegram.offsets) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert [strip_markup(text).split("\n")[0] for _, text in telegram.messages] == [
        "Available commands:",
        "‼️ No site is monitored. Add one with /add <url>.",
    ]
    assert telegram.offsets[:3] == [None, 12, 12]


async def test_status_command_uses_the_provider(tmp_path: Path) -> None:
    manager, telegram, _ = make(tmp_path)
    with_status = BotCommands(manager, telegram, status_provider=lambda: "STATUS TEXT")
    without = BotCommands(manager, telegram)
    assert await send(with_status, telegram, "/status") == "STATUS TEXT"
    assert await send(without, telegram, "/status") == "Status is not available."
    assert "/status" in await send(without, telegram, "/help")


async def test_missing_alert_recipients_are_pointed_out(tmp_path: Path) -> None:
    manager, telegram, _ = make(tmp_path)
    without = BotCommands(manager, telegram, alert_chat_ids=())
    configured = BotCommands(manager, telegram, alert_chat_ids=(1,))
    await manager.start()
    try:
        reply = await send(without, telegram, "/add example.com", chat_id=99)
        assert "TELEGRAM_CHAT_IDS is empty" in reply
        assert "chat id 99" in reply
        assert "TELEGRAM_CHAT_IDS" not in await send(without, telegram, "/help")
        assert "TELEGRAM_CHAT_IDS" not in await send(configured, telegram, "/list")
    finally:
        await manager.shutdown()


NOW = 1_700_000_000.0


def test_parse_turbo_request() -> None:
    assert parse_turbo_request(["x.io"], NOW) == TurboRequest("x.io", None, None)
    assert parse_turbo_request(["x.io", "15"], NOW).minutes == 15
    assert parse_turbo_request(["x.io", "in", "10m"], NOW).start_at == NOW + 600
    assert parse_turbo_request(["x.io", "5", "in", "2h"], NOW).start_at == NOW + 7200
    at = parse_turbo_request(["x.io", "at", "23:59"], NOW).start_at
    assert at is not None and NOW < at <= NOW + 86_400
    for bad in (["x.io", "in", "soon"], ["x.io", "at", "25:00"], ["x.io", "bogus"]):
        with pytest.raises(ValueError):
            parse_turbo_request(bad, NOW)


def make_turbo() -> TurboManager:
    return TurboManager(
        tuning=FakeTuning(turbo_interval=2, turbo_recheck_interval=5),
        deriver=SubUrlDeriver(("https://app.{domain}",)),
        prober=FakeProber({"https://app.example.com": 404}),
        session_factory=StubTurboSession,
    )


async def test_turbo_commands(tmp_path: Path) -> None:
    manager, telegram, _ = make(tmp_path)
    turbo = make_turbo()
    commands = BotCommands(manager, telegram, turbo=turbo)
    try:
        assert "No turbo session" in await send(commands, telegram, "/turbo")
        assert await send(commands, telegram, "/turbo example.com 10") == (
            "🚀 Turbo started for https://example.com: 1 page(s) every 2s for 10 min."
        )
        assert "https://example.com: 0 cycles" in await send(commands, telegram, "/turbo")
        assert "already running" in await send(commands, telegram, "/turbo example.com")
        assert "Turbo limit reached" in await send(commands, telegram, "/turbo other.io")
        assert await send(commands, telegram, "/turbo stop example.com") == (
            "⏹️ Turbo stopped for https://example.com."
        )
        assert "No turbo session for" in await send(commands, telegram, "/turbo stop example.com")
        assert (await send(commands, telegram, "/turbo example.com in 1h")).startswith(
            "⏰ Turbo for https://example.com scheduled at "
        )
        assert "starts at" in await send(commands, telegram, "/turbo")
        assert (await send(commands, telegram, "/turbo x.io bogus")).startswith("❌")
        await send(commands, telegram, "/turbo stop example.com")
        assert "for 1 min." in await send(commands, telegram, "/turbo example.com 0")
        assert await send(commands, telegram, "/turbo stop") == "Usage: /turbo stop <url>"
        assert "/turbo" in await send(commands, telegram, "/help")
    finally:
        await turbo.shutdown()


async def test_turbo_is_optional(tmp_path: Path) -> None:
    _, telegram, commands = make(tmp_path)
    assert await send(commands, telegram, "/turbo") == "Turbo mode is not available."


def with_preferences(tmp_path: Path, chat_ids: tuple[int, ...] = (1,)):
    manager, telegram, _ = make(tmp_path)
    prefs = Preferences(make_settings(tmp_path), tmp_path / "prefs.json")
    commands = BotCommands(manager, telegram, alert_chat_ids=chat_ids, preferences=prefs)
    return manager, telegram, commands


async def test_settings_overview_and_details(tmp_path: Path) -> None:
    _, telegram, commands = with_preferences(tmp_path)
    overview = await send(commands, telegram, "/settings")
    assert "poll_interval_min = 5 s" in overview and "require_erc20 = on" in overview
    details = await send(commands, telegram, "/settings require_erc20")
    assert "Only token-like contracts" in details and "Default from .env: on" in details
    assert (await send(commands, telegram, "/settings nope")).startswith("❌ unknown setting")


async def test_set_validates_confirms_and_persists(tmp_path: Path) -> None:
    _, telegram, commands = with_preferences(tmp_path)
    assert await send(commands, telegram, "/set poll_interval_min 10") == (
        "✅ poll_interval_min: 5 s → 10 s (applies immediately)"
    )
    assert "must not exceed poll_interval_max" in await send(
        commands, telegram, "/set poll_interval_min 20"
    )
    assert "takes on or off" in await send(commands, telegram, "/set deep_scan maybe")
    warning = await send(commands, telegram, "/set require_erc20 off")
    assert warning.startswith("⚠️") and "/set require_erc20 off confirm" in warning
    assert (await send(commands, telegram, "/set require_erc20 off confirm")).startswith("✅")
    assert "require_erc20 = off*" in await send(commands, telegram, "/settings")
    assert (tmp_path / "prefs.json").is_file()
    reset = await send(commands, telegram, "/settings reset all")
    assert "poll_interval_min: 10 s → 5 s" in reset and "require_erc20: off → on" in reset
    assert await send(commands, telegram, "/set") == strip_markup(SETTINGS_USAGE)


async def test_settings_changes_need_an_admin_chat(tmp_path: Path) -> None:
    _, telegram, commands = with_preferences(tmp_path, chat_ids=(1,))
    assert (await send(commands, telegram, "/set poll_interval_min 10", chat_id=99)).startswith(
        "🔒"
    )
    assert (await send(commands, telegram, "/settings reset all", chat_id=99)).startswith("🔒")
    assert "poll_interval_min = 5 s" in await send(commands, telegram, "/settings", chat_id=99)
    _, telegram, nobody = with_preferences(tmp_path, chat_ids=())
    assert "both are empty" in await send(nobody, telegram, "/set poll_interval_min 10")


async def test_unverified_command(tmp_path: Path) -> None:
    manager, telegram, commands = with_preferences(tmp_path)
    await manager.start()
    try:
        assert "off for every site" in await send(commands, telegram, "/unverified")
        await send(commands, telegram, "/add example.com")
        reply = await send(commands, telegram, "/unverified example.com on")
        assert reply.startswith("⚠️ Unverified mode ON for https://example.com")
        assert manager.sites()[0].unverified_enabled is True
        assert "[unverified]" in await send(commands, telegram, "/list")
        assert "• https://example.com" in await send(commands, telegram, "/unverified")
        assert (await send(commands, telegram, "/unverified example.com off")).startswith("✅")
        assert manager.sites()[0].unverified_enabled is False
        denied = await send(commands, telegram, "/unverified example.com on", chat_id=99)
        assert denied.startswith("🔒")
        assert await send(commands, telegram, "/unverified example.com maybe") == (
            "Usage: /unverified <url> on|off"
        )
        assert "is not monitored" in await send(commands, telegram, "/unverified other.io on")
    finally:
        await manager.shutdown()


async def test_replies_carry_html_markup_for_copyable_parts(tmp_path: Path) -> None:
    manager, telegram, commands = make(tmp_path)
    await commands.handle_update(update("/start", chat_id=77))
    raw = telegram.messages[-1][1]
    assert "<code>77</code>" in raw
    assert "<code>/add &lt;url&gt;</code>" in raw
    assert "<url>" not in raw  # angle brackets are escaped, never sent raw
    await manager.start()
    try:
        await commands.handle_update(update("/add example.com"))
        await commands.handle_update(update("/pause example.com"))
        assert "<code>/resume https://example.com</code>" in telegram.messages[-1][1]
    finally:
        await manager.shutdown()


async def test_internal_errors_are_reported_to_the_user(tmp_path: Path) -> None:
    manager, telegram, commands = make(tmp_path)

    async def boom(_url: str):
        raise RuntimeError("disk on fire")

    manager.add_site = boom  # type: ignore[method-assign]
    reply = await send(commands, telegram, "/add example.com")
    assert reply.startswith("⚠️ Internal error while running /add: RuntimeError: disk on fire")
    assert "keeps running" in reply
