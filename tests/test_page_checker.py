from pathlib import Path

from scraper_bot.address_extractor import AddressExtractor
from scraper_bot.change_detector import ChangeDetector
from scraper_bot.html_normalizer import HtmlNormalizer
from scraper_bot.models import Address, Chain, ConfirmedContract, PageStatus
from scraper_bot.page_checker import PageChecker
from scraper_bot.storage import SnapshotStore
from tests.conftest import FakeFetcher, FakeValidator, node

URL = "https://example.com"
USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
PAGE_A = "<html><body><h1>Title</h1><p>Hello</p><a href='/x'>Link</a></body></html>"
PAGE_B = PAGE_A.replace("Hello", "Goodbye")
TIMER = "<html><body><h1>Title</h1><span class='t'>{}</span></body></html>"
BIG = "<html><body>" + "".join(f"<p>para {i}</p>" for i in range(20)) + "</body></html>"
BIG_CHANGED = BIG.replace("para 3", "changed")
TINY = "<html><body><p>loading</p></body></html>"


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.fetcher = FakeFetcher()
        self.validator = FakeValidator()
        self.snapshots = SnapshotStore(tmp_path / "snaps")
        self.sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.checker = PageChecker(
            fetcher=self.fetcher,
            normalizer=HtmlNormalizer(),
            detector=ChangeDetector(),
            extractor=AddressExtractor(),
            validator=self.validator,
            snapshots=self.snapshots,
            confirm_delay=2.0,
            sleep=fake_sleep,
        )

    def signature(self, html: str):
        return HtmlNormalizer().signature(html)


async def test_first_check_stores_a_baseline(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.BASELINE
    assert h.snapshots.load(URL) == h.signature(PAGE_A)
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
    assert h.snapshots.load(URL) == h.signature(PAGE_B)
    assert (await h.checker.check(URL)).status is PageStatus.UNCHANGED


async def test_volatile_content_is_not_a_change_but_advances_the_snapshot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, TIMER.format("10:00"), TIMER.format("10:05"), TIMER.format("10:10"))
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNCHANGED
    assert h.fetcher.calls == [URL, URL, URL]
    assert h.snapshots.load(URL) == h.signature(TIMER.format("10:10"))


async def test_truncated_second_render_is_unstable_and_keeps_the_snapshot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, BIG, BIG_CHANGED, TINY)
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNSTABLE
    assert "20 vs 1" in (outcome.error or "")
    assert h.snapshots.load(URL) == h.signature(BIG)


async def test_http_errors_are_unavailable(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A, status=503)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNAVAILABLE
    assert outcome.error == "HTTP 503"
    assert h.snapshots.load(URL) is None


async def test_second_render_failure_leaves_snapshot_untouched(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.script(URL)
    h.fetcher.html(URL, PAGE_A, PAGE_B)
    h.fetcher.html(URL, PAGE_B, status=500)
    await h.checker.check(URL)
    outcome = await h.checker.check(URL)
    assert outcome.status is PageStatus.UNAVAILABLE
    assert h.snapshots.load(URL) == h.signature(PAGE_A)


async def test_contracts_come_from_both_renders(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    address = Address(Chain.ETHEREUM, USDC_ETH)
    h.validator.confirm(address, "Base")
    with_address = PAGE_B.replace("</body>", f"<p>{USDC_ETH}</p></body>")
    h.fetcher.html(URL, PAGE_A, PAGE_B, with_address)
    first = await h.checker.check(URL)
    assert first.contracts == ()
    second = await h.checker.check(URL)
    assert second.status is PageStatus.CHANGED
    assert second.contracts == (ConfirmedContract(address, "Base"),)
    assert h.validator.calls == [[address]]


async def test_rejected_candidates_are_not_reported(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fetcher.html(URL, PAGE_A.replace("Hello", USDC_ETH))
    outcome = await h.checker.check(URL)
    assert outcome.contracts == ()
    assert h.validator.calls == [[Address(Chain.ETHEREUM, USDC_ETH)]]
