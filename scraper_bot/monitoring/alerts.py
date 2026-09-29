"""One alert per token, however many pages, renders and modes find it at the same moment.

Pages, turbo sessions and the pending watch may confirm the same token at once; the first
to claim it sends the alert and the others stay silent. Only a claim marks a token as known
in the cache, so a check cancelled by /pause or a shutdown cannot hide an unannounced token.
"""

from __future__ import annotations

from scraper_bot.chains.addresses.formats import sui_package
from scraper_bot.models.addresses import Address, Chain, ConfirmedContract
from scraper_bot.storage.address_cache import AddressCache

_REMEMBERED = 10_000


class AlertRegistry:
    """Remembers the tokens this process announced, so each one gets a single alert.

    Callers claim a token right before they send its alert. Only the latest ``limit``
    claims stay in memory; the known cache remembers the tokens across restarts.
    """

    def __init__(self, cache: AddressCache | None = None, limit: int = _REMEMBERED) -> None:
        """Record claimed tokens in ``cache`` (if given) and remember at most ``limit``."""
        self._cache = cache
        self._limit = limit
        self._claimed: dict[str, None] = {}

    def claim(self, contract: ConfirmedContract) -> bool:
        """Claim a token and return True if it is the first claim: that caller sends the alert.

        Every claim also adds the address to the known cache.
        """
        key = self._key(contract.address)
        if key in self._claimed:
            if self._cache is not None:
                # A Sui coin type and its package are one token but two addresses, so cache both.
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
        """Return the registry key of an address: one key per token.

        A Sui coin type and its package share a key, and EVM addresses ignore case.
        """
        if address.chain is Chain.SUI:
            return f"Sui:{sui_package(address.value)}"
        if address.chain is Chain.ETHEREUM:
            return f"EVM:{address.value.lower()}"
        return f"Solana:{address.value}"  # base58 is case-sensitive
