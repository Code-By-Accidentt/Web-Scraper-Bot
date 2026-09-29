# 🔍 Website Contract Monitor Bot

![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python)

Telegram-controlled watcher for token launches. It renders websites in a headless
browser, reports **confirmed changes** of their content and hunts for **smart-contract
addresses** on EVM chains, Solana and Sui, confirming every candidate on-chain before it
bothers you.

## What it does

- **Real rendering, fast.** One shared Chromium (Playwright), pages rendered in parallel
  tabs, network-idle detection instead of fixed sleeps; Next.js prefetches and analytics
  beacons are dropped. A round over a site and its sub-pages takes about two seconds.
- **Root URL plus sub-pages.** Every site is watched together with a configurable set of
  derived pages (`app.<domain>`, `docs.<domain>`, `/dashboard`, ...); a sub-page is
  announced once when it first appears.
- **Change detection that does not lie.** Structural diff of the rendered DOM (text and
  attributes), every change confirmed by a second render, whatever differs between the two
  renders (timers, counters, banners) is ignored (a real change that lands between them is
  reported by the next round), elements that keep flapping are silenced. A timer or clock
  the bot has seen tick costs no second render. Alerts say *what* changed, not only how
  much.
- **Token discovery on three chain families.** ERC-20 tokens on 10 EVM networks
  (Ethereum, Base, Arbitrum, Optimism, BSC, Fantom, Sonic, Avalanche, Arc, Robinhood
  Chain), SPL / Token-2022 mints on Solana, Sui coins. An LP pair leads to its token; wallets
  (EIP-7702 included), NFTs, vaults, programs, stablecoins and wrapped natives never
  count, nor do copies of them (a token called USDC, WETH, ... at another address, judged
  per network). Candidates are taken from the whole rendered page and, with deep scan (off by
  default), from iframes, shadow DOM and every script or JSON answer the page loaded.
  Verdicts are cached; a rate-limited node never turns a real token into a cached
  rejection.
- **Light mode.** Between renders every page's HTML and the site's own JSON APIs are
  asked over plain HTTP every second (conditional requests, mostly a 304): an address
  that appears there is announced about half a second later, one the API names before
  the page shows it too ("site API"). A page whose HTML shows what its render shows is
  rendered only every 30 s; a new deploy of a single-page app is rendered at once. A site
  that refuses plain requests is asked through the browser.
- **Alerts first, details second.** A token alert leaves the moment the bot sees the
  token, a render included: while it is still loading (milliseconds after the document
  or the script that shows it, plus the on-chain check): where it stood on the page, the
  words in front of it and when it appeared. A second later the same message gains the name, supply,
  Solana authorities and risky extensions, the token's age, the other networks where the
  address is a token and the DEX pair. One alert per token, across pages, sites and modes.
- **Addresses published before their token.** An address that is on the page before
  its contract exists is watched (every second at first, then every 5 and 30 s, when the
  page names it as the CA or in turbo) and announced once it is deployed.
- **Turbo mode.** `/turbo <url>` renders a site every 2 s, optionally from a scheduled
  time, and announces every new token at once; an address that is not a token yet is
  checked every second.
- **Auto-turbo.** When a page shows a launch countdown, a launch time with its time zone,
  a countdown target in its scripts, or "CA: soon" / "presale is live" appears on it, the
  site is checked faster from 10 minutes before that time to an hour after it, or until
  its first token, with one message when that is planned and a summary at the end.
- **Unverified mode.** `/unverified <url> on` announces every new address on a site at
  once and keeps re-checking it on-chain for a day.
- **Live settings.** `/settings` explains every tunable knob, `/set` changes it without a
  restart, risky values ask for `confirm`.
- **Block awareness.** 401/403/407/429/503, WAF challenges and connection resets slow the
  site's interval down (doubling up to a cap, or longer when the site sends `Retry-After`),
  with one warning that names the protection (Cloudflare, Vercel, AWS WAF, DataDome, ...)
  and one recovery notice; while the site still refuses one light request, nothing is
  rendered. The browser does not announce itself as automated and keeps its cookies
  across restarts, so a check it passed on its own is not asked again. A captcha widget
  on a page is reported once. No proxy rotation, no captcha solving.
