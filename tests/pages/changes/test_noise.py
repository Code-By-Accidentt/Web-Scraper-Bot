from scraper_bot.models.pages import ChangeReport, NoiseEntry
from scraper_bot.pages.changes.noise import NoiseFilter
from tests.conftest import node

URL = "https://example.com"
TICKER_A = node("span", "10:00", class_="ticker")
TICKER_B = node("span", "10:05", class_="ticker")
HEADLINE = node("h1", "Launch is live")


def report(*, removed=(), added=(), modified=()) -> ChangeReport:
    return ChangeReport(URL, tuple(removed), tuple(added), tuple(modified))


def test_first_alerts_pass_and_are_counted() -> None:
    noise = NoiseFilter(window=3600, threshold=2)
    filtered, memory = noise.apply(
        report(removed=[TICKER_A], added=[TICKER_B]),
        {},
        now=100.0,
        before=(TICKER_A,),
        after=(TICKER_B,),
    )
    assert filtered.removed == (TICKER_A,) and filtered.added == (TICKER_B,)
    key = NoiseFilter.node_key(TICKER_A)
    assert key == NoiseFilter.node_key(TICKER_B)  # same element, different text
    assert memory == {key: NoiseEntry(1, 100.0)}


def test_element_is_silenced_once_threshold_is_reached() -> None:
    noise = NoiseFilter(window=3600, threshold=2)
    memory: dict[str, NoiseEntry] = {}
    for now in (100.0, 200.0):
        filtered, memory = noise.apply(
            report(removed=[TICKER_A], added=[TICKER_B]),
            memory,
            now,
            before=(TICKER_A,),
            after=(TICKER_B,),
        )
        assert not filtered.is_empty
    filtered, memory = noise.apply(
        report(removed=[TICKER_B], added=[TICKER_A]),
        memory,
        300.0,
        before=(TICKER_B,),
        after=(TICKER_A,),
    )
    assert filtered.is_empty
    assert memory[NoiseFilter.node_key(TICKER_A)] == NoiseEntry(3, 300.0)


def test_other_elements_are_not_affected() -> None:
    noise = NoiseFilter(window=3600, threshold=1)
    memory = {NoiseFilter.node_key(TICKER_A): NoiseEntry(5, 90.0)}
    filtered, _ = noise.apply(
        report(removed=[TICKER_A], added=[TICKER_B, HEADLINE]),
        memory,
        100.0,
        before=(TICKER_A,),
        after=(TICKER_B, HEADLINE),
    )
    assert filtered.removed == ()
    assert filtered.added == (HEADLINE,)


def test_look_alike_elements_are_silenced_separately() -> None:
    """Two flapping ``.stat`` spans must not muzzle the third one (same tag and class)."""
    noise = NoiseFilter(window=3600, threshold=2)
    stats = [node("span", f"{label} 1", class_="stat") for label in ("Holders", "Volume", "Soon")]
    memory: dict[str, NoiseEntry] = {}
    for index in (0, 0, 1, 1):  # the first two spans alert twice each
        changed = node("span", f"tick {index}", class_="stat")
        after = tuple(changed if i == index else s for i, s in enumerate(stats))
        filtered, memory = noise.apply(
            report(removed=[stats[index]], added=[changed]),
            memory,
            100.0,
            before=tuple(stats),
            after=after,
        )
        assert not filtered.is_empty
    live = node("span", "BUY NOW", class_="stat")
    filtered, memory = noise.apply(
        report(removed=[stats[2]], added=[live]),
        memory,
        200.0,
        before=tuple(stats),
        after=(*stats[:2], live),
    )
    assert filtered.added == (live,)  # its own memory entry, untouched by the others
    again = node("span", "tick again", class_="stat")
    filtered, _ = noise.apply(
        report(removed=[stats[0]], added=[again]),
        memory,
        300.0,
        before=tuple(stats),
        after=(again, *stats[1:]),
    )
    assert filtered.is_empty  # the first span is over its threshold


