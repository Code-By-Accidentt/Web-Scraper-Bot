import asyncio
import re

from scraper_bot.address_extractor import sui_package
from scraper_bot.enrichment import TokenInfo
from scraper_bot.markup import strip_markup
from scraper_bot.models import Address, Chain, ChangeReport, ConfirmedContract
from scraper_bot.notifier import Notifier
from scraper_bot.urls import url_to_filename
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


class FakeEnricher:
    def __init__(self, info: TokenInfo | None = None, error: Exception | None = None) -> None:
        self.info, self.error = info or TokenInfo(), error
        self.calls: list[ConfirmedContract] = []

    async def enrich(self, contract: ConfirmedContract) -> TokenInfo:
        self.calls.append(contract)
        if self.error is not None:
            raise self.error
        return self.info


async def test_contract_alert_carries_token_details_and_links() -> None:
    info = TokenInfo(
        name="USD Coin",
        symbol="USDC",
        decimals=6,
        total_supply=50_000_000 * 10**6,
        market_checked=True,
        dex="Uniswap v3",
        pair="USDC/WETH",
        pair_url="https://dexscreener.com/ethereum/pair",
        price_usd=1.0,
        liquidity_usd=12_400_000,
        fdv_usd=5e9,
        pair_created_at=1_000.0,
    )
    notifier, telegram = make(enricher=FakeEnricher(info), clock=lambda: 4_600.0)
    await notifier.contract_found(ConfirmedContract(ADDRESS, "Ethereum"), "https://x.io")
    text = telegram.messages[-1][1]
    plain = strip_markup(text)
    assert "Token: USD Coin (USDC) · 6 decimals · supply 50M" in plain
    assert (
        "Trading on Uniswap v3 USDC/WETH · price $1.00 · liquidity $12.4M · FDV $5B · "
        "listed 1h 0m ago"
    ) in plain
    assert "dexscreener.com/ethereum/pair" not in text  # a pair address is never shown
    assert f'<a href="https://etherscan.io/token/{ADDRESS.value}">Etherscan</a>' in text
    assert plain.endswith("Links: Etherscan · DexScreener · Uniswap")


async def test_no_pair_and_failed_lookups_still_produce_an_alert() -> None:
    notifier, telegram = make(enricher=FakeEnricher(TokenInfo(market_checked=True)))
    await notifier.address_confirmed(ConfirmedContract(ADDRESS, "Base"), "https://x.io")
    plain = strip_markup(telegram.messages[-1][1])
    assert "No DEX pair listed yet." in plain and "Basescan" in plain
    assert "Token:" not in plain

    broken, telegram = make(enricher=FakeEnricher(error=RuntimeError("api down")))
    await broken.contract_found(ConfirmedContract(ADDRESS, "Ethereum"), "https://x.io")
    plain = strip_markup(telegram.messages[-1][1])
    assert plain.startswith("🔔 Contract found") and "Links: Etherscan" in plain


ANY_ID_RE = re.compile(r"0x[0-9a-fA-F]{40,64}(?:::\w+::\w+)?|[1-9A-HJ-NP-Za-km-z]{32,44}")


async def test_an_alert_names_exactly_one_token() -> None:
    pair_address = "0x" + "b" * 40
    info = TokenInfo(
        name="Pepe",
        symbol="PEPE",
        decimals=18,
        total_supply=10**24,
        market_checked=True,
        dex="Uniswap v2",
        pair="PEPE/WETH",
        pair_url=f"https://dexscreener.com/ethereum/{pair_address}",
        price_usd=0.5,
        liquidity_usd=100.0,
    )
    notifier, telegram = make(enricher=FakeEnricher(info))
    await notifier.contract_found(ConfirmedContract(ADDRESS, "Ethereum"), "https://x.io")
    text = telegram.messages[-1][1]
    assert pair_address not in text
    assert set(ANY_ID_RE.findall(text)) == {ADDRESS.value}

    package = "0x" + "ab" * 32
    coin = f"{package}::meme::MEME"
    notifier, telegram = make(
        enricher=FakeEnricher(TokenInfo(coin_type=coin, symbol="MEME", market_checked=True))
    )
    await notifier.contract_found(
        ConfirmedContract(Address(Chain.SUI, package), "Sui"), "https://x.io"
    )
    text = telegram.messages[-1][1]
    plain = strip_markup(text)
    assert f"Coin type: {coin}" in plain and "Token: MEME" in plain
    assert f'<a href="https://suiscan.xyz/mainnet/coin/{coin}">Suiscan</a>' in text
    assert {sui_package(found) for found in ANY_ID_RE.findall(text)} == {package}


