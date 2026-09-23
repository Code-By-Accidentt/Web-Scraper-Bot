# 🔍 Website Contract Monitor Bot

![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python)

Telegram-controlled watcher that renders websites in a headless browser, reports **confirmed changes** of their content and hunts for **smart-contract addresses** (EVM, Solana and Sui), validating every candidate on-chain before it bothers you.

---

## ✨ What it does

* **Real rendering, fast** – one shared Chromium (Playwright), pages rendered in parallel tabs, waits for network idle instead of sleeping.
* **Root URL + sub-pages** – every site is watched together with a configurable set of derived pages (`app.<domain>`, `docs.<domain>`, `/dashboard`, …); a sub-page is announced once when it first appears.
* **Change detection that does not lie** – structural diff of the rendered DOM (text + attributes), every change confirmed by a second render, everything that differs between the two renders (timers, counters, banners) is ignored. Alerts say *what* changed, not only how much.
* **Contract discovery** – `0x…`, Solana and Sui addresses (coin types) are extracted from the full rendered HTML (text, attributes, comments, meta, inline scripts and JSON) and, with deep scan, from iframes, shadow DOM and every script or JSON answer the page loaded. Candidates are validated over JSON-RPC (bytecode + ERC-20 probe on 8 EVM networks in parallel, program / SPL-mint check on Solana) and cached.
* **Noise control** – an element that keeps changing round after round (counters, rotating banners) is silenced after two alerts for as long as it keeps flapping.
* **Turbo mode** – `/turbo <url>` renders a site every 2 s (optionally from a scheduled time) and announces every new address immediately, even before the chain confirms it; the session ends by itself after the first confirmed contract or when the time is up.
* **Unverified mode** – `/unverified <url> on` announces every new address on a site at once, wallets and all, then re-checks it on-chain for a day and confirms it the moment the contract exists; alerts are capped per hour.
* **Live settings** – `/settings` explains every tunable knob, `/set <key> <value>` changes it without a restart (validated, risky values ask for `confirm`), `/settings reset` returns to `.env`.
* **Block awareness** – 403/429/503 answers, WAF challenge pages and connection resets are recognised; the site's interval doubles per round (up to `BLOCK_MAX_BACKOFF`), turbo stops, and you get one warning plus a recovery notice.
* **Auto-pause** – when one page reveals ≥ N new contracts at once the addresses are shipped as a `.txt` document and the site is paused.
* **Persistent** – sites, pause/notification flags, snapshots and caches live under `data/` and survive restarts. `rebooter.py` restarts the bot when it dies or stops heart-beating.

---

## 📂 Layout

```text
main.py                     # entry point: python main.py
rebooter.py                 # supervisor: restarts the bot when it exits or hangs
scraper_bot/
  app.py                    # composition root, logging, heartbeat
  config.py                 # Settings loaded from .env / environment
  models.py                 # dataclasses shared by all modules
  urls.py                   # URL normalisation, sub-URL derivation, file names
  fetcher.py                # PlaywrightFetcher (browser) + HttpProber (HEAD/GET probe)
  html_normalizer.py        # rendered HTML -> ordered list of nodes
  change_detector.py        # diff of node lists, volatile-node filtering
  noise_filter.py           # silences elements that keep changing (flapping memory)
  blocking.py               # recognises rate limits, WAF challenges and connection resets
  turbo.py                  # turbo sessions and their manager (/turbo)
  unverified.py             # report-first mode: announce, then confirm on-chain
  preferences.py            # runtime settings registry: validation, persistence, /settings
  timefmt.py                # time and duration formatting
  address_extractor.py      # regex + syntactic validation (EIP-55, base58)
  contract_validators.py    # EVM / Solana / Sui RPC checks + caching facade
  enrichment.py             # token details: on-chain metadata, DexScreener, explorer/DEX links
  rpc.py                    # tiny JSON-RPC client
  sui.py                    # Sui RPC helpers: coin metadata, packages, coin discovery
  solana.py                 # Solana account layouts shared by validator and enricher
  evm.py                    # ERC-20 selectors shared by validator and enricher
  page_checker.py           # per-page pipeline: render -> contracts -> diff
  site_monitor.py           # loop for one root site and its sub-pages
  monitor_manager.py        # add / remove / pause / resume, task lifecycle
  commands.py               # Telegram command parsing + long-polling listener
  status.py                 # /status report (uptime, browser, rounds, caches)
  notifier.py               # alert formatting
  markup.py                 # Telegram HTML helpers: code, pre, links, escaping
  storage.py                # state.json, snapshots, address caches
  telegram_client.py        # Bot API client (httpx)
tests/                      # pytest suite: no network, no browser required
data/                       # runtime data, git-ignored (created on first start)
```

---

## 🚀 Quick start

