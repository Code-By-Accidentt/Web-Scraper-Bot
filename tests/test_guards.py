"""Guards against drift between the test doubles and the real classes they stand in for.

The fakes are handed to production code with ``# type: ignore``, so nothing else notices
when a real method gains a parameter the fake does not accept (or the other way round).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from scraper_bot.chains.validator import ContractValidator
from scraper_bot.pages.fetching.chromium import PlaywrightFetcher
from scraper_bot.pages.fetching.prober import HttpProber
from scraper_bot.pages.page_checker import PageChecker
from scraper_bot.settings.env import Settings
from scraper_bot.settings.live import Preferences, Tuning
from scraper_bot.telegram.client import TelegramClient
from scraper_bot.telegram.notifier import Notifier
from tests.conftest import (
    FakeChecker,
    FakeFetcher,
    FakeNotifier,
    FakeProber,
    FakeTelegram,
    FakeTuning,
    FakeValidator,
)

# Scripting helpers that only exist on the fakes.
FAKE_ONLY = frozenset({"script", "html", "confirm", "reject_then_confirm", "known"})

PAIRS = [
    (FakeFetcher, PlaywrightFetcher),
    (FakeValidator, ContractValidator),
    (FakeProber, HttpProber),
    (FakeNotifier, Notifier),
    (FakeChecker, PageChecker),
    (FakeTelegram, TelegramClient),
]


def test_every_tuning_knob_resolves_on_preferences_and_the_fake(
    settings: Settings, tmp_path: Path
) -> None:
    prefs = Preferences(settings, tmp_path / "prefs.json")
    fake = FakeTuning()
    for name in Tuning.__annotations__:
        getattr(prefs, name)  # AttributeError: a Tuning knob without a PreferenceSpec
        assert hasattr(fake, name), f"FakeTuning lacks {name}"


def _parameters(function: object) -> list[tuple[str, object]]:
    signature = inspect.signature(function)  # type: ignore[arg-type]
    return [(p.name, p.kind) for p in signature.parameters.values()]


@pytest.mark.parametrize(("fake", "real"), PAIRS, ids=lambda cls: cls.__name__)
def test_fake_methods_match_the_real_signatures(fake: type, real: type) -> None:
    for name, member in vars(fake).items():
        if name.startswith("_") or name in FAKE_ONLY or not callable(member):
            continue
        assert hasattr(real, name), f"{fake.__name__}.{name} does not exist on {real.__name__}"
        assert _parameters(member) == _parameters(getattr(real, name)), (
            f"{fake.__name__}.{name} and {real.__name__}.{name} take different parameters"
        )


# Attributes through which production code talks to a collaborator the tests replace.
COLLABORATORS = {
    "_notifier": FakeNotifier,
    "_validator": FakeValidator,
    "_fetcher": FakeFetcher,
    "_prober": FakeProber,
    "_checker": FakeChecker,
    "_telegram": FakeTelegram,
}


def test_every_call_production_code_makes_exists_on_the_fake() -> None:
    """The other direction: a fake lacking a method breaks only the test that reaches it."""
    pattern = re.compile(r"self\.(" + "|".join(COLLABORATORS) + r")\.(\w+)")
    package = Path(__file__).resolve().parents[1] / "scraper_bot"
    calls = [
        (path, attr, name)
        for path in package.rglob("*.py")
        for attr, name in pattern.findall(path.read_text(encoding="utf-8"))
    ]
    assert len(calls) > 30  # the scan sees the code at all
    missing = sorted(
        f"{path.relative_to(package.parent)}: {COLLABORATORS[attr].__name__}.{name}"
        for path, attr, name in calls
        if not hasattr(COLLABORATORS[attr], name)
    )
    assert missing == []
