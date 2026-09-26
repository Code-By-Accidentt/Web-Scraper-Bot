import asyncio
import contextlib
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from scraper_bot.models import ChangeCounts, LastChange, SiteState
from scraper_bot.monitoring.monitor_manager import MonitorError, MonitorManager
from scraper_bot.monitoring.turbo import TurboError, TurboManager
from scraper_bot.pages.urls import SubUrlDeriver
from scraper_bot.preferences import Preferences
from scraper_bot.storage import SnapshotStore, StateStore
from scraper_bot.telegram.commands import (
    SETTINGS_USAGE,
    BotCommands,
    TelegramListener,
    TurboRequest,
    parse_command,
    parse_turbo_request,
)
from scraper_bot.telegram.markup import strip_markup
from scraper_bot.telegram.timefmt import format_clock
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
    reply = await send(commands, telegram, "/start")
    assert "chat id is 1" in reply
    assert "/add <url>" in reply
    assert "/remove <url>" in await send(commands, telegram, "/help")


async def test_strangers_only_learn_their_chat_id_and_only_now_and_then(tmp_path: Path) -> None:
    manager, telegram, _ = make(tmp_path)
    now = [1000.0]
    commands = BotCommands(manager, telegram, alert_chat_ids=(1,), clock=lambda: now[0])
    reply = await send(commands, telegram, "/start", chat_id=77)
    assert "chat id is 77" in reply and "cannot control the bot" in reply
    assert "/add <url>" not in reply  # no command list for strangers
    assert telegram.messages[-1][0] == 77
    await commands.handle_update(update("/start", chat_id=77))
    await commands.handle_update(update("/list", chat_id=77))
    assert len(telegram.messages) == 1  # throttled, and other commands are ignored
    now[0] += 601
    await commands.handle_update(update("/start", chat_id=77))
    assert len(telegram.messages) == 2


async def test_the_stranger_reply_names_the_list_that_grants_commands(tmp_path: Path) -> None:
    manager, telegram, _ = make(tmp_path)
    open_alerts = BotCommands(manager, telegram, alert_chat_ids=(1,))
    assert "TELEGRAM_CHAT_IDS" in await send(open_alerts, telegram, "/start", chat_id=77)
    with_allowlist = BotCommands(manager, telegram, allowed_chat_ids=(2,), alert_chat_ids=(1,))
    reply = await send(with_allowlist, telegram, "/start", chat_id=78)
    assert "TELEGRAM_ALLOWED_CHAT_IDS" in reply and "TELEGRAM_CHAT_IDS (" not in reply


async def test_without_any_chat_ids_only_start_answers(tmp_path: Path) -> None:
    manager, telegram, _ = make(tmp_path)
    nobody = BotCommands(manager, telegram)
    await nobody.handle_update(update("/help", chat_id=5))
    await nobody.handle_update(update("/add example.com", chat_id=5))
    assert telegram.messages == []
    assert "chat id is 5" in await send(nobody, telegram, "/start", chat_id=5)
    assert manager.sites() == []


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
    with_status = BotCommands(
        manager, telegram, alert_chat_ids=(1,), status_provider=lambda: "STATUS TEXT"
    )
    without = BotCommands(manager, telegram, alert_chat_ids=(1,))
    assert await send(with_status, telegram, "/status") == "STATUS TEXT"
    assert await send(without, telegram, "/status") == "Status is not available."
    assert "/status" in await send(without, telegram, "/help")


async def test_missing_alert_recipients_are_pointed_out(tmp_path: Path) -> None:
    manager, telegram, _ = make(tmp_path)
    # Reachable only with an allowlist: without it, nobody could issue the command.
    without = BotCommands(manager, telegram, allowed_chat_ids=(99,), alert_chat_ids=())
    configured = BotCommands(manager, telegram, alert_chat_ids=(1,))
    await manager.start()
    try:
        reply = await send(without, telegram, "/add example.com", chat_id=99)
        assert "TELEGRAM_CHAT_IDS is empty" in reply
        assert "chat id 99" in reply
        assert "TELEGRAM_CHAT_IDS" not in await send(without, telegram, "/help", chat_id=99)
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
    commands = BotCommands(manager, telegram, alert_chat_ids=(1,), turbo=turbo)
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
    assert "poll_interval_min = 4 s" in overview and "require_erc20 = on" in overview
    details = await send(commands, telegram, "/settings require_erc20")
    assert "Only token-like contracts" in details and "Default from .env: on" in details
    assert (await send(commands, telegram, "/settings nope")).startswith("❌ unknown setting")


