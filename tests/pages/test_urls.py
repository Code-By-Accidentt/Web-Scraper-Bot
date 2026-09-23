import pytest

from scraper_bot.config import DEFAULT_SUB_URL_PATTERNS
from scraper_bot.pages.urls import (
    InvalidUrlError,
    SubUrlDeriver,
    normalize_url,
    registrable_domain,
    url_to_filename,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example.com", "https://example.com"),
        ("https://example.com/", "https://example.com"),
        ("  https://Example.COM/App/  ", "https://example.com/App/"),
        ("http://Foo.IO/path?q=1#frag", "http://foo.io/path?q=1"),
        ("localhost:8000", "https://localhost:8000"),
        ("https://sub.domain.co.uk:8443/x", "https://sub.domain.co.uk:8443/x"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not a url",
        "ftp://x.com",
        "https://",
        "javascript:alert(1)",
        "https://-bad-.com",
        "https://nodots",
        "https://exa mple.com",
    ],
)
def test_normalize_url_rejects_garbage(raw: str) -> None:
    with pytest.raises(InvalidUrlError):
        normalize_url(raw)


def test_registrable_domain_strips_www() -> None:
    assert registrable_domain("https://www.example.com/x") == "example.com"
    assert registrable_domain("https://app.example.com") == "app.example.com"


def test_sub_urls_follow_patterns_in_order() -> None:
    deriver = SubUrlDeriver(DEFAULT_SUB_URL_PATTERNS)
    assert deriver.derive("https://www.example.com") == [
        "https://app.example.com",
        "https://app.example.com/leaderboard",
        "https://example.com/dashboard",
        "https://docs.example.com",
        "https://example.com/app",
        "https://doc.example.com",
        "https://example.com/create",
    ]


def test_sub_urls_exclude_root_and_duplicates() -> None:
    deriver = SubUrlDeriver(("https://{domain}/app", "https://{domain}/app", "https://{domain}"))
    assert deriver.derive("https://example.com/app") == ["https://example.com"]
    assert deriver.pages("https://example.com") == [
        "https://example.com",
        "https://example.com/app",
    ]


def test_broken_pattern_is_skipped() -> None:
    deriver = SubUrlDeriver(("https://{missing}/x", "https://{domain}/ok"))
    assert deriver.derive("https://example.com") == ["https://example.com/ok"]


def test_url_to_filename_is_safe_and_unique() -> None:
    first = url_to_filename("https://example.com/a/b?c=1")
    second = url_to_filename("https://example.com/a/b?c=2")
    assert first != second
    assert first.startswith("example.com_a_b-")
    assert all(ch.isalnum() or ch in "._-" for ch in first)
    assert url_to_filename("https://example.com/a/b?c=1") == first
