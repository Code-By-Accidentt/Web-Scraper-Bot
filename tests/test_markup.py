from scraper_bot.markup import code, escape, link, pre, strip_markup


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
    assert (
        strip_markup(f"See {link('Etherscan', 'https://etherscan.io')} now") == "See Etherscan now"
    )
