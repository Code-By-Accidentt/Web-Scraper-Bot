"""Composition root: builds every component and runs the bot until interrupted."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Protocol

import httpx

from scraper_bot import __version__
from scraper_bot.chains.address_extractor import AddressExtractor
from scraper_bot.chains.contract_validators import (
    ContractValidator,
    EvmContractValidator,
    SolanaContractValidator,
    SuiCoinValidator,
)
from scraper_bot.chains.enrichment import TokenEnricher
from scraper_bot.chains.stoplist import StopList
from scraper_bot.chains.warmup import ConnectionWarmer
from scraper_bot.config import ChangedDefault, ConfigError, Settings
from scraper_bot.models import SiteState
from scraper_bot.monitoring.alerts import AlertRegistry
from scraper_bot.monitoring.monitor_manager import MonitorManager
from scraper_bot.monitoring.pending import PendingWatch
from scraper_bot.monitoring.site_monitor import AutoPauseCallback, PersistCallback, SiteMonitor
from scraper_bot.monitoring.turbo import LimitsSource, TurboManager, TurboSession
from scraper_bot.monitoring.unverified import UnverifiedWatcher
from scraper_bot.pages.blocking import BlockDetector
from scraper_bot.pages.change_detector import ChangeDetector
from scraper_bot.pages.fetcher import BrowserUnavailable, HttpProber, PageFetcher, PlaywrightFetcher
from scraper_bot.pages.html_normalizer import HtmlNormalizer
from scraper_bot.pages.page_checker import PageChecker
from scraper_bot.pages.urls import SubUrlDeriver
from scraper_bot.preferences import Preferences
from scraper_bot.storage import (
    AddressCache,
    LegacyStateImporter,
    SnapshotStore,
    StateStore,
    atomic_write_text,
)
from scraper_bot.telegram.client import TelegramAuthError, TelegramClient
from scraper_bot.telegram.commands import BotCommands, TelegramListener
from scraper_bot.telegram.notifier import Notifier
from scraper_bot.telegram.status import BrowserHealth, StatusReporter

log = logging.getLogger(__name__)

HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
HEARTBEAT_FILE = "heartbeat"
PENDING_FILE = "pending.json"  # addresses watched until they become a token
KEEPALIVE = 60.0  # seconds an idle HTTP connection is kept for reuse
IGNORE_FILE = "ignore.txt"  # the operator's own stop-list, one address per line
OUTDATED_NOTICE_FILE = "outdated_defaults.txt"  # the old .env values last reported
SHUTDOWN_STEP = 8.0  # seconds each shutdown step that may talk to Telegram gets
RPC_STATUS_AFTER = 60.0  # /status names an RPC endpoint failing at least this long

Sleep = Callable[[float], Awaitable[None]]


class Browser(PageFetcher, BrowserHealth, Protocol):
    """What the runtime needs from the renderer: page fetching plus a health flag."""


class Heartbeat:
    """Touches a file periodically so an external supervisor can detect a hung process."""

    def __init__(self, path: Path, interval: float) -> None:
        self._path = path
        self._interval = interval

    async def run(self) -> None:
        while True:
            try:
                atomic_write_text(self._path, f"{time.time():.0f}\n")
            except OSError as exc:
                log.warning("heartbeat write failed: %s", exc)
            await asyncio.sleep(self._interval)


@dataclass(slots=True)
class Runtime:
    """The wired bot. :class:`Application` serves it; tests drive its parts directly."""

    manager: MonitorManager
    turbo: TurboManager
    commands: BotCommands
    listener: TelegramListener
    heartbeat: Heartbeat
    notifier: Notifier
    validator: ContractValidator
    warmer: ConnectionWarmer
    pending: PendingWatch
    _pending_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        self.pending.load()
        await self.manager.start()
        self._pending_task = asyncio.create_task(self.pending.run(), name="pending-watch")
        sites = self.manager.sites()
        running = sum(1 for site in sites if self.manager.is_running(site.url))
        await self.notifier.started(__version__, len(sites), running)

    async def serve(self) -> None:
        """Start the monitors, then poll Telegram, write heartbeats and keep RPC warm.

        Whatever stops it, even a SIGTERM during the start, ends in :meth:`shutdown`.
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
        """Stop everything within the supervisor's grace period (``STOP_GRACE``, rebooter.py).

        The pending watch is saved first, so a slow Telegram cannot cost it; every step
        that talks to Telegram gets a bounded time.
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
        await self.notifier.drain()  # alerts already on their way still go out
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
    """Wire monitoring, turbo and commands around the given adapters.

    ``sleep`` replaces the pauses between monitor rounds and turbo cycles (tests).
    ``probe_http`` serves the sub-page probes (default: ``http``); in production it is a
    plain HTTP/1.1 client, so an HTTP/2 glitch never makes a live page look absent.
    """
    cfg = settings
    legacy = LegacyStateImporter(legacy_dir)
    cache = _load_address_cache(cfg, prefs, legacy)
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
        notifier=notifier,
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
    registry = AlertRegistry()  # one token, one alert: shared by every site and mode
    # Addresses that may still become a token; the manager (built below) says which
    # sites are paused.
    pending = PendingWatch(
        http,
        cfg,
        validator=validator,
        registry=registry,
        notifier=notifier,
        path=cfg.data_dir / PENDING_FILE,
        turbo_interval=lambda: prefs.turbo_recheck_interval,
        is_paused=lambda url: manager.is_paused(url),
        sleep=sleep or asyncio.sleep,
    )

    def build_monitor(
        site: SiteState, persist: PersistCallback, on_auto_pause: AutoPauseCallback
    ) -> SiteMonitor:
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
        warmer=ConnectionWarmer(http, cfg),
        pending=pending,
    )


def _load_address_cache(
    settings: Settings, prefs: Preferences, legacy: LegacyStateImporter
) -> AddressCache:
    known_path = settings.data_dir / "known_contracts.txt"
    first_run = not known_path.exists()
    cache = AddressCache(
        known_path,
        settings.data_dir / "rejected_addresses.txt",
        rejected_ttl=lambda: prefs.rejected_ttl,
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


class Application:
    """Owns the process-level resources (HTTP client, browser) and serves a :class:`Runtime`."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._prefs = Preferences(settings, settings.data_dir / "preferences.json")

    async def run(self) -> None:
        cfg = self._settings
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self._prefs.load()
        # httpx drops idle connections after 5 s by default. With KEEPALIVE the warm-up calls
        # every 30 s keep the RPC connections open between alerts. Telegram's long poll keeps
        # its own connection open, and over HTTP/2 the alerts share it.
        limits = httpx.Limits(
            max_connections=64, max_keepalive_connections=32, keepalive_expiry=KEEPALIVE
        )
        headers = {"User-Agent": HTTP_USER_AGENT}
        async with (
            httpx.AsyncClient(
                headers=headers, timeout=cfg.rpc_timeout, limits=limits, http2=_http2(cfg)
            ) as http,
            httpx.AsyncClient(headers=headers, timeout=cfg.rpc_timeout, limits=limits) as probes,
        ):
            telegram = TelegramClient(cfg.telegram_bot_token, http)
            await self._check_token(telegram)
            enricher = TokenEnricher(
                http,
                cfg.evm_networks,
                cfg.solana_rpc_url,
                timeout=cfg.rpc_timeout,
                tuning=self._prefs,
                sui_rpc_url=cfg.sui_rpc_url,
            )
            notifier = Notifier(
                telegram,
                cfg.chat_ids,
                max_examples=lambda: self._prefs.max_change_examples,
                enricher=enricher,
            )
            notice = cfg.data_dir / OUTDATED_NOTICE_FILE
            if outdated_notice_due(
                notice, cfg.outdated_defaults
            ) and await notifier.outdated_defaults(cfg.outdated_defaults):
                remember_outdated_notice(notice, cfg.outdated_defaults)
            fetcher = PlaywrightFetcher(cfg, tuning=self._prefs)
            try:
                await fetcher.start()
            except BrowserUnavailable as exc:
                await notifier.problem(
                    "browser",
                    "Browser failed to start; the bot stopped",
                    str(exc),
                    hint="Fix the browser installation and start the bot again.",
                )
                raise
            try:
                runtime = build_runtime(
                    cfg,
                    self._prefs,
                    http=http,
                    telegram=telegram,
                    notifier=notifier,
                    fetcher=fetcher,
                    legacy_dir=Path.cwd(),
                    probe_http=probes,
                )
                await runtime.serve()
            finally:
                await fetcher.stop()

    @staticmethod
    async def _check_token(telegram: TelegramClient) -> None:
        """Fail fast on a wrong token; a network hiccup only produces a warning."""
        profile = await telegram.get_me()
        if profile is None:
            log.warning("could not verify the Telegram token now; continuing anyway")
        else:
            log.info("Telegram bot @%s (id %s)", profile.get("username"), profile.get("id"))


