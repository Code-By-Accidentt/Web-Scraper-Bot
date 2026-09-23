"""Runtime-adjustable settings: what ``/settings`` and ``/set`` expose.

:class:`Settings` (from ``.env``) stays the source of defaults. :class:`Preferences`
layers validated overrides on top, persists them to ``data/preferences.json`` and
exposes the *effective* values under the same attribute names as ``Settings``, so
components that read a :class:`Tuning` at use time pick up changes immediately.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scraper_bot.config import FALSE_WORDS, TRUE_WORDS, Settings
from scraper_bot.markup import code, escape
from scraper_bot.storage import atomic_write_text

log = logging.getLogger(__name__)


class Tuning(Protocol):
    """The knobs components consult while running (base units: seconds, counts)."""

    poll_interval_min: float
    poll_interval_max: float
    confirm_delay: float
    network_idle_timeout: float
    settle_delay: float
    require_erc20: bool
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


class PreferenceError(ValueError):
    """The requested change is invalid; the message is meant for the user."""


class ConfirmationRequired(PreferenceError):
    """The change is allowed but risky; repeat the command with ``confirm``."""

    def __init__(self, spec: PreferenceSpec, value: Any, warning: str) -> None:
        super().__init__(warning)
        self.spec = spec
        self.value = value
        self.warning = warning


Caution = Callable[[Any], str | None]


@dataclass(frozen=True, slots=True)
class PreferenceSpec:
    key: str  # user-facing name, e.g. rejected_ttl_hours
    attr: str  # Settings attribute holding the base-unit value, e.g. rejected_ttl
    section: str
    kind: str  # bool | int | float
    label: str
    description: str  # what it does and why you would change it
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    scale: float = 1.0  # base value = user value * scale (hours -> seconds = 3600)
    caution: Caution | None = None  # returns a warning for a risky value


@dataclass(frozen=True, slots=True)
class PreferenceChange:
    spec: PreferenceSpec
    old: Any
    new: Any


SECTIONS: dict[str, str] = {
    "monitoring": "Monitoring & rendering",
    "contracts": "Contracts & scanning",
    "alerts": "Alerts & noise",
    "turbo": "Turbo & blocking",
}


def _below(limit: float, text: str) -> Caution:
    return lambda value: text if value < limit else None


def _above(limit: float, text: str) -> Caution:
    return lambda value: text if value > limit else None


def _when(flag: bool, text: str) -> Caution:
    return lambda value: text if value is flag else None


PREFERENCES: tuple[PreferenceSpec, ...] = (
    # ── monitoring ──────────────────────────────────────────────────────────
    PreferenceSpec(
        "poll_interval_min",
        "poll_interval_min",
        "monitoring",
        "float",
        "Shortest pause between rounds",
        "A round renders the root page and its live sub-pages. The pause before the next "
        "round is random between poll_interval_min and poll_interval_max; lower values find "
        "changes sooner but hit the site (and this machine) harder.",
        1,
        3600,
        "s",
        caution=_below(3, "Below 3 seconds most sites rate-limit or block the bot's IP."),
    ),
    PreferenceSpec(
        "poll_interval_max",
        "poll_interval_max",
        "monitoring",
        "float",
        "Longest pause between rounds",
        "Upper bound of the random pause between rounds of one site. Keep it above "
        "poll_interval_min; randomness makes the traffic look less like a bot.",
        1,
        3600,
        "s",
    ),
    PreferenceSpec(
        "confirm_delay",
        "confirm_delay",
        "monitoring",
        "float",
        "Delay before the confirming render",
        "When a page differs from its snapshot it is rendered again after this delay. "
        "Whatever differs between the two renders (timers, counters, banners) is treated as "
        "volatile and not reported. Longer delays catch slower tickers.",
        0,
        60,
        "s",
    ),
    PreferenceSpec(
        "network_idle_timeout",
        "network_idle_timeout",
        "monitoring",
        "float",
        "Wait for the page to go quiet",
        "How long the browser waits for the page to stop loading before the snapshot is "
        "taken. It is a cap, not a fixed wait: most pages go idle within a second. Pages "
        "that keep polling wait the full time on every render.",
        0,
        30,
        "s",
    ),
    PreferenceSpec(
        "settle_delay",
        "settle_delay",
        "monitoring",
        "float",
        "Extra settle time after network idle",
        "Pause after the page went quiet so JavaScript can finish drawing. 0.5 s produced "
        "identical results to 1 s on the measured sites.",
        0,
        10,
        "s",
    ),
    # ── contracts ───────────────────────────────────────────────────────────
    PreferenceSpec(
        "require_erc20",
        "require_erc20",
        "contracts",
        "bool",
        "Only token-like contracts",
        "On: an EVM contract counts only if it answers totalSupply, symbol or decimals. "
        "Off: any contract with bytecode counts (presales, launchpads, staking, multisigs, "
        "proxies). Plain wallets and example addresses are rejected either way, because "
        "the bytecode check always runs.",
        caution=_when(
            False, "Non-token contracts (routers, multisigs, proxies) will be reported as well."
        ),
    ),
    PreferenceSpec(
        "contract_pause_threshold",
        "contract_pause_threshold",
        "contracts",
        "int",
        "Contracts per page that auto-pause the site",
        "When one page yields this many new confirmed contracts in one round, they are sent "
        "as a .txt document and the site is paused until /resume. Below the threshold each "
        "contract is a separate message.",
        1,
        100,
    ),
    PreferenceSpec(
        "rejected_ttl_hours",
        "rejected_ttl",
        "contracts",
        "float",
        "Re-check rejected addresses after",
        "An address that had no contract is remembered as rejected for this long, then "
        "checked again. Shorter values catch contracts deployed after their address was "
        "published, at the price of more RPC calls. 0 = never re-check.",
        0,
        168,
        "h",
        scale=3600,
        caution=_when(
            0.0,
            "Addresses published before deployment will never be confirmed by the regular monitor.",
        ),
    ),
    PreferenceSpec(
        "deep_scan",
        "deep_scan",
        "contracts",
        "bool",
        "Deep address scan",
        "Also search iframes, shadow DOM and the scripts and JSON answers the page loaded. "
        "Costs about 0.02 s per render and no extra requests.",
        caution=_when(
            False,
            "Addresses hidden in scripts, API answers, iframes and shadow DOM will be missed.",
        ),
    ),
    PreferenceSpec(
        "unverified_recheck_minutes",
        "unverified_recheck",
        "contracts",
        "float",
        "Unverified mode: re-check every",
        "In /unverified mode every new address is announced at once and checked on-chain "
        "again at this interval until it is confirmed.",
        1,
        120,
        "min",
        scale=60,
    ),
    PreferenceSpec(
        "unverified_watch_hours",
        "unverified_watch",
        "contracts",
        "float",
        "Unverified mode: keep watching for",
        "How long an unverified address is re-checked before the bot gives up on it.",
        1,
        168,
        "h",
        scale=3600,
    ),
    PreferenceSpec(
        "unverified_max_per_hour",
        "unverified_max_per_hour",
        "contracts",
        "int",
        "Unverified mode: alerts per hour per site",
        "Above this rate new unverified addresses are collected into one digest message "
        "per hour instead of separate alerts, so a leaderboard cannot flood the chat.",
        1,
        100,
    ),
    # ── alerts ──────────────────────────────────────────────────────────────
    PreferenceSpec(
        "max_change_examples",
        "max_change_examples",
        "alerts",
        "int",
        "Examples in a change alert",
        "How many removed, added and changed elements a change alert quotes per category. "
        "0 sends counts only.",
        0,
        20,
    ),
    PreferenceSpec(
        "enrich_timeout",
        "enrich_timeout",
        "alerts",
        "float",
        "Time budget for token details",
        "How long a contract alert may wait for the token's name, supply and DEX market "
        "data (on-chain calls and DexScreener) before it is sent without them. 0 turns the "
        "lookups off; the explorer and DEX links stay.",
        0,
        30,
        "s",
    ),
    PreferenceSpec(
        "flap_window_hours",
        "flap_window",
        "alerts",
        "float",
        "Flapping memory window",
        "An element that already alerted flap_threshold times within this window is "
        "silenced for as long as it keeps changing (slow counters, rotating banners). "
        "0 turns the memory off.",
        0,
        168,
        "h",
        scale=3600,
        caution=_when(0.0, "Flapping elements will alert on every single change."),
    ),
    PreferenceSpec(
        "flap_threshold",
        "flap_threshold",
        "alerts",
        "int",
        "Alerts before an element is silenced",
        "Number of alerts about the same element inside the window before it counts as "
        "noise. 1 silences after the first alert.",
        1,
        10,
    ),
    # ── turbo & blocking ────────────────────────────────────────────────────
    PreferenceSpec(
        "turbo_interval",
        "turbo_interval",
        "turbo",
        "float",
        "Turbo: seconds between renders",
        "Pause between full renders of a site in turbo mode. Each render takes about 1.5 s "
        "on top of this.",
        1,
        60,
        "s",
        caution=_below(2, "One render per second hammers the site; expect to be blocked."),
    ),
    PreferenceSpec(
        "turbo_default_minutes",
        "turbo_default_minutes",
        "turbo",
        "float",
        "Turbo: default session length",
        "Used when /turbo is started without a duration.",
        1,
        1440,
        "min",
    ),
    PreferenceSpec(
        "turbo_max_minutes",
        "turbo_max_minutes",
        "turbo",
        "float",
        "Turbo: longest allowed session",
        "Any requested duration is clamped to this value.",
        1,
        1440,
        "min",
    ),
    PreferenceSpec(
        "turbo_stop_after",
        "turbo_stop_after",
        "turbo",
        "int",
        "Turbo: stop after N confirmed contracts",
        "The session ends by itself once this many contracts were confirmed. 0 keeps it "
        "running until the timer or /turbo stop.",
        0,
        50,
    ),
    PreferenceSpec(
        "turbo_max_sites",
        "turbo_max_sites",
        "turbo",
        "int",
        "Turbo: sites at the same time",
        "How many sites may be in turbo mode (running or scheduled) at once.",
        1,
        5,
        caution=_above(
            1,
            "Several sites in turbo multiply the browser load; rounds of the "
            "regular monitor will slow down.",
        ),
    ),
    PreferenceSpec(
        "turbo_max_pages",
        "turbo_max_pages",
        "turbo",
        "int",
        "Turbo: pages per render cycle",
        "Root page plus live sub-pages rendered in every turbo cycle, root included.",
        1,
        10,
        caution=_above(6, "More pages than browser tabs (6) makes every cycle wait."),
    ),
    PreferenceSpec(
        "turbo_recheck_interval",
        "turbo_recheck_interval",
        "turbo",
        "float",
        "Turbo: on-chain re-check interval",
        "How often an unverified address found in turbo mode is checked on-chain again.",
        1,
        60,
        "s",
    ),
    PreferenceSpec(
        "block_max_backoff",
        "block_max_backoff",
        "turbo",
        "float",
        "Longest pause after a suspected block",
        "While a site answers with 403/429/503 or challenge pages the pause between rounds "
        "doubles every round, up to this value.",
        10,
        3600,
        "s",
        caution=_below(60, "Backing off less than a minute rarely lets a block expire."),
    ),
)

_BY_KEY: dict[str, PreferenceSpec] = {spec.key: spec for spec in PREFERENCES}
_BY_ATTR: dict[str, PreferenceSpec] = {spec.attr: spec for spec in PREFERENCES}


# Related settings whose order matters: the first must never exceed the second.
_ORDERED_PAIRS: tuple[tuple[str, str], ...] = (
    ("poll_interval_min", "poll_interval_max"),
    ("turbo_default_minutes", "turbo_max_minutes"),
)
_PARTNER = {a: b for a, b in _ORDERED_PAIRS} | {b: a for a, b in _ORDERED_PAIRS}


class Preferences:
    """Validated, persisted overrides of :class:`Settings`; readable as a :class:`Tuning`."""

    def __init__(self, settings: Settings, path: Path) -> None:
        self._settings = settings
        self._path = path
        self._overrides: dict[str, Any] = {}

    # ── lookup ──────────────────────────────────────────────────────────────
    def spec(self, key: str) -> PreferenceSpec:
        spec = _BY_KEY.get(key.strip().lower())
        if spec is None:
            raise PreferenceError(f"unknown setting {key!r}; /settings lists them all")
        return spec

    def default(self, spec: PreferenceSpec) -> Any:
        base = getattr(self._settings, spec.attr)
        if spec.kind == "bool":
            return bool(base)
        value = base / spec.scale
        return round(value) if spec.kind == "int" else float(value)

    def current(self, spec: PreferenceSpec) -> Any:
        return self._overrides.get(spec.key, self.default(spec))

    def is_overridden(self, spec: PreferenceSpec) -> bool:
        return spec.key in self._overrides

    def __getattr__(self, name: str) -> Any:
        # Effective value in base units, under the Settings attribute name.
        spec = _BY_ATTR.get(name)
        if spec is None:
            raise AttributeError(name)
        value = self.current(spec)
        if spec.kind == "bool" or spec.scale == 1.0:
            return value
        return value * spec.scale

    # ── changes ─────────────────────────────────────────────────────────────
    def set(self, key: str, raw: str, *, confirmed: bool = False) -> PreferenceChange:
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
        if key is None:
            keys = [s.key for s in PREFERENCES if s.key in self._overrides]
        else:
            keys = [self.spec(key).key]
        changes = []
        for name in keys:
            if name not in self._overrides:
                continue
            changes.append(self._drop_override(name))
            # Resetting one side of a related pair must not leave e.g. min > max behind.
            partner = _PARTNER.get(name)
            if partner in self._overrides and not self._consistent(name):
                changes.append(self._drop_override(partner))
        if changes:
            self.save()
        return changes

    def _drop_override(self, key: str) -> PreferenceChange:
        spec = self.spec(key)
        return PreferenceChange(spec, self._overrides.pop(key), self.default(spec))

    def _consistent(self, key: str) -> bool:
        spec = self.spec(key)
        try:
            self._check_relations(spec, self.current(spec))
        except PreferenceError:
            return False
        return True

    # ── persistence ─────────────────────────────────────────────────────────
    def load(self) -> None:
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
        # Every value is range-checked above, but a hand-edited file can still hold
        # a pair that contradicts itself (min > max); such a pair is not trusted.
        for lower, upper in _ORDERED_PAIRS:
            if self.current(self.spec(lower)) > self.current(self.spec(upper)):
                log.warning(
                    "preferences %s and %s contradict each other; both dropped", lower, upper
                )
                self._overrides.pop(lower, None)
                self._overrides.pop(upper, None)
        log.info("%d preference override(s) loaded", len(self._overrides))

    def save(self) -> None:
        atomic_write_text(self._path, json.dumps(self._overrides, indent=2, sort_keys=True))

    # ── presentation ────────────────────────────────────────────────────────
    def format_value(self, spec: PreferenceSpec, value: Any) -> str:
        if spec.kind == "bool":
            return "on" if value else "off"
        text = f"{value:g}" if spec.kind == "float" else str(value)
        return f"{text} {spec.unit}".strip()

    def overview(self) -> str:
        lines = ["⚙️ Settings (live values; * = changed from .env)"]
        for section, title in SECTIONS.items():
            lines.append(f"\n{title}")
            for spec in PREFERENCES:
                if spec.section != section:
                    continue
                mark = "*" if self.is_overridden(spec) else ""
                value = escape(self.format_value(spec, self.current(spec)))
                lines.append(f"• {code(spec.key)} = {value}{mark}")
        lines.append(
            f"\n{code('/settings <key>')} explains one setting, "
            f"{code('/set <key> <value>')} changes it, "
            f"{code('/settings reset <key|all>')} restores the .env value."
        )
        return "\n".join(lines)

    def describe(self, key: str) -> str:
        spec = self.spec(key)
        lines = [f"⚙️ {code(spec.key)}: {escape(spec.label)}", escape(spec.description)]
        if spec.kind == "bool":
            lines.append("Values: on / off")
        else:
            unit = f" {spec.unit}" if spec.unit else ""
            lines.append(f"Range: {spec.minimum:g} to {spec.maximum:g}{unit}")
        lines.append(f"Current: {escape(self.format_value(spec, self.current(spec)))}")
        lines.append(f"Default from .env: {escape(self.format_value(spec, self.default(spec)))}")
        if spec.caution is not None:
            lines.append("Risky values ask you to repeat the command with 'confirm'.")
        lines.append(f"Change: {code(f'/set {spec.key} <value>')}")
        return "\n".join(lines)

    # ── internals ───────────────────────────────────────────────────────────
    @staticmethod
    def _parse(spec: PreferenceSpec, raw: str) -> Any:
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
            # nan compares false against every bound and would poison the delays.
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
        for lower, upper in _ORDERED_PAIRS:
            if spec.key == lower:
                limit = self.current(self.spec(upper))
                if value > limit:
                    raise PreferenceError(f"{lower} must not exceed {upper} ({limit:g})")
            elif spec.key == upper:
                limit = self.current(self.spec(lower))
                if value < limit:
                    raise PreferenceError(f"{upper} must not be below {lower} ({limit:g})")
