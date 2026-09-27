from pathlib import Path

import pytest

from scraper_bot.settings.live import PreferenceError, Preferences
from scraper_bot.telegram.markup import strip_markup
from scraper_bot.telegram.views.settings import describe_setting, settings_overview
from tests.conftest import make_settings


def make_prefs(tmp_path: Path) -> Preferences:
    return Preferences(make_settings(tmp_path), tmp_path / "prefs.json")


def test_overview_and_describe_are_human_readable(tmp_path: Path) -> None:
    prefs = make_prefs(tmp_path)
    prefs.set("settle_delay", "0.3")
    html = settings_overview(prefs)
    overview = strip_markup(html)
    assert "<code>settle_delay</code>" in html
    assert "Monitoring & rendering" in overview
    assert "• settle_delay = 0.3 s*" in overview
    assert "• require_erc20 = on" in overview
    text = strip_markup(describe_setting(prefs, "turbo_max_pages"))
    assert "root included" in text and "Range: 1 to 10" in text and "/set turbo_max_pages" in text
    assert "Default from .env: 4" in text and "Risky values" in text


def test_describe_rejects_an_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(PreferenceError, match="unknown setting"):
        describe_setting(make_prefs(tmp_path), "nope")


def test_a_bool_setting_lists_its_values(tmp_path: Path) -> None:
    text = strip_markup(describe_setting(make_prefs(tmp_path), "deep_scan"))
    assert "Values: on / off" in text and "Current: off" in text
