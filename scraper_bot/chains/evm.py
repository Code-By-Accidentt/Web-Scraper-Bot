"""EVM constants shared by the validator and the token details."""

from __future__ import annotations

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
