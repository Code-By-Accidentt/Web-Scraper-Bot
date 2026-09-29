"""Which sites are sped up right now, and by whom: turbo sessions and auto-turbo windows."""

from __future__ import annotations

from collections.abc import Callable

TURBO = "turbo"  # the owner name of a /turbo session's boost
AUTO = "auto"  # the owner name of an auto-turbo window's boost


class SitePace:
    """Keeps who speeds each site up, and answers what is fast for a site right now.

    A turbo session speeds up the site's light polls and the pending watch's checks of its
    addresses; an auto-turbo window also the monitor's own rounds. The site's monitor hears
    when its rounds speed up, so it can start the next one at once.
    """

    def __init__(self) -> None:
        """Start with no site sped up."""
        self._boosts: dict[str, dict[str, bool]] = {}  # site -> owner -> rounds sped up too
        self._wakers: dict[str, list[Callable[[], None]]] = {}
        self._on_boost: list[Callable[[str], None]] = []

    def boost(self, site_url: str, owner: str, *, rounds: bool = False) -> None:
        """Speed ``site_url`` up for ``owner`` until :meth:`release`; ``rounds`` too if asked."""
        owners = self._boosts.setdefault(site_url, {})
        was_fast = bool(owners)
        owners[owner] = rounds
        if not was_fast:
            for listener in list(self._on_boost):
                listener(site_url)
        if rounds:
            for wake in list(self._wakers.get(site_url, ())):
                wake()

    def release(self, site_url: str, owner: str) -> None:
        """End ``owner``'s boost of ``site_url``; the site stays fast while others remain."""
        owners = self._boosts.get(site_url)
        if owners is None:
            return
        owners.pop(owner, None)
        if not owners:
            del self._boosts[site_url]

    def light_fast(self, site_url: str) -> bool:
        """True while the site's light polls run at the turbo pace."""
        return site_url in self._boosts

    def pending_fast(self, site_url: str) -> bool:
        """True while the pending watch checks the site's addresses at the turbo pace."""
        return site_url in self._boosts

    def rounds_fast(self, site_url: str) -> bool:
        """True while the site's monitor runs its rounds at the turbo pace."""
        return any(self._boosts.get(site_url, {}).values())

    def listen(self, site_url: str, wake: Callable[[], None]) -> None:
        """Call ``wake`` whenever the rounds of ``site_url`` speed up."""
        self._wakers.setdefault(site_url, []).append(wake)

    def unlisten(self, site_url: str, wake: Callable[[], None]) -> None:
        """Stop calling ``wake``; unknown callbacks are ignored."""
        wakers = self._wakers.get(site_url, [])
        if wake in wakers:
            wakers.remove(wake)
        if not wakers:
            self._wakers.pop(site_url, None)

    def on_boost(self, listener: Callable[[str], None]) -> None:
        """Call ``listener`` with the site each time a site that was not fast gets a boost."""
        self._on_boost.append(listener)
