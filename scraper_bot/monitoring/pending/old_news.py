"""Tokens a site's pausing first check showed but could confirm only later: old news.

When the first round of a site pauses it (a page full of tokens), some addresses of that
round get their verdict late: an optional network answered after its grace, or no network
answered at all. They were on the page all along, so each burst of them goes out as one
document, never as alerts of their own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from scraper_bot.models.addresses import (
    Address,
    Chain,
    ConfirmedContract,
    ValidationResult,
    ValidationStatus,
    address_key,
    parse_address_key,
)

QUIET = 10.0  # a burst's document goes out after this long without a new late token (s)
KEEP_FOR = 24 * 3600.0  # the addresses of a pausing first check count as old news for a day
LIMIT = 1000  # addresses remembered per site
_REJECTED = frozenset({ValidationStatus.REJECTED, ValidationStatus.ALREADY_REJECTED})


@dataclass(slots=True)
class _Site:
    """What one site's pausing first check could not confirm, and its late tokens so far."""

    since: float  # when that check paused the site (epoch seconds)
    # address -> True when that check had no verdict at all, so every network counts
    addresses: dict[Address, bool] = field(default_factory=dict)
    queued: dict[Address, ConfirmedContract] = field(default_factory=dict)
    last_queued: float = 0.0


class OldNews:
    """The addresses of every site paused by its first check that the check left unconfirmed.

    A token found later at one of them, on a network that check had no answer from, is
    queued; a burst goes out once no new one came for ``QUIET`` seconds. The addresses are
    forgotten once sent, with their site, or a day after the pause.
    """

    def __init__(self) -> None:
        """Start without any site."""
        self._sites: dict[str, _Site] = {}

    @property
    def waiting(self) -> bool:
        """True while late tokens wait for their document."""
        return any(site.queued for site in self._sites.values())

    def remember(self, site_url: str, results: Iterable[ValidationResult], now: float) -> None:
        """Note the verdicts the first check that paused ``site_url`` at ``now`` did not confirm.

        An address without any verdict counts on every network, an EVM rejection only on the
        optional networks (a verdict does not wait for them). Solana and Sui rejections are final.
        """
        site = self._sites.setdefault(site_url, _Site(now))
        site.since = now
        for result in results:
            anywhere = result.status is ValidationStatus.UNAVAILABLE
            evm_rejection = result.status in _REJECTED and result.address.chain is Chain.ETHEREUM
            if not (anywhere or evm_rejection):
                continue
            known = site.addresses.get(result.address)
            if known is None and len(site.addresses) >= LIMIT:
                continue
            site.addresses[result.address] = bool(known) or anywhere

    def covers(
        self, site_url: str, addresses: Iterable[Address], *, optional: bool, now: float
    ) -> bool:
        """Return True when the site's pausing first check showed one of ``addresses`` unanswered.

        ``optional`` says whether the token's network is an optional one. A pause over a day
        old covers nothing.
        """
        site = self._sites.get(site_url)
        if site is None or now - site.since > KEEP_FOR:
            return False
        return any(
            address in site.addresses and (optional or site.addresses[address])
            for address in addresses
        )

    def queue(self, site_url: str, contract: ConfirmedContract, now: float) -> None:
        """Hold a late token of the site for its burst's document.

        Only a token not queued yet keeps the burst open.
        """
        site = self._sites.get(site_url)
        if site is not None and contract.address not in site.queued:
            site.queued[contract.address] = contract
            site.last_queued = now

    def due(self, now: float) -> list[tuple[str, list[ConfirmedContract]]]:
        """Return and forget the late tokens of every site whose burst has been quiet.

        A site with nothing queued is forgotten once its addresses are all sent or a day
        has passed since its pause.
        """
        ready: list[tuple[str, list[ConfirmedContract]]] = []
        for url, site in list(self._sites.items()):
            # If the clock jumps back, don't hold the burst until the clock catches up.
            if site.queued and not 0 <= now - site.last_queued < QUIET:
                ready.append((url, list(site.queued.values())))
                for address in site.queued:
                    site.addresses.pop(address, None)
                site.queued = {}
            if not site.queued and (not site.addresses or now - site.since > KEEP_FOR):
                del self._sites[url]
        return ready

    def forget(self, site_url: str) -> None:
        """Forget a removed site, with the late tokens it still held."""
        self._sites.pop(site_url, None)

    def to_dict(self) -> dict[str, Any]:
        """Return the sites and their addresses as a JSON-ready dict.

        Queued tokens are not saved: they stay among the addresses until sent, so after a
        restart their next confirmation queues them again.
        """
        return {
            url: {
                "since": site.since,
                "addresses": {address_key(a): anywhere for a, anywhere in site.addresses.items()},
            }
            for url, site in self._sites.items()
        }

    @classmethod
    def from_dict(cls, raw: Any) -> OldNews:
        """Rebuild the memory from its saved dict; addresses that do not parse are left out."""
        news = cls()
        if not isinstance(raw, dict):
            return news
        for url, saved in raw.items():
            addresses = {
                address: bool(anywhere)
                for key, anywhere in saved.get("addresses", {}).items()
                if (address := parse_address_key(str(key))) is not None
            }
            news._sites[str(url)] = _Site(float(saved["since"]), addresses)
        return news
