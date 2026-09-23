"""Report-first, verify-later mode for one site (``/unverified <url> on``).

Every new address is announced at once as *unverified*, then re-checked on-chain
every ``unverified_recheck`` seconds for ``unverified_watch`` seconds, bypassing the
negative cache. Confirmations get a message; alerts above the hourly cap go into one digest.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

from scraper_bot.contract_validators import ContractValidator
from scraper_bot.models import Address, Chain, SiteState, ValidationStatus
from scraper_bot.notifier import Notifier
from scraper_bot.preferences import Tuning
from scraper_bot.storage import AddressCache

log = logging.getLogger(__name__)

_MEMORY = 7 * 86_400  # how long an announced address is remembered, so it is not announced twice
_MAX_ENTRIES = 1000
_DIGEST_PERIOD = 3600.0


def address_key(address: Address) -> str:
    return f"{address.chain.value}:{address.value}"


def parse_key(key: str) -> Address | None:
    chain_name, _, value = key.partition(":")
    try:
        return Address(Chain(chain_name), value) if value else None
    except ValueError:
        return None


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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._site = site
        self._validator = validator
        self._cache = cache
        self._notifier = notifier
        self._persist = persist
        self._tuning = tuning
        self._clock = clock
        self._pages: dict[str, str] = {}
        self._last_check: dict[str, float] = {}
        self._alert_times: list[float] = []
        self._digest_pending = 0
        self._digest_sent_at: float | None = None

    @property
    def enabled(self) -> bool:
        return self._site.unverified_enabled

    async def observe(self, candidates: Sequence[Address], page_url: str) -> None:
        """Announce addresses never seen on this site before and start watching them."""
        if not self.enabled or not candidates:
            return
        now = self._clock()
        self._prune(now)
        fresh = [
            address
            for address in candidates
            if address_key(address) not in self._site.unverified
            and not self._cache.is_known(address)
        ]
        for address in fresh:
            key = address_key(address)
            self._site.unverified[key] = now
            self._pages[key] = page_url
            if self._under_cap(now):
                self._alert_times.append(now)
                await self._notifier.unverified_address(
                    address,
                    page_url,
                    self._tuning.unverified_recheck,
                    self._tuning.unverified_watch,
                )
            else:
                self._digest_pending += 1
        if fresh:
            self._persist()
        await self._flush_digest(now)

    async def recheck(self) -> None:
        """Validate the watched addresses that are due; announce confirmations."""
        if not self.enabled:
            return
        now = self._clock()
        await self._flush_digest(now)  # a quiet site must still get its overflow digest
        due: list[Address] = []
        for key, announced_at in self._site.unverified.items():
            if now - announced_at > self._tuning.unverified_watch:
                continue
            if now - self._last_check.get(key, 0.0) < self._tuning.unverified_recheck:
                continue
            address = parse_key(key)
            if address is not None:
                due.append(address)
        if not due:
            return
        changed = False
        for result in await self._validator.validate_many(due, negative_cache=False):
            key = address_key(result.address)
            self._last_check[key] = now
            if result.status is ValidationStatus.CONFIRMED and result.contract is not None:
                await self._notifier.address_confirmed(
                    result.contract, self._pages.get(key, self._site.url)
                )
                self._forget(key)
                changed = True
            elif result.status is ValidationStatus.ALREADY_KNOWN:
                self._forget(key)  # confirmed and announced by the regular monitor meanwhile
                changed = True
        if changed:
            self._persist()

    def _under_cap(self, now: float) -> bool:
        self._alert_times = [stamp for stamp in self._alert_times if now - stamp < 3600]
        return len(self._alert_times) < self._tuning.unverified_max_per_hour

    async def _flush_digest(self, now: float) -> None:
        if not self._digest_pending:
            return
        if self._digest_sent_at is None or now - self._digest_sent_at >= _DIGEST_PERIOD:
            count, self._digest_pending = self._digest_pending, 0
            self._digest_sent_at = now
            await self._notifier.unverified_digest(
                self._site.url, count, self._tuning.unverified_watch
            )

    def _forget(self, key: str) -> None:
        self._site.unverified.pop(key, None)
        self._pages.pop(key, None)
        self._last_check.pop(key, None)

    def _prune(self, now: float) -> None:
        entries = self._site.unverified
        for key in [key for key, stamp in entries.items() if now - stamp > _MEMORY]:
            self._forget(key)
        if len(entries) > _MAX_ENTRIES:
            oldest = sorted(entries, key=entries.__getitem__)[: len(entries) - _MAX_ENTRIES]
            for key in oldest:
                self._forget(key)
