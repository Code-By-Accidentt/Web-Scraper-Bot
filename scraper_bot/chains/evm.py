"""EVM constants and ABI decoding shared by the validators and the token details."""

from __future__ import annotations

from typing import Any

from scraper_bot.models import clean_label

# ERC-20 function selectors (first four bytes of the keccak256 of the signature).
ERC20_NAME = "0x06fdde03"  # name()
ERC20_SYMBOL = "0x95d89b41"  # symbol()
ERC20_DECIMALS = "0x313ce567"  # decimals()
ERC20_TOTAL_SUPPLY = "0x18160ddd"  # totalSupply()

# Uniswap V2/V3, Aerodrome and their forks: the two tokens of an LP pair or pool.
PAIR_TOKEN0 = "0x0dfe1681"  # token0()
PAIR_TOKEN1 = "0xd21220a7"  # token1()
# ERC-4626: the asset a vault holds; its shares are ERC-20 but never a launch.
VAULT_ASSET = "0x38d52e0f"  # asset()

# The calls that classify a contract (see EvmContractValidator), in this order.
TOKEN_PROBE = (ERC20_TOTAL_SUPPLY, ERC20_DECIMALS, PAIR_TOKEN0, PAIR_TOKEN1, VAULT_ASSET)

# EIP-7702: a wallet that delegates to a contract answers eth_getCode with
# 0xef0100 followed by the delegate's 20-byte address.
DELEGATION_PREFIX = "0xef0100"
DELEGATION_CODE_LENGTH = 2 + 2 * 23

# How nodes spell "no bytecode here" in an eth_getCode answer.
_EMPTY_CODE = frozenset({"", "0x", "0x0"})


def has_code(code: object) -> bool:
    """True when an eth_getCode answer holds bytecode (an EIP-7702 delegation included)."""
    return isinstance(code, str) and code.lower() not in _EMPTY_CODE


def is_delegated_wallet(code: str) -> bool:
    return len(code) == DELEGATION_CODE_LENGTH and code.lower().startswith(DELEGATION_PREFIX)


def word_address(result: object) -> str | None:
    """The address in an ABI-encoded 32-byte word, or ``None`` when it is not one."""
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
    """The number in an ABI-encoded 32-byte word, or ``None``."""
    if not isinstance(result, str) or len(result) < 66 or not result.startswith("0x"):
        return None
    try:
        return int(result[2:66], 16)
    except ValueError:
        return None


def decode_abi_string(result: Any) -> str | None:
    """Decode a ``string`` return value; ``bytes32`` symbols (MKR-style) are accepted."""
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
    if not isinstance(result, str) or not result.startswith("0x") or len(result) < 3:
        return None
    try:
        value = int(result, 16)
    except ValueError:
        return None
    if limit is not None and value > limit:
        return None
    return value
