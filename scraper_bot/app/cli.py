"""Command-line entry point: settings, logging and a clean shutdown on SIGTERM."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler

from scraper_bot import __version__
from scraper_bot.app.application import Application
from scraper_bot.pages.fetching.result import BrowserUnavailable
from scraper_bot.settings.env import ConfigError, Settings
from scraper_bot.telegram.client import TelegramAuthError

log = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    """Send the log to stdout and, when LOG_FILE is set, to a rotating file as well.

    Chatty libraries are limited to warnings. Raises ConfigError when LOG_FILE can't be
    written.
    """
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    handlers: list[logging.Handler] = []
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")
    console = logging.StreamHandler(stream)
    console.setFormatter(formatter)
    handlers.append(console)
    if settings.log_file is not None:
        try:
            settings.log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                settings.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            )
        except OSError as exc:
            raise ConfigError(f"LOG_FILE {settings.log_file} cannot be written: {exc}") from exc
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    logging.basicConfig(level=settings.log_level, handlers=handlers, force=True)
    # hpack logs every HTTP/2 header at DEBUG, and the request path holds the bot token.
    for noisy in ("httpx", "httpcore", "asyncio", "hpack", "h2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _serve(app: Application) -> None:
    """Run the bot; a SIGTERM (systemd, rebooter.py) shuts it down the way Ctrl-C does.

    The shutdown still sends the alerts already on their way. A second SIGTERM kills the
    process at once.
    """
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()

    def stop() -> None:
        """Cancel the bot's task and leave the next SIGTERM to its default action."""
        loop.remove_signal_handler(signal.SIGTERM)
        if task is not None:
            task.cancel()

    with contextlib.suppress(NotImplementedError):  # Windows: terminate() kills at once
        loop.add_signal_handler(signal.SIGTERM, stop)
    await app.run()


def main() -> int:
    """Run the bot and return the exit code for the supervisor.

    0 is a normal stop, 1 a crash, 2 a configuration error or a rejected token, and 3 a
    browser that could not start; rebooter.py waits longer after 2 and 3.
    """
    try:
        settings = Settings.from_env()
        configure_logging(settings)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
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
