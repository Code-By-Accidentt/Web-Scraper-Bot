import json
from pathlib import Path

import pytest

from scraper_bot.markup import strip_markup
from scraper_bot.preferences import (
    PREFERENCES,
    SECTIONS,
    ConfirmationRequired,
    PreferenceError,
    Preferences,
)
from tests.conftest import make_settings


def make_prefs(tmp_path: Path, **env: str) -> Preferences:
    prefs = Preferences(make_settings(tmp_path, **env), tmp_path / "prefs.json")
    prefs.load()
    return prefs


def test_every_spec_has_a_section_and_maps_to_a_settings_attribute(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    for spec in PREFERENCES:
        assert spec.section in SECTIONS
        assert hasattr(settings, spec.attr), spec.key
        assert spec.kind in {"bool", "int", "float"}


def test_defaults_mirror_settings_in_user_units_and_base_units(tmp_path: Path) -> None:
    prefs = make_prefs(tmp_path)
    assert prefs.poll_interval_min == 4.0
    assert prefs.rejected_ttl == 6 * 3600
    assert prefs.current(prefs.spec("rejected_ttl_hours")) == 6.0
    assert prefs.require_erc20 is True
    assert prefs.contract_pause_threshold == 5
    assert prefs.unverified_recheck == 300.0
    with pytest.raises(AttributeError):
        _ = prefs.telegram_bot_token  # only tunable knobs are exposed


def test_set_parses_validates_and_applies_scale(tmp_path: Path) -> None:
    prefs = make_prefs(tmp_path)
    change = prefs.set("rejected_ttl_hours", "12")
    assert (change.old, change.new) == (6.0, 12.0)
    assert prefs.rejected_ttl == 12 * 3600
    assert prefs.set("contract_pause_threshold", "3").new == 3
    assert prefs.set("deep_scan", "ON").new is True
    with pytest.raises(PreferenceError, match="takes an integer"):
        prefs.set("contract_pause_threshold", "3.5")
    with pytest.raises(PreferenceError, match="at least 1"):
        prefs.set("contract_pause_threshold", "0")
    with pytest.raises(PreferenceError, match="at most 20"):
        prefs.set("max_change_examples", "99")
    with pytest.raises(PreferenceError, match="on or off"):
        prefs.set("deep_scan", "yes please")
    with pytest.raises(PreferenceError, match="unknown setting"):
        prefs.set("telegram_bot_token", "x")


def test_related_settings_are_kept_consistent(tmp_path: Path) -> None:
    prefs = make_prefs(tmp_path)
    with pytest.raises(PreferenceError, match="must not exceed poll_interval_max"):
        prefs.set("poll_interval_min", "16")
    with pytest.raises(PreferenceError, match="must not be below poll_interval_min"):
        prefs.set("poll_interval_max", "3")
    with pytest.raises(PreferenceError, match="must not exceed turbo_max_minutes"):
        prefs.set("turbo_default_minutes", "999")


def test_risky_values_need_confirmation(tmp_path: Path) -> None:
    prefs = make_prefs(tmp_path)
    with pytest.raises(ConfirmationRequired) as info:
        prefs.set("require_erc20", "off")
    assert info.value.spec.key == "require_erc20" and info.value.value is False
    assert prefs.require_erc20 is True  # nothing changed
    assert prefs.set("require_erc20", "off", confirmed=True).new is False
    with pytest.raises(ConfirmationRequired):
        prefs.set("poll_interval_min", "1")
    assert prefs.set("poll_interval_min", "4").new == 4.0  # safe value: no confirmation


def test_overrides_persist_and_reload(tmp_path: Path) -> None:
    prefs = make_prefs(tmp_path)
    prefs.set("poll_interval_min", "7")
    prefs.set("flap_threshold", "3")
    prefs.set("flap_threshold", "2")  # back to the default: override dropped
    reloaded = make_prefs(tmp_path)
    assert reloaded.poll_interval_min == 7.0
    assert reloaded.is_overridden(reloaded.spec("poll_interval_min"))
    assert not reloaded.is_overridden(reloaded.spec("flap_threshold"))


def test_reset_restores_env_values(tmp_path: Path) -> None:
    prefs = make_prefs(tmp_path)
    prefs.set("poll_interval_min", "7")
    prefs.set("max_change_examples", "9")
    changes = prefs.reset("poll_interval_min")
    assert [(c.spec.key, c.old, c.new) for c in changes] == [("poll_interval_min", 7.0, 4.0)]
    assert prefs.reset() and prefs.max_change_examples == 5
    assert prefs.reset() == []


def test_invalid_entries_in_the_file_are_dropped(tmp_path: Path) -> None:
    (tmp_path / "prefs.json").write_text(
        '{"poll_interval_min": 8, "bogus": 1, "flap_threshold": "many"}', encoding="utf-8"
    )
    prefs = make_prefs(tmp_path)
    assert prefs.poll_interval_min == 8.0
    assert prefs.flap_threshold == 2


def test_overview_and_describe_are_human_readable(tmp_path: Path) -> None:
    prefs = make_prefs(tmp_path)
    prefs.set("settle_delay", "0.3")
    overview = strip_markup(prefs.overview())
    assert "<code>settle_delay</code>" in prefs.overview()
    assert "Monitoring & rendering" in overview
    assert "• settle_delay = 0.3 s*" in overview
    assert "• require_erc20 = on" in overview
    text = strip_markup(prefs.describe("turbo_max_pages"))
    assert "root included" in text and "Range: 1 to 10" in text and "/set turbo_max_pages" in text


def test_non_finite_numbers_are_rejected(tmp_path: Path) -> None:
    prefs = make_prefs(tmp_path)
    for raw in ("nan", "inf", "-inf", "1e999"):
        with pytest.raises(PreferenceError, match="finite number"):
            prefs.set("settle_delay", raw)
    assert not prefs.is_overridden(prefs.spec("settle_delay"))


def test_resetting_one_side_of_a_pair_keeps_the_pair_consistent(tmp_path: Path) -> None:
    prefs = make_prefs(tmp_path)
    prefs.set("poll_interval_max", "20")
    prefs.set("poll_interval_min", "18")
    changes = prefs.reset("poll_interval_max")  # alone it would leave min 18 > max 10
    assert [(c.spec.key, c.new) for c in changes] == [
        ("poll_interval_max", 10.0),
        ("poll_interval_min", 4.0),
    ]
    assert prefs.poll_interval_min <= prefs.poll_interval_max
    prefs.set("poll_interval_max", "30")
    prefs.set("poll_interval_min", "10")
    assert [c.spec.key for c in prefs.reset("poll_interval_min")] == ["poll_interval_min"]
    assert prefs.poll_interval_max == 30.0  # still consistent, so left alone


def test_inconsistent_pairs_in_the_file_are_dropped(tmp_path: Path) -> None:
    (tmp_path / "prefs.json").write_text(
        '{"poll_interval_min": 18, "turbo_default_minutes": 60, "turbo_max_minutes": 40,'
        ' "flap_threshold": 3}',
        encoding="utf-8",
    )
    prefs = make_prefs(tmp_path)
    assert prefs.poll_interval_min == 4.0  # 18 would exceed the .env maximum of 10
    assert not prefs.is_overridden(prefs.spec("turbo_default_minutes"))
    assert not prefs.is_overridden(prefs.spec("turbo_max_minutes"))
    assert prefs.turbo_default_minutes <= prefs.turbo_max_minutes
    assert prefs.flap_threshold == 3  # unrelated overrides survive


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"poll_interval_min"', "7"])
def test_unreadable_or_non_object_preferences_file_is_ignored(tmp_path: Path, content: str) -> None:
    (tmp_path / "prefs.json").write_text(content, encoding="utf-8")
    prefs = make_prefs(tmp_path)
    assert prefs.poll_interval_min == 4.0
    assert not any(prefs.is_overridden(spec) for spec in PREFERENCES)
    prefs.set("flap_threshold", "3")  # the next save replaces the broken file
    assert make_prefs(tmp_path).flap_threshold == 3


# key -> (risky value, safe neighbour). No ordered-pair preparation is needed: the .env
# poll_interval_max (10) is above 3, and turbo_default/max_minutes carry no caution.
CAUTION_BOUNDARIES: dict[str, tuple[str, str]] = {
    "poll_interval_min": ("2.99", "3"),
    "require_erc20": ("off", "on"),
    "rejected_ttl_hours": ("0", "0.5"),
    "deep_scan": ("off", "on"),
    "flap_window_hours": ("0", "0.5"),
    "turbo_interval": ("1.99", "2"),
    "turbo_max_sites": ("2", "1"),
    "turbo_max_pages": ("9", "8"),
    "block_max_backoff": ("59.9", "60"),
}


def test_boundary_table_covers_every_caution() -> None:
    assert set(CAUTION_BOUNDARIES) == {s.key for s in PREFERENCES if s.caution is not None}


@pytest.mark.parametrize("key", list(CAUTION_BOUNDARIES))
def test_every_caution_fires_at_its_boundary(tmp_path: Path, key: str) -> None:
    risky, safe = CAUTION_BOUNDARIES[key]
    prefs = make_prefs(tmp_path)
    spec = prefs.spec(key)
    with pytest.raises(ConfirmationRequired) as info:
        prefs.set(key, risky)
    assert info.value.spec is spec and info.value.warning
    assert not prefs.is_overridden(spec)  # nothing changed without confirmation
    assert prefs.set(key, safe).spec is spec  # the neighbour needs no confirmation
    assert prefs.set(key, risky, confirmed=True).new == prefs.current(spec)


def test_reset_of_a_key_without_override_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    prefs = make_prefs(tmp_path)
    assert prefs.reset("settle_delay") == [] and prefs.reset() == []
    assert not path.exists()  # nothing to save
    prefs.set("flap_threshold", "3")
    saved = path.read_text(encoding="utf-8")
    assert prefs.reset("settle_delay") == []
    assert prefs.flap_threshold == 3 and path.read_text(encoding="utf-8") == saved
    assert json.loads(saved) == {"flap_threshold": 3}
    with pytest.raises(PreferenceError, match="unknown setting"):
        prefs.reset("nope")
