# Security

This document describes what the bot touches, what it deliberately never does, how to
run it safely and how to report a problem.

## Supported versions

Only the `main` branch (version 2.x) receives fixes. The version 1 scripts are gone;
their data files are imported once on the first start (see docs/operations.md,
Upgrading) and not
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
| Monitored URLs, page snapshots, found and rejected addresses, unsent alerts, live settings | `data/` | Plain files, git-ignored. They hold page text of the sites you watch and every address the bot saw. Back them up and protect them like any other data. |
| The browser's cookies and local storage | `data/browser_state.json` (with `HIDE_AUTOMATION`, the default) | Only what the monitored sites set for an anonymous visitor (the bot logs in nowhere), such as a passed Cloudflare or Vercel check. Anyone who reads the file can present those cookies to the sites: keep it private like the rest of `data/`. |
| Logs | console or `LOG_FILE` | Contain URLs and addresses, never the token. |

The bot only reads: it renders pages, calls read-only JSON-RPC methods (`eth_getCode`,
`eth_call`, `getMultipleAccounts`, `sui_getObject`, `suix_getCoinMetadata`, ...) and reads
DexScreener. It holds no keys, signs nothing and never sends a transaction.

## Who can control the bot

- Commands are accepted from the chats in `TELEGRAM_ALLOWED_CHAT_IDS` or, when that list
  is empty (the default), from the alert chats in `TELEGRAM_CHAT_IDS`. No other chat can
  add or remove sites, start turbo sessions, read `/status` or change settings.
- Any other chat that sends `/start` only learns its own chat id (at most once per ten
  minutes per chat), so you can find the id to put into `.env`. Its other commands are
  ignored and logged.
- With both lists empty nobody can control the bot; only `/start` answers.
- Commands are read from messages only: posts in a channel are ignored, so a channel
  cannot command the bot. In an allowed group every member can run every command. A
  command addressed to another bot (`/pause@OtherBot`) is ignored.

## Rendering untrusted pages

Monitored sites are untrusted input rendered by a headless Chromium under Playwright.
Keep these points in mind:

- `/add` accepts any `http(s)` URL, including `localhost`, `127.0.0.1` and private
  ranges such as `10.x`, `192.168.x` and `169.254.x`. On a bot with open commands this
  lets anyone who can command the bot make your browser fetch hosts inside your network
  and read what a change alert or a contract alert reveals about them. This is a
  deliberate trade-off (monitoring local staging sites is a supported use), so the
  protection is the list of commanding chats above.
- The browser is started with `ignore_https_errors=True`, so a monitored site with a
  broken or intercepted certificate still renders. The bot only reads pages, but the
  content it reports could be tampered with on the way.
- Images, media and fonts are not downloaded, nor are Next.js prefetches and analytics
  beacons; scripts run. Downloads are refused, and a tab that a page opens itself (a
  popup, a link to a new window) is closed at once. WebGL is off by default
  (`BROWSER_ARGS`). Chromium runs with its own sandbox. Where the sandbox cannot start (as
  root, or without user namespaces, as in containers) the bot runs the browser without it
  and sends one 🛑; the dedicated user is then the only isolation (`--no-sandbox` in
  `BROWSER_ARGS` does the same on purpose).
  Keep Playwright and the bundled browser current: raise the `playwright` pin in
  `requirements.txt`, `pip install -r requirements.txt`, then `playwright install chromium`.
- Everything the bot sends to Telegram is HTML-escaped (`scraper_bot/telegram/markup.py`),
  so page text and URLs cannot inject markup into your chat. If Telegram still rejects a
  message, the same text is re-sent as plain text.

## Third parties that see your data

- The public RPC endpoints in `.env` see every address the bot checks and your IP. Use
  your own endpoints if that matters.
- DexScreener sees every confirmed contract address (token details).
- The monitored sites see Chrome coming from your IP, at the pace set by
  `POLL_INTERVAL_MIN` / `POLL_INTERVAL_MAX`, plus, per derived sub-page and round, a
  HEAD request (a GET when it answers 404/405/410/501; redirects followed): the probes
  that find which sub-pages exist; while a site blocks, one GET of its root page per
  round. With `HIDE_AUTOMATION` (the default) the browser and these requests send the
  browser's own User-Agent without "Headless" and `navigator.webdriver` is false; with
  `false` the browser says it is headless and the probes send a fixed desktop Chrome
  user agent. Their analytics services see nothing: the bot drops the beacons.

## What the bot never does

- No captcha solving, no proxy or IP rotation, no tampering with a site's check scripts.
  `HIDE_AUTOMATION` only drops the marks of automation (the "Headless" in the User-Agent,
  `navigator.webdriver`) and keeps cookies; the browser never poses as another browser,
  device or place. When a site answers 401/403/407/429/503, shows a challenge page or
  resets connections, the bot slows down (the interval doubles up to `BLOCK_MAX_BACKOFF`;
  a longer `Retry-After` from the site is honoured; while the site still refuses one
  light request, nothing is rendered), tells you once, naming the protection when it can,
  and resumes the normal pace after a round without block signals in which the root page
  answered.
- No writes to any blockchain, no wallets, no private keys.
- No outbound traffic other than Telegram, the configured RPC endpoints, DexScreener and
  the sites you asked it to watch, including the third-party hosts their pages load
  (scripts, APIs, CDNs); analytics beacons are dropped.

## Operating safely

- Run the bot under a dedicated user with access only to its own directory; `.env`
  should be readable by that user alone (`chmod 600 .env`).
- Rotate the token in @BotFather (`/revoke`) if it ever leaks; the bot fails fast with
  exit code 2 on a rejected token.
- Keep dependencies current: `requirements.txt` pins exact versions, so check `pip list
  --outdated`, raise the pins you want, then `pip install -r requirements-dev.txt`
  (runtime dependencies plus the test and lint tools) and `playwright install chromium`. Run `pytest -q`
  afterwards.
- Watch the 🛑 alerts: they report crashed monitors and turbo sessions, page checks that
  raised, a failing pending watch, RPC endpoints silent for minutes (once per outage), an
  RPC endpoint that serves another chain, a browser that could not start, Chromium running
  without its sandbox, a damaged or unsaved `state.json` or `outbox.json`, and address
  caches with unreadable lines or that cannot be read or saved, at most once per ten
  minutes per problem while the bot runs (a restart may repeat one).