def _http2(settings: Settings) -> bool:
    """HTTP/2 when configured and h2 is installed (an update may skip ``pip install``)."""
    if not settings.http2:
        return False
    try:
        import h2  # noqa: F401  (httpx needs it for http2=True)
    except ImportError:
        log.warning(
            "HTTP2=true but the h2 package is missing (pip install -r requirements.txt); "
            "using HTTP/1.1"
        )
        return False
    return True


def outdated_notice_due(path: Path, changes: Sequence[ChangedDefault]) -> bool:
    """True when ``changes`` differ from the set last reported in Telegram.

    The log warns on every start; the chat hears about one set of old values once, not on
    every restart the supervisor makes (see :func:`remember_outdated_notice`).
    """
    if not changes:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return False
    try:
        return path.read_text(encoding="utf-8") != _listed(changes)
    except (OSError, ValueError):  # missing, or re-saved in another encoding
        return True


def remember_outdated_notice(path: Path, changes: Sequence[ChangedDefault]) -> None:
    """Note ``changes`` as reported, once the message went out."""
    with contextlib.suppress(OSError):
        atomic_write_text(path, _listed(changes))


def _listed(changes: Sequence[ChangedDefault]) -> str:
    return "\n".join(sorted(f"{c.key}={c.previous}" for c in changes)) + "\n"


