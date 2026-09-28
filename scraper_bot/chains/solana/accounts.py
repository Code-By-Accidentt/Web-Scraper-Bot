"""Solana account layouts and derivations shared by the validator and the token details."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Iterator, Sequence
from typing import Any

import base58

from scraper_bot.models.addresses import clean_label

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
METAPLEX_METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
MINT_SIZE = 82
TOKEN_ACCOUNT_SIZE = 165
TOKEN_2022_MINT_TYPE = 1  # account-type byte that follows the padded mint

# Token-2022 extension types (spl-token-2022 ExtensionType)
_TRANSFER_FEE_CONFIG = 1
_DEFAULT_ACCOUNT_STATE = 6
_NON_TRANSFERABLE = 9
_PERMANENT_DELEGATE = 12
_TRANSFER_HOOK = 14
_TOKEN_METADATA_EXTENSION = 19
_PAUSABLE = 26
_FROZEN = b"\x02"  # AccountState::Frozen


def account_data(result: Any) -> bytes:
    """Return the decoded data of a ``getAccountInfo`` answer.

    Returns empty bytes when there is no account or its data cannot be decoded.
    """
    value = result.get("value") if isinstance(result, dict) else None
    data = value.get("data") if isinstance(value, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], str):
        try:
            return base64.b64decode(data[0])
        except (ValueError, binascii.Error):
            return b""
    return b""


def is_mint(owner: Any, data: bytes) -> bool:
    """Return True when the account is an SPL or Token-2022 mint, judged by owner and size."""
    if owner == TOKEN_PROGRAM:
        return len(data) == MINT_SIZE
    if owner == TOKEN_2022_PROGRAM:
        # Bare 82 bytes, or padded to 165 with the account-type byte there, then extensions.
        return len(data) == MINT_SIZE or (
            len(data) > TOKEN_ACCOUNT_SIZE and data[TOKEN_ACCOUNT_SIZE] == TOKEN_2022_MINT_TYPE
        )
    return False


# ed25519: the field prime, the curve constant d and a square root of -1.
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def is_on_curve(point: bytes) -> bool:
    """Return True when the 32 bytes decode to an ed25519 point, i.e. could be a public key."""
    y = int.from_bytes(point, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return False
    y2 = y * y % _P
    x2 = (y2 - 1) * pow(_D * y2 + 1, _P - 2, _P) % _P
    if x2 == 0:
        return sign == 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    return (x * x - x2) % _P == 0


def find_program_address(seeds: Sequence[bytes], program_id: bytes) -> tuple[bytes, int]:
    """Return (address, bump) like Solana's ``Pubkey::find_program_address``.

    Bumps are tried from 255 down; the first hash that is not a valid public key wins.
    """
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(
            b"".join(seeds) + bytes([bump]) + program_id + b"ProgramDerivedAddress"
        ).digest()
        if not is_on_curve(digest):
            return digest, bump
    raise ValueError("no program address found")


def metadata_address(mint: str) -> str:
    """Return the address of the Metaplex Token Metadata account of ``mint``."""
    program = base58.b58decode(METAPLEX_METADATA_PROGRAM)
    pda, _ = find_program_address([b"metadata", program, base58.b58decode(mint)], program)
    return base58.b58encode(pda).decode("ascii")


def parse_mint(data: bytes) -> tuple[int, int] | None:
    """Return (supply, decimals) of an SPL or Token-2022 mint, or None when data is too short.

    The supply is the u64 at bytes 36-43, right after the mint authority; decimals is byte 44.
    """
    if len(data) < MINT_SIZE:
        return None
    return int.from_bytes(data[36:44], "little"), data[44]


def _read_borsh_string(data: bytes, pos: int) -> tuple[str | None, int]:
    """Read a Borsh string (u32 length, then the bytes) at ``pos``; return it and the next pos.

    On truncated data the string is None and the position is the end of the data.
    """
    if pos + 4 > len(data):
        return None, len(data)
    length = int.from_bytes(data[pos : pos + 4], "little")
    raw = data[pos + 4 : pos + 4 + length]
    if len(raw) < length:
        return None, len(data)
    return raw.decode("utf-8", "replace"), pos + 4 + length


def parse_metaplex_metadata(data: bytes) -> tuple[str | None, str | None]:
    """Return (name, symbol) from a Metaplex metadata account; a missing one is None.

    The strings follow a 1-byte key, the 32-byte update authority and the 32-byte mint.
    """
    name, pos = _read_borsh_string(data, 1 + 32 + 32)
    symbol, _ = _read_borsh_string(data, pos)
    return clean_label(name or ""), clean_label(symbol or "")


def token2022_extensions(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (type, body) for every extension of a Token-2022 mint, in account order.

    Extensions follow the account-type byte as entries of u16 type, u16 length and body;
    an all-zero header is padding and ends the list.
    """
    if len(data) <= TOKEN_ACCOUNT_SIZE + 1 or data[TOKEN_ACCOUNT_SIZE] != TOKEN_2022_MINT_TYPE:
        return
    pos = TOKEN_ACCOUNT_SIZE + 1
    while pos + 4 <= len(data):
        kind = int.from_bytes(data[pos : pos + 2], "little")
        length = int.from_bytes(data[pos + 2 : pos + 4], "little")
        if kind == 0 and length == 0:
            break
        yield kind, data[pos + 4 : pos + 4 + length]
        pos += 4 + length


