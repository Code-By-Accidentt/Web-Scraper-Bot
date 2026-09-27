from scraper_bot.models.pages import ChangeCounts
from scraper_bot.pages.changes.detector import ChangeDetector
from tests.conftest import node

URL = "https://example.com"


def test_identical_signatures_produce_empty_report() -> None:
    sig = (node("h1", "Title"), node("p", "Body"))
    report = ChangeDetector().compare(sig, sig, URL)
    assert report.is_empty
    assert report.counts == ChangeCounts()
    assert not ChangeDetector.differs(sig, sig)


def test_added_and_removed_nodes_are_listed() -> None:
    old = (node("h1", "Title"), node("p", "Old"), node("p", "Keep"))
    new = (node("h1", "Title"), node("p", "Keep"), node("p", "New"))
    report = ChangeDetector().compare(old, new, URL)
    assert report.removed == (node("p", "Old"),)
    assert report.added == (node("p", "New"),)
    assert report.modified == ()
    assert (report.counts.removed, report.counts.added, report.counts.modified) == (1, 1, 0)


def test_attribute_only_change_is_reported_as_modified() -> None:
    old = (node("a", "Buy", href="/old"), node("p", "x"))
    new = (node("a", "Buy", href="/new"), node("p", "x"))
    report = ChangeDetector().compare(old, new, URL)
    assert report.removed == () and report.added == ()
    assert report.modified == ((node("a", "Buy", href="/old"), node("a", "Buy", href="/new")),)
    before, after = report.modified[0]
    assert "href: /old -> /new" in before.describe_attr_change(after)


def test_volatile_nodes_are_the_differences_between_two_renders() -> None:
    first = (node("h1", "T"), node("span", "10:05", class_="timer"))
    second = (node("h1", "T"), node("span", "10:10", class_="timer"))
    volatile = ChangeDetector().volatile_nodes(first, second)
    assert volatile == {
        node("span", "10:05", class_="timer"),
        node("span", "10:10", class_="timer"),
    }


def test_volatile_nodes_and_their_previous_values_are_ignored() -> None:
    old = (node("h1", "T"), node("span", "10:00", class_="timer"), node("p", "same"))
    first = (node("h1", "T"), node("span", "10:05", class_="timer"), node("p", "same"))
    second = (node("h1", "T"), node("span", "10:10", class_="timer"), node("p", "same"))
    detector = ChangeDetector()
    volatile = detector.volatile_nodes(first, second)
    assert detector.compare(old, second, URL, ignore=volatile).is_empty


def test_real_change_survives_volatile_filtering() -> None:
    old = (node("h1", "T"), node("span", "10:00"), node("p", "old text"))
    first = (node("h1", "T"), node("span", "10:05"), node("p", "new text"))
    second = (node("h1", "T"), node("span", "10:10"), node("p", "new text"))
    detector = ChangeDetector()
    report = detector.compare(old, second, URL, ignore=detector.volatile_nodes(first, second))
    assert report.removed == (node("p", "old text"),)
    assert report.added == (node("p", "new text"),)


def test_unstable_when_one_render_is_much_smaller() -> None:
    big = tuple(node("p", str(i)) for i in range(100))
    detector = ChangeDetector()
    assert detector.is_unstable(big, big[:40])
    assert not detector.is_unstable(big, big[:90])
    assert not detector.is_unstable((), ())


def test_old_snapshot_node_that_is_itself_volatile_is_ignored() -> None:
    # The stored value reappeared in the first render, so it is volatile itself.
    old = (node("h1", "T"), node("span", "10:00", class_="timer"), node("p", "same"))
    first = (node("h1", "T"), node("span", "10:00", class_="timer"), node("p", "same"))
    second = (node("h1", "T"), node("span", "10:10", class_="timer"), node("p", "same"))
    detector = ChangeDetector()
    volatile = detector.volatile_nodes(first, second)
    assert node("span", "10:00", class_="timer") in volatile
    assert detector.compare(old, second, URL, ignore=volatile).is_empty

    # It is dropped outright, without using up the counterpart another old node needs.
    old = (node("span", "10:00", class_="timer"), node("span", "10:05", class_="timer"))
    new = (node("span", "10:10", class_="timer"),)
    ignore = frozenset(
        {node("span", "10:00", class_="timer"), node("span", "10:10", class_="timer")}
    )
    assert detector.compare(old, new, URL, ignore=ignore).is_empty


def test_a_static_text_that_turned_into_a_ticking_countdown_is_a_change() -> None:
    old = (node("h1", "Pons"), node("p", "Presale is live", class_="status"))
    first = (node("h1", "Pons"), node("p", "Launch in 00:05:30", class_="status"))
    second = (node("h1", "Pons"), node("p", "Launch in 00:05:28", class_="status"))
    detector = ChangeDetector()
    volatile = detector.volatile_nodes(first, second)
    report = detector.compare(old, second, URL, ignore=volatile)
    assert report.removed == (node("p", "Presale is live", class_="status"),)
    assert report.added == ()  # the countdown itself is volatile


def test_a_counter_with_another_value_is_the_previous_value_of_a_volatile_node() -> None:
    old = (node("p", "Raised: 12.5 SOL", class_="raised"),)
    first = (node("p", "Raised: 12.9 SOL", class_="raised"),)
    second = (node("p", "Raised: 13,004.25 SOL", class_="raised"),)
    detector = ChangeDetector()
    volatile = detector.volatile_nodes(first, second)
    assert detector.compare(old, second, URL, ignore=volatile).is_empty


def test_shape_masks_every_number() -> None:
    from scraper_bot.pages.changes.detector import shape

    assert shape("Launch in 00:05:30") == "Launch in #:#:#"
    assert shape("Raised: 1,204.5 SOL of 2000") == "Raised: # SOL of #"
    assert shape("Presale is live") == "Presale is live"


def test_diff_keeps_only_confirmed_content_as_the_next_snapshot() -> None:
    timer_old, timer_new = node("span", "10:00", class_="t"), node("span", "10:10", class_="t")
    news = node("p", "Whitelist is open")  # appeared between the two renders
    gone = node("p", "Presale soon")  # vanished between the two renders
    old = (node("h1", "T"), timer_old, gone)
    first = (node("h1", "T"), node("span", "10:05", class_="t"), gone)
    second = (node("h1", "T"), timer_new, news)
    detector = ChangeDetector()
    volatile = detector.volatile_nodes(first, second)
    report, accepted = detector.diff(old, second, URL, ignore=volatile)
    assert report.is_empty  # nothing confirmed yet
    # The unconfirmed news stays out, the unconfirmed removal stays in: both are reported
    # by the next round if they last.
    assert accepted == (node("h1", "T"), gone)
