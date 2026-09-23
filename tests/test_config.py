import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

from scraper_bot.config import (
    DEFAULT_EVM_NETWORKS,
    FALSE_WORDS,
    TRUE_WORDS,
    ConfigError,
    Settings,
    _EnvReader,
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
    assert cfg.deep_scan is True and cfg.deep_scan_max_bytes == 5 * 1024 * 1024
    assert cfg.flap_window == 24 * 3600 and cfg.flap_threshold == 2


def test_token_is_required() -> None:
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env({}, dotenv_path=None)


def test_poll_interval_order_is_validated(tmp_path) -> None:
    with pytest.raises(ConfigError, match="POLL_INTERVAL_MAX"):
        make_settings(tmp_path, POLL_INTERVAL_MIN="20", POLL_INTERVAL_MAX="10")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MAX_CONCURRENT_PAGES", "zero"),
        ("MAX_CONCURRENT_PAGES", "0"),
        ("HEADLESS", "maybe"),
        ("TELEGRAM_CHAT_IDS", "1,x"),
        ("RPC_TIMEOUT", "0.1"),
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


def test_all_evm_endpoints_disabled_is_an_error(tmp_path) -> None:
    disabled = {var: "" for _, var, _ in DEFAULT_EVM_NETWORKS}
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
    assert make_settings(tmp_path, DEEP_SCAN=" ").deep_scan is True
    with pytest.raises(ConfigError, match="DEEP_SCAN must be a boolean, got 'y'"):
        make_settings(tmp_path, DEEP_SCAN="y")


def test_every_key_in_env_example_is_read_by_settings(tmp_path, monkeypatch) -> None:
    example = Path(__file__).resolve().parents[1] / ".env.example"
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
