"""Settings changed at runtime with /set, saved in ``data/preferences.json``."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scraper_bot.settings.catalog import (
    BY_ATTR,
    BY_KEY,
    ORDERED_PAIRS,
    PARTNERS,
    PREFERENCES,
    PreferenceSpec,
)
from scraper_bot.settings.env import FALSE_WORDS, TRUE_WORDS, Settings
from scraper_bot.storage.files import atomic_write_text

log = logging.getLogger(__name__)


class Tuning(Protocol):
    """Settings the bot reads each time it uses them, in base units (seconds, counts)."""

    poll_interval_min: float
    poll_interval_max: float
    confirm_delay: float
    light_interval: float
    light_turbo_interval: float
    render_interval: float
    network_idle_timeout: float
    settle_delay: float
    render_failure_threshold: int
    require_erc20: bool
    skip_stable_copies: bool
    contract_pause_threshold: int
    rejected_ttl: float
    deep_scan: bool
    unverified_recheck: float
    unverified_watch: float
    unverified_max_per_hour: int
    max_change_examples: int
    enrich_timeout: float
    flap_window: float
    flap_threshold: int
    turbo_interval: float
    turbo_default_minutes: float
    turbo_max_minutes: float
    turbo_stop_after: int
    turbo_max_sites: int
    turbo_max_pages: int
    turbo_recheck_interval: float
    block_max_backoff: float
    auto_turbo: bool
    auto_turbo_before: float
    auto_turbo_after: float
    auto_turbo_max_sites: int


class PreferenceError(ValueError):
    """The requested change is invalid; the message is meant for the user."""


class ConfirmationRequired(PreferenceError):
    """The change is allowed but risky; repeat the command with ``confirm``."""

    def __init__(self, spec: PreferenceSpec, value: Any, warning: str) -> None:
        """Keep the setting, the parsed value and the warning for the reply."""
        super().__init__(warning)
        self.spec = spec
        self.value = value
        self.warning = warning


@dataclass(frozen=True, slots=True)
class PreferenceChange:
    """One applied change: the setting with its old and new value, in the user's unit."""

    spec: PreferenceSpec
    old: Any
    new: Any


