"""Process-level settings, read once from the environment and ``.env``.

Knobs that may change at runtime are layered on top by :mod:`scraper_bot.preferences`.
"""

from __future__ import annotations

import dataclasses
import math
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import dotenv_values

from scraper_bot.chains.networks import EVM_CHAINS


def _masked(url: str) -> str:
    """``url`` fit for a log: scheme and host only, since API keys hide in paths and logins."""
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*://)?([^/?#]*)(.*)$", url.strip(), re.S)
    if match is None:
        return "'…'"
    scheme, authority, rest = match.groups()
    host = authority.rpartition("@")[2]
    login = "…@" if "@" in authority else ""
    return repr(f"{scheme or ''}{login}{host}{'/…' if rest else ''}")


class ConfigError(ValueError):
    """The environment does not describe a valid configuration."""


@dataclass(frozen=True, slots=True)
class EvmNetwork:
    """One EVM-compatible chain queried while validating 20-byte ``0x`` addresses."""

    name: str
    rpc_url: str
    chain_id: int | None = None  # what eth_chainId must answer (None: not checked)
    optional: bool = False  # its failure never blocks a verdict (see EvmChain)


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
# Without a GPU a 3D scene renders in software and starves the browser: flutch.fun took
# 33-38 s per page with WebGL and 5.5 s without on a 4-core VPS.
DEFAULT_BROWSER_ARGS = "--disable-3d-apis --disable-webgl --disable-webgl2"

_LIST_SEPARATOR_RE = re.compile(r"[,;\s]+")

# Accepted spellings of booleans in .env and /set
TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
FALSE_WORDS = frozenset({"0", "false", "no", "off"})


LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


@dataclass(frozen=True, slots=True)
class ChangedDefault:
    """A built-in default that changed since an earlier release."""

    key: str
    previous: str  # the old default, as older .env.example files spell it
    current: str


