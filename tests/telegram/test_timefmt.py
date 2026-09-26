import re
from datetime import datetime

from scraper_bot.telegram.timefmt import format_precise, format_utc_offset


def test_precise_times_carry_hundredths_of_a_second() -> None:
    moment = datetime(2026, 9, 25, 12, 34, 56, 150_000).timestamp()
    assert format_precise(moment) == "12:34:56.15"
    assert format_precise(moment + 0.009) == "12:34:56.15"  # truncated, never rounded up


def test_the_utc_offset_is_spelled_out() -> None:
    now = datetime(2026, 9, 25, 12, 0).timestamp()
    text = format_utc_offset(now)
    assert re.fullmatch(r"UTC[+-]\d{2}:\d{2}", text)
    offset = datetime.fromtimestamp(now).astimezone().utcoffset()
    assert offset is not None
    hours, minutes = divmod(abs(int(offset.total_seconds() // 60)), 60)
    assert text.endswith(f"{hours:02d}:{minutes:02d}")