class Preferences:
    """The /set overrides of :class:`Settings`, checked and saved to a JSON file.

    Reading a Settings attribute on it (``prefs.rejected_ttl``) gives the value in effect,
    in base units, so it serves as a :class:`Tuning`.
    """

    def __init__(self, settings: Settings, path: Path) -> None:
        """Start with no overrides; :meth:`load` reads them from ``path``."""
        self._settings = settings
        self._path = path
        self._overrides: dict[str, Any] = {}

    def spec(self, key: str) -> PreferenceSpec:
        """Return the setting named ``key`` (any case); raise PreferenceError if unknown."""
        spec = BY_KEY.get(key.strip().lower())
        if spec is None:
            raise PreferenceError(f"unknown setting {key!r}; /settings lists them all")
        return spec

    def default(self, spec: PreferenceSpec) -> Any:
        """Return the value from .env or the built-in default, in the user's unit."""
        base = getattr(self._settings, spec.attr)
        if spec.kind == "bool":
            return bool(base)
        value = base / spec.scale
        return round(value) if spec.kind == "int" else float(value)

    def current(self, spec: PreferenceSpec) -> Any:
        """Return the value in effect, in the user's unit: the override, else the default."""
        return self._overrides.get(spec.key, self.default(spec))

    def is_overridden(self, spec: PreferenceSpec) -> bool:
        """Return True when /set changed this setting away from its default."""
        return spec.key in self._overrides

    def __getattr__(self, name: str) -> Any:
        """Return the value in effect in base units, looked up by its Settings attribute name."""
        spec = BY_ATTR.get(name)
        if spec is None:
            raise AttributeError(name)
        value = self.current(spec)
        if spec.kind == "bool" or spec.scale == 1.0:
            return value
        return value * spec.scale

    def set(self, key: str, raw: str, *, confirmed: bool = False) -> PreferenceChange:
        """Change a setting to the value the user typed, save it and return the change.

        Raises PreferenceError for a bad value, and ConfirmationRequired for a risky one
        unless ``confirmed``. Setting the default value removes the override.
        """
        spec = self.spec(key)
        value = self._parse(spec, raw)
        self._check_relations(spec, value)
        warning = spec.caution(value) if spec.caution is not None else None
        if warning and not confirmed:
            raise ConfirmationRequired(spec, value, warning)
        old = self.current(spec)
        if value == self.default(spec):
            self._overrides.pop(spec.key, None)
        else:
            self._overrides[spec.key] = value
        self.save()
        log.info("preference %s: %s -> %s", spec.key, old, value)
        return PreferenceChange(spec, old, value)

    def reset(self, key: str | None = None) -> list[PreferenceChange]:
        """Drop the override of ``key``, or of every setting without a key; return the changes.

        When one side of a min/max pair goes back to its default and no longer fits the
        other side, the other side is reset too.
        """
        if key is None:
            keys = [s.key for s in PREFERENCES if s.key in self._overrides]
        else:
            keys = [self.spec(key).key]
        changes = []
        for name in keys:
            if name not in self._overrides:
                continue
            changes.append(self._drop_override(name))
            partner = PARTNERS.get(name)
            if partner in self._overrides and not self._consistent(name):
                changes.append(self._drop_override(partner))
        if changes:
            self.save()
        return changes

    def _drop_override(self, key: str) -> PreferenceChange:
        """Remove the override of ``key`` and return the change back to the default."""
        spec = self.spec(key)
        return PreferenceChange(spec, self._overrides.pop(key), self.default(spec))

    def _consistent(self, key: str) -> bool:
        """Return True when the value of ``key`` still fits its min/max partner."""
        spec = self.spec(key)
        try:
            self._check_relations(spec, self.current(spec))
        except PreferenceError:
            return False
        return True

    def load(self) -> None:
        """Read the overrides from the file, dropping values that are unknown or invalid.

        A missing or unreadable file keeps the overrides as they are. A hand-edited min/max
        pair that contradicts itself is dropped whole.
        """
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("preferences file %s is unreadable (%s); ignoring it", self._path, exc)
            return
        if not isinstance(data, dict):
            return
        self._overrides = {}
        for key, raw in data.items():
            try:
                spec = self.spec(str(key))
                value = self._parse(spec, str(raw))
            except PreferenceError as exc:
                log.warning("preference %r dropped: %s", key, exc)
                continue
            if value != self.default(spec):
                self._overrides[spec.key] = value
        for lower, upper in ORDERED_PAIRS:
            if self.current(self.spec(lower)) > self.current(self.spec(upper)):
                log.warning(
                    "preferences %s and %s contradict each other; both dropped", lower, upper
                )
                self._overrides.pop(lower, None)
                self._overrides.pop(upper, None)
        log.info("%d preference override(s) loaded", len(self._overrides))

    def save(self) -> None:
        """Write the overrides to the file."""
        atomic_write_text(self._path, json.dumps(self._overrides, indent=2, sort_keys=True))

    @staticmethod
    def _parse(spec: PreferenceSpec, raw: str) -> Any:
        """Return the value the user typed for ``spec``, checked against its type and limits.

        Raises PreferenceError, with a message for the user, when the value does not fit.
        """
        text = raw.strip().lower()
        if spec.kind == "bool":
            if text in TRUE_WORDS:
                return True
            if text in FALSE_WORDS:
                return False
            raise PreferenceError(f"{spec.key} takes on or off, got {raw!r}")
        try:
            value = int(text) if spec.kind == "int" else float(text)
        except ValueError as exc:
            kind = "an integer" if spec.kind == "int" else "a number"
            raise PreferenceError(f"{spec.key} takes {kind}, got {raw!r}") from exc
        if spec.kind == "float" and not math.isfinite(value):
            # nan compares false to everything, so it would pass the limits and poison the delays.
            raise PreferenceError(f"{spec.key} takes a finite number, got {raw!r}")
        if spec.minimum is not None and value < spec.minimum:
            raise PreferenceError(
                f"{spec.key} must be at least {spec.minimum:g} {spec.unit}".strip()
            )
        if spec.maximum is not None and value > spec.maximum:
            raise PreferenceError(
                f"{spec.key} must be at most {spec.maximum:g} {spec.unit}".strip()
            )
        return value

    def _check_relations(self, spec: PreferenceSpec, value: Any) -> None:
        """Raise PreferenceError when ``value`` would put a min above its max or the reverse."""
        for lower, upper in ORDERED_PAIRS:
            if spec.key == lower:
                limit = self.current(self.spec(upper))
                if value > limit:
                    raise PreferenceError(f"{lower} must not exceed {upper} ({limit:g})")
            elif spec.key == upper:
                limit = self.current(self.spec(lower))
                if value < limit:
                    raise PreferenceError(f"{upper} must not be below {lower} ({limit:g})")
