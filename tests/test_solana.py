import base64
from typing import Any

import base58
import pytest

from scraper_bot.solana import (
    METAPLEX_METADATA_PROGRAM,
    MINT_SIZE,
    TOKEN_2022_MINT_TYPE,
    TOKEN_2022_PROGRAM,
    TOKEN_ACCOUNT_SIZE,
    TOKEN_PROGRAM,
    account_data,
    is_mint,
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