# .env.example spells out every default, so a .env copied from an older release pins the
# old values. A key still set to a previous default is reported at start-up.
CHANGED_DEFAULTS: tuple[ChangedDefault, ...] = (
    # 2.1 ─ api.mainnet-beta answered 60 of 100 parallel getAccountInfo calls with HTTP 429
    ChangedDefault("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com", DEFAULT_SOLANA_RPC_URL),
    # a 10-minute pause after one failed check could hide a launch
    ChangedDefault("RPC_RETRY_DELAY", "600", "10"),
    # the pending watch checks a turbo site's addresses every second
    ChangedDefault("TURBO_RECHECK_INTERVAL", "5", "1"),
    # scripts and JSON of wallet-connect apps carry dozens of infrastructure addresses
    ChangedDefault("DEEP_SCAN", "true", "false"),
    # 2.0 ─ measured on real sites before the 2.0 merge (docs/development.md)
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
    render_failure_threshold: int
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
    browser_args: tuple[str, ...]  # extra Chromium command-line switches

    # Blockchain
    http2: bool  # RPC and Telegram over HTTP/2: parallel calls share one warm connection
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

    # Keys still set to a default of an earlier release (see CHANGED_DEFAULTS)
    outdated_defaults: tuple[ChangedDefault, ...] = ()

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv_path: str | os.PathLike[str] | None = ".env",
    ) -> Settings:
        """Settings from ``env`` (default ``os.environ``) over ``.env``; real variables win."""
        merged: dict[str, str] = {}
        if dotenv_path is not None and Path(dotenv_path).is_file():
            merged.update({k: v for k, v in dotenv_values(dotenv_path).items() if v is not None})
        merged.update(os.environ if env is None else env)
        reader = _EnvReader(merged)

        token = reader.text("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required (get one from @BotFather)")

        poll_min = reader.number("POLL_INTERVAL_MIN", 4.0, minimum=1.0)
        poll_max = reader.number("POLL_INTERVAL_MAX", 10.0, minimum=1.0)
        if poll_max < poll_min:
            raise ConfigError("POLL_INTERVAL_MAX must be >= POLL_INTERVAL_MIN")

        networks = tuple(
            EvmNetwork(chain.name, reader.url(chain.env_var, url), chain.chain_id, chain.optional)
            for chain in EVM_CHAINS
            if (url := reader.text(chain.env_var, chain.default_rpc))
        )
        if not networks:
            raise ConfigError("at least one EVM RPC endpoint must be configured")
        solana_rpc_url = reader.url(
            "SOLANA_RPC_URL", reader.text_or_default("SOLANA_RPC_URL", DEFAULT_SOLANA_RPC_URL)
        )
        sui_rpc_url = reader.url(
            "SUI_RPC_URL", reader.text_or_default("SUI_RPC_URL", DEFAULT_SUI_RPC_URL)
        )

        log_file = reader.text("LOG_FILE")
        return cls(
            telegram_bot_token=token,
            chat_ids=reader.int_list("TELEGRAM_CHAT_IDS"),
            allowed_chat_ids=reader.int_list("TELEGRAM_ALLOWED_CHAT_IDS"),
            poll_interval_min=poll_min,
            poll_interval_max=poll_max,
            max_concurrent_pages=reader.integer("MAX_CONCURRENT_PAGES", 8, minimum=1),
            page_load_timeout=reader.number("PAGE_LOAD_TIMEOUT", 20.0, minimum=1.0),
            network_idle_timeout=reader.number("NETWORK_IDLE_TIMEOUT", 2.0, minimum=0.0),
            settle_delay=reader.number("SETTLE_DELAY", 0.25, minimum=0.0),
            render_failure_threshold=reader.integer("RENDER_FAILURE_THRESHOLD", 5, minimum=0),
            confirm_delay=reader.number("CONFIRM_DELAY", 2.0, minimum=0.0),
            contract_pause_threshold=reader.integer("CONTRACT_PAUSE_THRESHOLD", 5, minimum=1),
            sub_url_patterns=reader.text_list("SUB_URL_PATTERNS", DEFAULT_SUB_URL_PATTERNS),
            ignored_attributes=tuple(
                a.lower()
                for a in reader.text_list("IGNORED_ATTRIBUTES", DEFAULT_IGNORED_ATTRIBUTES)
            ),
            max_change_examples=reader.integer("MAX_CHANGE_EXAMPLES", 5, minimum=0),
            enrich_timeout=reader.number("ENRICH_TIMEOUT", 2.0, minimum=0.0),
            flap_window=reader.number("FLAP_WINDOW_HOURS", 24.0, minimum=0.0) * 3600.0,
            flap_threshold=reader.integer("FLAP_THRESHOLD", 2, minimum=1),
            deep_scan=reader.flag("DEEP_SCAN", False),
            deep_scan_max_bytes=int(
                reader.number("DEEP_SCAN_MAX_MB", 5.0, minimum=0.0) * 1024 * 1024
            ),
            turbo_interval=reader.number("TURBO_INTERVAL", 2.0, minimum=1.0),
            turbo_default_minutes=reader.number("TURBO_DEFAULT_MINUTES", 30.0, minimum=1.0),
            turbo_max_minutes=reader.number("TURBO_MAX_MINUTES", 180.0, minimum=1.0),
            turbo_stop_after=reader.integer("TURBO_STOP_AFTER", 1, minimum=0),
            turbo_max_sites=reader.integer("TURBO_MAX_SITES", 1, minimum=1),
            turbo_max_pages=reader.integer("TURBO_MAX_PAGES", 4, minimum=1),
            turbo_recheck_interval=reader.number("TURBO_RECHECK_INTERVAL", 1.0, minimum=1.0),
            block_max_backoff=reader.number("BLOCK_MAX_BACKOFF", 600.0, minimum=1.0),
            unverified_recheck=reader.number("UNVERIFIED_RECHECK_MINUTES", 5.0, minimum=1.0) * 60,
            unverified_watch=reader.number("UNVERIFIED_WATCH_HOURS", 24.0, minimum=1.0) * 3600,
            unverified_max_per_hour=reader.integer("UNVERIFIED_MAX_PER_HOUR", 10, minimum=1),
            browser_channel=reader.text("BROWSER_CHANNEL", "chromium"),
            headless=reader.flag("HEADLESS", True),
            blocked_resource_types=reader.text_list(
                "BLOCKED_RESOURCE_TYPES", DEFAULT_BLOCKED_RESOURCE_TYPES
            ),
            browser_args=reader.arguments("BROWSER_ARGS", DEFAULT_BROWSER_ARGS),
            http2=reader.flag("HTTP2", True),
            evm_networks=networks,
            # (an empty EVM URL skips that network; Solana and Sui cannot be skipped)
            solana_rpc_url=solana_rpc_url,
            sui_rpc_url=sui_rpc_url,
            require_erc20=reader.flag("REQUIRE_ERC20", True),
            rpc_timeout=reader.number("RPC_TIMEOUT", 5.0, minimum=1.0),
            max_concurrent_rpc=reader.integer("MAX_CONCURRENT_RPC", 8, minimum=1),
            rejected_ttl=reader.number("REJECTED_TTL_HOURS", 6.0, minimum=0.0) * 3600.0,
            rpc_retry_delay=reader.number("RPC_RETRY_DELAY", 10.0, minimum=0.0),
            data_dir=Path(reader.text_or_default("DATA_DIR", "data")),
            log_level=reader.choice("LOG_LEVEL", "INFO", LOG_LEVELS),
            log_file=Path(log_file) if log_file else None,
            heartbeat_interval=reader.number("HEARTBEAT_INTERVAL", 15.0, minimum=1.0),
            outdated_defaults=find_outdated_defaults(merged),
        )


