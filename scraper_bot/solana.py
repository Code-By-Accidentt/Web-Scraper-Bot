"""Solana account layouts shared by the validator and the token details."""

from __future__ import annotations

import base64
import binascii
from typing import Any

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
METAPLEX_METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
MINT_SIZE = 82
TOKEN_ACCOUNT_SIZE = 165
TOKEN_2022_MINT_TYPE = 1  # account-type byte that follows the padded mint


def account_data(result: Any) -> bytes:
    """Decoded bytes of a ``getAccountInfo`` answer; empty when there is no account."""
    value = result.get("value") if isinstance(result, dict) else None
    data = value.get("data") if isinstance(value, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], str):
        try:
            return base64.b64decode(data[0])
        except (ValueError, binascii.Error):
            return b""
    return b""


def is_mint(owner: Any, data: bytes) -> bool:
    """True for an SPL or Token-2022 mint account."""
    if owner == TOKEN_PROGRAM:
        return len(data) == MINT_SIZE
    if owner == TOKEN_2022_PROGRAM:
        # A Token-2022 mint is either the bare 82 bytes or 82 bytes + padding + an
        # account-type byte (1 = Mint) at offset 165, followed by extensions.
        return len(data) == MINT_SIZE or (
            len(data) > TOKEN_ACCOUNT_SIZE and data[TOKEN_ACCOUNT_SIZE] == TOKEN_2022_MINT_TYPE
        )
    return False
