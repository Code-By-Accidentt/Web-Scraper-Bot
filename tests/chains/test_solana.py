import base64
from typing import Any

import base58
import pytest

from scraper_bot.chains.solana import (
    METAPLEX_METADATA_PROGRAM,
    MINT_SIZE,
    TOKEN_2022_MINT_TYPE,
    TOKEN_2022_PROGRAM,
    TOKEN_ACCOUNT_SIZE,
    TOKEN_PROGRAM,
    account_data,
    is_mint,
    is_on_curve,
    metadata_address,
    parse_authorities,
    parse_metaplex_metadata,
    parse_mint,
    parse_risky_extensions,
    parse_token2022_metadata,
)

SYSTEM_PROGRAM = "11111111111111111111111111111111"


def account(data: bytes, owner: str = TOKEN_PROGRAM) -> dict[str, Any]:
    """A ``getAccountInfo`` answer with base64-encoded ``data``."""
    encoded = base64.b64encode(data).decode()
    value = {"owner": owner, "executable": False, "lamports": 1, "data": [encoded, "base64"]}
    return {"context": {"slot": 1}, "value": value}


def test_program_ids_and_layout_constants() -> None:
    for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM, METAPLEX_METADATA_PROGRAM):
        assert len(base58.b58decode(program)) == 32
    assert (MINT_SIZE, TOKEN_ACCOUNT_SIZE, TOKEN_2022_MINT_TYPE) == (82, 165, 1)


def test_account_data_decodes_base64_or_gives_nothing() -> None:
    assert account_data(account(b"\x01\x02")) == b"\x01\x02"
    assert account_data(account(b"")) == b""
    assert account_data(None) == b""
    assert account_data({"context": {"slot": 1}, "value": None}) == b""  # no such account
    assert account_data({"value": {"data": "AQI="}}) == b""  # not the [data, encoding] list
    assert account_data({"value": {"data": []}}) == b""
    assert account_data({"value": {"data": [42, "base64"]}}) == b""
    assert account_data({"value": {"data": ["not*base64!", "base64"]}}) == b""
    assert account_data({"value": {"data": {"parsed": {}}}}) == b""  # jsonParsed encoding
    assert account_data("nonsense") == b""


@pytest.mark.parametrize(
    ("owner", "data", "expected"),
    [
        (TOKEN_PROGRAM, bytes(MINT_SIZE), True),
        (TOKEN_PROGRAM, bytes(MINT_SIZE - 1), False),
        (TOKEN_PROGRAM, bytes(TOKEN_ACCOUNT_SIZE), False),  # a token account
        (TOKEN_PROGRAM, bytes(TOKEN_ACCOUNT_SIZE) + b"\x01" + bytes(4), False),  # no extensions
        (TOKEN_PROGRAM, b"", False),
        (TOKEN_2022_PROGRAM, bytes(MINT_SIZE), True),
        (TOKEN_2022_PROGRAM, bytes(TOKEN_ACCOUNT_SIZE) + b"\x01", True),
        (TOKEN_2022_PROGRAM, bytes(TOKEN_ACCOUNT_SIZE) + b"\x01" + bytes(40), True),
        (TOKEN_2022_PROGRAM, bytes(TOKEN_ACCOUNT_SIZE) + b"\x02" + bytes(40), False),  # account
        (TOKEN_2022_PROGRAM, bytes(TOKEN_ACCOUNT_SIZE), False),  # no type byte
        (TOKEN_2022_PROGRAM, bytes(100), False),
        (TOKEN_2022_PROGRAM, b"", False),
        (SYSTEM_PROGRAM, bytes(MINT_SIZE), False),
        (METAPLEX_METADATA_PROGRAM, bytes(MINT_SIZE), False),
        (None, bytes(MINT_SIZE), False),
    ],
)
def test_is_mint(owner: Any, data: bytes, expected: bool) -> None:
    assert is_mint(owner, data) is expected


# ── mint and metadata layouts ───────────────────────────────────────────────
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_METADATA = "5x38Kp4hvdomTCnCrAny4UtMUt5rQBdB6px2K1Ui45Wq"


def borsh(value: str, pad_to: int = 0) -> bytes:
    raw = value.encode().ljust(pad_to, b"\x00")
    return len(raw).to_bytes(4, "little") + raw


