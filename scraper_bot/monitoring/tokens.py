"""News of a site's new tokens for the turbo sessions and auto-turbo windows that wait for them."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from scraper_bot.models.addresses import Address, ConfirmedContract

Subscriber = Callable[[ConfirmedContract], None]


class SiteTokens:
    """Hands every new token of a site to whoever subscribed to that site.

    The monitor, the pending watch and turbo sessions publish what they announce or see
    announced; turbo sessions and auto-turbo windows subscribe, to count their finds and to
    stop at ``turbo_stop_after``. A token published without its network gets the one the
    known cache has.
    """

    def __init__(self, known_network: Callable[[Address], str | None] = lambda _a: None) -> None:
        """Start without subscribers; ``known_network`` names the network of a known token."""
        self._known_network = known_network
        self._subscribers: dict[str, list[Subscriber]] = {}

    def subscribe(self, site_url: str, callback: Subscriber) -> None:
        """Hand every token published for ``site_url`` to ``callback`` from now on."""
        self._subscribers.setdefault(site_url, []).append(callback)

    def unsubscribe(self, site_url: str, callback: Subscriber) -> None:
        """Stop handing tokens to ``callback``; unknown callbacks are ignored."""
        callbacks = self._subscribers.get(site_url, [])
        if callback in callbacks:
            callbacks.remove(callback)
        if not callbacks:
            self._subscribers.pop(site_url, None)

    def publish(
        self, site_urls: Iterable[str], address: Address, network: str | None = None
    ) -> None:
        """Tell the subscribers of each of ``site_urls`` that ``address`` is a token."""
        sites = [site for site in dict.fromkeys(site_urls) if site in self._subscribers]
        if not sites:
            return
        name = network or self._known_network(address) or "unknown network"
        contract = ConfirmedContract(address, name)
        for site in sites:
            for callback in list(self._subscribers.get(site, ())):
                callback(contract)
