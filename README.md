# 🔍 Website Contract Monitor Bot

![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python)

Telegram-controlled watcher that renders websites in a headless browser, reports **confirmed changes** of their content and hunts for **smart-contract addresses** (EVM + Solana), validating every candidate on-chain before it bothers you.

---

## ✨ What it does

* **Real rendering, fast** – one shared Chromium (Playwright), pages rendered in parallel tabs, waits for network idle instead of sleeping.
* **Root URL + sub-pages** – every site is watched together with a configurable set of derived pages (`app.<domain>`, `docs.<domain>`, `/dashboard`, …); a sub-page is announced once when it first appears.
* **Change detection that does not lie** – structural diff of the rendered DOM (text + attributes), every change confirmed by a second render, everything that differs between the two renders (timers, counters, banners) is ignored. Alerts say *what* changed, not only how much.
* **Contract discovery** – `0x…` and Solana addresses are extracted from the full HTML, validated over JSON-RPC (bytecode + ERC-20 probe on 8 EVM networks in parallel, program / SPL-mint check on Solana) and cached.
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
  address_extractor.py      # regex + syntactic validation (EIP-55, base58)
  contract_validators.py    # EVM / Solana RPC checks + caching facade
  rpc.py                    # tiny JSON-RPC client
  page_checker.py           # per-page pipeline: render -> contracts -> diff
  site_monitor.py           # loop for one root site and its sub-pages
  monitor_manager.py        # add / remove / pause / resume, task lifecycle
  commands.py               # Telegram command parsing + long-polling listener
  notifier.py               # alert formatting
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

Every setting is an environment variable. A `.env` file in the working directory is read first; real environment variables win. `.env.example` lists everything with its default.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | – | **Required.** Token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_IDS` | – | Comma separated chats that receive alerts |
| `TELEGRAM_ALLOWED_CHAT_IDS` | empty = everyone | Chats allowed to send commands |
| `POLL_INTERVAL_MIN` / `POLL_INTERVAL_MAX` | 5 / 15 | Seconds between two rounds of one site (random within the range) |
| `MAX_CONCURRENT_PAGES` | 6 | Browser tabs rendered at the same time |
| `PAGE_LOAD_TIMEOUT` / `NETWORK_IDLE_TIMEOUT` / `SETTLE_DELAY` | 30 / 6 / 1 | Rendering timing, seconds |
| `CONFIRM_DELAY` | 2 | Seconds between the two renders that confirm a change |
| `CONTRACT_PAUSE_THRESHOLD` | 5 | New contracts on one page that trigger the auto-pause |
| `SUB_URL_PATTERNS` | see `.env.example` | Sub-pages derived from every root URL (`{domain}` = host without `www.`) |
| `IGNORED_ATTRIBUTES` | nonce, csrf-token, … | HTML attributes that are never compared |
| `MAX_CHANGE_EXAMPLES` | 5 | Examples per category in a change alert (0 = counts only) |
| `BROWSER_CHANNEL` | chromium | `chromium` (bundled) or an installed `chrome` / `msedge` |
| `BLOCKED_RESOURCE_TYPES` | image,media,font | Resources the browser does not download |
| `ETH_RPC_URL`, `BASE_RPC_URL`, `ARB_RPC_URL`, `OPT_RPC_URL`, `BSC_RPC_URL`, `FANTOM_RPC_URL`, `SONIC_RPC_URL`, `AVAX_RPC_URL`, `SOLANA_RPC_URL` | public nodes | RPC endpoints; set an EVM one to an empty string to skip that network |
| `REQUIRE_ERC20` | true | Count an EVM contract only if it answers `totalSupply` / `symbol` / `decimals` |
| `RPC_TIMEOUT` / `MAX_CONCURRENT_RPC` | 8 / 8 | RPC timeout in seconds, concurrent address checks |
| `REJECTED_TTL_HOURS` | 6 | Hours before an address that was *not* a contract is checked again (0 = never) |
| `RPC_RETRY_DELAY` | 600 | Seconds before an address whose RPC check failed is retried |
| `DATA_DIR` / `LOG_LEVEL` / `LOG_FILE` | data / INFO / – | Storage directory, verbosity, optional rotating log file |
| `HEARTBEAT_INTERVAL` | 15 | Seconds between heartbeat writes (used by `rebooter.py`) |

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
/help            command list
/start           command list + your chat id
```

---

## 🔍 How a check works

1. Derived sub-pages are probed with a cheap `HEAD` (falling back to `GET`). Only pages that are conclusively missing (DNS failure, 404/410) are skipped; everything else is rendered.
2. The page is rendered in Chromium: DOM content loaded → network idle (bounded) → short settle delay.
3. The full HTML is scanned for addresses. Unknown candidates are validated: `eth_getCode` on every EVM network in parallel (plus the ERC-20 calls when required) or `getAccountInfo` on Solana. Verdicts are cached: confirmed forever, rejected for `REJECTED_TTL_HOURS`, failed lookups retried after `RPC_RETRY_DELAY`. Concurrent checks of the same address share one lookup.
4. The HTML is reduced to an ordered list of nodes (tag, attributes, own text). Scripts, styles, svg and meta tags are dropped, tracking parameters are stripped from URLs, class tokens are sorted. The list is compared with the stored snapshot.
5. If it differs, the page is rendered again after `CONFIRM_DELAY`. Nodes that differ between the two renders are volatile and excluded from the report together with their previous values. If one render is much smaller than the other (a truncated load) the round is skipped. Otherwise the snapshot advances to the second render, so noise never accumulates.
6. A non-empty remaining diff is sent as an alert with counts and examples: `-` removed, `+` added, `~` attribute changes.
7. Contracts found on a page: fewer than the threshold → one message each; threshold or more → a `.txt` document and the site is auto-paused until `/resume`.

---

## 🧪 Development

```bash
pip install -r requirements-dev.txt
pytest -q                                # unit tests (no network, no browser)
ruff check . && ruff format --check .    # lint + formatting
```

---

## 🔄 Upgrading from version 1

On the first start without `data/state.json` the bot imports `monitored_sites.txt`, `notifications_status.json` and `found_wallets.txt` from the working directory. Selenium, web3 and solana-py are no longer required; run `pip install -r requirements.txt` and `playwright install chromium`.

---

## 📜 License

Distributed under the license in `LICENSE.md`.
