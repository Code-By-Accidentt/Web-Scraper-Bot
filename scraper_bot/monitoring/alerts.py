"""One alert per token, however many pages, renders and modes find it at the same moment.

Concurrent checks of one address share a single lookup, so every page, turbo session and
the pending watch receive CONFIRMED for it at once. The first to claim the token announces
it; the others stay silent. Across restarts the known cache (``known_contracts.txt``) does
the same job, because a confirmed token is written there before anyone announces it.
"""

from __future__ import annotations

from scraper_bot.chains.address_extractor import sui_package
from scraper_bot.models import Address, Chain

_REMEMBERED = 10_000


class AlertRegistry:
    """Tokens announced by this process; a claim is made right before sending."""

    def __init__(self, limit: int = _REMEMBERED) -> None:
        self._limit = limit
        self._claimed: dict[str, None] = {}

    def claim(self, address: Address) -> bool:
        """True for the first claim of ``address``: that caller sends the alert."""
        key = self._key(address)
        if key in self._claimed:
            return False
        if len(self._claimed) >= self._limit:
            del self._claimed[next(iter(self._claimed))]
        self._claimed[key] = None
        return True

    def claimed(self, address: Address) -> bool:
        return self._key(address) in self._claimed

    @staticmethod
    def _key(address: Address) -> str:
        if address.chain is Chain.SUI:
            # A coin type and its bare package are one token.
            return f"Sui:{sui_package(address.value)}"
        if address.chain is Chain.ETHEREUM:
            return f"EVM:{address.value.lower()}"
        return f"Solana:{address.value}"  # base58 is case-sensitive
