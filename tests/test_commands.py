import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from scraper_bot.commands import BotCommands, TelegramListener, parse_command
from scraper_bot.monitor_manager import MonitorManager
from scraper_bot.storage import SnapshotStore, StateStore
from scraper_bot.urls import SubUrlDeriver
from tests.conftest import FakeTelegram, StubMonitor


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
    return telegram.messages[-1][1] if telegram.messages else ""


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

        assert (
            await send(commands, telegram, "/pause example.com") == "⏸️ https://example.com paused."
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
    assert [text.split("\n")[0] for _, text in telegram.messages] == [
        "Available commands:",
        "‼️ No site is monitored. Add one with /add <url>.",
    ]
    assert telegram.offsets[:3] == [None, 12, 12]


async def test_missing_alert_recipients_are_pointed_out(tmp_path: Path) -> None:
    manager, telegram, _ = make(tmp_path)
    without = BotCommands(manager, telegram, alert_chat_ids=())
    configured = BotCommands(manager, telegram, alert_chat_ids=(1,))
    await manager.start()
    try:
        reply = await send(without, telegram, "/add example.com", chat_id=99)
        assert "TELEGRAM_CHAT_IDS is empty" in reply
        assert "(99)" in reply
        assert "TELEGRAM_CHAT_IDS" not in await send(without, telegram, "/help")
        assert "TELEGRAM_CHAT_IDS" not in await send(configured, telegram, "/list")
    finally:
        await manager.shutdown()