def parse_token2022_metadata(data: bytes) -> tuple[str | None, str | None]:
    """Return (name, symbol) from a Token-2022 mint's TokenMetadata extension, or (None, None)."""
    for kind, body in token2022_extensions(data):
        if kind == _TOKEN_METADATA_EXTENSION:
            name, after = _read_borsh_string(body, 32 + 32)  # update authority, mint
            symbol, _ = _read_borsh_string(body, after)
            return clean_label(name or ""), clean_label(symbol or "")
    return None, None


def token_names(mint: bytes, metadata: bytes) -> tuple[str | None, str | None]:
    """Return (name, symbol) of a mint from its Metaplex metadata, else from its Token-2022 data.

    ``mint`` and ``metadata`` are the data of the mint and of its Metaplex metadata account
    (empty when there is none).
    """
    name, symbol = parse_metaplex_metadata(metadata)
    if name is None and symbol is None:
        name, symbol = parse_token2022_metadata(mint)
    return name, symbol


def parse_authorities(data: bytes) -> tuple[bool, bool] | None:
    """Return (mint authority kept, freeze authority kept) of a mint, or None when too short.

    Each authority is a COption<Pubkey>; its 4-byte tag sits at byte 0 and byte 46.
    """
    if len(data) < MINT_SIZE:
        return None
    return data[0:4] != bytes(4), data[46:50] != bytes(4)


def parse_risky_extensions(data: bytes) -> tuple[str, ...]:
    """List the Token-2022 extensions that let the issuer tax, freeze, seize or block holders.

    Each one comes as a short text for the alert, e.g. "transfer fee 5%". The transfer hook's
    program and the pausable flag sit after a 32-byte authority.
    """
    found: list[str] = []
    for kind, body in token2022_extensions(data):
        if kind == _TRANSFER_FEE_CONFIG and len(body) >= 108:
            # bps of the older and the newer fee schedule; either one taking a cut is a tax
            bps = max(
                int.from_bytes(body[88:90], "little"), int.from_bytes(body[106:108], "little")
            )
            if bps:
                found.append(f"transfer fee {bps / 100:g}%")
        elif kind == _DEFAULT_ACCOUNT_STATE and body[:1] == _FROZEN:
            found.append("new accounts start frozen")
        elif kind == _NON_TRANSFERABLE:
            found.append("non-transferable")
        elif kind == _PERMANENT_DELEGATE and body[:32] != bytes(32):
            found.append("permanent delegate")
        elif kind == _TRANSFER_HOOK and body[32:64] != bytes(32):
            found.append("transfer hook")
        elif kind == _PAUSABLE:
            found.append("paused" if body[32:33] == b"\x01" else "pausable")
    return tuple(found)