def test_memory_expires_after_the_window() -> None:
    noise = NoiseFilter(window=3600, threshold=1)
    memory = {NoiseFilter.node_key(TICKER_A): NoiseEntry(5, 0.0)}
    filtered, memory = noise.apply(
        report(added=[TICKER_B]), memory, now=3600.0, before=(), after=(TICKER_B,)
    )
    assert filtered.added == (TICKER_B,)
    assert memory[NoiseFilter.node_key(TICKER_B)] == NoiseEntry(1, 3600.0)


def test_attribute_changes_are_keyed_by_text() -> None:
    noise = NoiseFilter(window=3600, threshold=1)
    old, new = node("a", "Buy", href="/a"), node("a", "Buy", href="/b")
    memory = {NoiseFilter.pair_key(old, new): NoiseEntry(1, 50.0)}
    filtered, _ = noise.apply(
        report(modified=[(old, new)]), memory, 100.0, before=(old,), after=(new,)
    )
    assert filtered.modified == ()


def test_paragraphs_without_attributes_are_keyed_by_first_word() -> None:
    online_1, online_2 = node("p", "Online: 12"), node("p", "Online: 15")
    other = node("p", "Whitelist opens tomorrow")
    assert NoiseFilter.node_key(online_1) == NoiseFilter.node_key(online_2)
    assert NoiseFilter.node_key(online_1) != NoiseFilter.node_key(other)
    # A number as the first word is masked: the counter stays one element while it counts.
    assert NoiseFilter.node_key(node("td", "1203")) == NoiseFilter.node_key(node("td", "1,204.5"))
    assert NoiseFilter.node_key(node("td", "12.5 SOL")) == NoiseFilter.node_key(
        node("td", "13 SOL")
    )


def test_a_counting_cell_without_attributes_is_silenced() -> None:
    noise = NoiseFilter(window=3600, threshold=2)
    memory: dict[str, NoiseEntry] = {}
    passed = 0
    for step in range(1, 6):
        before = (node("td", "Holders"), node("td", str(1200 + step - 1)))
        after = (node("td", "Holders"), node("td", str(1200 + step)))
        filtered, memory = noise.apply(
            report(removed=[before[1]], added=[after[1]]),
            memory,
            100.0 * step,
            before=before,
            after=after,
        )
        passed += not filtered.is_empty
    assert passed == 2


def test_a_new_element_is_not_silenced_by_a_rotating_look_alike() -> None:
    noise = NoiseFilter(window=3600, threshold=1)
    banner = node("li", "New: partner AMA")
    memory = {NoiseFilter.node_key(banner, 0): NoiseEntry(5, 50.0)}  # rotates all day
    fresh = node("li", "New: token live, CA below")
    after = (node("li", "New: meme contest"), node("li", "Roadmap Q4"), fresh)
    filtered, _ = noise.apply(report(added=[fresh]), memory, 100.0, before=after[:2], after=after)
    assert filtered.added == (fresh,)  # second "New:" item, not the banner in first place


def test_disabled_filter_passes_everything_and_keeps_no_memory() -> None:
    noise = NoiseFilter(window=0)
    assert not noise.enabled
    original = report(removed=[TICKER_A], added=[TICKER_B])
    filtered, memory = noise.apply(
        original, {"x": NoiseEntry(9, 1.0)}, 100.0, before=(TICKER_A,), after=(TICKER_B,)
    )
    assert filtered == original
    assert memory == {}


def test_a_page_that_keeps_swapping_wholesale_is_silenced_as_a_whole() -> None:
    noise = NoiseFilter(window=3600, threshold=2)
    page = tuple(node("p", f"row {i}") for i in range(30))
    error = (node("h1", "502 Bad Gateway"),)
    memory: dict[str, NoiseEntry] = {}
    alerts = 0
    for step in range(4):
        filtered, memory = noise.apply(
            report(removed=page, added=error), memory, 100.0 * step, before=page, after=error
        )
        alerts += not filtered.is_empty
    assert alerts == 2  # then silenced, like any element that keeps changing
    assert set(memory) == {"page|wholesale"}  # no element of the page was counted
