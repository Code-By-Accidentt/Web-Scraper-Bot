import zoneinfo
from datetime import UTC, datetime

import pytest

from scraper_bot.models.launch import TimeSource
from scraper_bot.pages.launch import read_launch


def utc(hour: int, minute: int = 0) -> float:
    return datetime(2026, 9, 30, hour, minute, tzinfo=UTC).timestamp()


NOW = utc(12)
DAY = 86_400
try:
    zoneinfo.ZoneInfo("America/New_York")
except zoneinfo.ZoneInfoNotFoundError:
    EASTERN = 15  # without time zone data EST stays UTC-5
else:
    EASTERN = 14  # EST in September means New York time: UTC-4


def page(body: str) -> str:
    # Every page has a launch phrase in its title, which is not text on the page.
    return f"<html><head><title>Trading is live</title></head><body>{body}</body></html>"


def flow(number: int) -> str:
    return f'<span aria-label="{number}"><number-flow-react></number-flow-react></span>'


@pytest.mark.parametrize(
    ("html", "countdowns", "times", "phrases"),
    [
        # digits and colons in separate elements, launch words nearby
        (
            "<h2>Token launch</h2><span>00</span>:<span>05</span>:<span>30</span>",
            [(330, 1)],
            [],
            [],
        ),
        (
            "<p>TGE in</p><b>02</b><i>Days</i><b>03</b><i>Hours</i><b>04</b><i>Minutes</i>",
            [(2 * DAY + 3 * 3_600 + 4 * 60, 60)],
            [],
            [],
        ),
        (
            "<p>TGE in</p><div>DAYS 02</div><div>HOURS 03</div><div>MINUTES 04</div>"
            "<div>SECONDS 05</div><p>Mint in Days: 01 Hours: 02 Minutes: 03</p>",
            [(2 * DAY + 3 * 3_600 + 4 * 60 + 5, 1), (DAY + 7_200 + 180, 60)],
            [],
            [],
        ),
        ("<p>Mint starts in</p>" + ":".join(map(flow, (0, 22, 53, 6))), [(82_386, 1)], [], []),
        ("<p>Trading starts in 5m</p>", [(300, 60)], [], []),
        # other countdowns, time passed and clocks are no launch
        ("<p>Next round starts in 00:45</p><p>Presale ends in 02:03:04</p>", [], [], []),
        ("<p>Presale is live!</p><p>Presale ends in 02:03:04</p>", [], [], ["presale is live"]),
        ("<p>Presale live now! Launch in 02:03:04</p>", [(7_384, 1)], [], ["presale live"]),
        ("<div>Open Interest $2.8B</div><div>Funding / Countdown</div><p>00:52:23</p>", [], [], []),
        (
            "<p>Trading is live</p><p>" + "News " * 30 + "</p><p>00:10:00</p>",
            [(600, 1)],
            [],
            ["trading is live"],
        ),
        ("<p>Launched 3h 20m ago</p><p>Server time 15:04:05 UTC</p>", [], [], []),
        # launch times with their zone in the next 24 hours
        ("<p>Launch: Sep 30, 15:00 UTC</p>", [], [utc(15)], []),
        ("<p>Mint opens 30.09 20:00 UTC+3</p>", [], [utc(17)], []),
        (
            "<p>Trading opens at 2026-09-30 15:00 (UTC)</p>"
            "<p>Listing: 2026-09-30 17:30 (GMT+2)</p>",
            [],
            [utc(15), utc(15, 30)],
            [],
        ),
        (
            "<p>Mint starts 2026/09/30 14:00 UTC</p><p>Listing 09-30 13:00 UTC</p>"
            "<p>Launchpool 04-06 10:00 UTC</p>",
            [],
            [utc(14), utc(13)],
            [],
        ),
        (
            "<p>Trading opens at 2026-10-02 03:00 (UTC)</p><p>Listing 30/09/2026 13:00 UTC</p>",
            [],
            [],
            [],
        ),
        ("<p>Trading opens at 16:00 UTC</p><p>Rewards reset at 18:00 UTC</p>", [], [utc(16)], []),
        (  # a clock passed today is not tomorrow's, unless the page says so
            "<p>Trading opens at 11:00 UTC</p><p>Listing opens tomorrow at 11:00 UTC</p>",
            [],
            [utc(11) + DAY],
            [],
        ),
        ("<p>Trading opens at 10:00 AM EST</p>", [], [utc(EASTERN)], []),
        ("<p>Launch Oct 30, 15:00 UTC</p>", [], [], []),  # further than a day ahead
        # countdown targets in scripts and attributes
        (
            "<script>var countDownDate = new Date('2026-09-30T14:00:00Z')</script>",
            [],
            [utc(14)],
            [],
        ),
        (f'<script>self.__data={{"launchTime":{int(utc(13))}}}</script>', [], [utc(13)], []),
        (
            '<div data-countdown-at="2026-09-30T15:30:00Z" data-end-at="2026-09-30T16:00:00Z">'
            "Coming soon</div>",
            [],
            [utc(15, 30)],
            [],
        ),
        ("<p>CA: TBA</p><p>Presale is live</p>", [], [], ["ca: tba", "presale is live"]),
    ],
)
def test_the_launch_clues_a_page_shows(
    html: str,
    countdowns: list[tuple[float, float]],
    times: list[float],
    phrases: list[str],
) -> None:
    clues = read_launch(page(html), NOW)
    assert [(c.remaining, c.step) for c in clues.countdowns] == countdowns
    assert [t.at for t in clues.times] == times
    assert sorted(p.key for p in clues.phrases) == phrases


def test_what_the_messages_show() -> None:
    clues = read_launch(
        page(
            "<p>01:00:00 until the listing</p><h1>Token launch</h1><p>Launch in</p>"
            "<span>00</span>:<span>05</span>:<span>30</span><p>Trading opens at 13:22 UTC</p>"
            "<p>Presale is LIVE</p><div data-countdown-at='2026-09-30T15:30:00Z'></div>"
            "<script>var countDownDate = new Date('2026-09-30T14:00:00Z')</script>"
        ),
        NOW,
    )
    assert [c.text for c in clues.countdowns] == [
        "01:00:00 until the listing",
        "Launch in 00:05:30",
    ]
    assert [(t.text, t.source) for t in clues.times] == [
        ("Trading opens at 13:22 UTC", TimeSource.TEXT),
        ("countDownDate", TimeSource.SCRIPT),
        ("data-countdown-at", TimeSource.ATTRIBUTE),
    ]
    assert [(p.key, p.text) for p in clues.phrases] == [("presale is live", "Presale is LIVE")]
    assert clues.text_size == len(
        "01:00:00 until the listing Token launch Launch in 00 : 05 : 30 "
        "Trading opens at 13:22 UTC Presale is LIVE"
    )
    # the same countdown a render later: its key does not depend on how many digits it shows
    first, later = (
        read_launch(page(f"<p>Launch in {shown}</p>"), NOW) for shown in ("4m 1s", "3m 46s")
    )
    assert first.countdowns[0].key == later.countdowns[0].key