```bash
git clone https://github.com/code-by-accident/Web-Scraper-Bot.git
cd Web-Scraper-Bot

python -m venv .venv
.venv\Scripts\activate            # Windows      (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
playwright install chromium       # once: downloads the browser used for rendering

copy .env.example .env            # Linux/macOS: cp .env.example .env
#   edit .env: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS

python main.py
```

Don't know your chat id? Start the bot with only the token set, send it `/start`, and it replies with the id.

Keep it alive unattended with the supervisor (same interpreter, restarts on exit or when `data/heartbeat` is older than five minutes):

```bash
python rebooter.py
```

---

## ⚙️ Configuration

Every setting is an environment variable. A `.env` file in the working directory is read first; real environment variables win. `.env.example` lists everything with its default. The knobs marked *live* below can also be changed from Telegram with `/set`; those overrides are stored in `data/preferences.json`, survive restarts and take precedence over `.env` until `/settings reset`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | – | **Required.** Token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_IDS` | – | Comma separated chats that receive alerts |
| `TELEGRAM_ALLOWED_CHAT_IDS` | empty = everyone | Chats allowed to send commands |
| `POLL_INTERVAL_MIN` / `POLL_INTERVAL_MAX` | 4 / 10 | Seconds between two rounds of one site (random within the range); a round itself takes about 2 s |
| `MAX_CONCURRENT_PAGES` | 8 | Browser tabs rendered at the same time |
| `PAGE_LOAD_TIMEOUT` / `NETWORK_IDLE_TIMEOUT` / `SETTLE_DELAY` | 20 / 2 / 0.25 | Rendering timing, seconds (measured: pages go idle in ~0.8 s, 0.25 s settle gives the same output as 0.5 s; a page that never goes idle costs the full idle cap per render) |
| `RENDER_FAILURE_THRESHOLD` | 5 | Load timeouts in a row that flag a page: one alert, its load timeout is doubled (up to 3×) until it renders again; 0 = off |
| `CONFIRM_DELAY` | 2 | Seconds between the two renders that confirm a change |
| `FLAP_WINDOW_HOURS` / `FLAP_THRESHOLD` | 24 / 2 | Silence an element after it alerted `FLAP_THRESHOLD` times within the window (0 hours = off) |
| `DEEP_SCAN` / `DEEP_SCAN_MAX_MB` | true / 5 | Also search iframes, shadow DOM and the scripts/JSON the page loaded; byte budget per render (5 MB of JS take about 0.8 s to scan) |
| `TURBO_INTERVAL` / `TURBO_DEFAULT_MINUTES` / `TURBO_MAX_MINUTES` | 2 / 30 / 180 | Seconds between turbo renders, default and maximum session length |
| `TURBO_STOP_AFTER` / `TURBO_MAX_SITES` / `TURBO_MAX_PAGES` / `TURBO_RECHECK_INTERVAL` | 1 / 1 / 4 / 5 | Confirmed contracts that end a session (0 = timer only), concurrent turbo sites, pages rendered per cycle (root included), seconds between on-chain re-checks of unverified addresses |
| `BLOCK_MAX_BACKOFF` | 600 | Longest pause between rounds while a site looks like it blocks the bot |
| `UNVERIFIED_RECHECK_MINUTES` / `UNVERIFIED_WATCH_HOURS` / `UNVERIFIED_MAX_PER_HOUR` | 5 / 24 / 10 | Unverified mode: on-chain re-check interval, watch window, alerts per hour per site |

| `CONTRACT_PAUSE_THRESHOLD` | 5 | New contracts on one page that trigger the auto-pause |
| `SUB_URL_PATTERNS` | see `.env.example` | Sub-pages derived from every root URL (`{domain}` = host without `www.`) |
| `IGNORED_ATTRIBUTES` | nonce, csrf-token, … | HTML attributes that are never compared |
| `MAX_CHANGE_EXAMPLES` | 5 | Examples per category in a change alert (0 = counts only) |
| `ENRICH_TIMEOUT` | 2 | Seconds a contract alert may wait for token details (name, supply, DEX pair via DexScreener; measured 0.05–0.2 s); 0 = links only |
| `BROWSER_CHANNEL` | chromium | `chromium` (bundled) or an installed `chrome` / `msedge` |
| `BLOCKED_RESOURCE_TYPES` | image,media,font | Resources the browser does not download |
| `ETH_RPC_URL`, `BASE_RPC_URL`, `ARB_RPC_URL`, `OPT_RPC_URL`, `BSC_RPC_URL`, `FANTOM_RPC_URL`, `SONIC_RPC_URL`, `AVAX_RPC_URL`, `SOLANA_RPC_URL`, `SUI_RPC_URL` | public nodes | RPC endpoints; set an EVM one to an empty string to skip that network |
| `REQUIRE_ERC20` | true | Count an EVM contract only if it answers `totalSupply` / `symbol` / `decimals` |
| `RPC_TIMEOUT` / `MAX_CONCURRENT_RPC` | 5 / 8 | RPC timeout in seconds (public nodes answer in 0.05–0.3 s), concurrent address checks |
| `REJECTED_TTL_HOURS` | 6 | Hours before an address that was *not* a contract is checked again (0 = never) |
| `RPC_RETRY_DELAY` | 600 | Seconds before an address whose RPC check failed is retried |
| `DATA_DIR` / `LOG_LEVEL` / `LOG_FILE` | data / INFO / – | Storage directory, verbosity, optional rotating log file |
| `HEARTBEAT_INTERVAL` | 15 | Seconds between heartbeat writes (used by `rebooter.py`) |

Live (changeable with `/set`): poll intervals, confirm/idle/settle delays, render failure threshold, `REQUIRE_ERC20`, pause threshold, rejected TTL, deep scan, unverified-mode knobs, alert examples, token details timeout, flapping memory, every turbo limit and the block backoff. Token, chat ids, browser, concurrency, RPC endpoints and paths need a restart.

---

## 💬 Telegram commands

```text
/add <url>       start monitoring ("example.com" works, https is assumed)
/remove <url>    stop monitoring and delete the site's snapshots
/pause <url>     pause monitoring (survives restarts)
/resume <url>    resume monitoring
/list            sites with alert status, running state and last change
/nonotifi <url>  mute change alerts for a site (contracts are still reported)   alias: /mute
/notify <url>    un-mute change alerts                                         alias: /unmute
/turbo <url> [minutes] [at HH:MM | in 10m]   turbo mode: render every 2 s until a contract appears
/turbo stop <url>                            end turbo mode;  /turbo alone shows turbo status
/unverified <url> on|off                     announce every new address at once, confirm on-chain later
/settings [key]                              live settings with explanations;  /set <key> <value> [confirm]
/settings reset <key|all>                    back to the .env value
/status          uptime, browser state, blocked sites, turbo sessions, cache sizes
/help            command list
/start           command list + your chat id
```

---

## 🔍 How a check works

1. Derived sub-pages are probed with a cheap `HEAD` (falling back to `GET`). Only pages that are conclusively missing (DNS failure, 404/410) are skipped; everything else is rendered.
2. The page is rendered in Chromium: DOM content loaded → network idle (bounded) → short settle delay. A page that fails to load `RENDER_FAILURE_THRESHOLD` times in a row gets one 🐢 alert and a longer load timeout (doubled, up to three times `PAGE_LOAD_TIMEOUT`) until it renders again; `/status` counts such pages.
3. The full rendered HTML is scanned for addresses; with `DEEP_SCAN` the markup of iframes and shadow roots and the bodies of the scripts and JSON answers the page loaded are scanned too (they were downloaded by the browser anyway). Unknown candidates are validated: `eth_getCode` on every EVM network in parallel (plus the ERC-20 calls when required) or `getAccountInfo` on Solana. Verdicts are cached: confirmed forever, rejected for `REJECTED_TTL_HOURS`, failed lookups retried after `RPC_RETRY_DELAY`. Concurrent checks of the same address share one lookup.
4. The HTML is reduced to an ordered list of nodes (tag, attributes, own text). Scripts, styles, svg and meta tags are dropped, tracking parameters are stripped from URLs, class tokens are sorted. The list is compared with the stored snapshot.
5. If it differs, the page is rendered again after `CONFIRM_DELAY`. Nodes that differ between the two renders are volatile and excluded from the report together with their previous values. If one render is much smaller than the other (a truncated load) the round is skipped. Otherwise the snapshot advances to the second render, so noise never accumulates.
6. Elements that already alerted `FLAP_THRESHOLD` times within `FLAP_WINDOW_HOURS` are dropped from the diff (the memory lives in the page's snapshot file and expires on its own). A non-empty remaining diff is sent as an alert with counts and examples: `-` removed, `+` added, `~` attribute changes.
7. Contracts found on a page: fewer than the threshold → one message each; threshold or more → a `.txt` document and the site is auto-paused until `/resume`.
8. Answers that look like a block (HTTP 401/403/407/429/503, a WAF challenge title such as "Just a moment…", a `cf-mitigated: challenge` header, connection resets) are never diffed. The site's interval doubles on every such round (bounded by `BLOCK_MAX_BACKOFF` and honouring `Retry-After`), one warning is sent, and a recovery notice follows when the root page answers normally again. The bot never tries to get around a block: slowing down is the only sane reaction.

When a contract is confirmed, the alert also carries the token's name, symbol, decimals and supply (read from the chain: ERC-20 calls, the SPL mint plus its Metaplex or Token-2022 metadata, or the Sui coin metadata), its most liquid DEX pair from DexScreener (price, liquidity, FDV, listing age) or *No DEX pair listed yet*, and links to the explorer, DexScreener and a swap page. These lookups are best effort and bounded by `ENRICH_TIMEOUT`; when a source is slow or down the alert goes out without that part. An alert names exactly one token: every link carries the token's own identifier and never a pair address, so a bot that parses the chat for addresses cannot pick up anything else.

Sui tokens are recognised by their coin type (`0x<package>::module::NAME`); a bare 64-hex id counts as a Sui candidate only on pages that mention Sui, because Ethereum transaction hashes look the same. A coin type is confirmed when the chain holds metadata for it; a bare id must be a package that defines a coin (any package with `REQUIRE_ERC20=false`). When a package was published instead of the coin type, the alert adds the coin type it found.

## 🚀 Turbo mode

`/turbo https://launch.example` renders up to `TURBO_MAX_PAGES` pages per cycle (the root page plus its live sub-pages) every `TURBO_INTERVAL` seconds (full renders with deep scan, no change alerts) for `TURBO_DEFAULT_MINUTES` minutes, or `/turbo <url> 15` for 15 minutes. `/turbo <url> at 15:30` or `/turbo <url> in 10m` arms it for a launch time. Addresses that were already on the pages when the session started are watched silently; every address that appears later is announced at once as *unverified* and re-checked on-chain every `TURBO_RECHECK_INTERVAL` seconds, ignoring the regular negative cache, so an address published before its contract is deployed is confirmed as soon as the deployment lands. The session ends after `TURBO_STOP_AFTER` confirmed contracts, when the time is up, on `/turbo stop <url>`, or when the site starts refusing us; the closing message lists what was found. At most `TURBO_MAX_SITES` sites can be in turbo at once and sessions do not survive a restart.

