import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

from scraper_bot.chains.networks import EVM_CHAINS
from scraper_bot.settings.defaults import ChangedDefault
from scraper_bot.settings.env import (
    FALSE_WORDS,
    TRUE_WORDS,
    ConfigError,
    Settings,
    _EnvReader,
    find_outdated_defaults,
    split_arguments,
)
from tests.conftest import MINIMAL_ENV, make_settings


def test_minimal_environment_is_enough(tmp_path) -> None:
    cfg = make_settings(tmp_path)
    assert cfg.telegram_bot_token == "123:abc"
    assert cfg.chat_ids == (1, 2)
    assert cfg.allowed_chat_ids == ()
    assert cfg.poll_interval_min == 4.0 and cfg.poll_interval_max == 10.0
    assert cfg.evm_networks[0].name == "Ethereum"
    assert cfg.rejected_ttl == 6 * 3600
    assert cfg.sub_url_patterns[0] == "https://app.{domain}"
    assert (cfg.network_idle_timeout, cfg.settle_delay, cfg.confirm_delay) == (2.0, 0.25, 2.0)
    assert cfg.deep_scan is False and cfg.deep_scan_max_bytes == 5 * 1024 * 1024
    assert cfg.browser_args == ("--disable-3d-apis", "--disable-webgl", "--disable-webgl2")
    assert cfg.flap_window == 24 * 3600 and cfg.flap_threshold == 2


def test_token_is_required() -> None:
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env({}, dotenv_path=None)


def test_poll_interval_order_is_validated(tmp_path) -> None:
    with pytest.raises(ConfigError, match="POLL_INTERVAL_MAX"):
        make_settings(tmp_path, POLL_INTERVAL_MIN="20", POLL_INTERVAL_MAX="10")
    with pytest.raises(ConfigError, match="TURBO_MAX_MINUTES must be >= TURBO_DEFAULT"):
        make_settings(tmp_path, TURBO_DEFAULT_MINUTES="240")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MAX_CONCURRENT_PAGES", "zero"),
        ("MAX_CONCURRENT_PAGES", "0"),
        ("HEADLESS", "maybe"),
        ("TELEGRAM_CHAT_IDS", "1,x"),
        ("RPC_TIMEOUT", "0.1"),
        ("HEARTBEAT_INTERVAL", "300"),  # rebooter.py would restart the bot every 5 min
    ],
)
def test_invalid_values_are_reported(tmp_path, key: str, value: str) -> None:
    with pytest.raises(ConfigError, match=key):
        make_settings(tmp_path, **{key: value})


def test_lists_and_flags_are_parsed(tmp_path) -> None:
    cfg = make_settings(
        tmp_path,
        SUB_URL_PATTERNS=" https://{domain}/a , https://{domain}/b ",
        IGNORED_ATTRIBUTES="Nonce,X-Y",
        REQUIRE_ERC20="no",
        TELEGRAM_ALLOWED_CHAT_IDS="5",
        ETH_RPC_URL="",
    )
    assert cfg.sub_url_patterns == ("https://{domain}/a", "https://{domain}/b")
    assert cfg.ignored_attributes == ("nonce", "x-y")
    assert cfg.require_erc20 is False
    assert cfg.allowed_chat_ids == (5,)
    assert "Ethereum" not in [network.name for network in cfg.evm_networks]


@pytest.mark.parametrize("raw", ["[1, 2]", "(1;2)", "'1', \"2\"", "1 2", " 1,2, "])
def test_lists_tolerate_brackets_quotes_and_odd_separators(tmp_path, raw: str) -> None:
    cfg = make_settings(tmp_path, TELEGRAM_CHAT_IDS=raw)
    assert cfg.chat_ids == (1, 2)


