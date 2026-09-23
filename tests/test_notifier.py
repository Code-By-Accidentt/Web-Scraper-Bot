from scraper_bot.markup import strip_markup
from scraper_bot.models import Address, Chain, ChangeReport, ConfirmedContract
from scraper_bot.notifier import Notifier
from tests.conftest import FakeTelegram, node

ADDRESS = Address(Chain.ETHEREUM, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")


def make(chat_ids=(1,), **kwargs) -> tuple[Notifier, FakeTelegram]:
    telegram = FakeTelegram()
    return Notifier(telegram, chat_ids, **kwargs), telegram  # type: ignore[arg-type]


async def test_addresses_and_commands_are_monospace() -> None:
    notifier, telegram = make()
    await notifier.contract_found(ConfirmedContract(ADDRESS, "Base"), "https://x.io/a?b=1&c=2")
    text = telegram.messages[-1][1]
    assert f"<code>{ADDRESS.value}</code>" in text
    assert "https://x.io/a?b=1&amp;c=2" in text  # escaped, not raw
    await notifier.site_auto_paused("https://x.io", "https://x.io/app", 5, ["https://x.io"])
    assert "<code>/resume https://x.io</code>" in telegram.messages[-1][1]


async def test_change_report_quotes_examples_in_a_pre_block() -> None:
    report = ChangeReport(
        "https://x.io",
        removed=(node("h1", "Old <b>title</b>"),),
        added=(node("a", "Buy", href="/x?a=1&b=2"),),
        modified=((node("p", "t", id="1"), node("p", "t", id="2")),),
    )
    text = Notifier.format_report(report, 5)
    assert "<pre>- &lt;h1&gt; Old &lt;b&gt;title&lt;/b&gt;</pre>" in text
    assert '<pre>+ &lt;a href="/x?a=1&amp;b=2"&gt; Buy</pre>' in text
    assert "<pre>~ &lt;p&gt; t: id: 1 -&gt; 2</pre>" in text
    assert strip_markup(Notifier.format_report(report, 0)).count("\n") == 5


async def test_problem_reports_are_rate_limited_per_key() -> None:
    now = 1_000.0
    notifier, telegram = make(problem_cooldown=600, clock=lambda: now)
    assert await notifier.problem("rpc", "RPC failing", "no answer <x>", hint="Check .env")
    assert not await notifier.problem("rpc", "RPC failing", "again")
    assert await notifier.problem("browser", "Browser gone", "crashed")
    now += 601
    assert await notifier.problem("rpc", "RPC failing", "still")
    first = telegram.messages[0][1]
    assert first.startswith("🛑 RPC failing\n<pre>no answer &lt;x&gt;</pre>\nCheck .env")
    assert len(telegram.messages) == 3


async def test_started_and_dropped_alerts() -> None:
    notifier, telegram = make()
    await notifier.started("2.0.0", 3, 2)
    assert strip_markup(telegram.messages[-1][1]).startswith(
        "🤖 Bot started (v2.0.0): 2/3 site(s) running. Send /status"
    )
    silent, quiet = make(chat_ids=())
    await silent.contract_found(ConfirmedContract(ADDRESS, "Base"), "https://x.io")
    assert quiet.messages == []
