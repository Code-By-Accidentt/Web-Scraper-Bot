"""Lifecycle of every site monitor: add, remove, pause, resume, persist, restart."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from scraper_bot.models.sites import SiteState
from scraper_bot.monitoring.site_monitor import (
    AutoPauseCallback,
    MonitorStats,
    PersistCallback,
    SiteMonitor,
)
from scraper_bot.pages.urls import InvalidUrlError, SubUrlDeriver, normalize_url
from scraper_bot.storage.legacy import LegacyStateImporter
from scraper_bot.storage.snapshots import SnapshotStore
from scraper_bot.storage.state import StateStore
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)

STOP_WAIT = 10.0  # seconds /remove, /pause and a shutdown wait for a cancelled monitor to end

MonitorFactory = Callable[[SiteState, PersistCallback, AutoPauseCallback], SiteMonitor]


class MonitorError(Exception):
    """Base of the site errors that a command turns into a reply (see ``BotCommands``)."""

    def __init__(self, url: str) -> None:
        """Keep the URL of the site the error is about."""
        super().__init__(url)
        self.url = url


class SiteAlreadyTracked(MonitorError):
    """The site is already monitored."""


class SiteNotTracked(MonitorError):
    """No monitored site matches the URL."""


class SiteAlreadyPaused(MonitorError):
    """The site is already paused."""


class SiteNotPaused(MonitorError):
    """The site is running, not paused."""


class MonitorManager:
    """Owns the site list and one asyncio task per running monitor.

    Every change to the list is saved to state.json at once.
    """

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
        """Set up the manager; :meth:`start` loads the sites and starts their monitors.

        ``restart_delay`` is how long a crashed monitor waits before it restarts (0: never),
        and ``on_removed`` is called with the URL of every removed site.
        """
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
        self._closing = False
        # state.json is damaged and could not be set aside: saving would overwrite it.
        self._frozen = False

    async def start(self) -> None:
        """Load the sites and start the monitors of those that are not paused.

        A damaged state.json is reported to the user. The files of version 1 are imported
        only when there is no site list at all.
        """
        self._sites = self._state_store.load()
        broken = self._state_store.broken
        # A damaged copy may still be repaired: keep its snapshots, import no version-1 files.
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
        """Stop every monitor and cancel pending restarts; nothing starts afterwards."""
        self._closing = True
        for timer in self._restart_timers:
            timer.cancel()
        self._restart_timers.clear()
        await asyncio.gather(*(self._stop_monitor(url) for url in list(self._tasks)))

    def persist(self) -> None:
        """Save the site list to state.json.

        A failed write is reported to the user and tried again on the next change. Nothing
        is saved while a damaged state.json is still in place.
        """
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

    def sites(self) -> list[SiteState]:
        """Return every site, sorted by URL."""
        return sorted(self._sites.values(), key=lambda site: site.url)

    def is_running(self, url: str) -> bool:
        """Return True when the site's monitor task is running."""
        task = self._tasks.get(url)
        return task is not None and not task.done()

    def is_paused(self, url: str) -> bool:
        """Return True when the site is monitored and paused."""
        site = self._sites.get(url)
        return site is not None and site.paused

    @property
    def restart_delay(self) -> float:
        """Seconds before a crashed monitor restarts on its own (0: it does not)."""
        return self._restart_delay

    def is_tracked(self, url: str) -> bool:
        """Return True when ``url`` is the key of a monitored site."""
        return url in self._sites

    def resolve(self, raw_url: str) -> str:
        """Return the key of the monitored site ``raw_url`` names; raise SiteNotTracked if none."""
        return self._resolve(raw_url)

    def sharing_pages(self, url: str) -> list[str]:
        """Return the other sites whose monitors also render one of the pages of ``url``."""
        pages = set(self._deriver.pages(url))
        return sorted(
            other
            for other in self._sites
            if other != url and not pages.isdisjoint(self._deriver.pages(other))
        )

    def stats(self, url: str) -> MonitorStats | None:
        """Return the counters of the site's monitor, or None when it has no monitor."""
        monitor = self._monitors.get(url)
        return monitor.stats if monitor is not None else None

    async def add_site(self, raw_url: str) -> SiteState:
        """Add a site, save the list and start its monitor.

        Raises SiteAlreadyTracked for a site already monitored and InvalidUrlError for a bad
        URL.
        """
        url = normalize_url(raw_url)
        if url in self._sites:
            raise SiteAlreadyTracked(url)
        site = SiteState(url=url)
        self._sites[url] = site
        self.persist()
        self._start_monitor(site)
        return site

    async def remove_site(self, raw_url: str) -> SiteState:
        """Stop and forget a site, delete its page snapshots and return it.

        Snapshots of pages that another site renders too are kept.
        """
        url = self._resolve(raw_url)
        site = self._sites.pop(url)
        await self._stop_monitor(url)
        still_used = {page for other in self._sites for page in self._deriver.pages(other)}
        self._snapshot_store.delete_many(
            page for page in self._deriver.pages(url) if page not in still_used
        )
        self.persist()
        if self._on_removed is not None:
            self._on_removed(url)
        return site

    async def pause_site(self, raw_url: str) -> SiteState:
        """Pause a site and stop its monitor; raise SiteAlreadyPaused if it is paused."""
        url = self._resolve(raw_url)
        site = self._sites[url]
        if site.paused:
            raise SiteAlreadyPaused(url)
        site.paused = True
        await self._stop_monitor(url)
        self.persist()
        return site

    async def resume_site(self, raw_url: str) -> tuple[SiteState, bool]:
        """Resume a paused site and start its monitor; return the site and a flag.

        The flag is True when the site was not paused but had lost its monitor to a crash
        the automatic restart did not cure; a fresh monitor starts then. A running site
        raises SiteNotPaused.
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
        """Turn the change notifications of a site on or off, and save."""
        url = self._resolve(raw_url)
        site = self._sites[url]
        site.notifications_enabled = enabled
        self.persist()
        return site

    def set_unverified(self, raw_url: str, enabled: bool) -> SiteState:
        """Turn the report-first mode of a site on or off; its monitor sees it next round."""
        url = self._resolve(raw_url)
        site = self._sites[url]
        site.unverified_enabled = enabled
        self.persist()
        return site

    async def auto_pause(self, url: str, page_url: str) -> None:
        """Pause a site whose page listed too many new tokens at once, and save.

        A monitor calls this from inside its own task, so it only asks the monitor to stop
        and never waits for it.
        """
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
        """Take the sites from the version-1 state files, if there are any, and save them."""
        if self._legacy_importer is None or not self._legacy_importer.has_data():
            return
        self._sites = self._legacy_importer.sites()
        if self._sites:
            self.persist()
            log.info("imported %d site(s) from the legacy state files", len(self._sites))

    def _resolve(self, raw_url: str) -> str:
        """Return the key of the monitored site ``raw_url`` names, or raise SiteNotTracked."""
        try:
            url = normalize_url(raw_url)
        except InvalidUrlError as exc:
            raise SiteNotTracked(raw_url) from exc
        if url not in self._sites and "#" in url:
            # Before 2.1.1 "/add app.io/#/presale" stored app.io: match that key too.
            parts = urlsplit(url)
            path = parts.path if parts.path not in {"", "/"} else ""
            url = urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
        if url not in self._sites:
            raise SiteNotTracked(url)
        return url

    def _start_monitor(self, site: SiteState) -> None:
        """Create the site's monitor and run it as a task, unless it runs or the bot stops."""
        if self._closing or self.is_running(site.url):
            return
        monitor = self._monitor_factory(site, self.persist, self.auto_pause)
        task = asyncio.create_task(monitor.run(), name=f"monitor:{site.url}")
        task.add_done_callback(lambda done, url=site.url: self._on_monitor_done(url, done))
        self._monitors[site.url] = monitor
        self._tasks[site.url] = task

    async def _stop_monitor(self, url: str) -> None:
        """Ask the site's monitor to stop, cancel its task and wait for it, ``STOP_WAIT`` s at most.

        A task still running then (stuck in a browser call) is left to end on its own, so the
        command that stopped it still gets its answer.
        """
        monitor = self._monitors.pop(url, None)
        task = self._tasks.pop(url, None)
        if monitor is not None:
            monitor.request_stop()
        if task is not None and not task.done():
            task.cancel()
            # asyncio.wait, not `await task`: suppressing its CancelledError would hide ours too.
            done, _ = await asyncio.wait({task}, timeout=STOP_WAIT)
            if not done:
                log.warning("monitor for %s did not stop within %.0fs; moving on", url, STOP_WAIT)

    def _on_monitor_done(self, url: str, task: asyncio.Task[None]) -> None:
        """Forget a finished monitor task, then restart or report it.

        A monitor that stopped itself (auto-pause) gets a fresh one if /resume came
        meanwhile. A crashed one restarts after ``restart_delay`` and the user is told.
        """
        if self._tasks.get(url) is task:
            del self._tasks[url]
            self._monitors.pop(url, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
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
        """Restart the site's monitor after ``restart_delay`` seconds; shutdown cancels it."""
        loop = asyncio.get_running_loop()

        def fire() -> None:
            """Forget the timer and restart the monitor."""
            self._restart_timers.discard(timer)
            self._restart(url)

        timer = loop.call_later(self._restart_delay, fire)
        self._restart_timers.add(timer)

    def _restart(self, url: str) -> None:
        """Start the site's monitor again if it is still monitored, not paused and not running."""
        site = self._sites.get(url)
        if self._closing or site is None or site.paused or self.is_running(url):
            return
        log.info("restarting monitor for %s", url)
        self._start_monitor(site)
