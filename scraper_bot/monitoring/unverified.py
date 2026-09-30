"""Report-first, verify-later mode for one site (``/unverified <url> on``).

Every new address is announced at once as unverified, then re-checked on-chain every
``unverified_recheck`` seconds for ``unverified_watch`` seconds, bypassing the negative
cache; a round re-checks 25 addresses at most. Confirmations get a message; alerts above
the hourly cap go into one digest.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

from scraper_bot.chains.validator import ContractValidator
from scraper_bot.models.addresses import (
    Address,
    ConfirmedContract,
    ValidationStatus,
    address_key,
    parse_address_key,
)
from scraper_bot.models.sites import SiteState
from scraper_bot.monitoring.alerts import AlertRegistry, AnnouncedWatch, Announcer
from scraper_bot.monitoring.tokens import SiteTokens
from scraper_bot.settings.live import Tuning
from scraper_bot.storage.address_cache import AddressCache
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)

_MEMORY = 7 * 86_400  # how long we remember an announced address, so it isn't announced twice (s)
_MAX_ENTRIES = 1000
_DIGEST_PERIOD = 3600.0
_RECHECK_AT_ONCE = 25  # addresses validated per recheck; the others wait for the next rounds


class UnverifiedWatcher:
    """Announces new addresses of one site immediately and keeps re-checking them."""

    def __init__(
        self,
        site: SiteState,
        *,
        validator: ContractValidator,
        cache: AddressCache,
        notifier: Notifier,
        persist: Callable[[], None],
        tuning: Tuning,
        registry: AlertRegistry | None = None,
        pending: AnnouncedWatch | None = None,
        tokens: SiteTokens | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Set up the watcher of ``site``; announced addresses are saved in ``site.unverified``.

        A confirmed address also leaves the ``pending`` watch and reaches the site's turbo
        sessions and auto-turbo windows through ``tokens``.
        """
        self._site = site
        self._validator = validator
        self._cache = cache
        self._notifier = notifier
        self._persist = persist
        self._tuning = tuning
        self._registry = registry if registry is not None else AlertRegistry()
        self._announcer = Announcer(self._registry, notifier, tokens or SiteTokens(), pending)
        self._clock = clock
        self._pages: dict[str, str] = {}
        self._last_check: dict[str, float] = {}
        self._alert_times: list[float] = []
        self._digest_pending = 0
        self._digest_sent_at: float | None = None

    @property
    def enabled(self) -> bool:
        """True while the site's unverified mode is on."""
        return self._site.unverified_enabled

    async def observe(self, candidates: Sequence[Address], page_url: str) -> None:
        """Announce addresses never seen on this site before and start watching them.

        Every new address is marked as seen before the first message goes out, so a page
        checked at the same moment can't announce it a second time.
        """
        if not self.enabled or not candidates:
            return
        now = self._clock()
        self._prune(now)
        fresh = False
        alerts: list[Address] = []
        for address in candidates:
            key = address_key(address)
            if key in self._site.unverified or self._cache.is_known(address):
                continue
            fresh = True
            self._site.unverified[key] = now
            self._pages[key] = page_url
            if self._under_cap(now):
                self._alert_times.append(now)
                alerts.append(address)
            else:
                self._digest_pending += 1
        if fresh:
            self._persist()
        for address in alerts:
            await self._notifier.unverified_address(
                address,
                page_url,
                self._tuning.unverified_recheck,
                self._tuning.unverified_watch,
            )
        await self._flush_digest(now)

    async def recheck(self) -> None:
        """Validate the watched addresses that are due and announce those that became tokens.

        At most ``_RECHECK_AT_ONCE`` are validated per round, the ones checked longest ago
        first, so a round never waits on hundreds of them. It also sends the overflow digest
        once its hour is up.
        """
        if not self.enabled:
            return
        now = self._clock()
        await self._flush_digest(now)  # a quiet site must still get its overflow digest
        due: list[tuple[float, Address]] = []
        for key, announced_at in self._site.unverified.items():
            if now - announced_at > self._tuning.unverified_watch:
                continue
            checked = self._last_check.get(key, 0.0)
            if now - checked < self._tuning.unverified_recheck:
                continue
            address = parse_address_key(key)
            if address is not None:
                due.append((checked, address))
        if not due:
            return
        due.sort(key=lambda item: item[0])
        batch = [address for _, address in due[:_RECHECK_AT_ONCE]]
        results = await self._validator.validate_many(batch, negative_cache=False)
        for key in map(address_key, batch):
            if key in self._site.unverified:  # not forgotten while it was validated
                self._last_check[key] = now
        changed = False
        for result in results:
            key = address_key(result.address)
            if result.status is ValidationStatus.CONFIRMED and result.contract is not None:
                # The pending watch may confirm it at the same moment, so only one of us alerts.
                await self._announcer.announce(
                    self._site.url, [result.contract], self._send_confirmed
                )
                self._forget(key)
                changed = True
            elif result.status is ValidationStatus.ALREADY_KNOWN:
                self._forget(key)
                changed = True
        if changed:
            self._persist()

    async def _send_confirmed(self, contracts: list[ConfirmedContract]) -> None:
        """Say that watched addresses became tokens, each with the page it was seen on."""
        for contract in contracts:
            page = self._pages.get(address_key(contract.address), self._site.url)
            await self._notifier.address_confirmed(contract, page)

    def _under_cap(self, now: float) -> bool:
        """Return True while fewer than ``unverified_max_per_hour`` alerts went out this hour."""
        self._alert_times = [stamp for stamp in self._alert_times if now - stamp < 3600]
        return len(self._alert_times) < self._tuning.unverified_max_per_hour

    async def _flush_digest(self, now: float) -> None:
        """Send one digest for the addresses over the hourly cap, at most once an hour."""
        if not self._digest_pending:
            return
        if self._digest_sent_at is None or now - self._digest_sent_at >= _DIGEST_PERIOD:
            count, self._digest_pending = self._digest_pending, 0
            self._digest_sent_at = now
            await self._notifier.unverified_digest(
                self._site.url, count, self._tuning.unverified_watch
            )

    def _forget(self, key: str) -> None:
        """Stop watching an address and forget its page and last check."""
        self._site.unverified.pop(key, None)
        self._pages.pop(key, None)
        self._last_check.pop(key, None)

    def _prune(self, now: float) -> None:
        """Forget addresses announced over a week ago, and the oldest beyond ``_MAX_ENTRIES``."""
        entries = self._site.unverified
        for key in [key for key, stamp in entries.items() if now - stamp > _MEMORY]:
            self._forget(key)
        if len(entries) > _MAX_ENTRIES:
            oldest = sorted(entries, key=entries.__getitem__)[: len(entries) - _MAX_ENTRIES]
            for key in oldest:
                self._forget(key)
