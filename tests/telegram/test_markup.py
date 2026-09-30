from scraper_bot.telegram.markup import code, escape, link, pre, strip_markup


def test_escape_neutralises_html_but_keeps_quotes() -> None:
    assert escape("<a href='x'>&</a>") == "&lt;a href='x'&gt;&amp;&lt;/a&gt;"
    assert escape(42) == "42"


def test_wrappers_escape_their_content() -> None:
    assert code("/add <url>") == "<code>/add &lt;url&gt;</code>"
    assert pre("- <h1> Title") == "<pre>- &lt;h1&gt; Title</pre>"


def test_strip_markup_restores_plain_text() -> None:
    text = f"Use {code('/add <url>')} or {pre('x < y')} and go"
    assert strip_markup(text) == "Use /add <url> or x < y and go"
    assert strip_markup("no markup") == "no markup"


def test_link_escapes_text_and_url() -> None:
    assert link("A & B", 'https://x.io/?a=1&b="2"') == (
        '<a href="https://x.io/?a=1&amp;b=&quot;2&quot;">A &amp; B</a>'
    )
    # The plain fallback keeps where a link leads, and drops any tag, not only ours.
    assert strip_markup(f"See {link('Etherscan', 'https://etherscan.io/?a=1&b=2')} now") == (
        "See Etherscan (https://etherscan.io/?a=1&b=2) now"
    )
    assert (
        strip_markup(f"{link('https://x.io', 'https://x.io')} <b>bold</b>") == "https://x.io bold"
    )
