"""Entry point kept for backwards compatibility: ``python main.py``."""

from scraper_bot.app import main

if __name__ == "__main__":
    raise SystemExit(main())
