"""Settings read once from the environment and ``.env``."""

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
from scraper_bot.settings.defaults import (
    CHANGED_DEFAULTS,
    DEFAULT_BLOCKED_RESOURCE_TYPES,
    DEFAULT_BROWSER_ARGS,
    DEFAULT_IGNORED_ATTRIBUTES,
    DEFAULT_SOLANA_RPC_URL,
    DEFAULT_SUB_URL_PATTERNS,
    DEFAULT_SUI_RPC_URL,
    ChangedDefault,
)


def _masked(url: str) -> str:
    """Return ``url`` quoted and cut down to its scheme and host, fit for a log.

    API keys often hide in the path or the login, so both are replaced with an ellipsis.
    """
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


_LIST_SEPARATOR_RE = re.compile(r"[,;\s]+")

TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
FALSE_WORDS = frozenset({"0", "false", "no", "off"})


LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
# rebooter.py restarts a bot whose heartbeat is 300 s old, so several beats must fit in that.
MAX_HEARTBEAT_INTERVAL = 60.0


@dataclass(frozen=True, slots=True)
class Settings:
    """The checked, unchangeable configuration of one bot process.

    Durations are in seconds, even where the .env key takes minutes or hours; only the
    fields named ``*_minutes`` hold minutes.
    """

    telegram_bot_token: str
    chat_ids: tuple[int, ...]
    allowed_chat_ids: tuple[int, ...]

    poll_interval_min: float
    poll_interval_max: float
    max_concurrent_pages: int
    page_load_timeout: float
    network_idle_timeout: float
    settle_delay: float
    render_failure_threshold: int
    confirm_delay: float
    light_interval: float  # seconds between light polls, 0 turns the light mode off
    light_turbo_interval: float
    render_interval: float  # seconds between renders of pages the light mode covers
    contract_pause_threshold: int
    sub_url_patterns: tuple[str, ...]
    ignored_attributes: tuple[str, ...]
    max_change_examples: int
    enrich_timeout: float
    flap_window: float
    flap_threshold: int

    deep_scan: bool
    deep_scan_max_bytes: int

    turbo_interval: float
    turbo_default_minutes: float
    turbo_max_minutes: float
    turbo_stop_after: int
    turbo_max_sites: int
    turbo_max_pages: int
    turbo_recheck_interval: float
    block_max_backoff: float

    auto_turbo: bool  # speed a site up around a launch time one of its pages shows
    auto_turbo_before: float  # seconds before that time the faster checks start
    auto_turbo_after: float  # seconds after it they end, unless a token ends them first
    auto_turbo_max_sites: int

    unverified_recheck: float
    unverified_watch: float
    unverified_max_per_hour: int

    browser_channel: str
    headless: bool
    blocked_resource_types: tuple[str, ...]
    browser_args: tuple[str, ...]
    hide_automation: bool  # no "Headless" User-Agent or webdriver flag, cookies outlive restarts

    http2: bool  # RPC and Telegram over HTTP/2, so parallel calls share one warm connection
    evm_networks: tuple[EvmNetwork, ...]
    solana_rpc_url: str
    sui_rpc_url: str
    require_erc20: bool
    skip_stable_copies: bool
    rpc_timeout: float
    max_concurrent_rpc: int
    rejected_ttl: float
    rpc_retry_delay: float

    data_dir: Path
    log_level: str
    log_file: Path | None
    heartbeat_interval: float

    outdated_defaults: tuple[ChangedDefault, ...] = ()  # keys still set to an old default

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv_path: str | os.PathLike[str] | None = ".env",
    ) -> Settings:
        """Read and check the settings from ``env`` (default ``os.environ``) and ``.env``.

        Real variables win over ``.env``. An empty EVM RPC URL skips that network, but
        Solana and Sui cannot be skipped. Raises ConfigError for a missing or bad value.
        """
        merged: dict[str, str] = {}
        if dotenv_path is not None and Path(dotenv_path).is_file():
            merged.update({k: v for k, v in _read_dotenv(dotenv_path).items() if v is not None})
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

        turbo_default = reader.number("TURBO_DEFAULT_MINUTES", 30.0, minimum=1.0)
        turbo_max = reader.number("TURBO_MAX_MINUTES", 180.0, minimum=1.0)
        if turbo_max < turbo_default:
            raise ConfigError("TURBO_MAX_MINUTES must be >= TURBO_DEFAULT_MINUTES")

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
            light_interval=reader.number("LIGHT_INTERVAL", 1.0, minimum=0.0),
            light_turbo_interval=reader.number("LIGHT_TURBO_INTERVAL", 0.3, minimum=0.1),
            render_interval=reader.number("RENDER_INTERVAL", 30.0, minimum=4.0),
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
            turbo_default_minutes=turbo_default,
            turbo_max_minutes=turbo_max,
            turbo_stop_after=reader.integer("TURBO_STOP_AFTER", 1, minimum=0),
            turbo_max_sites=reader.integer("TURBO_MAX_SITES", 1, minimum=1),
            turbo_max_pages=reader.integer("TURBO_MAX_PAGES", 4, minimum=1),
            turbo_recheck_interval=reader.number("TURBO_RECHECK_INTERVAL", 1.0, minimum=1.0),
            block_max_backoff=reader.number("BLOCK_MAX_BACKOFF", 600.0, minimum=1.0),
            auto_turbo=reader.flag("AUTO_TURBO", True),
            auto_turbo_before=reader.number("AUTO_TURBO_BEFORE_MINUTES", 10.0, minimum=0.0) * 60,
            auto_turbo_after=reader.number("AUTO_TURBO_AFTER_MINUTES", 60.0, minimum=1.0) * 60,
            auto_turbo_max_sites=reader.integer("AUTO_TURBO_MAX_SITES", 3, minimum=1),
            unverified_recheck=reader.number("UNVERIFIED_RECHECK_MINUTES", 5.0, minimum=1.0) * 60,
            unverified_watch=reader.number("UNVERIFIED_WATCH_HOURS", 24.0, minimum=1.0) * 3600,
            unverified_max_per_hour=reader.integer("UNVERIFIED_MAX_PER_HOUR", 10, minimum=1),
            browser_channel=reader.text("BROWSER_CHANNEL", "chromium"),
            headless=reader.flag("HEADLESS", True),
            blocked_resource_types=reader.text_list(
                "BLOCKED_RESOURCE_TYPES", DEFAULT_BLOCKED_RESOURCE_TYPES
            ),
            browser_args=reader.arguments("BROWSER_ARGS", DEFAULT_BROWSER_ARGS),
            hide_automation=reader.flag("HIDE_AUTOMATION", True),
            http2=reader.flag("HTTP2", True),
            evm_networks=networks,
            solana_rpc_url=solana_rpc_url,
            sui_rpc_url=sui_rpc_url,
            require_erc20=reader.flag("REQUIRE_ERC20", True),
            skip_stable_copies=reader.flag("SKIP_STABLE_COPIES", True),
            rpc_timeout=reader.number("RPC_TIMEOUT", 5.0, minimum=1.0),
            max_concurrent_rpc=reader.integer("MAX_CONCURRENT_RPC", 8, minimum=1),
            rejected_ttl=reader.number("REJECTED_TTL_HOURS", 6.0, minimum=0.0) * 3600.0,
            rpc_retry_delay=reader.number("RPC_RETRY_DELAY", 10.0, minimum=0.0),
            data_dir=Path(reader.text_or_default("DATA_DIR", "data")),
            log_level=reader.choice("LOG_LEVEL", "INFO", LOG_LEVELS),
            log_file=Path(log_file) if log_file else None,
            heartbeat_interval=reader.number(
                "HEARTBEAT_INTERVAL", 15.0, minimum=1.0, maximum=MAX_HEARTBEAT_INTERVAL
            ),
            outdated_defaults=find_outdated_defaults(merged),
        )