---

## ⚠️ Unverified mode

`REQUIRE_ERC20` only decides whether a contract must look like a token; the bytecode check that separates contracts from plain wallets and example addresses always runs. The one thing the regular monitor cannot catch is an address published *before* its contract is deployed: it is rejected and remembered for `REJECTED_TTL_HOURS`. `/unverified <url> on` closes that gap for a launch site: every address that appears on its pages is announced immediately with an *unverified* label (plain wallets included, so expect noise on leaderboards), then re-checked on-chain every `UNVERIFIED_RECHECK_MINUTES` for `UNVERIFIED_WATCH_HOURS`, bypassing the negative cache; a confirmation message follows the moment the contract exists. More than `UNVERIFIED_MAX_PER_HOUR` new addresses in an hour are folded into one digest. The mode is per site, persisted, and only chats allowed to change settings can switch it.

## 🛑 When something breaks

Every message uses Telegram's HTML formatting: commands, addresses and chat ids are monospace, so one tap copies them, and examples of changed elements come as code blocks. If Telegram ever rejects the markup of a message, the same text is re-sent as plain text, so no alert is lost. Command errors answer with the reason (`❌ 'x' is not a URL …`) and an internal failure answers with the exception type and message while the bot keeps running. Operational failures reach the alert chats as `🛑` messages, at most once per ten minutes for the same problem: a crashed site monitor (restarted automatically), a crashed turbo session, a page check that raised, on-chain checks failing on every endpoint, and a browser that could not start (the bot stops with exit code 3 and tells you to run `playwright install chromium`). A `🤖 Bot started` message on every start makes restart loops visible.