async def test_set_validates_confirms_and_persists(tmp_path: Path) -> None:
    _, telegram, commands = with_preferences(tmp_path)
    assert await send(commands, telegram, "/set poll_interval_min 10") == (
        "✅ poll_interval_min: 4 s → 10 s (applies immediately)"
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
    assert "poll_interval_min: 10 s → 4 s" in reset and "require_erc20: off → on" in reset
    assert await send(commands, telegram, "/set") == strip_markup(SETTINGS_USAGE)


async def test_settings_cannot_be_read_or_changed_from_other_chats(tmp_path: Path) -> None:
    _, telegram, commands = with_preferences(tmp_path, chat_ids=(1,))
    for text in ("/set poll_interval_min 10", "/settings reset all", "/settings"):
        await commands.handle_update(update(text, chat_id=99))
    assert telegram.messages == []
    assert "poll_interval_min = 4 s" in await send(commands, telegram, "/settings")
    _, telegram, nobody = with_preferences(tmp_path, chat_ids=())
    await nobody.handle_update(update("/set poll_interval_min 10"))
    assert telegram.messages == []
    assert not (tmp_path / "prefs.json").exists()


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
        replies = len(telegram.messages)
        await commands.handle_update(update("/unverified example.com on", chat_id=99))
        assert len(telegram.messages) == replies  # another chat is ignored
        assert manager.sites()[0].unverified_enabled is False
        assert await send(commands, telegram, "/unverified example.com maybe") == (
            "Usage: /unverified <url> on|off"
        )
        assert "is not monitored" in await send(commands, telegram, "/unverified other.io on")
    finally:
        await manager.shutdown()


async def test_replies_carry_html_markup_for_copyable_parts(tmp_path: Path) -> None:
    manager, telegram, commands = make(tmp_path)
    await commands.handle_update(update("/start", chat_id=77))
    assert "<code>77</code>" in telegram.messages[-1][1]
    await commands.handle_update(update("/start"))
    raw = telegram.messages[-1][1]
    assert "<code>1</code>" in raw
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


async def test_mute_and_unmute_aliases(tmp_path: Path) -> None:
    manager, telegram, commands = make(tmp_path)
    await manager.start()
    try:
        await manager.add_site("https://example.com")
        assert await send(commands, telegram, "/mute example.com") == (
            "🔇 Change alerts disabled for https://example.com."
        )
        assert manager.sites()[0].notifications_enabled is False
        assert await send(commands, telegram, "/unmute example.com") == (
            "🔔 Change alerts enabled for https://example.com."
        )
        assert manager.sites()[0].notifications_enabled is True
    finally:
        await manager.shutdown()


async def test_usage_hints_for_every_url_command(tmp_path: Path) -> None:
    _, telegram, commands = make(tmp_path)
    for name in ("remove", "pause", "resume", "notify", "nonotifi"):
        assert await send(commands, telegram, f"/{name}") == f"Usage: /{name} <url>"
    # The aliases share their handler, so they answer with the canonical usage line.
    assert await send(commands, telegram, "/mute") == "Usage: /nonotifi <url>"
    assert await send(commands, telegram, "/unmute") == "Usage: /notify <url>"


async def test_list_shows_stopped_blocked_and_last_change(tmp_path: Path) -> None:
    def factory(site: SiteState, persist: Any, on_auto_pause: Any) -> StubMonitor:
        return StubMonitor(site, persist, on_auto_pause, crash=site.url == "https://crash.io")

    manager = MonitorManager(
        state_store=StateStore(tmp_path / "state.json"),
        snapshot_store=SnapshotStore(tmp_path / "snaps"),
        deriver=SubUrlDeriver(()),
        monitor_factory=factory,
        restart_delay=0,  # a crashed monitor stays down: the site reads as STOPPED
    )
    telegram = FakeTelegram()
    commands = BotCommands(manager, telegram, alert_chat_ids=(1,))
    changed_at = 1_700_000_000.0
    await manager.start()
    try:
        await manager.add_site("https://crash.io")
        blocked = await manager.add_site("https://blocked.io")
        changed = await manager.add_site("https://changed.io")
        stats = manager.stats(blocked.url)
        assert stats is not None
        stats.block_level, stats.block_reason = 1, "HTTP 403"
        changed.last_change = LastChange(changed_at, changed.url, ChangeCounts(added=1))
        for _ in range(100):
            if not manager.is_running("https://crash.io"):
                break
            await asyncio.sleep(0.01)
        reply = await send(commands, telegram, "/list")
    finally:
        await manager.shutdown()
    assert reply.split("\n") == [
        "➡️ Monitored sites (3):",
        "• https://blocked.io [alerts: ON] [RUNNING] ⚠️ BLOCKED? (HTTP 403)",
        f"• https://changed.io [alerts: ON] [RUNNING] last change: {format_clock(changed_at)}",
        "• https://crash.io [alerts: ON] [STOPPED]",
    ]


async def test_settings_reset_with_nothing_overridden(tmp_path: Path) -> None:
    _, telegram, commands = with_preferences(tmp_path)
    assert (await send(commands, telegram, "/settings reset all")).startswith(
        "Nothing to reset: every setting already has its .env value."
    )
    assert (await send(commands, telegram, "/settings reset poll_interval_min")).startswith(
        "Nothing to reset"
    )


async def test_settings_and_set_without_preferences_wired(tmp_path: Path) -> None:
    _, telegram, commands = make(tmp_path)
    for text in ("/settings", "/settings reset all", "/set poll_interval_min 10"):
        assert await send(commands, telegram, text) == "Settings are not available."


async def test_missing_recipients_warning_on_list_and_resume_but_not_pause(
    tmp_path: Path,
) -> None:
    manager, telegram, _ = make(tmp_path)
    commands = BotCommands(manager, telegram, allowed_chat_ids=(7,), alert_chat_ids=())
    warning = "⚠️ TELEGRAM_CHAT_IDS is empty"
    await manager.start()
    try:
        await manager.add_site("https://example.com")
        listed = await send(commands, telegram, "/list", chat_id=7)
        assert listed.startswith("➡️ Monitored sites (1):") and warning in listed
        paused = await send(commands, telegram, "/pause example.com", chat_id=7)
        assert paused.startswith("⏸️ https://example.com paused.") and warning not in paused
        resumed = await send(commands, telegram, "/resume example.com", chat_id=7)
        assert resumed.startswith("▶️ https://example.com resumed.") and warning in resumed
        assert "chat id 7" in resumed
    finally:
        await manager.shutdown()


async def test_updates_without_text_or_int_chat_are_ignored(tmp_path: Path) -> None:
    _, telegram, commands = make(tmp_path)
    await commands.handle_update({"update_id": 1, "message": {"chat": {"id": 1}}})
    await commands.handle_update(
        {"update_id": 2, "message": {"chat": {"id": "1"}, "text": "/help"}}
    )
    await commands.handle_update({"update_id": 3, "message": {"chat": {"id": 1}, "text": 42}})
    await commands.handle_update({"update_id": 4, "message": "/help"})
    await commands.handle_update({"update_id": 5})
    assert telegram.messages == []
    assert "Available commands" in await send(commands, telegram, "/help")


class FlakyCommands:
    """Stands in for BotCommands: handling the first update blows up, the rest go through."""

    def __init__(self, inner: BotCommands) -> None:
        self.inner = inner
        self.calls = 0

    async def handle_update(self, update: dict[str, Any]) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("handler exploded")
        await self.inner.handle_update(update)


async def test_listener_survives_a_failing_handler(tmp_path: Path) -> None:
    _, telegram, commands = make(tmp_path)
    flaky = FlakyCommands(commands)
    telegram.updates = [[update("/help", update_id=10)], [update("/list", update_id=11)]]
    listener = TelegramListener(telegram, flaky, poll_timeout=1, error_backoff=0.01)  # type: ignore[arg-type]
    task = asyncio.create_task(listener.run())
    for _ in range(200):
        if telegram.messages and len(telegram.offsets) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert flaky.calls == 2
    assert [strip_markup(text).split("\n")[0] for _, text in telegram.messages] == [
        "‼️ No site is monitored. Add one with /add <url>."
    ]
    assert telegram.offsets[:3] == [None, 11, 12]  # the failed update is not fetched again


def test_turbo_at_clock_time_rolls_over_to_tomorrow() -> None:
    now = datetime(2024, 2, 13, 12, 0).timestamp()
    tomorrow = datetime(2024, 2, 14, 11, 30).timestamp()
    assert parse_turbo_request(["x.io", "at", "11:30"], now).start_at == tomorrow
    # Exactly "now" counts as past, so it rolls over too; one minute later does not.
    assert parse_turbo_request(["x.io", "at", "12:00"], now).start_at == (
        datetime(2024, 2, 14, 12, 0).timestamp()
    )
    assert parse_turbo_request(["x.io", "at", "12:01"], now).start_at == (
        datetime(2024, 2, 13, 12, 1).timestamp()
    )


class OddMonitorError(MonitorError):
    pass


class OddTurboError(TurboError):
    pass


async def test_generic_monitor_and_turbo_errors_fall_back_to_the_message(
    tmp_path: Path,
) -> None:
    manager, telegram, _ = make(tmp_path)
    turbo = make_turbo()
    commands = BotCommands(manager, telegram, alert_chat_ids=(1,), turbo=turbo)

    async def odd_pause(_url: str) -> SiteState:
        raise OddMonitorError("https://example.com")

    async def odd_stop(_url: str) -> str:
        raise OddTurboError("<https://example.com>")

    manager.pause_site = odd_pause  # type: ignore[method-assign]
    turbo.stop = odd_stop  # type: ignore[method-assign]
    assert await send(commands, telegram, "/pause example.com") == "❌ https://example.com"
    assert await send(commands, telegram, "/turbo stop example.com") == "❌ <https://example.com>"
    assert "&lt;https://example.com&gt;" in telegram.messages[-1][1]  # escaped for HTML