def find_outdated_defaults(
    values: Mapping[str, str], changes: tuple[ChangedDefault, ...] | None = None
) -> tuple[ChangedDefault, ...]:
    """The changed defaults (``CHANGED_DEFAULTS``) that ``values`` still set to the old value.

    ``previous`` of each result is the value as ``values`` spell it (``on`` for ``true``).
    """
    return tuple(
        dataclasses.replace(change, previous=values[change.key].strip())
        for change in (CHANGED_DEFAULTS if changes is None else changes)
        if change.key in values and _same_setting(values[change.key], change.previous)
    )


def _same_setting(value: str, previous: str) -> bool:
    """Equal when read as ``previous``'s type: ``on`` == ``true``, ``600`` == ``600.0``."""
    text, old = value.strip().lower(), previous.strip().lower()
    if old in {"true", "false"}:
        return (text in TRUE_WORDS) if old == "true" else (text in FALSE_WORDS)
    try:
        return float(text) == float(old)
    except ValueError:
        return text.rstrip("/") == old.rstrip("/")


def split_arguments(
    text: str, key: str = "value", *, windows: bool | None = None
) -> tuple[str, ...]:
    """``text`` split into command-line arguments, quotes removed."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    if os.name == "nt" if windows is None else windows:
        lexer.escape = ""  # a backslash in a Windows path is a separator, not an escape
    try:
        return tuple(lexer)
    except ValueError as exc:
        raise ConfigError(f"{key} is not a valid argument list: {exc}") from exc


class _EnvReader:
    """Typed accessors over a flat string mapping with uniform error reporting."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values

    def text(self, key: str, default: str = "") -> str:
        return self._values.get(key, default).strip()

    def text_or_default(self, key: str, default: str) -> str:
        """Like :meth:`text`, but an empty value also means the default."""
        return self.text(key) or default

    @staticmethod
    def url(key: str, value: str) -> str:
        """``value`` when it is an http(s) URL httpx can send to; a typo stops the start."""
        try:
            parsed = httpx.URL(value)
        except httpx.InvalidURL as exc:
            raise ConfigError(f"{key} is not a valid URL ({exc}): {_masked(value)}") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ConfigError(f"{key} must be an http(s) URL with a host, got {_masked(value)}")
        return value

    def choice(self, key: str, default: str, allowed: frozenset[str]) -> str:
        value = self.text_or_default(key, default).upper()
        if value not in allowed:
            raise ConfigError(f"{key} must be one of {', '.join(sorted(allowed))}, got {value!r}")
        return value

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
        if not math.isfinite(value):
            raise ConfigError(f"{key} must be a finite number, got {raw!r}")
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

    def arguments(self, key: str, default: str) -> tuple[str, ...]:
        """Command-line switches split like a shell does, so ``--x=a,b`` stays whole.

        Only an absent key gets ``default``; an empty value means no switches at all. On
        Windows backslashes are path separators, not escapes (``--user-data-dir=C:\\x``).
        """
        return split_arguments(self.text(key, default), key)

    def int_list(self, key: str) -> tuple[int, ...]:
        items = self.text_list(key)
        try:
            return tuple(int(item) for item in items)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a comma separated list of integers") from exc
