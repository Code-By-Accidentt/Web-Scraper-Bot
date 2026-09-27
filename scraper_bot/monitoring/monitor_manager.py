"""Lifecycle of every site monitor: add, remove, pause, resume, persist, restart."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from scraper_bot.models import SiteState
from scraper_bot.monitoring.site_monitor import (
    AutoPauseCallback,
    MonitorStats,
    PersistCallback,
    SiteMonitor,
)
from scraper_bot.pages.urls import InvalidUrlError, SubUrlDeriver, normalize_url
from scraper_bot.storage import LegacyStateImporter, SnapshotStore, StateStore
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)

MonitorFactory = Callable[[SiteState, PersistCallback, AutoPauseCallback], SiteMonitor]


class MonitorError(Exception):
    """Base class for errors that a command turns into a reply (see ``BotCommands``)."""

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
        on_removed: Callable[[str], None] | None = None,
    ) -> None:
        self._state_store = state_store
        self._snapshot_store = snapshot_store
        self._deriver = deriver
        self._monitor_factory = monitor_factory
        self._legacy_importer = legacy_importer
        self._restart_delay = restart_delay
        self._notifier = notifier
        self._on_removed = on_removed
        self._sites: dict[str, SiteState] = {}
        self._monitors: dict[str, SiteMonitor] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._restart_timers: set[asyncio.TimerHandle] = set()
        self._closing = False  # shutting down: no monitor starts any more
        # state.json is damaged and could not be set aside: saving would overwrite it.
        self._frozen = False

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._sites = self._state_store.load()
        broken = self._state_store.broken
        # While a damaged copy waits for repair, its sites may come back: their snapshots
        # stay, and the files of version 1 are not imported in their place.
        repairing = bool(self._state_store.broken_copies())
        if broken is not None:
            problem, moved_to = broken
            if moved_to is None:
                self._frozen = True
                where = "It could not be moved aside, so the bot does not save the site list."
            else:
                where = f"The damaged file was moved to: {moved_to}"
                self.persist()  # the readable sites, so that the next start has them too
            if self._notifier is not None:
                self._notifier.report_later(
                    "state",
                    "The site list (state.json) was damaged",
                    f"{problem}\n{where}",
                    hint="Stop the bot, repair that file and put it back as state.json, or "
                    "/add the missing sites again; delete the damaged copy afterwards.",
                )
        elif not self._sites and not self._state_store.exists() and not repairing:
            self._import_legacy()
        self._canonical_keys()
        if broken is None and not repairing:
            self._snapshot_store.prune(
                page for site in self._sites.values() for page in self._deriver.pages(site.url)
            )
        for site in self._sites.values():
            if not site.paused:
                self._start_monitor(site)
        log.info("%d site(s) loaded, %d running", len(self._sites), len(self._tasks))

    async def shutdown(self) -> None:
        self._closing = True
        for timer in self._restart_timers:
            timer.cancel()
        self._restart_timers.clear()
        await asyncio.gather(*(self._stop_monitor(url) for url in list(self._tasks)))

    def persist(self) -> None:
        """Save the site list; a failed write is reported and retried by the next change."""
        if self._frozen:
            log.error("the site list is not saved: state.json is damaged and still in place")
            return
        try:
            self._state_store.save(self._sites)
        except OSError as exc:
            log.error("saving the site list failed: %s", exc)
            if self._notifier is not None:
                self._notifier.report_later(
                    "state-save",
                    "Saving the site list (state.json) failed",
                    str(exc),
                    hint="The next change saves again; check free disk space and whether "
                    "another program keeps the file open.",
                )

    # ── queries ─────────────────────────────────────────────────────────────
    def sites(self) -> list[SiteState]:
        return sorted(self._sites.values(), key=lambda site: site.url)

    def is_running(self, url: str) -> bool:
        task = self._tasks.get(url)
        return task is not None and not task.done()

    def is_paused(self, url: str) -> bool:
        site = self._sites.get(url)
        return site is not None and site.paused

    @property
    def restart_delay(self) -> float:
        """Seconds before a crashed monitor restarts on its own (0: it does not)."""
        return self._restart_delay

    def is_tracked(self, url: str) -> bool:
        return url in self._sites

    def resolve(self, raw_url: str) -> str:
        """The key of the monitored site ``raw_url`` names; :class:`SiteNotTracked` if none."""
        return self._resolve(raw_url)

    def sharing_pages(self, url: str) -> list[str]:
        """The other sites whose monitors also render one of the pages of ``url``."""
        pages = set(self._deriver.pages(url))
        return sorted(
            other
            for other in self._sites
            if other != url and not pages.isdisjoint(self._deriver.pages(other))
        )

    def stats(self, url: str) -> MonitorStats | None:
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
        # Pages another site renders too keep their snapshots.
        still_used = {page for other in self._sites for page in self._deriver.pages(other)}
        self._snapshot_store.delete_many(
            page for page in self._deriver.pages(url) if page not in still_used
        )
        self.persist()
        if self._on_removed is not None:
            self._on_removed(url)
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

    async def resume_site(self, raw_url: str) -> tuple[SiteState, bool]:
        """Resume a paused site; ``True`` second when it was STOPPED and got a new monitor.

        A site that is neither paused nor running lost its monitor to a crash the automatic
        restart did not cure; resuming it starts a fresh one.
        """
        url = self._resolve(raw_url)
        site = self._sites[url]
        if not site.paused:
            if self.is_running(url):
                raise SiteNotPaused(url)
            log.info("restarting the stopped monitor for %s on request", url)
            self._start_monitor(site)
            return site, True
        site.paused = False
        self.persist()
        self._start_monitor(site)
        return site, False

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

    def _canonical_keys(self) -> None:
        """Store sites under today's form of their URL (2.1 kept a default port, ":443")."""
        moved = False
        for key in list(self._sites):
            try:
                canonical = normalize_url(key)
            except InvalidUrlError:
                continue
            if canonical != key and canonical not in self._sites:
                site = self._sites.pop(key)
                site.url = canonical
                self._sites[canonical] = site
                moved = True
                log.info("site %s is stored as %s from now on", key, canonical)
        if moved:
            self.persist()

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
        if url not in self._sites and "#" in url:
            # Before 2.1.1 a route was dropped: "/add app.io/#/presale" stored app.io.
            parts = urlsplit(url)
            path = parts.path if parts.path not in {"", "/"} else ""
            url = urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
        if url not in self._sites:
            raise SiteNotTracked(url)
        return url

    def _start_monitor(self, site: SiteState) -> None:
        if self._closing or self.is_running(site.url):
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
            # asyncio.wait, not `await task`: a suppressed CancelledError would also
            # swallow the cancellation of the caller (e.g. Ctrl-C during /remove).
            await asyncio.wait({task})

    def _on_monitor_done(self, url: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(url) is task:
            del self._tasks[url]
            self._monitors.pop(url, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            # The monitor stopped itself (auto-pause) while finishing its round. If
            # /resume cleared the pause flag meanwhile, the site needs a fresh monitor.
            self._restart(url)
            return
        log.error("monitor for %s crashed: %r", url, error, exc_info=error)
        restarts = self._restart_delay > 0 and url in self._sites and not self._closing
        if restarts:
            self._restart_later(url)
        if self._notifier is not None:
            hint = (
                f"It restarts automatically in {self._restart_delay:.0f} s."
                if restarts
                else "It will not restart on its own; check the log."
            )
            self._notifier.report_later(
                f"monitor:{url}",
                f"Monitor for {url} crashed",
                f"{type(error).__name__}: {error}",
                hint=hint,
            )

    def _restart_later(self, url: str) -> None:
        loop = asyncio.get_running_loop()

        def fire() -> None:
            self._restart_timers.discard(timer)
            self._restart(url)

        timer = loop.call_later(self._restart_delay, fire)
        self._restart_timers.add(timer)

    def _restart(self, url: str) -> None:
        site = self._sites.get(url)
        if self._closing or site is None or site.paused or self.is_running(url):
            return
        log.info("restarting monitor for %s", url)
        self._start_monitor(site)