MINT = bytes(36) + (123_456).to_bytes(8, "little") + bytes([6, 1]) + bytes(36)
METAPLEX = bytes([4]) + bytes(64) + borsh("USD Coin", 32) + borsh("USDC", 10) + borsh("", 200)
T22_BODY = bytes(64) + borsh("PayPal USD") + borsh("PYUSD") + borsh("https://x")
TOKEN_2022 = (
    MINT.ljust(165, b"\x00")
    + b"\x01"
    + (7).to_bytes(2, "little")  # some other extension first
    + (2).to_bytes(2, "little")
    + b"\x00\x00"
    + (19).to_bytes(2, "little")
    + len(T22_BODY).to_bytes(2, "little")
    + T22_BODY
)


def extension(kind: int, body: bytes) -> bytes:
    return kind.to_bytes(2, "little") + len(body).to_bytes(2, "little") + body


def t22_mint(*extensions: bytes, authorities: bool = False) -> bytes:
    mint = bytearray(MINT)
    if authorities:
        mint[0:4] = (1).to_bytes(4, "little")  # mint authority kept
        mint[46:50] = (1).to_bytes(4, "little")  # freeze authority kept
    return bytes(mint).ljust(165, b"\x00") + b"\x01" + b"".join(extensions)


def fee(bps: int) -> bytes:
    older = bytes(16) + (0).to_bytes(2, "little")
    newer = bytes(16) + bps.to_bytes(2, "little")
    return bytes(72) + older + newer


def test_metadata_pda_matches_the_known_usdc_account() -> None:
    assert metadata_address(USDC_SOL) == USDC_METADATA
    assert is_on_curve(base58.b58decode(USDC_SOL))  # a real key pair
    assert not is_on_curve(base58.b58decode(USDC_METADATA))  # a program-derived address


def test_solana_account_parsers() -> None:
    assert parse_mint(MINT) == (123_456, 6)
    assert parse_mint(b"short") is None
    assert parse_metaplex_metadata(METAPLEX) == ("USD Coin", "USDC")
    assert parse_metaplex_metadata(b"\x04") == (None, None)
    assert parse_token2022_metadata(TOKEN_2022) == ("PayPal USD", "PYUSD")
    assert parse_token2022_metadata(MINT) == (None, None)
    assert parse_token2022_metadata(MINT.ljust(165, b"\x00") + b"\x02") == (None, None)


def test_token2022_edge_cases() -> None:
    padded = MINT.ljust(165, b"\x00")
    metadata = extension(19, T22_BODY)
    other = extension(7, b"\x00\x00")
    nothing = (None, None)
    assert parse_token2022_metadata(padded + b"\x01") == nothing  # no extensions at all
    assert parse_token2022_metadata(padded + b"\x01" + other) == nothing  # no metadata one
    assert parse_token2022_metadata(padded + b"\x01" + other + metadata) == ("PayPal USD", "PYUSD")
    # The (0, 0) padding sentinel ends the walk even when bytes follow it.
    assert parse_token2022_metadata(padded + b"\x01" + bytes(4) + metadata) == nothing
    # Account-type byte 2 is a token account, whatever comes after it.
    assert parse_token2022_metadata(padded + b"\x02" + metadata) == nothing
    assert parse_token2022_metadata(padded + b"\x01" + metadata[:3]) == nothing  # cut header
    assert parse_token2022_metadata(padded + b"\x01" + metadata[:70]) == nothing  # cut body


def test_risky_token_2022_extensions_are_named() -> None:
    data = t22_mint(
        extension(1, fee(250)),
        extension(6, b"\x02"),
        extension(9, b""),
        extension(12, b"\x07" * 32),
        extension(14, bytes(32) + b"\x05" * 32),
        extension(26, bytes(32) + b"\x01"),
        authorities=True,
    )
    assert parse_risky_extensions(data) == (
        "transfer fee 2.5%",
        "new accounts start frozen",
        "non-transferable",
        "permanent delegate",
        "transfer hook",
        "paused",
    )
    assert parse_authorities(data) == (True, True)
    harmless = t22_mint(
        extension(1, fee(0)),
        extension(6, b"\x01"),
        extension(12, bytes(32)),
        extension(14, b"\x05" * 32 + bytes(32)),
    )
    assert parse_risky_extensions(harmless) == ()
    assert parse_authorities(MINT) == (False, False)
    assert parse_authorities(b"short") is None