def configure_logging(settings: Settings) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    handlers: list[logging.Handler] = []
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")
    console = logging.StreamHandler(stream)
    console.setFormatter(formatter)
    handlers.append(console)
    if settings.log_file is not None:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    logging.basicConfig(level=settings.log_level, handlers=handlers, force=True)
    # hpack logs every HTTP/2 header at DEBUG, the request path with the bot token included
    for noisy in ("httpx", "httpcore", "asyncio", "hpack", "h2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _serve(app: Application) -> None:
    """Run the bot; SIGTERM (systemd, rebooter.py) shuts it down the way Ctrl-C does.

    The shutdown sends the alerts already on their way. A second SIGTERM kills at once.
    """
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()

    def stop() -> None:
        loop.remove_signal_handler(signal.SIGTERM)
        if task is not None:
            task.cancel()

    with contextlib.suppress(NotImplementedError):  # Windows: terminate() kills at once
        loop.add_signal_handler(signal.SIGTERM, stop)
    await app.run()


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings)
    log.info("scraper-bot %s starting (data dir: %s)", __version__, settings.data_dir.resolve())
    if not settings.chat_ids:
        log.warning(
            "TELEGRAM_CHAT_IDS is empty: alerts will be dropped. "
            "Send /start to the bot to learn your chat id and put it into .env"
        )
    for change in settings.outdated_defaults:
        log.warning(
            "%s=%s is the default of an earlier release; the default is now %s. "
            "Delete the line from .env to use it, or keep it on purpose.",
            change.key,
            change.previous,
            change.current,
        )
    try:
        asyncio.run(_serve(Application(settings)))
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
    except asyncio.CancelledError:
        log.info("stopped by SIGTERM")
    except TelegramAuthError as exc:
        log.error("%s", exc)
        return 2
    except BrowserUnavailable as exc:
        log.error("%s", exc)
        return 3
    except Exception:
        log.exception("fatal error; the supervisor (rebooter.py) restarts the bot")
        return 1
    return 0
