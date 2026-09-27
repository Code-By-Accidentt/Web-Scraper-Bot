from pathlib import Path

from scraper_bot.chains.addresses.extractor import AddressExtractor
from scraper_bot.models.addresses import Address, Chain, ConfirmedContract
from scraper_bot.models.pages import PageCheckOutcome, PageStatus
from scraper_bot.models.sightings import Observation
from scraper_bot.pages.changes.detector import ChangeDetector
from scraper_bot.pages.changes.normalizer import HtmlNormalizer
from scraper_bot.pages.fetching.result import FetchResult
from scraper_bot.pages.page_checker import PageChecker
from scraper_bot.storage.snapshots import SnapshotStore
from tests.conftest import FakeFetcher, FakeTuning, FakeValidator, node

URL = "https://example.com"
USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
PAGE_A = "<html><body><h1>Title</h1><p>Hello</p><a href='/x'>Link</a></body></html>"
PAGE_B = PAGE_A.replace("Hello", "Goodbye")
TIMER = "<html><body><h1>Title</h1><span class='t'>{}</span></body></html>"
BIG = "<html><body>" + "".join(f"<p>para {i}</p>" for i in range(20)) + "</body></html>"
BIG_CHANGED = BIG.replace("para 3", "changed")
TINY = "<html><body><p>loading</p></body></html>"


class Harness:
    def __init__(self, tmp_path: Path, flap_window: float = 0.0) -> None:
        self.fetcher = FakeFetcher()
        self.validator = FakeValidator()
        self.snapshots = SnapshotStore(tmp_path / "snaps")
        self.sleeps: list[float] = []
        self.now = 1_000.0

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.checker = PageChecker(
            fetcher=self.fetcher,
            normalizer=HtmlNormalizer(),
            detector=ChangeDetector(),
            extractor=AddressExtractor(),
            validator=self.validator,
            snapshots=self.snapshots,
            tuning=FakeTuning(confirm_delay=2.0, flap_window=flap_window, flap_threshold=2),
            sleep=fake_sleep,
            clock=lambda: self.now,
        )

    def signature(self, html: str):
        return HtmlNormalizer().signature(html)

    async def check(self, url: str = URL) -> PageCheckOutcome:
        """A check that keeps its observations; ``tokens`` are the contracts they confirmed."""
        self.tokens: tuple[ConfirmedContract, ...] = ()
        self.observations: list[Observation] = []

        async def observe(observation: Observation) -> None:
            self.observations.append(observation)
            for result in observation.results:
                if result.contract is not None and result.contract not in self.tokens:
                    self.tokens += (result.contract,)

        return await self.checker.check(url, on_observation=observe)


async def test_first_check_stores_a_baseline(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.BASELINE
    assert h.snapshots.load(URL).signature == h.signature(PAGE_A)
    assert h.fetcher.calls == [URL]


async def test_identical_page_is_unchanged_without_a_second_render(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A)
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNCHANGED
    assert h.fetcher.calls == [URL, URL]
    assert h.sleeps == []


async def test_change_confirmed_by_second_render_is_reported(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A, PAGE_B, PAGE_B)
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.CHANGED
    assert outcome.report is not None
    assert outcome.report.removed == (node("p", "Hello"),)
    assert outcome.report.added == (node("p", "Goodbye"),)
    assert h.sleeps == [2.0]
    assert h.snapshots.load(URL).signature == h.signature(PAGE_B)
    assert (await h.checker.check(URL)).status is PageStatus.UNCHANGED


async def test_volatile_content_is_not_a_change_and_stays_out_of_the_snapshot(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, TIMER.format("10:00"), TIMER.format("10:05"), TIMER.format("10:10"))
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNCHANGED
    assert h.fetcher.calls == [URL, URL, URL]
    # The new timer value was never confirmed: the snapshot keeps what both renders agree on.
    assert h.snapshots.load(URL).signature == (node("h1", "Title"),)
    h.fetcher.scripts[URL].clear()
    h.fetcher.html(URL, TIMER.format("10:15"), TIMER.format("10:20"))
    assert (await h.checker.check(URL)).status is PageStatus.UNCHANGED


NEWS = (
    "<html><body><h1>Title</h1><span class='t'>{}</span>"
    "<p class='news'>Whitelist is open: apply now</p></body></html>"
)


async def test_a_change_that_lands_between_the_renders_is_reported_next_round(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, TIMER.format("01:00:00"))
    await h.checker.check(URL)
    # The line appears after the first render of a round, before its confirming render.
    h.fetcher.scripts[URL].clear()
    h.fetcher.html(URL, TIMER.format("00:59:50"), NEWS.format("00:59:48"))
    assert (await h.checker.check(URL)).status is PageStatus.UNCHANGED
    h.fetcher.scripts[URL].clear()
    h.fetcher.html(URL, NEWS.format("00:59:35"), NEWS.format("00:59:33"))
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.CHANGED and outcome.report is not None
    assert outcome.report.added == (node("p", "Whitelist is open: apply now", class_="news"),)
    h.fetcher.scripts[URL].clear()
    h.fetcher.html(URL, NEWS.format("00:59:20"), NEWS.format("00:59:18"))
    assert (await h.checker.check(URL)).status is PageStatus.UNCHANGED  # reported once


async def test_truncated_second_render_is_unstable_and_keeps_the_snapshot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, BIG, BIG_CHANGED, TINY)
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNSTABLE
    assert "20 vs 1" in (outcome.error or "")
    assert h.snapshots.load(URL).signature == h.signature(BIG)


