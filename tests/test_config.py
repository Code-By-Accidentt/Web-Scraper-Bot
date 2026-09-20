import pytest

from scraper_bot.config import ConfigError, Settings
from tests.conftest import make_settings


def test_minimal_environment_is_enough(tmp_path) -> None:
    cfg = make_settings(tmp_path)
    assert cfg.telegram_bot_token == "123:abc"
    assert cfg.chat_ids == (1, 2)
    assert cfg.allowed_chat_ids == ()
    assert cfg.poll_interval_min == 5.0 and cfg.poll_interval_max == 15.0
    assert cfg.evm_networks[0].name == "Ethereum"
    assert cfg.rejected_ttl == 6 * 3600
    assert cfg.sub_url_patterns[0] == "https://app.{domain}"


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


def test_dotenv_file_is_read_but_environment_wins(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=from-file\nLOG_LEVEL=debug\n", encoding="utf-8")
    cfg = Settings.from_env({"LOG_LEVEL": "warning"}, dotenv_path=env_file)
    assert cfg.telegram_bot_token == "from-file"
    assert cfg.log_level == "WARNING"
