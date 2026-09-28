"""Every setting that can be changed live with /set."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Caution = Callable[[Any], str | None]


@dataclass(frozen=True, slots=True)
class PreferenceSpec:
    """One setting /set can change: its names, type, limits and help text.

    The user types values in ``unit``; ``scale`` turns them into the base unit Settings uses.
    """

    key: str  # user-facing name, e.g. rejected_ttl_hours
    attr: str  # Settings attribute holding the base-unit value, e.g. rejected_ttl
    section: str
    kind: str  # bool | int | float
    label: str
    description: str
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    scale: float = 1.0  # base value = user value * scale (hours -> seconds = 3600)
    caution: Caution | None = None  # returns a warning for a risky value, else None

    def format(self, value: Any) -> str:
        """Return ``value``, given in the user's unit, as people read it: ``on``, ``4 s``."""
        if self.kind == "bool":
            return "on" if value else "off"
        text = f"{value:g}" if self.kind == "float" else str(value)
        return f"{text} {self.unit}".strip()


SECTIONS: dict[str, str] = {
    "monitoring": "Monitoring & rendering",
    "contracts": "Contracts & scanning",
    "alerts": "Alerts & noise",
    "turbo": "Turbo & blocking",
}


def _below(limit: float, text: str) -> Caution:
    """Return a caution that warns with ``text`` when the value is below ``limit``."""
    return lambda value: text if value < limit else None


def _above(limit: float, text: str) -> Caution:
    """Return a caution that warns with ``text`` when the value is above ``limit``."""
    return lambda value: text if value > limit else None


def _when(flag: bool | float, text: str) -> Caution:
    """Return a caution that warns with ``text`` when the value equals ``flag``."""
    return lambda value: text if value == flag else None


PREFERENCES: tuple[PreferenceSpec, ...] = (
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
        "Pause after the page went quiet so JavaScript can finish drawing. 0.25 s produced "
        "the same result as 0.5 s and 1 s on every measured page; each 0.25 s costs 0.25 s "
        "per render.",
        0,
        10,
        "s",
    ),
    PreferenceSpec(
        "render_failure_threshold",
        "render_failure_threshold",
        "monitoring",
        "int",
        "Load timeouts in a row before a page is flagged",
        "When a page fails to load this many times in a row you get one alert and its load "
        "timeout is stretched (doubled, up to three times PAGE_LOAD_TIMEOUT) until it renders "
        "again. 0 turns the counter off.",
        0,
        50,
    ),
    PreferenceSpec(
        "require_erc20",
        "require_erc20",
        "contracts",
        "bool",
        "Only token-like contracts",
        "On: an EVM contract counts only if it answers totalSupply and decimals and is "
        "neither an LP pair (its token is checked instead) nor a vault. Off: any contract "
        "with bytecode counts (presales, launchpads, staking, multisigs, proxies). Wallets, "
        "EIP-7702 wallets included, and example addresses are rejected either way.",
        caution=_when(
            False, "Non-token contracts (routers, multisigs, proxies) will be reported as well."
        ),
    ),
    PreferenceSpec(
        "skip_stable_copies",
        "skip_stable_copies",
        "contracts",
        "bool",
        "Skip copies of stablecoins and wrapped coins",
        "On: a token whose symbol is a stablecoin's or a wrapped or staked coin's (USDC, "
        "USDT, WETH, stETH, ...) but whose address is not on the stop-list is a copy (bridged, "
        "plumbing or a scam) and is not announced. Look-alike letters count (USDС with a "
        "Cyrillic С), longer symbols do not (USDCAT). It is judged per network: the same "
        "address with another symbol elsewhere is still announced there. On EVM and Sui it "
        "needs require_erc20 on.",
        caution=_when(
            False, "Bridged and fake USDC, USDT, WETH and the like will be announced as launches."
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
        "An address that was not a token is remembered as rejected for this long, then "
        "checked again. Addresses published before deployment are re-checked by the "
        "pending watch on its own; this decides when anything else (a proxy initialised "
        "later, an address the watch gave up on) is looked at again. 0 = never re-check.",
        0,
        168,
        "h",
        scale=3600,
        caution=_when(
            0.0,
            "Rejected addresses (e.g. a proxy initialised later) will never be re-checked.",
        ),
    ),
    PreferenceSpec(
        "deep_scan",
        "deep_scan",
        "contracts",
        "bool",
        "Deep address scan",
        "Also search iframes, shadow DOM and the scripts and JSON answers the page loaded. "
        "What the scripts carried when deep scan first saw a page is ignored until the page "
        "itself shows it (wallet-connect bundles hold dozens of well-known addresses); later "
        "ones are checked like any other. No extra requests, but several MB of scripts per "
        "render cost CPU (0.8 s per 5 MB on a desktop CPU, per 2.5 MB on a 4-core VPS).",
        caution=_when(
            True,
            "App pages carry many infrastructure addresses in their scripts, and each render "
            "costs noticeably more CPU on a small server.",
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
        "A contract alert goes out at once; the token's name, supply, age and DEX market "
        "data (on-chain calls and DexScreener) are looked up afterwards for at most this "
        "long and edited into the same message. 0 turns the lookups off; the explorer and "
        "DEX links stay.",
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
    PreferenceSpec(
        "turbo_interval",
        "turbo_interval",
        "turbo",
        "float",
        "Turbo: seconds between renders",
        "Seconds from the start of one turbo cycle to the start of the next; a cycle that "
        "takes longer starts the next one at once.",
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
        caution=_above(
            8,
            "More pages than browser tabs (MAX_CONCURRENT_PAGES, 8 by default) makes "
            "every cycle wait.",
        ),
    ),
    PreferenceSpec(
        "turbo_recheck_interval",
        "turbo_recheck_interval",
        "turbo",
        "float",
        "Turbo: on-chain check interval",
        "How often the addresses of a site in turbo mode that are not a token yet are checked "
        "on-chain (the watch for tokens published before deployment). Elsewhere an address "
        "the page names as the CA is checked every second for 15 min, then every 5 s, then "
        "every 30 s; any other address every 30 s.",
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
        "While a site answers with 401/403/407/429/503, challenge pages or connection "
        "resets, the pause between rounds doubles every round, up to this value (a longer "
        "Retry-After from the site is honoured).",
        10,
        3600,
        "s",
        caution=_below(60, "Backing off less than a minute rarely lets a block expire."),
    ),
)

BY_KEY: dict[str, PreferenceSpec] = {spec.key: spec for spec in PREFERENCES}
BY_ATTR: dict[str, PreferenceSpec] = {spec.attr: spec for spec in PREFERENCES}


# In each pair the first setting must never exceed the second.
ORDERED_PAIRS: tuple[tuple[str, str], ...] = (
    ("poll_interval_min", "poll_interval_max"),
    ("turbo_default_minutes", "turbo_max_minutes"),
)
PARTNERS = {a: b for a, b in ORDERED_PAIRS} | {b: a for a, b in ORDERED_PAIRS}