async def test_http_errors_are_unavailable(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A, status=503)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNAVAILABLE
    assert outcome.error == "HTTP 503"
    assert h.snapshots.load(URL) is None


async def test_blank_answer_is_unavailable_and_keeps_the_snapshot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A, "<html><body></body></html>")
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNAVAILABLE
    assert outcome.error == "empty page"
    assert h.fetcher.calls == [URL, URL]  # no confirming render for a blank page
    assert h.snapshots.load(URL).signature == h.signature(PAGE_A)


async def test_second_render_failure_leaves_snapshot_untouched(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.script(URL)
    h.fetcher.html(URL, PAGE_A, PAGE_B)
    h.fetcher.html(URL, PAGE_B, status=500)
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNAVAILABLE
    assert h.snapshots.load(URL).signature == h.signature(PAGE_A)


async def test_contracts_come_from_both_renders(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    address = Address(Chain.ETHEREUM, USDC_ETH)
    h.validator.confirm(address, "Base")
    with_address = PAGE_B.replace("</body>", f"<p>{USDC_ETH}</p></body>")
    h.fetcher.html(URL, PAGE_A, PAGE_B, with_address)
    await h.check()
    assert h.tokens == ()
    second = await h.check()
    assert second.status is PageStatus.CHANGED
    assert h.tokens == (ConfirmedContract(address, "Base"),)
    assert h.validator.calls == [[address]]


async def test_rejected_candidates_are_not_reported(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A.replace("Hello", USDC_ETH))
    await h.check()
    assert h.tokens == ()
    assert h.validator.calls == [[Address(Chain.ETHEREUM, USDC_ETH)]]


async def test_flapping_element_is_silenced_after_two_alerts(tmp_path: Path) -> None:
    h = Harness(tmp_path, flap_window=3_600)
    a, b = TIMER.format("PREPARING"), TIMER.format("THE GATE OPENS")
    h.fetcher.html(URL, a, b, b, a, a, b, b, a, a)
    assert (await h.checker.check(URL)).status is PageStatus.BASELINE
    assert (await h.checker.check(URL)).status is PageStatus.CHANGED  # first flip: alerted
    assert (await h.checker.check(URL)).status is PageStatus.CHANGED  # second flip: alerted
    assert (await h.checker.check(URL)).status is PageStatus.UNCHANGED  # third flip: silenced
    assert h.snapshots.load(URL).signature == h.signature(b)
    h.now += 7_200  # memory expired: the element may alert again
    assert (await h.checker.check(URL)).status is PageStatus.CHANGED


BUNDLE = (f"window.KIT={{usdc:'{USDC_ETH}'}}",)


async def test_deep_scan_skips_what_the_first_round_found_only_in_scripts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    launch = Address(Chain.ETHEREUM, WETH)
    h.validator.confirm(launch, "Base")
    h.fetcher.html(URL, PAGE_A, extra=BUNDLE)
    h.fetcher.html(URL, PAGE_A, extra=(f"window.KIT={{usdc:'{USDC_ETH}',ca:'{WETH}'}}",))
    first = await h.checker.check(URL)
    assert first.status is PageStatus.BASELINE and first.candidates == ()
    assert h.validator.calls == []  # the wallet kit's tokens are not launches
    assert h.snapshots.load(URL).deep_seen == {USDC_ETH}
    second = await h.check()
    assert second.status is PageStatus.UNCHANGED
    assert h.tokens == (ConfirmedContract(launch, "Base"),)  # new in the scripts
    assert h.validator.calls == [[launch]]
    assert h.snapshots.load(URL).deep_seen == {USDC_ETH}  # only the first round is recorded


async def test_a_ca_inside_a_shadow_root_counts_on_the_first_deep_round(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    token = Address(Chain.ETHEREUM, USDC_ETH)
    h.validator.confirm(token, "Base")
    shadow = f"<div><p>CA: {USDC_ETH}</p></div>"  # page content, not a script
    h.fetcher.script(URL, FetchResult(URL, 200, PAGE_A, extra_texts=(shadow,), dom_texts=(shadow,)))
    await h.check()
    assert h.tokens == (ConfirmedContract(token, "Base"),)
    assert h.snapshots.load(URL).deep_seen == frozenset()  # not taken for a wallet-kit address


async def test_a_skipped_script_address_counts_once_the_page_shows_it(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    token = Address(Chain.ETHEREUM, USDC_ETH)
    h.validator.confirm(token, "Base")
    shown = PAGE_A.replace("Hello", f"CA: {USDC_ETH}")
    h.fetcher.html(URL, PAGE_A, PAGE_A, shown, shown, extra=BUNDLE)
    await h.checker.check(URL)
    await h.check()
    assert h.tokens == ()
    outcome = await h.check()
    assert outcome.status is PageStatus.CHANGED
    assert h.tokens == (ConfirmedContract(token, "Base"),)
    assert h.validator.calls == [[token]]


async def test_a_page_stored_without_deep_scan_records_its_scripts_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A)
    h.fetcher.html(URL, PAGE_A, extra=BUNDLE)
    await h.checker.check(URL)
    assert h.snapshots.load(URL).deep_seen is None  # deep scan off: nothing to record
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNCHANGED and outcome.candidates == ()
    assert h.snapshots.load(URL).deep_seen == {USDC_ETH}  # saved although nothing changed
    assert h.validator.calls == []


async def test_the_confirming_render_of_the_first_deep_round_is_recorded_too(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A)
    h.fetcher.html(URL, PAGE_B, extra=BUNDLE)
    h.fetcher.html(URL, PAGE_B, extra=(f"a='{USDC_ETH}' b='{WETH}'",))
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.CHANGED and outcome.candidates == ()
    assert h.snapshots.load(URL).deep_seen == {USDC_ETH, WETH}
    assert h.validator.calls == []


CHALLENGE = "<html><head><title>Just a moment...</title></head><body>Checking</body></html>"


async def test_blocked_answer_is_unavailable_with_a_block_signal(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, CHALLENGE, status=403)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNAVAILABLE
    assert outcome.block is not None and outcome.block.reason.startswith("HTTP 403")
    assert h.snapshots.load(URL) is None


async def test_challenge_page_with_200_never_replaces_the_snapshot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A, CHALLENGE)
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNAVAILABLE
    assert outcome.block is not None
    assert h.snapshots.load(URL).signature == h.signature(PAGE_A)


async def test_first_fetch_error_without_block_is_unavailable(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.script(URL, FetchResult(URL, None, error="timeout after 30s"))
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNAVAILABLE
    assert outcome.error == "timeout after 30s" and outcome.block is None
    assert outcome.candidates == ()
    assert h.snapshots.load(URL) is None
    assert h.validator.calls == [] and h.sleeps == []


USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


async def test_candidates_carry_every_valid_address_from_both_renders(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    usdc, weth = Address(Chain.ETHEREUM, USDC_ETH), Address(Chain.ETHEREUM, WETH)
    sol = Address(Chain.SOLANA, USDC_SOL)
    h.validator.confirm(weth, "Base")  # usdc and sol stay rejected
    first = PAGE_B.replace("</body>", f"<p>{USDC_ETH} {USDC_SOL}</p></body>")
    second = PAGE_B.replace("</body>", f"<p>{USDC_ETH} {WETH}</p></body>")
    h.fetcher.html(URL, PAGE_A, first, second)
    assert (await h.checker.check(URL)).candidates == ()
    outcome = await h.check()
    assert outcome.status is PageStatus.CHANGED
    assert outcome.candidates == (usdc, sol, weth)  # rejected ones included, no duplicates
    assert h.tokens == (ConfirmedContract(weth, "Base"),)
    assert h.validator.calls == [[usdc, sol], [weth]]  # the second render checks only news


async def test_block_on_the_confirming_render_keeps_the_snapshot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A, PAGE_B)
    h.fetcher.html(URL, CHALLENGE, status=403)
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNAVAILABLE
    assert outcome.block is not None and outcome.block.reason.startswith("HTTP 403")
    assert outcome.error == outcome.block.reason
    assert h.sleeps == [2.0] and h.fetcher.calls == [URL, URL, URL]
    assert h.snapshots.load(URL).signature == h.signature(PAGE_A)


async def test_load_timeout_reaches_the_fetcher_and_timeouts_are_flagged(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.script(URL, FetchResult(URL, None, error="timeout after 60s", timed_out=True))
    outcome = await h.checker.check(URL, load_timeout=60.0)
    assert outcome.status is PageStatus.UNAVAILABLE and outcome.timed_out
    assert h.fetcher.timeouts == [60.0]
    assert h.snapshots.load(URL) is None


async def test_timeout_on_the_confirming_render_is_flagged_too(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A, PAGE_B)
    h.fetcher.script(URL, FetchResult(URL, None, error="timeout after 30s", timed_out=True))
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNAVAILABLE and outcome.timed_out
    assert h.fetcher.timeouts == [None, None, None]
    assert h.snapshots.load(URL).signature == h.signature(PAGE_A)


async def test_addresses_are_reported_before_the_confirming_render(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    token = Address(Chain.ETHEREUM, USDC_ETH)
    h.validator.confirm(token, "Base")
    with_token = PAGE_B.replace("</body>", f"<p>CA: {USDC_ETH}</p></body>")
    h.fetcher.html(URL, PAGE_A, with_token, with_token)
    await h.checker.check(URL)  # the baseline
    events: list[str] = []
    original = h.fetcher.fetch

    async def fetch(url: str, *, timeout: float | None = None) -> FetchResult:
        events.append("fetch")
        return await original(url, timeout=timeout)

    async def observe(observation: Observation) -> None:
        events.append("observe")
        [sighting] = observation.sightings
        assert sighting.context == "CA:" and sighting.hinted
        assert [r.contract for r in observation.results] == [ConfirmedContract(token, "Base")]
        assert observation.seen_at == h.now

    h.fetcher.fetch = fetch  # type: ignore[method-assign]
    outcome = await h.checker.check(URL, on_observation=observe)
    assert outcome.status is PageStatus.CHANGED
    assert events == ["fetch", "observe", "fetch"]  # the alert does not wait for render two


async def test_an_observation_says_when_its_render_began_and_ended(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A.replace("</body>", f"<p>{USDC_ETH}</p></body>"))
    original = h.fetcher.fetch

    async def slow_fetch(url: str, *, timeout: float | None = None) -> FetchResult:
        h.now += 1.5  # the render takes 1.5 s
        return await original(url, timeout=timeout)

    h.fetcher.fetch = slow_fetch  # type: ignore[method-assign]
    observed: list[Observation] = []

    async def observe(observation: Observation) -> None:
        observed.append(observation)

    await h.checker.check(URL, on_observation=observe)
    [observation] = observed
    assert (observation.started_at, observation.seen_at) == (1_000.0, 1_001.5)


async def test_the_render_start_excludes_the_wait_for_a_free_tab(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    html = PAGE_A.replace("</body>", f"<p>{USDC_ETH}</p></body>")
    h.fetcher.script(URL, FetchResult(URL, 200, html, started_at=1_001.0))
    observed: list[Observation] = []

    async def observe(observation: Observation) -> None:
        observed.append(observation)

    await h.checker.check(URL, on_observation=observe)
    assert observed[0].started_at == 1_001.0  # loading began after a second in the queue


async def test_a_failing_observer_does_not_break_the_check(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A.replace("</body>", f"<p>{USDC_ETH}</p></body>"))

    async def broken(_observation: Observation) -> None:
        raise RuntimeError("telegram exploded")

    outcome = await h.checker.check(URL, on_observation=broken)
    assert outcome.status is PageStatus.BASELINE
    assert outcome.candidates == (Address(Chain.ETHEREUM, USDC_ETH),)


CA_PAGE = "<html><head><title>{}</title></head><body><p>CA: " + WETH + "</p></body></html>"


async def test_a_title_that_mentions_a_captcha_does_not_hide_the_page(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.validator.confirm(Address(Chain.ETHEREUM, WETH), "Ethereum")
    h.fetcher.html(URL, CA_PAGE.format("CAPTCHA ($CAPTCHA) | fair launch"))
    outcome = await h.check()
    assert outcome.status is PageStatus.BASELINE and outcome.block is None
    assert [c.address.value for c in h.tokens] == [WETH]


async def test_a_2xx_page_that_looks_like_a_challenge_is_read_but_not_compared(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.validator.confirm(Address(Chain.ETHEREUM, WETH), "Ethereum")
    page = CA_PAGE.format("Just a moment...")
    h.fetcher.html(URL, page, extra=(f"window.KIT={{usdc:'{USDC_ETH}'}}",))
    outcome = await h.check()
    assert outcome.status is PageStatus.UNAVAILABLE and outcome.block is not None
    assert [c.address.value for c in h.tokens] == [WETH]
    # What the page shows counts; its scripts do not, and nothing is stored or recorded.
    assert [s.address.value for s in h.observations[0].sightings] == [WETH]
    assert h.snapshots.load(URL) is None


async def test_a_visible_captcha_widget_comes_with_the_outcome(tmp_path: Path) -> None:
    widget = (
        '<iframe src="https://newassets.hcaptcha.com/captcha/v1/a/static/hcaptcha.html'
        '#frame=checkbox&amp;id=0"></iframe>'
    )
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A.replace("</body>", widget + "</body>"), PAGE_A)
    assert (await h.checker.check(URL)).captcha == "hCaptcha"
    assert (await h.checker.check(URL)).captcha is None


LIST = "<html><body><h1>Title</h1><span class='t'>{}</span><p>{}</p></body></html>"


async def test_an_element_removed_between_the_renders_is_reported_next_round(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, LIST.format("01:00", "Presale open"))
    await h.checker.check(URL)
    h.fetcher.scripts[URL].clear()
    # The line vanishes after the first render of a round, before the confirming one.
    h.fetcher.html(URL, LIST.format("00:59", "Presale open"), LIST.format("00:58", ""))
    assert (await h.checker.check(URL)).status is PageStatus.UNCHANGED
    h.fetcher.scripts[URL].clear()
    h.fetcher.html(URL, LIST.format("00:57", ""), LIST.format("00:56", ""))
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.CHANGED and outcome.report is not None
    assert outcome.report.removed == (node("p", "Presale open"),)


async def test_a_challenge_looking_confirming_render_is_read_too(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.validator.confirm(Address(Chain.ETHEREUM, WETH), "Ethereum")
    h.fetcher.html(URL, PAGE_A)
    await h.checker.check(URL)
    h.fetcher.scripts[URL].clear()
    h.fetcher.html(URL, PAGE_B, CA_PAGE.format("Just a moment..."))
    outcome = await h.check()
    assert outcome.status is PageStatus.UNAVAILABLE and outcome.block is not None
    assert [c.address.value for c in h.tokens] == [WETH]
    assert h.observations[-1].read is False


async def test_a_challenge_render_is_not_a_read(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, CA_PAGE.format("Just a moment..."))
    await h.check()
    assert h.observations and h.observations[0].read is False
