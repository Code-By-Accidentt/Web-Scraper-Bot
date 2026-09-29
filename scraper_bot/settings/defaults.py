"""Built-in defaults, and the ones that changed since earlier releases."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_SOLANA_RPC_URL = "https://solana-rpc.publicnode.com"
DEFAULT_SUI_RPC_URL = "https://sui-rpc.publicnode.com"

DEFAULT_SUB_URL_PATTERNS: tuple[str, ...] = (
    "https://app.{domain}",
    "https://app.{domain}/leaderboard",
    "https://{domain}/dashboard",
    "https://docs.{domain}",
    "https://{domain}/app",
    "https://doc.{domain}",
    "https://{domain}/create",
)

DEFAULT_IGNORED_ATTRIBUTES: tuple[str, ...] = (
    "nonce",
    "integrity",
    "csrf-token",
    "data-csrf",
    "data-csrf-token",
    "data-nonce",
    "data-timestamp",
    "data-reactid",
    "data-react-checksum",
)

DEFAULT_BLOCKED_RESOURCE_TYPES: tuple[str, ...] = ("image", "media", "font")
# No GPU means software WebGL: on a 4-core VPS a page took 33-38 s with it, 5.5 s without.
DEFAULT_BROWSER_ARGS = "--disable-3d-apis --disable-webgl --disable-webgl2"


@dataclass(frozen=True, slots=True)
class ChangedDefault:
    """A built-in default that changed since an earlier release."""

    key: str
    previous: str  # the old default, as older .env.example files spell it
    current: str


# A .env copied from an older .env.example pins its old defaults, and we report them at start.
CHANGED_DEFAULTS: tuple[ChangedDefault, ...] = (
    # 2.1: api.mainnet-beta answered 60 of 100 parallel calls with HTTP 429
    ChangedDefault("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com", DEFAULT_SOLANA_RPC_URL),
    # a 10-minute pause after one failed check could hide a launch
    ChangedDefault("RPC_RETRY_DELAY", "600", "10"),
    # the pending watch checks a turbo site's addresses every second
    ChangedDefault("TURBO_RECHECK_INTERVAL", "5", "1"),
    # wallet-connect scripts carry dozens of infrastructure addresses
    ChangedDefault("DEEP_SCAN", "true", "false"),
    # 2.0: measured on real sites (docs/development.md)
    ChangedDefault("POLL_INTERVAL_MIN", "5", "4"),
    ChangedDefault("POLL_INTERVAL_MAX", "15", "10"),
    ChangedDefault("MAX_CONCURRENT_PAGES", "6", "8"),
    ChangedDefault("PAGE_LOAD_TIMEOUT", "30", "20"),
    ChangedDefault("NETWORK_IDLE_TIMEOUT", "6", "2"),
    ChangedDefault("NETWORK_IDLE_TIMEOUT", "4", "2"),
    ChangedDefault("SETTLE_DELAY", "1.0", "0.25"),
    ChangedDefault("SETTLE_DELAY", "0.5", "0.25"),
    ChangedDefault("RPC_TIMEOUT", "8", "5"),
    ChangedDefault("ENRICH_TIMEOUT", "3", "2"),
    ChangedDefault("DEEP_SCAN_MAX_MB", "10", "5"),
)
