"""One alert per token, however many pages, renders and modes find it at the same moment.

Concurrent checks of one address share a single lookup, so every page, turbo session and
the pending watch receive CONFIRMED for it at once. The first to claim the token announces
it; the others stay silent. A claim also writes the token to the known cache
(``known_contracts.txt``), which does the same job across restarts: a token becomes
"known" only when an alert for it is on its way, so a check whose caller was cancelled
(``/pause``, a shutdown) cannot hide a token that nobody announced.
"""

from __future__ import annotations

from scraper_bot.chains.address_extractor import sui_package
from scraper_bot.models import Address, Chain, ConfirmedContract
from scraper_bot.storage import AddressCache

_REMEMBERED = 10_000


class AlertRegistry:
    """Tokens announced by this process; a claim is made right before sending."""

    def __init__(self, cache: AddressCache | None = None, limit: int = _REMEMBERED) -> None:
        self._cache = cache
        self._limit = limit
        self._claimed: dict[str, None] = {}

    def claim(self, contract: ConfirmedContract) -> bool:
        """True for the first claim of the token: that caller sends the alert."""
        key = self._key(contract.address)
        if key in self._claimed:
            if self._cache is not None:
                # A Sui coin type and its bare package are one token but two addresses:
                # both must be known, or the other one is checked (and claimed) again later.
                self._cache.add_known(contract)
            return False
        if len(self._claimed) >= self._limit:
            del self._claimed[next(iter(self._claimed))]
        self._claimed[key] = None
        if self._cache is not None:
            self._cache.add_known(contract)
        return True

    @staticmethod
    def _key(address: Address) -> str:
        if address.chain is Chain.SUI:
            # A coin type and its bare package are one token.
            return f"Sui:{sui_package(address.value)}"
        if address.chain is Chain.ETHEREUM:
            return f"EVM:{address.value.lower()}"
        return f"Solana:{address.value}"  # base58 is case-sensitive
