from scraper_bot.config import DEFAULT_IGNORED_ATTRIBUTES
from scraper_bot.pages.html_normalizer import HtmlNormalizer
from tests.conftest import node


def sig(html: str):
    return HtmlNormalizer(DEFAULT_IGNORED_ATTRIBUTES).signature(html)


def test_scripts_styles_and_comments_are_dropped() -> None:
    html = (
        "<html><head><title>T</title><script>var a='0xdead';</script><style>p{}</style>"
        "<meta charset='utf-8'><link rel='icon' href='/i.png'></head>"
        "<body><!-- comment --><p>Hi</p><noscript>x</noscript>"
        "<svg><path d='M0'/></svg></body></html>"
    )
    assert sig(html) == (node("title", "T"), node("p", "Hi"))


def test_text_after_dropped_script_stays_with_parent() -> None:
    assert sig("<p>a<script>x</script>b</p>") == (node("p", "ab"),)


def test_nodes_carry_own_text_only() -> None:
    assert sig("<div><p>Hello <b>world</b>!</p></div>") == (
        node("p", "Hello !"),
        node("b", "world"),
    )


def test_attributes_are_sorted_and_class_tokens_normalised() -> None:
    html = '<a href="/x" class=" b  a " id="k">L</a>'
    assert sig(html) == (node("a", "L", class_="a b", href="/x", id="k"),)


def test_ignored_attributes_are_removed() -> None:
    html = '<form data-csrf="abc" action="/s"><input nonce="1" name="q"></form>'
    assert sig(html) == (node("form", action="/s"), node("input", name="q"))


def test_tracking_parameters_are_stripped_from_urls() -> None:
    html = '<a href="/p?utm_source=x&id=5&t=123&ref=abc">L</a><img src="/i.png?v=9&w=10">'
    assert sig(html) == (node("a", "L", href="/p?id=5"), node("img", src="/i.png?w=10"))


def test_query_strings_keep_the_encoding_stored_snapshots_have() -> None:
    # A stored snapshot holds "%3A": encoding it differently would report every share link
    # on the page as changed after an upgrade.
    html = '<a href="https://x.com/intent/tweet?url=https://a.io&utm_source=x">Share</a>'
    assert sig(html) == (
        node("a", "Share", href="https://x.com/intent/tweet?url=https%3A%2F%2Fa.io"),
    )


def test_whitespace_and_nbsp_are_collapsed() -> None:
    assert sig("<p>  a \n\t b&nbsp;c​ </p>") == (node("p", "a b c"),)


def test_elements_without_information_are_skipped() -> None:
    assert sig("<div><span></span><br><section><p>x</p></section></div>") == (node("p", "x"),)


def test_empty_or_garbage_input_gives_empty_signature() -> None:
    assert sig("") == ()
    assert sig("   \x00 ") == ()


def test_signature_is_stable_for_same_input() -> None:
    html = "<ul><li class='a'>1</li><li class='b'>2</li></ul>"
    assert sig(html) == sig(html)


def test_canvas_is_ignored_because_scripts_resize_it() -> None:
    with_size = "<div><canvas width='1266' height='340'></canvas><p>chart</p></div>"
    without_size = "<div><canvas></canvas><p>chart</p></div>"
    assert sig(with_size) == sig(without_size) == (node("p", "chart"),)


def test_comment_only_or_unparsable_document_gives_empty_signature() -> None:
    assert sig("<!-- nothing but a comment -->") == ()
    assert sig("<?xml version='1.0'?>") == ()
    assert sig("<!DOCTYPE html>") == ()
    assert sig("<html></html>") == ()
    assert sig("﻿") == ()  # a lone byte-order mark is invisible text


def test_a_malformed_link_is_compared_as_written() -> None:
    signature = HtmlNormalizer().signature(
        '<a href="https://[YOUR_RPC_HOST]/v1?api-key=x">docs</a>'
        '<img src="http://[object Object]?v=1"><p>CA below</p>'
    )
    attrs = [dict(n.attrs) for n in signature]
    assert {"href": "https://[YOUR_RPC_HOST]/v1?api-key=x"} in attrs
    assert {"src": "http://[object Object]?v=1"} in attrs


def test_content_after_a_huge_text_node_is_kept() -> None:
    bundle = "x" * (11 * 1024 * 1024)  # over libxml2's 10 MB limit for one text node
    signature = HtmlNormalizer().signature(f"<div>{bundle}</div><p>CA: after the bundle</p>")
    assert signature[-1].text == "CA: after the bundle"
