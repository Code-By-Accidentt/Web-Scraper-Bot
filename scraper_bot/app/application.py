"""The running process: shared HTTP clients, the browser and the bot built on top of them."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from scraper_bot.app.notices import (
    OUTDATED_NOTICE_FILE,
    SANDBOX_HINT,
    SANDBOX_NOTICE_FILE,
    outdated_notice_due,
    remember_outdated_notice,
    tell_about_sandbox,
)
from scraper_bot.app.runtime import build_runtime
from scraper_bot.chains.enrichment import TokenEnricher
from scraper_bot.pages.fetching.chromium import PlaywrightFetcher
from scraper_bot.pages.fetching.result import BrowserUnavailable
from scraper_bot.settings.env import Settings
from scraper_bot.settings.live import Preferences
from scraper_bot.telegram.client import TelegramClient
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)


HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
KEEPALIVE = 60.0  # seconds an idle HTTP connection is kept for reuse


class Application:
    """Owns the process-wide resources (HTTP clients, browser) and serves a :class:`Runtime`."""

    def __init__(self, settings: Settings) -> None:
        """Keep the settings; the /set preferences are read later, in :meth:`run`."""
        self._settings = settings
        self._prefs = Preferences(settings, settings.data_dir / "preferences.json")

    async def run(self) -> None:
        """Open the HTTP clients and the browser, serve the bot, and close them when it stops.

        Raises TelegramAuthError for a rejected token, and BrowserUnavailable (after telling
        the chat) when the browser cannot start.
        """
        cfg = self._settings
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self._prefs.load()
        # httpx drops idle connections after 5 s; KEEPALIVE lets the 30 s warm-up keep RPC open.
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
            fetcher.on_sandbox_lost = lambda error: notifier.report_later(
                "sandbox", "Chromium runs without its sandbox", error, hint=SANDBOX_HINT
            )
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
            await tell_about_sandbox(notifier, fetcher, cfg.data_dir / SANDBOX_NOTICE_FILE)
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
        """Stop at once on a rejected token; if Telegram cannot be reached, only warn."""
        profile = await telegram.get_me()
        if profile is None:
            log.warning("could not verify the Telegram token now; continuing anyway")
        else:
            log.info("Telegram bot @%s (id %s)", profile.get("username"), profile.get("id"))


def _http2(settings: Settings) -> bool:
    """Return True when HTTP2 is on and the h2 package is installed.

    An update may skip ``pip install``, so a missing h2 falls back to HTTP/1.1 with a warning.
    """
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