## ⚙️ Settings from Telegram

`/settings` lists every live setting with its current value (`*` marks values changed from `.env`), `/settings <key>` explains what it does, why you would change it, its range and default, and `/set <key> <value>` applies a change immediately. Values are type- and range-checked, related pairs stay consistent (poll_interval_min ≤ poll_interval_max), and risky values such as polling below 3 s or turning the bytecode check for tokens off must be repeated with `confirm`. `/settings reset <key|all>` returns to the `.env` values. Only chats in `TELEGRAM_ALLOWED_CHAT_IDS` (or, when that list is empty, in `TELEGRAM_CHAT_IDS`) may change settings; everyone else can read them.

## 🧪 Development

```bash
pip install -r requirements-dev.txt
pytest -q                                # unit tests (no network, no browser)
ruff check . && ruff format --check .    # lint + formatting
SCRAPER_BOT_BROWSER_TESTS=1 pytest -q tests/test_fetcher_browser.py   # real-browser test (opt-in)
```

---

## 🔄 Upgrading from version 1

On the first start without `data/state.json` the bot imports `monitored_sites.txt`, `notifications_status.json` and `found_wallets.txt` from the working directory. Selenium, web3 and solana-py are no longer required; run `pip install -r requirements.txt` and `playwright install chromium`.

---

## 📜 License

Distributed under the license in `LICENSE.md`.
