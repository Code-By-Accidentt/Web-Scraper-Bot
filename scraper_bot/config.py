"""Application settings.

Everything configurable lives in :class:`Settings`, which is built once from the
process environment (plus an optional ``.env`` file) by :meth:`Settings.from_env`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


class ConfigError(ValueError):
    """The environment does not describe a valid configuration."""


@dataclass(frozen=True, slots=True)
class EvmNetwork:
    """One EVM-compatible chain queried while validating ``0x`` addresses."""

    name: str
    rpc_url: str


# (display name, environment variable, default endpoint)
DEFAULT_EVM_NETWORKS: tuple[tuple[str, str, str], ...] = (
    ("Ethereum", "ETH_RPC_URL", "https://ethereum-rpc.publicnode.com"),
    ("Base", "BASE_RPC_URL", "https://base-rpc.publicnode.com"),
    ("Arbitrum", "ARB_RPC_URL", "https://arbitrum-one-rpc.publicnode.com"),
    ("Optimism", "OPT_RPC_URL", "https://optimism-rpc.publicnode.com"),
    ("BSC", "BSC_RPC_URL", "https://bsc-rpc.publicnode.com"),
    ("Fantom", "FANTOM_RPC_URL", "https://rpcapi.fantom.network"),
    ("Sonic", "SONIC_RPC_URL", "https://rpc.soniclabs.com"),
    ("Avalanche", "AVAX_RPC_URL", "https://avalanche-c-chain-rpc.publicnode.com"),
)

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

_LIST_SEPARATOR_RE = re.compile(r"[,;\s]+")

# Accepted spellings of booleans in .env and /set
TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
FALSE_WORDS = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, validated configuration for one bot process."""

    # Telegram
    telegram_bot_token: str
    chat_ids: tuple[int, ...]
    allowed_chat_ids: tuple[int, ...]

    # Monitoring
    poll_interval_min: float
    poll_interval_max: float
    max_concurrent_pages: int
    page_load_timeout: float
    network_idle_timeout: float
    settle_delay: float
    confirm_delay: float
    contract_pause_threshold: int
    sub_url_patterns: tuple[str, ...]
    ignored_attributes: tuple[str, ...]
    max_change_examples: int
    enrich_timeout: float
    flap_window: float
    flap_threshold: int

    # Address discovery
    deep_scan: bool
    deep_scan_max_bytes: int

    # Turbo mode and blocking
    turbo_interval: float
    turbo_default_minutes: float
    turbo_max_minutes: float
    turbo_stop_after: int
    turbo_max_sites: int
    turbo_max_pages: int
    turbo_recheck_interval: float
    block_max_backoff: float

    # Unverified mode (/unverified <url> on)
    unverified_recheck: float
    unverified_watch: float
    unverified_max_per_hour: int

    # Browser
    browser_channel: str
    headless: bool
    blocked_resource_types: tuple[str, ...]

    # Blockchain
    evm_networks: tuple[EvmNetwork, ...]
    solana_rpc_url: str
    sui_rpc_url: str
    require_erc20: bool
    rpc_timeout: float
    max_concurrent_rpc: int
    rejected_ttl: float
    rpc_retry_delay: float

    # Misc
    data_dir: Path
    log_level: str
    log_file: Path | None
    heartbeat_interval: float

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv_path: str | os.PathLike[str] | None = ".env",
    ) -> Settings:
        """Build settings from ``env`` (default: ``os.environ``) layered over ``.env``.

        Real environment variables win over values from the ``.env`` file.
        """
        merged: dict[str, str] = {}
        if dotenv_path is not None and Path(dotenv_path).is_file():
            merged.update({k: v for k, v in dotenv_values(dotenv_path).items() if v is not None})
        merged.update(os.environ if env is None else env)
        reader = _EnvReader(merged)

        token = reader.text("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required (get one from @BotFather)")

        poll_min = reader.number("POLL_INTERVAL_MIN", 5.0, minimum=1.0)
        poll_max = reader.number("POLL_INTERVAL_MAX", 15.0, minimum=1.0)
        if poll_max < poll_min:
            raise ConfigError("POLL_INTERVAL_MAX must be >= POLL_INTERVAL_MIN")

        networks = tuple(
            EvmNetwork(name, reader.text(var, default))
            for name, var, default in DEFAULT_EVM_NETWORKS
            if reader.text(var, default)
        )
        if not networks:
            raise ConfigError("at least one EVM RPC endpoint must be configured")

        log_file = reader.text("LOG_FILE")
        return cls(
            telegram_bot_token=token,
            chat_ids=reader.int_list("TELEGRAM_CHAT_IDS"),
            allowed_chat_ids=reader.int_list("TELEGRAM_ALLOWED_CHAT_IDS"),
            poll_interval_min=poll_min,
            poll_interval_max=poll_max,
            max_concurrent_pages=reader.integer("MAX_CONCURRENT_PAGES", 6, minimum=1),
            page_load_timeout=reader.number("PAGE_LOAD_TIMEOUT", 30.0, minimum=1.0),
            network_idle_timeout=reader.number("NETWORK_IDLE_TIMEOUT", 4.0, minimum=0.0),
            settle_delay=reader.number("SETTLE_DELAY", 0.5, minimum=0.0),
            confirm_delay=reader.number("CONFIRM_DELAY", 2.0, minimum=0.0),
            contract_pause_threshold=reader.integer("CONTRACT_PAUSE_THRESHOLD", 5, minimum=1),
            sub_url_patterns=reader.text_list("SUB_URL_PATTERNS", DEFAULT_SUB_URL_PATTERNS),
            ignored_attributes=tuple(
                a.lower()
                for a in reader.text_list("IGNORED_ATTRIBUTES", DEFAULT_IGNORED_ATTRIBUTES)
            ),
            max_change_examples=reader.integer("MAX_CHANGE_EXAMPLES", 5, minimum=0),
            enrich_timeout=reader.number("ENRICH_TIMEOUT", 3.0, minimum=0.0),
            flap_window=reader.number("FLAP_WINDOW_HOURS", 24.0, minimum=0.0) * 3600.0,
            flap_threshold=reader.integer("FLAP_THRESHOLD", 2, minimum=1),
            deep_scan=reader.flag("DEEP_SCAN", True),
            deep_scan_max_bytes=int(
                reader.number("DEEP_SCAN_MAX_MB", 10.0, minimum=0.0) * 1024 * 1024
            ),
            turbo_interval=reader.number("TURBO_INTERVAL", 2.0, minimum=1.0),
            turbo_default_minutes=reader.number("TURBO_DEFAULT_MINUTES", 30.0, minimum=1.0),
            turbo_max_minutes=reader.number("TURBO_MAX_MINUTES", 180.0, minimum=1.0),
            turbo_stop_after=reader.integer("TURBO_STOP_AFTER", 1, minimum=0),
            turbo_max_sites=reader.integer("TURBO_MAX_SITES", 1, minimum=1),
            turbo_max_pages=reader.integer("TURBO_MAX_PAGES", 4, minimum=1),
            turbo_recheck_interval=reader.number("TURBO_RECHECK_INTERVAL", 5.0, minimum=1.0),
            block_max_backoff=reader.number("BLOCK_MAX_BACKOFF", 600.0, minimum=1.0),
            unverified_recheck=reader.number("UNVERIFIED_RECHECK_MINUTES", 5.0, minimum=1.0) * 60,
            unverified_watch=reader.number("UNVERIFIED_WATCH_HOURS", 24.0, minimum=1.0) * 3600,
            unverified_max_per_hour=reader.integer("UNVERIFIED_MAX_PER_HOUR", 10, minimum=1),
            browser_channel=reader.text("BROWSER_CHANNEL", "chromium"),
            headless=reader.flag("HEADLESS", True),
            blocked_resource_types=reader.text_list(
                "BLOCKED_RESOURCE_TYPES", DEFAULT_BLOCKED_RESOURCE_TYPES
            ),
            evm_networks=networks,
            solana_rpc_url=reader.text("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
            sui_rpc_url=reader.text("SUI_RPC_URL", "https://sui-rpc.publicnode.com"),
            require_erc20=reader.flag("REQUIRE_ERC20", True),
            rpc_timeout=reader.number("RPC_TIMEOUT", 8.0, minimum=1.0),
            max_concurrent_rpc=reader.integer("MAX_CONCURRENT_RPC", 8, minimum=1),
            rejected_ttl=reader.number("REJECTED_TTL_HOURS", 6.0, minimum=0.0) * 3600.0,
            rpc_retry_delay=reader.number("RPC_RETRY_DELAY", 600.0, minimum=0.0),
            data_dir=Path(reader.text("DATA_DIR", "data")),
            log_level=reader.text("LOG_LEVEL", "INFO").upper(),
            log_file=Path(log_file) if log_file else None,
            heartbeat_interval=reader.number("HEARTBEAT_INTERVAL", 15.0, minimum=1.0),
        )


class _EnvReader:
    """Typed accessors over a flat string mapping with uniform error reporting."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values

    def text(self, key: str, default: str = "") -> str:
        return self._values.get(key, default).strip()

    def flag(self, key: str, default: bool) -> bool:
        raw = self.text(key)
        if not raw:
            return default
        lowered = raw.lower()
        if lowered in TRUE_WORDS:
            return True
        if lowered in FALSE_WORDS:
            return False
        raise ConfigError(f"{key} must be a boolean, got {raw!r}")

    def integer(self, key: str, default: int, *, minimum: int | None = None) -> int:
        raw = self.text(key)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
        if minimum is not None and value < minimum:
            raise ConfigError(f"{key} must be >= {minimum}")
        return value

    def number(self, key: str, default: float, *, minimum: float | None = None) -> float:
        raw = self.text(key)
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
        if minimum is not None and value < minimum:
            raise ConfigError(f"{key} must be >= {minimum}")
        return value

    def text_list(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        """Comma separated list; brackets, semicolons, quotes and stray spaces are tolerated."""
        raw = self.text(key).strip("[]()")
        if not raw.strip():
            return default
        items = (item.strip().strip("'\"") for item in _LIST_SEPARATOR_RE.split(raw))
        return tuple(item for item in items if item)

    def int_list(self, key: str) -> tuple[int, ...]:
        items = self.text_list(key)
        try:
            return tuple(int(item) for item in items)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a comma separated list of integers") from exc