async def test_broken_token_details_do_not_drop_the_alert() -> None:
    weird = TokenInfo(market_checked=True, pair_url="https://x", pair_created_at=float("nan"))
    notifier, telegram = make(enricher=FakeEnricher(weird))
    await notifier.contract_found(ConfirmedContract(ADDRESS, "Ethereum"), "https://x.io")
    plain = strip_markup(telegram.messages[-1][1])
    assert plain.startswith("🔔 Contract found") and "Links: Etherscan" in plain


async def test_every_alert_type_renders_escaped_html() -> None:
    site, page = "https://x.io/?a=1&b=2", "https://x.io/app?p=1&q=2"
    esc_site, esc_page = "https://x.io/?a=1&amp;b=2", "https://x.io/app?p=1&amp;q=2"
    notifier, telegram = make(chat_ids=(1, 2))

    def last() -> str:
        sent = telegram.messages[-2:]
        assert [chat for chat, _ in sent] == [1, 2]  # one message per chat, same text
        assert sent[0][1] == sent[1][1]
        assert "&b=2" not in sent[0][1] and "&q=2" not in sent[0][1]  # escaped, not raw
        return sent[0][1]

    await notifier.page_discovered(page)
    assert last() == f"🔍 Sub-page found: {esc_page}\nMonitoring started."

    await notifier.unverified_address(ADDRESS, page, 300)
    text = last()
    assert text.startswith(f"⚠️ Unverified address on {esc_page}\nChain: Ethereum\n")
    assert f"\n<code>{ADDRESS.value}</code>\n" in text
    assert text.endswith("re-checking every 5m 0s.")
    await notifier.unverified_address(ADDRESS, page, 300, watch_seconds=3600)
    assert last().endswith("re-checking every 5m 0s for 1h 0m.")

    await notifier.unverified_digest(site, 3, 7200)
    text = last()
    assert text.startswith(f"📨 3 more unverified address(es) appeared on {esc_site} in the last")
    assert "They are watched for 2h 0m;" in text

    await notifier.turbo_started(site, 4, 5, 30, 1)
    text = last()
    assert text.startswith(f"🚀 Turbo mode on {esc_site}\n")
    assert "Rendering 4 page(s) every 5s, stops after 1 confirmed contract(s) or in 30 min." in text
    assert text.endswith(f"Stop early with <code>/turbo stop {esc_site}</code>")
    await notifier.turbo_started(site, 4, 5, 30, 0)
    assert "every 5s, stops in 30 min.\n" in last()

    await notifier.turbo_finished(site, "time is up", (), 61.4, 12)
    assert last() == (
        f"🎯 Turbo finished on {esc_site}: time is up\n"
        "Duration 61s, 12 renders, 0 confirmed contract(s)."
    )
    found = [ConfirmedContract(ADDRESS, "Base"), ConfirmedContract(ADDRESS, "BSC")]
    await notifier.turbo_finished(site, "2 contract(s) found", found, 9.6, 2)
    text = last()
    assert "\nDuration 10s, 2 renders, 2 confirmed contract(s).\n" in text
    assert text.endswith(
        f"• <code>{ADDRESS.value}</code> (Base)\n• <code>{ADDRESS.value}</code> (BSC)"
    )

    await notifier.site_blocked(site, page, "HTTP 403 (just a moment)", 120)
    text = last()
    assert text.startswith(f"🚫 Possible IP block on {esc_site}\nPage: {esc_page}\n")
    assert "Reason: <code>HTTP 403 (just a moment)</code>\n" in text
    assert "next check in 120s" in text

    await notifier.site_recovered(site)
    assert last() == f"✅ {esc_site} answers normally again; normal interval restored."
    assert len(telegram.messages) == 2 * 10  # ten alerts, each to both chats


