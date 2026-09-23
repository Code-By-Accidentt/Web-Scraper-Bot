"""Composition root: builds every component and runs the bot until interrupted."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx

from scraper_bot import __version__
from scraper_bot.address_extractor import AddressExtractor
from scraper_bot.blocking import BlockDetector
from scraper_bot.change_detector import ChangeDetector
from scraper_bot.commands import BotCommands, TelegramListener
from scraper_bot.config import ConfigError, Settings
from scraper_bot.contract_validators import (
    ContractValidator,
    EvmContractValidator,
    SolanaContractValidator,
)
from scraper_bot.fetcher import BrowserUnavailable, HttpProber, PlaywrightFetcher
from scraper_bot.html_normalizer import HtmlNormalizer
from scraper_bot.models import SiteState
from scraper_bot.monitor_manager import MonitorManager
from scraper_bot.notifier import Notifier
from scraper_bot.page_checker import PageChecker
from scraper_bot.preferences import Preferences
from scraper_bot.site_monitor import AutoPauseCallback, PersistCallback, SiteMonitor
from scraper_bot.status import StatusReporter
from scraper_bot.storage import (
    AddressCache,
    LegacyStateImporter,
    SnapshotStore,
    StateStore,
    atomic_write_text,
)
from scraper_bot.telegram_client import TelegramAuthError, TelegramClient
from scraper_bot.turbo import LimitsSource, TurboManager, TurboSession
from scraper_bot.unverified import UnverifiedWatcher
from scraper_bot.urls import SubUrlDeriver

log = logging.getLogger(__name__)

HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
HEARTBEAT_FILE = "heartbeat"


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


class Application:
    """Wires the components together; :meth:`run` blocks until cancelled."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._prefs = Preferences(settings, settings.data_dir / "preferences.json")

    async def run(self) -> None:
        cfg = self._settings
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self._prefs.load()
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
        async with httpx.AsyncClient(
            headers={"User-Agent": HTTP_USER_AGENT}, timeout=cfg.rpc_timeout, limits=limits
        ) as http:
            telegram = TelegramClient(cfg.telegram_bot_token, http)
            await self._check_token(telegram)
            notifier = Notifier(
                telegram, cfg.chat_ids, max_examples=lambda: self._prefs.max_change_examples
            )
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
                await self._run_with(http, telegram, notifier, fetcher)
            finally:
                await fetcher.stop()

    async def _run_with(
        self,
        http: httpx.AsyncClient,
        telegram: TelegramClient,
        notifier: Notifier,
        fetcher: PlaywrightFetcher,
    ) -> None:
        cfg, prefs = self._settings, self._prefs
        legacy = LegacyStateImporter(Path.cwd())
        cache = self._build_address_cache(legacy)
        validator = ContractValidator(
            EvmContractValidator(http, cfg.evm_networks, timeout=cfg.rpc_timeout, tuning=prefs),
            SolanaContractValidator(http, cfg.solana_rpc_url, timeout=cfg.rpc_timeout),
            cache,
            max_concurrent=cfg.max_concurrent_rpc,
            retry_delay=cfg.rpc_retry_delay,
            notifier=notifier,
        )
        snapshots = SnapshotStore(cfg.data_dir / "snapshots")
        extractor = AddressExtractor()
        block_detector = BlockDetector()
        checker = PageChecker(
            fetcher=fetcher,
            normalizer=HtmlNormalizer(cfg.ignored_attributes),
            detector=ChangeDetector(),
            extractor=extractor,
            validator=validator,
            snapshots=snapshots,
            tuning=prefs,
            block_detector=block_detector,
        )
        prober = HttpProber(http)
        deriver = SubUrlDeriver(cfg.sub_url_patterns)

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
        )
        await manager.start()
        sites = manager.sites()
        await notifier.started(
            __version__, len(sites), sum(1 for site in sites if manager.is_running(site.url))
        )

        status = StatusReporter(
            version=__version__, manager=manager, cache=cache, browser=fetcher, turbo=turbo
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
        listener = TelegramListener(telegram, commands)
        heartbeat = Heartbeat(cfg.data_dir / HEARTBEAT_FILE, cfg.heartbeat_interval)
        tasks = [
            asyncio.create_task(listener.run(), name="telegram-listener"),
            asyncio.create_task(heartbeat.run(), name="heartbeat"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await turbo.shutdown()
            await manager.shutdown()

    @staticmethod
    async def _check_token(telegram: TelegramClient) -> None:
        """Fail fast on a wrong token; a network hiccup only produces a warning."""
        profile = await telegram.get_me()
        if profile is None:
            log.warning("could not verify the Telegram token now; continuing anyway")
        else:
            log.info("Telegram bot @%s (id %s)", profile.get("username"), profile.get("id"))

    def _build_address_cache(self, legacy: LegacyStateImporter) -> AddressCache:
        cfg = self._settings
        known_path = cfg.data_dir / "known_contracts.txt"
        first_run = not known_path.exists()
        cache = AddressCache(
            known_path,
            cfg.data_dir / "rejected_addresses.txt",
            rejected_ttl=lambda: self._prefs.rejected_ttl,
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
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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
    try:
        asyncio.run(Application(settings).run())
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
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