def test_dotenv_file_is_read_but_environment_wins(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=from-file\nLOG_LEVEL=debug\n", encoding="utf-8")
    cfg = Settings.from_env({"LOG_LEVEL": "warning"}, dotenv_path=env_file)
    assert cfg.telegram_bot_token == "from-file"
    assert cfg.log_level == "WARNING"
    env_file.write_bytes("# комментарий\nTELEGRAM_BOT_TOKEN=x\n".encode("cp1251"))
    with pytest.raises(ConfigError, match="is not UTF-8 text"):  # exit code 2, not a traceback
        Settings.from_env({}, dotenv_path=env_file)


def test_all_evm_endpoints_disabled_is_an_error(tmp_path) -> None:
    disabled = {chain.env_var: "" for chain in EVM_CHAINS}
    disabled["ETH_RPC_URL"] = "   "  # blank counts as disabled too
    with pytest.raises(ConfigError, match="at least one EVM RPC endpoint"):
        make_settings(tmp_path, **disabled)
    cfg = make_settings(tmp_path, **{**disabled, "BSC_RPC_URL": "http://localhost:8545"})
    assert [(n.name, n.rpc_url) for n in cfg.evm_networks] == [("BSC", "http://localhost:8545")]


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("POLL_INTERVAL_MIN", "fast", "POLL_INTERVAL_MIN must be a number, got 'fast'"),
        ("REJECTED_TTL_HOURS", "6h", "REJECTED_TTL_HOURS must be a number, got '6h'"),
        ("HEARTBEAT_INTERVAL", "1,5", "HEARTBEAT_INTERVAL must be a number, got '1,5'"),
        ("TURBO_STOP_AFTER", "1.5", "TURBO_STOP_AFTER must be an integer, got '1.5'"),
    ],
)
def test_non_numeric_number_is_reported(tmp_path, key: str, value: str, message: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        make_settings(tmp_path, **{key: value})


def test_flags_accept_every_spelling(tmp_path) -> None:
    assert set(TRUE_WORDS) == {"1", "true", "yes", "on"}
    assert set(FALSE_WORDS) == {"0", "false", "no", "off"}
    for word in TRUE_WORDS:
        for raw in (word, word.upper(), word.capitalize(), f"  {word} "):
            assert make_settings(tmp_path, HEADLESS=raw).headless is True, raw
    for word in FALSE_WORDS:
        for raw in (word, word.upper(), word.capitalize(), f"  {word} "):
            assert make_settings(tmp_path, HEADLESS=raw).headless is False, raw
    assert make_settings(tmp_path, HEADLESS="").headless is True  # empty: the default
    assert make_settings(tmp_path, DEEP_SCAN=" ").deep_scan is False
    with pytest.raises(ConfigError, match="DEEP_SCAN must be a boolean, got 'y'"):
        make_settings(tmp_path, DEEP_SCAN="y")


def test_browser_args_are_split_like_a_shell_does(tmp_path) -> None:
    custom = make_settings(tmp_path, BROWSER_ARGS=" --lang=en '--window-size=1366,900' ")
    assert custom.browser_args == ("--lang=en", "--window-size=1366,900")
    assert make_settings(tmp_path, BROWSER_ARGS="").browser_args == ()  # empty: no switches
    with pytest.raises(ConfigError, match="BROWSER_ARGS is not a valid argument list"):
        make_settings(tmp_path, BROWSER_ARGS="--lang='en")


def test_every_key_in_env_example_is_read_by_settings(tmp_path, monkeypatch) -> None:
    example = Path(__file__).resolve().parents[2] / ".env.example"
    documented = set(dotenv_values(example))
    assert len(documented) > 40  # the file was found and parsed
    requested: set[str] = set()
    original = _EnvReader.text

    def recording_text(self: _EnvReader, key: str, default: str = "") -> str:
        requested.add(key)
        return original(self, key, default)

    monkeypatch.setattr(_EnvReader, "text", recording_text)
    make_settings(tmp_path)
    assert requested == documented  # every typed accessor goes through text()


def test_paths_and_log_settings(tmp_path) -> None:
    cfg = make_settings(tmp_path)
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.log_file is None
    assert cfg.log_level == "INFO" and cfg.heartbeat_interval == 15.0
    assert Settings.from_env(MINIMAL_ENV, dotenv_path=None).data_dir == Path("data")

    custom = make_settings(
        tmp_path,
        DATA_DIR="  ./state ",
        LOG_FILE=str(tmp_path / "logs" / "bot.log"),
        LOG_LEVEL="debug",
        HEARTBEAT_INTERVAL="2.5",
    )
    assert custom.data_dir == Path("state")
    assert custom.log_file == tmp_path / "logs" / "bot.log" and isinstance(custom.log_file, Path)
    assert custom.log_level == "DEBUG" and custom.heartbeat_interval == 2.5
    assert make_settings(tmp_path, LOG_FILE="   ").log_file is None
    with pytest.raises(ConfigError, match="HEARTBEAT_INTERVAL must be >= 1"):
        make_settings(tmp_path, HEARTBEAT_INTERVAL="0.5")


CHANGES = (
    ChangedDefault("DEEP_SCAN", "true", "false"),
    ChangedDefault("RPC_RETRY_DELAY", "600", "10"),
    ChangedDefault("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com", "https://x.rpc"),
)


def test_outdated_defaults_are_recognised_whatever_the_spelling() -> None:
    old = {
        "DEEP_SCAN": " On ",
        "RPC_RETRY_DELAY": "600.0",
        "SOLANA_RPC_URL": "https://API.mainnet-beta.solana.com/",
    }
    found = find_outdated_defaults(old, CHANGES)
    assert [change.key for change in found] == [change.key for change in CHANGES]
    assert found[0].previous == "On"  # named as the .env spells it
    chosen = {"DEEP_SCAN": "false", "RPC_RETRY_DELAY": "60", "SOLANA_RPC_URL": "https://x.rpc"}
    assert find_outdated_defaults(chosen, CHANGES) == ()
    assert find_outdated_defaults({}, CHANGES) == ()  # an absent key uses the new default
    assert [c.key for c in find_outdated_defaults({"DEEP_SCAN": "1"}, CHANGES)] == ["DEEP_SCAN"]


def test_older_releases_are_recognised_too(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path, POLL_INTERVAL_MIN="5", NETWORK_IDLE_TIMEOUT="4", SETTLE_DELAY="1")
    assert {(c.key, c.current) for c in cfg.outdated_defaults} == {
        ("POLL_INTERVAL_MIN", "4"),
        ("NETWORK_IDLE_TIMEOUT", "2"),
        ("SETTLE_DELAY", "0.25"),
    }


def test_windows_paths_keep_their_backslashes() -> None:
    assert split_arguments(r"--user-data-dir=C:\bot\prof --lang=en", windows=True) == (
        r"--user-data-dir=C:\bot\prof",
        "--lang=en",
    )
    assert split_arguments(r"'--user-data-dir=C:\a b'", windows=True) == (
        r"--user-data-dir=C:\a b",
    )
    assert split_arguments(r"--x=a\ b", windows=False) == ("--x=a b",)  # POSIX escapes


@pytest.mark.parametrize(
    ("key", "value"),
    [("RPC_TIMEOUT", "nan"), ("PAGE_LOAD_TIMEOUT", "inf"), ("POLL_INTERVAL_MIN", "NaN")],
)
def test_numbers_must_be_finite(tmp_path: Path, key: str, value: str) -> None:
    with pytest.raises(ConfigError, match=f"{key} must be a finite number"):
        make_settings(tmp_path, **{key: value})


def test_log_level_is_checked_and_empty_values_mean_the_default(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL must be one of"):
        make_settings(tmp_path, LOG_LEVEL="verbose")
    assert make_settings(tmp_path, LOG_LEVEL="debug").log_level == "DEBUG"
    blank = make_settings(tmp_path, LOG_LEVEL="", SOLANA_RPC_URL=" ", SUI_RPC_URL="", DATA_DIR="")
    assert blank.data_dir == Path("data")
    assert blank.log_level == "INFO"
    assert blank.solana_rpc_url.startswith("https://") and blank.sui_rpc_url.startswith("https://")


def test_this_release_flags_the_old_solana_endpoint_and_retry_delay(tmp_path: Path) -> None:
    cfg = make_settings(
        tmp_path, SOLANA_RPC_URL="https://api.mainnet-beta.solana.com", RPC_RETRY_DELAY="600"
    )
    assert {change.key for change in cfg.outdated_defaults} == {
        "SOLANA_RPC_URL",
        "RPC_RETRY_DELAY",
    }
    assert make_settings(tmp_path, RPC_RETRY_DELAY="30").outdated_defaults == ()  # chosen, not old
    fresh = make_settings(tmp_path)
    assert fresh.outdated_defaults == ()
    assert fresh.solana_rpc_url == "https://solana-rpc.publicnode.com"
    assert fresh.rpc_retry_delay == 10 and fresh.http2 is True
    assert make_settings(tmp_path, HTTP2="off").http2 is False


def test_env_example_values_are_the_code_defaults(tmp_path: Path) -> None:
    """Every value shown in .env.example must equal the built-in default (documentation drift)."""
    example_path = Path(__file__).resolve().parents[2] / ".env.example"
    example = {k: v for k, v in dotenv_values(example_path).items() if v is not None}
    data_dir = str(tmp_path / "data")
    from_example = Settings.from_env({**example, "DATA_DIR": data_dir}, dotenv_path=None)
    identity = {key: example[key] for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS")}
    defaults = Settings.from_env({**identity, "DATA_DIR": data_dir}, dotenv_path=None)
    assert from_example == defaults


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("ARC_RPC_URL", "https://arc-rpc.publicnode.com:8545x", "ARC_RPC_URL is not a valid URL"),
        ("BASE_RPC_URL", "base-rpc.publicnode.com", "BASE_RPC_URL must be an http"),
        ("SOLANA_RPC_URL", "ftp://solana.example", "SOLANA_RPC_URL must be an http"),
        ("SUI_RPC_URL", "https://", "SUI_RPC_URL must be an http"),
    ],
)
def test_a_typo_in_an_rpc_url_stops_the_start(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        make_settings(tmp_path, **{key: value})


def test_a_wrong_rpc_url_is_named_without_its_secret_parts(tmp_path: Path) -> None:
    secret = "k3y-1234567890"
    with pytest.raises(ConfigError) as caught:
        make_settings(tmp_path, ETH_RPC_URL=f"https://eth.example.com:85x/v2/{secret}")
    assert "ETH_RPC_URL" in str(caught.value) and secret not in str(caught.value)
    assert "'https://eth.example.com:85x/…'" in str(caught.value)
    with pytest.raises(ConfigError) as caught:
        make_settings(tmp_path, ETH_RPC_URL=f"ftp://user:{secret}@eth.example.com/x")
    assert secret not in str(caught.value) and "'ftp://…@eth.example.com/…'" in str(caught.value)