async def test_contracts_batch_sends_a_sorted_tab_separated_document() -> None:
    site, page = "https://x.io", "https://x.io/app"
    sol = ConfirmedContract(
        Address(Chain.SOLANA, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"), "Solana"
    )
    notifier, telegram = make(chat_ids=(1, 2))
    await notifier.contracts_batch(site, page, [sol, ConfirmedContract(ADDRESS, "Base")])
    assert [chat for chat, *_ in telegram.documents] == [1, 2]
    _, filename, content, caption = telegram.documents[0]
    assert re.fullmatch(rf"contracts_{re.escape(url_to_filename(site))}_\d+\.txt", filename)
    assert caption == "⚠️ 2 confirmed contracts found on https://x.io/app"
    assert content.decode() == f"{ADDRESS.value}\tBase\n{sol.address.value}\tSolana\n"
    assert telegram.messages == []  # a document, not a burst of messages

    silent, quiet = make(chat_ids=())
    await silent.contracts_batch(site, page, [sol])
    assert quiet.documents == [] and quiet.messages == []


def test_change_report_truncates_examples_with_a_more_line() -> None:
    removed = tuple(node("p", f"gone {i}") for i in range(7))
    report = ChangeReport("https://x.io", removed=removed)
    text = Notifier.format_report(report, 5)
    assert "• Removed: 7" in text
    assert text.count("- &lt;p&gt; gone") == 5
    assert "- &lt;p&gt; gone 4\n... and 2 more</pre>" in text
    assert "gone 5" not in text
    assert "more" not in Notifier.format_report(report, 7)  # exactly at the limit: no tail


def test_report_with_only_one_section_shows_only_that_section() -> None:
    report = ChangeReport("https://x.io", added=(node("p", "new"),))
    text = Notifier.format_report(report, 5)
    assert "\nAdded:\n<pre>+ &lt;p&gt; new</pre>" in text
    assert "\nRemoved:\n" not in text and "\nChanged:\n" not in text
    assert text.count("<pre>") == 1
    assert text.endswith("\n➡️ https://x.io")


async def test_max_examples_can_be_a_live_provider() -> None:
    limit = 1
    notifier, telegram = make(max_examples=lambda: limit)
    report = ChangeReport("https://x.io", added=tuple(node("p", f"new {i}") for i in range(3)))
    await notifier.page_changed(report)
    text = telegram.messages[-1][1]
    assert text.count("+ &lt;p&gt; new") == 1 and "... and 2 more" in text
    limit = 10  # the user raised the setting between two alerts
    await notifier.page_changed(report)
    text = telegram.messages[-1][1]
    assert text.count("+ &lt;p&gt; new") == 3 and "... and" not in text


async def test_problem_detail_is_truncated_to_1500_chars() -> None:
    notifier, telegram = make()
    assert await notifier.problem("k", "Title", "x" * 2000)
    text = telegram.messages[-1][1]
    assert f"<pre>{'x' * 1500}</pre>" in text
    assert "x" * 1501 not in text


async def test_report_later_delivers_the_problem() -> None:
    notifier, telegram = make()
    notifier.report_later("monitor", "Monitor crashed", "Traceback <x>", hint="Restarting")
    assert telegram.messages == []  # scheduled, not sent synchronously
    await asyncio.sleep(0)
    assert telegram.messages == [
        (1, "🛑 Monitor crashed\n<pre>Traceback &lt;x&gt;</pre>\nRestarting")
    ]
    notifier.report_later("monitor", "Monitor crashed", "again")  # inside the cooldown
    await asyncio.sleep(0)
    assert len(telegram.messages) == 1


async def test_render_timeout_alerts() -> None:
    notifier, telegram = make()
    await notifier.page_timing_out("https://x.io/docs?a=1&b=2", 5, "timeout after 30s", 60.0)
    slow = telegram.messages[-1][1]
    assert slow.startswith("🐢 https://x.io/docs?a=1&amp;b=2 has not rendered 5 times in a row")
    assert "(timeout after 30s)" in slow and "raised to 60s" in slow
    await notifier.page_renders_again("https://x.io/docs", 30.0)
    assert telegram.messages[-1][1] == (
        "✅ https://x.io/docs renders again; load timeout back to 30s."
    )
