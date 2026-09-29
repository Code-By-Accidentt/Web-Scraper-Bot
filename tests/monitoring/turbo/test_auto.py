import asyncio

from scraper_bot.models.addresses import Address, Chain
from scraper_bot.models.launch import Countdown, LaunchClues, LaunchTime, Phrase, TimeSource
from scraper_bot.monitoring.pace import SitePace
from scraper_bot.monitoring.tokens import SiteTokens
from scraper_bot.monitoring.turbo.auto import DAY, AutoTurbo
from tests.conftest import FakeNotifier, FakeTuning

SITE = "https://example.com"
PAGE = "https://example.com/presale"
TOKEN = Address(Chain.ETHEREUM, "0x" + "ab" * 20)


class FakeTime:
    """A clock that moves only when the test says so, and sleeps that wait for it."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self._sleeping: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self._sleeping.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for at, future in self._sleeping:
            if at <= self.now and not future.done():
                future.set_result(None)
        self._sleeping = [(at, f) for at, f in self._sleeping if not f.done()]
        for _ in range(10):
            await asyncio.sleep(0)


class Bench:
    """An AutoTurbo on a fake clock, with its pace, token news and notices at hand."""

    def __init__(self) -> None:
        self.time = FakeTime()
        self.notifier = FakeNotifier()
        self.pace = SitePace()
        self.tokens = SiteTokens()
        self.auto = AutoTurbo(
            tuning=FakeTuning(),  # 10 min before, 60 min after, the first token ends it
            notifier=self.notifier,  # type: ignore[arg-type]
            pace=self.pace,
            tokens=self.tokens,
            clock=self.time,
            sleep=self.time.sleep,
        )

    def countdown(self, remaining: float, *, key: str = "##:##:###0") -> LaunchClues:
        shown = f"00:{int(remaining) // 60:02d}:{int(remaining) % 60:02d}"
        return LaunchClues(self.time.now, countdowns=(Countdown(key, remaining, 1.0, shown),))

    def stated(self, at: float, key: str = "k") -> LaunchClues:
        return LaunchClues(
            self.time.now, times=(LaunchTime(key, at, "15:00 UTC", TimeSource.TEXT),)
        )

    def phrases(self, *keys: str, size: int = 1000) -> LaunchClues:
        found = tuple(Phrase(key, key.upper()) for key in keys)
        return LaunchClues(self.time.now, phrases=found, text_size=size)


async def test_a_countdown_speeds_the_site_up_before_its_launch_until_the_first_token() -> None:
    b = Bench()
    woken: list[bool] = []
    b.pace.listen(SITE, lambda: woken.append(True))
    launch = b.time.now + 1800
    await b.auto.observe(SITE, SITE, b.countdown(1800))
    assert b.notifier.auto_planned == []  # one reading proves nothing: it may be a static text
    await b.time.advance(5)
    await b.auto.observe(SITE, SITE, b.countdown(1795))
    assert b.notifier.auto_planned == [
        (SITE, "countdown “00:29:55”", launch, launch - 600, launch + 3600, None)
    ]
    await b.time.advance(1194)
    assert not b.pace.rounds_fast(SITE)
    await b.time.advance(1)
    assert b.pace.rounds_fast(SITE) and b.pace.light_fast(SITE) and woken == [True]
    await b.time.advance(30)
    b.tokens.publish({SITE}, TOKEN, "Base")
    await b.time.advance(0)
    assert not b.pace.light_fast(SITE) and b.auto.windows() == []
    assert b.notifier.auto_finished == [(SITE, "found 1 confirmed contract(s)", 1, 30.0)]


async def test_a_stopped_window_is_not_planned_again_and_a_round_timer_plans_none() -> None:
    b = Bench()
    stated = b.stated(b.time.now + 7200)
    await b.auto.observe(SITE, SITE, stated)
    assert len(b.notifier.auto_planned) == 1
    assert await b.auto.stop(SITE) and b.auto.windows() == []
    await b.auto.observe(SITE, SITE, stated)
    assert len(b.notifier.auto_planned) == 1 and b.notifier.auto_finished == []  # it never began
    moved = b.time.now + 3 * 3600  # the page moves its launch: that is news again
    await b.auto.observe(SITE, SITE, b.stated(moved))
    assert len(b.notifier.auto_planned) == 2 and await b.auto.stop(SITE)
    await b.auto.observe(SITE, SITE, b.stated(moved + DAY))  # the same text on the next day
    assert len(b.notifier.auto_planned) == 2

    other = "https://other.example"

    async def round_timer() -> None:  # a 60-second round, read twice
        await b.auto.observe(other, other, b.countdown(50))
        await b.time.advance(5)
        await b.auto.observe(other, other, b.countdown(45))
        await b.time.advance(55)

    for _ in range(3):
        await round_timer()
    await b.time.advance(3600)  # the window of the first round ends
    await round_timer()
    planned = [entry for entry in b.notifier.auto_planned if entry[0] == other]
    assert len(planned) == 1  # the first round looked like a launch; its restarts do not


async def test_a_far_countdown_plans_nothing_and_an_earlier_launch_takes_a_window_over() -> None:
    b = Bench()
    far = 30 * 3600.0
    await b.auto.observe(SITE, SITE, b.countdown(far))
    await b.time.advance(5)
    await b.auto.observe(SITE, SITE, b.countdown(far - 5))
    assert b.notifier.auto_planned == []  # more than a day away: read again later
    later, sooner = b.time.now + 5 * 3600, b.time.now + 2 * 3600
    await b.auto.observe(SITE, SITE, b.stated(later))
    await b.auto.observe(SITE, PAGE, b.stated(sooner))
    [(_, _, first, *_), (_, _, moved_to, start, _, moved_from)] = b.notifier.auto_planned
    assert (first, moved_to, moved_from, start) == (later, sooner, later, sooner - 600)
    [window] = b.auto.windows()
    assert window.page_url == PAGE and not window.active
    b.tokens.publish({SITE}, TOKEN, "Base")
    await b.time.advance(0)
    await b.auto.observe(SITE, SITE, b.stated(later))
    assert len(b.notifier.auto_planned) == 2  # the time it took over from ended with it


async def test_a_phrase_that_flickers_or_was_stopped_plans_no_second_window() -> None:
    b = Bench()
    await b.auto.observe(SITE, SITE, b.phrases("ca: tba"))  # the first render: known phrases
    await b.auto.observe(SITE, SITE, b.phrases("ca: tba", size=300))  # a half-loaded render
    await b.auto.observe(SITE, SITE, b.phrases("ca: tba", "presale is live"))
    assert b.notifier.auto_planned == []  # it came with the rest of a half-loaded page
    await b.auto.observe(SITE, SITE, b.phrases("ca: tba", "presale is live", "trading is live"))
    [(_, what, target, start, _, _)] = b.notifier.auto_planned
    assert (what, target, start) == ("“TRADING IS LIVE”", None, b.time.now)  # no launch time
    assert await b.auto.stop(SITE)
    await b.auto.observe(SITE, SITE, b.phrases("ca: tba"))  # it flickers away and back
    await b.time.advance(60)
    await b.auto.observe(SITE, SITE, b.phrases("ca: tba", "trading is live"))
    assert len(b.notifier.auto_planned) == 1
