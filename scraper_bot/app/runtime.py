"""Wires every part of the bot together and runs it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from scraper_bot import __version__
from scraper_bot.app.heartbeat import Heartbeat
from scraper_bot.chains.addresses.extractor import AddressExtractor
from scraper_bot.chains.addresses.stoplist import StopList
from scraper_bot.chains.evm.validator import EvmContractValidator
from scraper_bot.chains.rpc.warmup import ConnectionWarmer
from scraper_bot.chains.solana.validator import SolanaContractValidator
from scraper_bot.chains.sui.validator import SuiCoinValidator
from scraper_bot.chains.validator import ContractValidator
from scraper_bot.models.addresses import Chain
from scraper_bot.models.sites import SiteState
from scraper_bot.monitoring.alerts import AlertRegistry
from scraper_bot.monitoring.monitor_manager import MonitorManager
from scraper_bot.monitoring.pending.watch import PendingWatch
from scraper_bot.monitoring.site_monitor import AutoPauseCallback, PersistCallback, SiteMonitor
from scraper_bot.monitoring.turbo.manager import TurboManager
from scraper_bot.monitoring.turbo.session import LimitsSource, TurboSession
from scraper_bot.monitoring.unverified import UnverifiedWatcher
from scraper_bot.pages.changes.detector import ChangeDetector
from scraper_bot.pages.changes.normalizer import HtmlNormalizer
from scraper_bot.pages.fetching.blocking import BlockDetector
from scraper_bot.pages.fetching.prober import HttpProber
from scraper_bot.pages.fetching.result import PageFetcher
from scraper_bot.pages.page_checker import PageChecker
from scraper_bot.pages.urls import SubUrlDeriver
from scraper_bot.settings.env import Settings
from scraper_bot.settings.live import Preferences
from scraper_bot.storage.address_cache import AddressCache
from scraper_bot.storage.files import atomic_write_text
from scraper_bot.storage.legacy import LegacyStateImporter
from scraper_bot.storage.outbox import Outbox
from scraper_bot.storage.snapshots import SnapshotStore
from scraper_bot.storage.state import StateStore
from scraper_bot.telegram.client import TelegramClient
from scraper_bot.telegram.commands.handlers import BotCommands
from scraper_bot.telegram.commands.listener import TelegramListener
from scraper_bot.telegram.notifier import Notifier
from scraper_bot.telegram.views.status import BrowserHealth, StatusReporter

log = logging.getLogger(__name__)


HEARTBEAT_FILE = "heartbeat"
PENDING_FILE = "pending.json"  # addresses watched until they become a token
OUTBOX_FILE = "outbox.json"  # token alerts and documents on their way to Telegram
IGNORE_FILE = "ignore.txt"  # the operator's own stop-list, one address per line
NETWORKS_FILE = "evm_networks.txt"  # the EVM networks the caches were built with
SHUTDOWN_STEP = 8.0  # seconds for each shutdown step that may talk to Telegram
RPC_STATUS_AFTER = 60.0  # seconds an RPC endpoint must fail before /status names it

Sleep = Callable[[float], Awaitable[None]]


class Browser(PageFetcher, BrowserHealth, Protocol):
    """What the runtime needs from the renderer: page fetching plus a health flag."""


@dataclass(slots=True)
class Runtime:
    """The wired bot, ready to serve.

    :class:`Application` serves it; tests drive its parts directly.
    """

    manager: MonitorManager
    turbo: TurboManager
    commands: BotCommands
    listener: TelegramListener
    heartbeat: Heartbeat
    notifier: Notifier
    validator: ContractValidator
    warmer: ConnectionWarmer
    pending: PendingWatch
    # runs once the start went through: saves the EVM networks the caches now match
    started_hook: Callable[[], None] | None = None
    _pending_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        """Start the monitors and the pending watch, then tell the chat the bot is up.

        Alerts and documents the last run could not deliver go out right after that.
        """
        self.pending.load()
        await self.manager.start()
        self._pending_task = asyncio.create_task(self.pending.run(), name="pending-watch")
        self._pending_task.add_done_callback(_log_crash)
        if self.started_hook is not None:
            self.started_hook()
        sites = self.manager.sites()
        running = sum(1 for site in sites if self.manager.is_running(site.url))
        await self.notifier.started(__version__, len(sites), running)
        self.notifier.resend_unsent()

    async def serve(self) -> None:
        """Start the monitors, then poll Telegram, write heartbeats and keep RPC warm.

        An RPC endpoint that serves the wrong chain is reported first. Whatever stops the
        bot, even a SIGTERM during the start, ends in :meth:`shutdown`.
        """
        tasks: list[asyncio.Task[None]] = []
        try:
            await self.start()
            wrong = await self.warmer.wrong_chains()
            if wrong:
                await self.notifier.problem(
                    "chain-id",
                    "An RPC endpoint serves another chain",
                    "\n".join(wrong),
                    hint="Fix the URL in .env and restart; addresses are checked on the wrong "
                    "chain.",
                )
            tasks = [
                asyncio.create_task(self.listener.run(), name="telegram-listener"),
                asyncio.create_task(self.heartbeat.run(), name="heartbeat"),
                asyncio.create_task(self.warmer.run(), name="rpc-warm-up"),
            ]
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.shutdown()

    async def shutdown(self) -> None:
        """Stop everything within the supervisor's grace period (``STOP_GRACE`` in rebooter.py).

        The pending watch is saved first, so a slow Telegram cannot lose it. Each step that
        talks to Telegram gets ``SHUTDOWN_STEP`` seconds; alerts already on their way still go
        out, and a token alert that does not make it in time is sent by the next start.
        """
        self.pending.save()
        for name, step in (
            ("turbo sessions", self.turbo.shutdown),
            ("monitors", self.manager.shutdown),
        ):
            try:
                async with asyncio.timeout(SHUTDOWN_STEP):
                    await step()
            except TimeoutError:
                log.warning("stopping the %s took over %.0fs; moving on", name, SHUTDOWN_STEP)
        if self._pending_task is not None:
            self._pending_task.cancel()
            await asyncio.gather(self._pending_task, return_exceptions=True)
            self._pending_task = None
        self.pending.save()
        await self.notifier.drain()
        await self.validator.aclose()


def build_runtime(
    settings: Settings,
    prefs: Preferences,
    *,
    http: httpx.AsyncClient,
    telegram: TelegramClient,
    notifier: Notifier,
    fetcher: Browser,
    legacy_dir: Path,
    sleep: Sleep | None = None,
    probe_http: httpx.AsyncClient | None = None,
) -> Runtime:
    """Build the whole bot around the given clients and browser, and return it unstarted.

    Tests pass ``sleep`` to replace the pauses between monitor rounds and turbo cycles.
    ``probe_http`` (default ``http``) checks which sub-pages exist; production gives it a
    plain HTTP/1.1 client, so an HTTP/2 glitch never makes a live page look absent. The
    notifier gets its outbox in the data directory.
    """
    cfg = settings
    notifier.outbox = Outbox(cfg.data_dir / OUTBOX_FILE)
    legacy = LegacyStateImporter(legacy_dir)
    networks_file = cfg.data_dir / NETWORKS_FILE
    networks_known = networks_file.is_file()
    current = tuple(net.name for net in cfg.evm_networks)
    added = added_networks(networks_file, current, existing_data=_has_data(cfg))
    cache = _load_address_cache(cfg, prefs, legacy, notifier.report_later)
    if added:
        # A rejection made without these networks may be a token on one of them.
        forgotten = cache.forget_rejected(Chain.ETHEREUM)
        log.info(
            "EVM network(s) added: %s; %d cached EVM rejection(s) forgotten",
            ", ".join(sorted(added)),
            forgotten,
        )
    stoplist = StopList.load(cfg.data_dir / IGNORE_FILE)
    validator = ContractValidator(
        EvmContractValidator(
            http, cfg.evm_networks, timeout=cfg.rpc_timeout, tuning=prefs, stoplist=stoplist
        ),
        SolanaContractValidator(http, cfg.solana_rpc_url, timeout=cfg.rpc_timeout),
        cache,
        sui=SuiCoinValidator(http, cfg.sui_rpc_url, timeout=cfg.rpc_timeout, tuning=prefs),
        max_concurrent=cfg.max_concurrent_rpc,
        retry_delay=cfg.rpc_retry_delay,
        on_stuck=notifier.rpc_endpoints_stuck,
    )
    snapshots = SnapshotStore(cfg.data_dir / "snapshots")
    extractor = AddressExtractor(stoplist=stoplist)
    block_detector = BlockDetector()
    normalizer = HtmlNormalizer(cfg.ignored_attributes)
    checker = PageChecker(
        fetcher=fetcher,
        normalizer=normalizer,
        detector=ChangeDetector(),
        extractor=extractor,
        validator=validator,
        snapshots=snapshots,
        tuning=prefs,
        block_detector=block_detector,
    )
    prober = HttpProber(probe_http or http)
    deriver = SubUrlDeriver(cfg.sub_url_patterns)
    # One registry for every site and mode, so each token alerts only once.
    registry = AlertRegistry(cache)
    pending = PendingWatch(
        http,
        cfg,
        validator=validator,
        registry=registry,
        notifier=notifier,
        path=cfg.data_dir / PENDING_FILE,
        turbo_interval=lambda: prefs.turbo_recheck_interval,
        # manager is built below. A turbo-only site is never paused: its launch may come later.
        is_paused=lambda url: manager.is_paused(url),
        sleep=sleep or asyncio.sleep,
        health=validator.record,
        new_networks=added,
        # A re-enabled network's old absences are stale. With no networks file yet every
        # network counts as added, but the absences an earlier release recorded are real.
        stale_absences=added if networks_known else frozenset(),
    )
    # A token that Arc or Robinhood Chain reports too late is confirmed at the next tick.
    validator.on_late_token = pending.hurry

    def build_monitor(
        site: SiteState, persist: PersistCallback, on_auto_pause: AutoPauseCallback
    ) -> SiteMonitor:
        """Build the monitor of one site, with its own unverified-mode watcher."""
        watcher = UnverifiedWatcher(
            site,
            validator=validator,
            cache=cache,
            notifier=notifier,
            persist=persist,
            tuning=prefs,
            registry=registry,
        )
        return SiteMonitor(
            site,
            checker=checker,
            prober=prober,
            deriver=deriver,
            notifier=notifier,
            persist=persist,
            on_auto_pause=on_auto_pause,
            tuning=prefs,
            watcher=watcher,
            registry=registry,
            pending=pending,
            sleep=sleep,
            page_load_timeout=cfg.page_load_timeout,
        )

    def build_turbo(
        url: str, pages: list[str], duration: float, limits: LimitsSource
    ) -> TurboSession:
        """Build a turbo session that renders ``pages`` of ``url`` for ``duration`` seconds."""
        return TurboSession(
            url,
            pages,
            fetcher=fetcher,
            extractor=extractor,
            validator=validator,
            cache=cache,
            notifier=notifier,
            block_detector=block_detector,
            limits=limits,
            duration=duration,
            pending=pending,
            registry=registry,
            normalizer=normalizer,
            sleep=sleep,
        )

    turbo = TurboManager(
        tuning=prefs,
        deriver=deriver,
        prober=prober,
        session_factory=build_turbo,
        notifier=notifier,
    )
    manager = MonitorManager(
        state_store=StateStore(cfg.data_dir / "state.json"),
        snapshot_store=snapshots,
        deriver=deriver,
        monitor_factory=build_monitor,
        legacy_importer=legacy,
        notifier=notifier,
        on_removed=pending.forget_site,
    )
    status = StatusReporter(
        version=__version__,
        manager=manager,
        cache=cache,
        browser=fetcher,
        turbo=turbo,
        pending=pending,
        rpc_health=lambda: validator.failing_endpoints(RPC_STATUS_AFTER),
    )
    commands = BotCommands(
        manager,
        telegram,
        allowed_chat_ids=cfg.allowed_chat_ids,
        alert_chat_ids=cfg.chat_ids,
        status_provider=status.render,
        turbo=turbo,
        preferences=prefs,
    )
    return Runtime(
        manager=manager,
        turbo=turbo,
        commands=commands,
        listener=TelegramListener(telegram, commands),
        heartbeat=Heartbeat(cfg.data_dir / HEARTBEAT_FILE, cfg.heartbeat_interval),
        notifier=notifier,
        validator=validator,
        warmer=ConnectionWarmer(http, cfg, record=validator.record),
        pending=pending,
        started_hook=lambda: remember_networks(networks_file, current),
    )


def added_networks(path: Path, current: Sequence[str], *, existing_data: bool) -> frozenset[str]:
    """Return the EVM networks the saved verdicts were not made with.

    Without the file (the first start of this version) every network counts as new when an
    earlier run left data, and none on a fresh install. An unreadable file makes all new.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset(current) if existing_data else frozenset()
    except (OSError, ValueError):
        return frozenset(current)
    previous = {line.strip() for line in text.splitlines() if line.strip()}
    return frozenset(current) - previous


