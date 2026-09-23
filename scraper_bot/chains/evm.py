"""EVM constants shared by the validator and the token details."""

from __future__ import annotations

# ERC-20 function selectors (first four bytes of the keccak256 of the signature).
ERC20_NAME = "0x06fdde03"  # name()
ERC20_SYMBOL = "0x95d89b41"  # symbol()
ERC20_DECIMALS = "0x313ce567"  # decimals()
ERC20_TOTAL_SUPPLY = "0x18160ddd"  # totalSupply()

# A contract that answers any of these counts as a token (see REQUIRE_ERC20).
ERC20_PROBE = (ERC20_TOTAL_SUPPLY, ERC20_SYMBOL, ERC20_DECIMALS)
