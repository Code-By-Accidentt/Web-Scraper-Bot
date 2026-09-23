import re

from scraper_bot.address_extractor import sui_package
from scraper_bot.enrichment import TokenInfo
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