def remember_networks(path: Path, current: Sequence[str]) -> None:
    """Save the names of the current EVM networks to ``path``; a failed write is ignored."""
    with contextlib.suppress(OSError):
        atomic_write_text(path, "".join(f"{name}\n" for name in sorted(current)))


def _has_data(settings: Settings) -> bool:
    """Return True when an earlier run left address data in the data directory."""
    data = settings.data_dir
    return any(
        (data / name).is_file()
        for name in ("known_contracts.txt", "rejected_addresses.txt", PENDING_FILE)
    )


def _log_crash(task: asyncio.Task[None]) -> None:
    """Log the error of a background task that crashed; such loops should never end."""
    if not task.cancelled() and task.exception() is not None:
        log.error("%s stopped: %r", task.get_name(), task.exception(), exc_info=task.exception())


def _load_address_cache(
    settings: Settings,
    prefs: Preferences,
    legacy: LegacyStateImporter,
    report: Callable[[str, str, str, str], None] | None = None,
) -> AddressCache:
    """Load the address cache from the data directory and return it.

    On the very first run the known contracts of the old bot (found_wallets.txt) are imported.
    ``report`` hears about cache files that cannot be read or saved.
    """
    known_path = settings.data_dir / "known_contracts.txt"
    first_run = not known_path.exists()
    cache = AddressCache(
        known_path,
        settings.data_dir / "rejected_addresses.txt",
        rejected_ttl=lambda: prefs.rejected_ttl,
        report=report,
    )
    cache.load()
    if first_run and legacy.has_data():
        imported = cache.import_known(legacy.known_addresses())
        if imported:
            log.info("imported %d known contract(s) from found_wallets.txt", imported)
    log.info(
        "address cache: %d known contract(s), %d rejected address(es)",
        cache.known_count,
        cache.rejected_count,
    )
    return cache
