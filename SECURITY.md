# Security

This document describes what the bot touches, what it deliberately never does, how to
run it safely and how to report a problem.

## Supported versions

Only the `main` branch (version 2.x) receives fixes. The version 1 scripts are gone;
their data files are imported once on the first start (see the README) and not
supported beyond that.

## Reporting a vulnerability

Report security problems privately to the maintainer, through the repository's
Security tab on GitHub or a direct message, not in a public issue. Please include what
you found, how to reproduce it and what an attacker could gain. You will get an answer,
and a fix lands on `main` before anything is discussed publicly.

## What the bot handles

| Asset | Where it lives | Protection |
| --- | --- | --- |
| Telegram bot token | `.env` only (`TELEGRAM_BOT_TOKEN`) | Never logged: every error message that could contain the API URL is redacted to `<token>`. `.env` is git-ignored. |
| Chat ids | `.env` (`TELEGRAM_CHAT_IDS`, `TELEGRAM_ALLOWED_CHAT_IDS`) | Alerts go only to `TELEGRAM_CHAT_IDS`. |
| Monitored URLs, page snapshots, found and rejected addresses, live settings | `data/` | Plain files, git-ignored. They hold page text of the sites you watch and every address the bot saw. Back them up and protect them like any other data. |
| Logs | console or `LOG_FILE` | Contain URLs and addresses, never the token. |

The bot only reads: it renders pages, calls read-only JSON-RPC methods (`eth_getCode`,
`eth_call`, `getAccountInfo`, `sui_getObject`, `suix_getCoinMetadata`, ...) and reads
DexScreener. It holds no keys, signs nothing and never sends a transaction.

## Who can control the bot

- With `TELEGRAM_ALLOWED_CHAT_IDS` empty (the default) **every Telegram chat that finds
  the bot can run commands**: add or remove sites, start turbo sessions, read `/status`.
  Set `TELEGRAM_ALLOWED_CHAT_IDS` to your own chat ids on any bot that is reachable by
  strangers.
- Settings (`/set`, `/settings reset`) and the unverified mode can only be changed from
  `TELEGRAM_ALLOWED_CHAT_IDS`, or from `TELEGRAM_CHAT_IDS` when the allowlist is empty.
  Everyone else can read the settings.
- Commands from chats outside the allowlist are ignored and logged.

## Rendering untrusted pages

Monitored sites are untrusted input rendered by a headless Chromium under Playwright.
Keep these points in mind:

- `/add` accepts any `http(s)` URL, including `localhost`, `127.0.0.1` and private
  ranges such as `10.x`, `192.168.x` and `169.254.x`. On a bot with open commands this
  lets anyone make your browser fetch hosts inside your network and read what a change
  alert or a contract alert reveals about them. This is a deliberate trade-off (monitoring
  local staging sites is a supported use), so the protection is the chat allowlist
  above. Do not run a bot with open commands on a machine that can reach anything
  sensitive.
- The browser is started with `ignore_https_errors=True`, so a monitored site with a
  broken or intercepted certificate still renders. The bot only reads pages, but the
  content it reports could be tampered with on the way.
- Images, media and fonts are not downloaded; scripts run. Chromium's own sandbox
  applies. Keep Playwright and the bundled browser current (`pip install -U playwright`
  followed by `playwright install chromium`).
- Everything the bot sends to Telegram is HTML-escaped (`scraper_bot/telegram/markup.py`),
  so page text and URLs cannot inject markup into your chat. If Telegram still rejects a
  message, the same text is re-sent as plain text.

## Third parties that see your data

- The public RPC endpoints in `.env` see every address the bot checks and your IP. Use
  your own endpoints if that matters.
- DexScreener sees every confirmed contract address (token details).
- The monitored sites see a headless Chrome with a normal desktop user agent coming from
  your IP, at the pace set by `POLL_INTERVAL_MIN` / `POLL_INTERVAL_MAX`.

## What the bot never does

- No captcha solving, no proxy or IP rotation, no fingerprint spoofing. When a site
  answers 401/403/429/503, shows a challenge page or resets connections, the bot slows
  down (the interval doubles up to `BLOCK_MAX_BACKOFF`), tells you once, and resumes the
  normal pace when the root page answers again.
- No writes to any blockchain, no wallets, no private keys.
- No outbound traffic other than Telegram, the configured RPC endpoints, DexScreener and
  the sites you asked it to watch.

## Operating safely

- Run the bot under a dedicated user with access only to its own directory; `.env`
  should be readable by that user alone.
- Rotate the token in @BotFather (`/revoke`) if it ever leaks; the bot fails fast with
  exit code 2 on a rejected token.
- Keep dependencies current: `pip list --outdated`, then `pip install -U -r
  requirements.txt` and `playwright install chromium`. Run `pytest -q` afterwards.
- Watch the 🛑 alerts: they report crashed monitors, failing RPC endpoints and a
  browser that could not start, at most once per ten minutes per problem.
