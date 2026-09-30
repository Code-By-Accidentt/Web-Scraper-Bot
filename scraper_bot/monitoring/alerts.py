"""One alert per token, however many pages, renders and modes find it at the same moment.

Pages, turbo sessions and the pending watch may confirm the same token at once; the first
to claim it sends the alert and the others stay silent. Only a claim marks a token as known
in the cache, so a check cancelled by /pause or a shutdown cannot hide an unannounced token.
Every path announces through an :class:`Announcer`, so each has the same side effects.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Container, Iterable
from typing import Protocol

from scraper_bot.chains.addresses.formats import sui_package
from scraper_bot.models.addresses import Address, Chain, ConfirmedContract
from scraper_bot.monitoring.tokens import SiteTokens
from scraper_bot.storage.address_cache import AddressCache
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)

_REMEMBERED = 10_000

# Gets the tokens a caller claimed first and starts their alerts.
Send = Callable[[list[ConfirmedContract]], Awaitable[object]]


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


class AnnouncedWatch(Protocol):
    """The part of the pending watch that hears about announced tokens."""

    def announced(self, site_url: str, address: Address) -> frozenset[str]:
        """Stop watching an announced token and return the other sites that showed it."""
        ...


class Announcer:
    """Announces confirmed tokens the same way, whichever path confirmed them.

    The monitor, turbo sessions, the pending watch and the unverified mode all claim a token
    first, then send its alert, and only then tell the pending watch and the turbo sessions
    and auto-turbo windows of the sites that showed it.
    """

    def __init__(
        self,
        registry: AlertRegistry,
        notifier: Notifier,
        tokens: SiteTokens,
        pending: AnnouncedWatch | None = None,
    ) -> None:
        """Claim tokens in ``registry`` and publish them to ``tokens``.

        ``pending`` is the pending watch, which stops watching what was announced;
        ``notifier`` reports when passing a token on fails.
        """
        self._registry = registry
        self._notifier = notifier
        self._tokens = tokens
        self._pending = pending

    async def announce(
        self,
        site_url: str,
        contracts: Iterable[ConfirmedContract],
        send: Send,
        *,
        news: Container[ConfirmedContract] | None = None,
        also: Iterable[str] = (),
    ) -> list[ConfirmedContract]:
        """Claim ``contracts``, send the alerts of those claimed here, then pass them all on.

        ``send`` gets the tokens this call claimed first. It must start their alerts before
        it first waits, as the notifier's alert methods do, so a cancellation can't lose the
        alert of a token that is already known. Then every token in ``news`` (all of them by
        default), whoever claimed it, leaves the pending watch and reaches the turbo sessions
        and auto-turbo windows of ``site_url``, of the ``also`` sites and of the other sites
        the watch saw it on. Return the tokens this call claimed.
        """
        contracts = list(contracts)
        fresh = [contract for contract in contracts if self._registry.claim(contract)]
        if fresh:
            await send(fresh)
        try:
            for contract in contracts:
                if news is not None and contract not in news:
                    continue
                sites = {site_url, *also}
                if self._pending is not None:
                    sites |= self._pending.announced(site_url, contract.address)
                self._tokens.publish(sites, contract.address, contract.network)
        except Exception as exc:
            log.exception("[%s] telling others about announced tokens failed", site_url)
            self._notifier.report_later(
                f"tell:{site_url}",
                f"Could not pass the tokens found on {site_url} on",
                f"{type(exc).__name__}: {exc}",
                hint="The alerts went out; a turbo session or auto-turbo window of the site "
                "may not stop at its first token.",
            )
        return fresh