def _read_dotenv(path: str | os.PathLike[str]) -> dict[str, str | None]:
    """Return the values in a ``.env`` file; raise ConfigError when it can't be read as UTF-8."""
    try:
        return dotenv_values(path, encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{path} is not UTF-8 text ({exc.reason} at byte {exc.start}); "
            "save it as UTF-8 and start again"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{path} cannot be read: {exc}") from exc


def find_outdated_defaults(
    values: Mapping[str, str], changes: tuple[ChangedDefault, ...] | None = None
) -> tuple[ChangedDefault, ...]:
    """Return the changed defaults that ``values`` still set to their old value.

    ``changes`` defaults to ``CHANGED_DEFAULTS``. In each result ``previous`` holds the
    value as ``values`` spell it, e.g. ``on`` for ``true``.
    """
    return tuple(
        dataclasses.replace(change, previous=values[change.key].strip())
        for change in (CHANGED_DEFAULTS if changes is None else changes)
        if change.key in values and _same_setting(values[change.key], change.previous)
    )


def _same_setting(value: str, previous: str) -> bool:
    """Return True when ``value`` means the same as ``previous``, read as a flag, number or text.

    So ``on`` equals ``true``, ``600`` equals ``600.0``, and a trailing slash does not count.
    """
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
    """Split ``text`` into command-line arguments the way a shell does, removing quotes.

    On Windows (or with ``windows=True``) a backslash is a path separator, not an escape.
    Raises ConfigError naming ``key`` when the quotes do not match.
    """
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    if os.name == "nt" if windows is None else windows:
        lexer.escape = ""
    try:
        return tuple(lexer)
    except ValueError as exc:
        raise ConfigError(f"{key} is not a valid argument list: {exc}") from exc


class _EnvReader:
    """Reads typed values from the merged environment; a bad one raises ConfigError."""

    def __init__(self, values: Mapping[str, str]) -> None:
        """Read from ``values``, the environment merged with ``.env``."""
        self._values = values

    def text(self, key: str, default: str = "") -> str:
        """Return the value of ``key`` without surrounding spaces, or ``default`` if unset."""
        return self._values.get(key, default).strip()

    def text_or_default(self, key: str, default: str) -> str:
        """Like :meth:`text`, but an empty value also means the default."""
        return self.text(key) or default

    @staticmethod
    def url(key: str, value: str) -> str:
        """Return ``value`` if it is an http(s) URL with a host; otherwise raise ConfigError.

        The error shows only the masked URL, so an API key in it stays out of the log.
        """
        try:
            parsed = httpx.URL(value)
        except httpx.InvalidURL as exc:
            raise ConfigError(f"{key} is not a valid URL ({exc}): {_masked(value)}") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ConfigError(f"{key} must be an http(s) URL with a host, got {_masked(value)}")
        return value

    def choice(self, key: str, default: str, allowed: frozenset[str]) -> str:
        """Return the value of ``key`` in upper case; it must be one of ``allowed``."""
        value = self.text_or_default(key, default).upper()
        if value not in allowed:
            raise ConfigError(f"{key} must be one of {', '.join(sorted(allowed))}, got {value!r}")
        return value

    def flag(self, key: str, default: bool) -> bool:
        """Return the value of ``key`` as a boolean (on/off, yes/no, ...), or ``default``."""
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
        """Return the value of ``key`` as an integer not below ``minimum``, or ``default``."""
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

    def number(
        self,
        key: str,
        default: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        """Return the value of ``key`` as a finite number within the limits, or ``default``."""
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
        if maximum is not None and value > maximum:
            raise ConfigError(f"{key} must be <= {maximum}")
        return value

    def text_list(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        """Return the value of ``key`` as a list of items, or ``default`` when it is empty.

        Commas, semicolons or spaces separate the items; brackets and quotes are dropped.
        """
        raw = self.text(key).strip("[]()")
        if not raw.strip():
            return default
        items = (item.strip().strip("'\"") for item in _LIST_SEPARATOR_RE.split(raw))
        return tuple(item for item in items if item)

    def arguments(self, key: str, default: str) -> tuple[str, ...]:
        """Return the value of ``key`` split into switches the way a shell does.

        So ``--x=a,b`` stays whole, and on Windows ``--user-data-dir=C:\\x`` keeps its
        backslash. Only an absent key gets ``default``; an empty value means no switches.
        """
        return split_arguments(self.text(key, default), key)

    def int_list(self, key: str) -> tuple[int, ...]:
        """Return the value of ``key`` as a list of integers, such as chat ids."""
        items = self.text_list(key)
        try:
            return tuple(int(item) for item in items)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a comma separated list of integers") from exc
