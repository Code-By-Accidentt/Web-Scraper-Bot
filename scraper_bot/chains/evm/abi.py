"""EVM constants and ABI decoding shared by the validator and the token details."""

from __future__ import annotations

from typing import Any

from scraper_bot.models.addresses import clean_label

# Function selectors: the first four bytes of keccak256 of the signature, e.g. "name()".
ERC20_NAME = "0x06fdde03"
ERC20_SYMBOL = "0x95d89b41"
ERC20_DECIMALS = "0x313ce567"
ERC20_TOTAL_SUPPLY = "0x18160ddd"

# token0()/token1() of Uniswap V2/V3, Aerodrome and their forks; asset() of ERC-4626 vaults.
PAIR_TOKEN0 = "0x0dfe1681"
PAIR_TOKEN1 = "0xd21220a7"
VAULT_ASSET = "0x38d52e0f"

# EvmContractValidator unpacks the answers in exactly this order.
TOKEN_PROBE = (ERC20_TOTAL_SUPPLY, ERC20_DECIMALS, PAIR_TOKEN0, PAIR_TOKEN1, VAULT_ASSET)

# EIP-7702: a delegated wallet's code is 0xef0100 plus the delegate's 20-byte address.
DELEGATION_PREFIX = "0xef0100"
DELEGATION_CODE_LENGTH = 2 + 2 * 23

_EMPTY_CODE = frozenset({"", "0x", "0x0"})


def has_code(code: object) -> bool:
    """Return True when an eth_getCode answer holds bytecode, EIP-7702 delegations included."""
    return isinstance(code, str) and code.lower() not in _EMPTY_CODE


def is_delegated_wallet(code: str) -> bool:
    """Return True when the code is an EIP-7702 delegation: a wallet, not a contract."""
    return len(code) == DELEGATION_CODE_LENGTH and code.lower().startswith(DELEGATION_PREFIX)


def word_address(result: object) -> str | None:
    """Return the address in the first 32-byte ABI word of a call result.

    Returns None when the word is missing, is not hex, or does not hold an address (its high
    bytes are set, or it is the zero address).
    """
    if not isinstance(result, str) or len(result) < 66 or not result.startswith("0x"):
        return None
    word = result[2:66].lower()
    if word[:24] != "0" * 24 or word[24:] == "0" * 40:
        return None
    try:
        int(word, 16)
    except ValueError:
        return None
    return "0x" + word[24:]


def word_uint(result: object) -> int | None:
    """Return the number in the first 32-byte ABI word of a call result, or None."""
    if not isinstance(result, str) or len(result) < 66 or not result.startswith("0x"):
        return None
    try:
        return int(result[2:66], 16)
    except ValueError:
        return None


def decode_abi_string(result: Any) -> str | None:
    """Decode a ``string`` returned by a call, or return None when it is not one.

    Old tokens such as MKR return their symbol as ``bytes32``; that form is read too.
    """
    if not isinstance(result, str) or not result.startswith("0x"):
        return None
    try:
        data = bytes.fromhex(result[2:])
    except ValueError:
        return None
    if not data:
        return None
    if len(data) == 32:
        return clean_label(data.rstrip(b"\x00").decode("utf-8", "replace"))
    if len(data) < 64:
        return None
    offset = int.from_bytes(data[:32], "big")
    if offset + 32 > len(data):
        return None
    length = int.from_bytes(data[offset : offset + 32], "big")
    chunk = data[offset + 32 : offset + 32 + length]
    if len(chunk) < length:
        return None
    return clean_label(chunk.decode("utf-8", "replace"))


def decode_abi_uint(result: Any, *, limit: int | None = None) -> int | None:
    """Return a call result as a number, or None when it is not hex or is above ``limit``."""
    if not isinstance(result, str) or not result.startswith("0x") or len(result) < 3:
        return None
    try:
        value = int(result, 16)
    except ValueError:
        return None
    if limit is not None and value > limit:
        return None
    return value
