"""Lifecycle of every site monitor: add, remove, pause, resume, persist, restart."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from scraper_bot.models import SiteState
from scraper_bot.notifier import Notifier
from scraper_bot.site_monitor import (
    AutoPauseCallback,
    MonitorStats,
    PersistCallback,
    SiteMonitor,
)
from scraper_bot.storage import LegacyStateImporter, SnapshotStore, StateStore
from scraper_bot.urls import InvalidUrlError, SubUrlDeriver, normalize_url

log = logging.getLogger(__name__)

MonitorFactory = Callable[[SiteState, PersistCallback, AutoPauseCallback], SiteMonitor]


class MonitorError(Exception):
    """Base class for problems reported back to the user verbatim."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


class SiteAlreadyTracked(MonitorError):
    pass


class SiteNotTracked(MonitorError):
    pass


class SiteAlreadyPaused(MonitorError):
    pass


class SiteNotPaused(MonitorError):
    pass


class MonitorManager:
    """Owns the site registry and one asyncio task per running monitor."""

    def __init__(
        self,
        *,
        state_store: StateStore,
        snapshot_store: SnapshotStore,
        deriver: SubUrlDeriver,
        monitor_factory: MonitorFactory,
        legacy_importer: LegacyStateImporter | None = None,
        restart_delay: float = 30.0,
        notifier: Notifier | None = None,
    ) -> None:
        self._state_store = state_store
        self._snapshot_store = snapshot_store
        self._deriver = deriver
        self._monitor_factory = monitor_factory
        self._legacy_importer = legacy_importer
        self._restart_delay = restart_delay
        self._notifier = notifier
        self._sites: dict[str, SiteState] = {}
        self._monitors: dict[str, SiteMonitor] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[object]] = set()

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._sites = self._state_store.load()
        if not self._sites and not self._state_store.exists():
            self._import_legacy()
        self._snapshot_store.prune(
            page for site in self._sites.values() for page in self._deriver.pages(site.url)
        )
        for site in self._sites.values():
            if not site.paused:
                self._start_monitor(site)
        log.info("%d site(s) loaded, %d running", len(self._sites), len(self._tasks))

    async def shutdown(self) -> None:
        await asyncio.gather(*(self._stop_monitor(url) for url in list(self._tasks)))

    def persist(self) -> None:
        self._state_store.save(self._sites)

    # ── queries ─────────────────────────────────────────────────────────────
    def sites(self) -> list[SiteState]:
        return sorted(self._sites.values(), key=lambda site: site.url)

    def is_running(self, url: str) -> bool:
        task = self._tasks.get(url)
        return task is not None and not task.done()

    def stats(self, url: str) -> MonitorStats | None:
        """Runtime counters of the running monitor for ``url``, if any."""
        monitor = self._monitors.get(url)
        return monitor.stats if monitor is not None else None

    # ── commands ────────────────────────────────────────────────────────────
    async def add_site(self, raw_url: str) -> SiteState:
        url = normalize_url(raw_url)
        if url in self._sites:
            raise SiteAlreadyTracked(url)
        site = SiteState(url=url)
        self._sites[url] = site
        self.persist()
        self._start_monitor(site)
        return site

    async def remove_site(self, raw_url: str) -> SiteState:
        url = self._resolve(raw_url)
        site = self._sites.pop(url)
        await self._stop_monitor(url)
        self._snapshot_store.delete_many(self._deriver.pages(url))
        self.persist()
        return site

    async def pause_site(self, raw_url: str) -> SiteState:
        url = self._resolve(raw_url)
        site = self._sites[url]
        if site.paused:
            raise SiteAlreadyPaused(url)
        site.paused = True
        await self._stop_monitor(url)
        self.persist()
        return site

    async def resume_site(self, raw_url: str) -> SiteState:
        url = self._resolve(raw_url)
        site = self._sites[url]
        if not site.paused:
            raise SiteNotPaused(url)
        site.paused = False
        self.persist()
        self._start_monitor(site)
        return site

    def set_notifications(self, raw_url: str, enabled: bool) -> SiteState:
        url = self._resolve(raw_url)
        site = self._sites[url]
        site.notifications_enabled = enabled
        self.persist()
        return site

    def set_unverified(self, raw_url: str, enabled: bool) -> SiteState:
        """Switch the report-first mode of a site; picked up by its monitor on the next round."""
        url = self._resolve(raw_url)
        site = self._sites[url]
        site.unverified_enabled = enabled
        self.persist()
        return site

    async def auto_pause(self, url: str, page_url: str) -> None:
        """Invoked by a monitor from inside its own task, so it never awaits that task."""
        site = self._sites.get(url)
        if site is None or site.paused:
            return
        site.paused = True
        self.persist()
        monitor = self._monitors.get(url)
        if monitor is not None:
            monitor.request_stop()
        log.info("auto-paused %s (trigger: %s)", url, page_url)

    # ── internals ───────────────────────────────────────────────────────────
    def _import_legacy(self) -> None:
        if self._legacy_importer is None or not self._legacy_importer.has_data():
            return
        self._sites = self._legacy_importer.sites()
        if self._sites:
            self.persist()
            log.info("imported %d site(s) from the legacy state files", len(self._sites))

    def _resolve(self, raw_url: str) -> str:
        try:
            url = normalize_url(raw_url)
        except InvalidUrlError as exc:
            raise SiteNotTracked(raw_url) from exc
        if url not in self._sites:
            raise SiteNotTracked(url)
        return url

    def _start_monitor(self, site: SiteState) -> None:
        if self.is_running(site.url):
            return
        monitor = self._monitor_factory(site, self.persist, self.auto_pause)
        task = asyncio.create_task(monitor.run(), name=f"monitor:{site.url}")
        task.add_done_callback(lambda done, url=site.url: self._on_monitor_done(url, done))
        self._monitors[site.url] = monitor
        self._tasks[site.url] = task

    async def _stop_monitor(self, url: str) -> None:
        monitor = self._monitors.pop(url, None)
        task = self._tasks.pop(url, None)
        if monitor is not None:
            monitor.request_stop()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _on_monitor_done(self, url: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(url) is task:
            del self._tasks[url]
            self._monitors.pop(url, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        log.error("monitor for %s crashed: %r", url, error, exc_info=error)
        restarts = self._restart_delay > 0 and url in self._sites
        if restarts:
            asyncio.get_running_loop().call_later(self._restart_delay, self._restart, url)
        if self._notifier is not None:
            hint = (
                f"It restarts automatically in {self._restart_delay:.0f} s."
                if restarts
                else "It will not restart on its own; check the log."
            )
            self._in_background(
                self._notifier.problem(
                    f"monitor:{url}",
                    f"Monitor for {url} crashed",
                    f"{type(error).__name__}: {error}",
                    hint=hint,
                )
            )

    def _in_background(self, coroutine: Awaitable[object]) -> None:
        """Run a notification from a synchronous callback without losing the task."""
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _restart(self, url: str) -> None:
        site = self._sites.get(url)
        if site is None or site.paused or self.is_running(url):
            return
        log.info("restarting monitor for %s", url)
        self._start_monitor(site)
