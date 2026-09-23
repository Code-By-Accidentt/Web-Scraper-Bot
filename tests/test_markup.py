from scraper_bot.markup import bold, code, escape, pre, strip_markup


def test_escape_neutralises_html_but_keeps_quotes() -> None:
    assert escape("<a href='x'>&</a>") == "&lt;a href='x'&gt;&amp;&lt;/a&gt;"
    assert escape(42) == "42"


def test_wrappers_escape_their_content() -> None:
    assert code("/add <url>") == "<code>/add &lt;url&gt;</code>"
    assert pre("- <h1> Title") == "<pre>- &lt;h1&gt; Title</pre>"
    assert bold("a & b") == "<b>a &amp; b</b>"


def test_strip_markup_restores_plain_text() -> None:
    text = f"Use {code('/add <url>')} or {pre('x < y')} and {bold('go')}"
    assert strip_markup(text) == "Use /add <url> or x < y and go"
    assert strip_markup("no markup") == "no markup"