- **Pages that keep timing out** get one alert and a longer load timeout until they render
  again.
- **Persistent and supervised.** Sites, flags, snapshots and caches live under `data/` and
  survive restarts; `rebooter.py` restarts the bot when it dies or stops heart-beating,
  with growing pauses when it keeps failing at start.

## Quick start

```bash
git clone https://github.com/code-by-accident/Web-Scraper-Bot.git
cd Web-Scraper-Bot
python -m venv .venv
.venv\Scripts\activate            # Windows      (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
playwright install chromium       # once: downloads the browser used for rendering
#   create .env with TELEGRAM_BOT_TOKEN=... and TELEGRAM_CHAT_IDS=...
python main.py
```

Put only the keys you change into `.env`; `.env.example` lists all of them with their
defaults. Don't know your chat id? Start the bot with only the token set, send it
`/start`, and it replies with the id. Keep it alive unattended with `python
rebooter.py`. On Ubuntu 20.04 use Google Chrome (`BROWSER_CHANNEL=chrome`); see
[docs/operations.md](docs/operations.md) for that, a dedicated user and a systemd unit.

## Commands

```text
/add <url>          start monitoring ("example.com" works, https is assumed)
/remove <url>       stop monitoring and delete the site's snapshots
/pause <url>        pause monitoring (survives restarts)
/resume <url>       resume monitoring
/list               sites with alert status, running state and last change
/nonotifi <url>     mute change alerts for a site (contracts are still reported)   alias: /mute
/notify <url>       un-mute change alerts                                         alias: /unmute
/turbo <url> [minutes] [at HH:MM | in 10m]   turbo mode; /turbo stop <url> ends it or auto-turbo; /turbo shows both
/unverified <url> on|off                     announce every new address at once, confirm later
/settings [key]                              live settings with explanations
/set <key> <value> [confirm]                 change one live setting
/settings reset <key|all>                    back to the .env value
/status             uptime, browser, rounds per site, blocks, turbo, cache sizes
/help               command list;  /start also shows your chat id
```

Every command is described with its replies in [docs/commands.md](docs/commands.md).

## Documentation

| Page | Read it for |
| --- | --- |
| [docs/operations.md](docs/operations.md) | Installing, running, the supervisor, logs, **what every alert means and what to do**, the data directory, troubleshooting, upgrading |
| [docs/configuration.md](docs/configuration.md) | Every setting with its default, meaning and live `/set` key |
| [docs/commands.md](docs/commands.md) | Every Telegram command, its replies and who may use it |
| [docs/architecture.md](docs/architecture.md) | How a round works, the rules behind change detection and validation, state on disk, concurrency |
| [docs/development.md](docs/development.md) | Tests, conventions, measured performance numbers, how to add a setting, a chain, an alert or a command |
| [SECURITY.md](SECURITY.md) | What the bot touches, who can control it, safe operation, reporting |

## Layout

```text
main.py, rebooter.py        entry point and supervisor
scraper_bot/
  app/          start-up: entry point, process resources, wiring, heartbeat
  settings/     .env settings and the live ones changed with /set
  models/       plain data types shared by every package
  storage/      state.json, page snapshots, address caches, unsent alerts, v1 import
  pages/        fetching (browser, prober, block detection), changes (normalising,
                diffing, noise filter), the per-page check, where addresses stand
  chains/       the network table, addresses (formats, extraction, stop-list), JSON-RPC,
                EVM / Solana / Sui validation, token details
  monitoring/   site monitor, monitor manager, turbo mode, unverified mode, pending watch
                (addresses published before deployment), alert registry
  telegram/     Bot API client, alerts, commands, views (token details, /status, /settings)
tests/          pytest suite mirroring the packages, offline; browser tests behind a flag
docs/           the pages above
data/           runtime data, git-ignored, created on first start
```

## Development

```bash
pip install -r requirements-dev.txt
pytest -q                                # offline: no network, no browser
ruff check . && ruff format --check .
SCRAPER_BOT_BROWSER_TESTS=1 pytest -q tests/pages/fetching/test_fetcher_browser.py   # real Chromium
```

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md): use, run and change it for any noncommercial
purpose; commercial use needs a separate agreement with the author.
