# Operations

How to install, run and keep the bot alive, what every alert means and what to do when
something is off.

## Install

```bash
git clone https://github.com/code-by-accident/Web-Scraper-Bot.git
cd Web-Scraper-Bot
python -m venv .venv
.venv\Scripts\activate            # Windows      (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
playwright install chromium       # once: the browser used for rendering
copy .env.example .env            # Linux/macOS: cp .env.example .env
```

Edit `.env`: `TELEGRAM_BOT_TOKEN` (from @BotFather) and `TELEGRAM_CHAT_IDS`. If you do
not know your chat id, start the bot with only the token, send it `/start`, and it
answers with the id. Python 3.11 or newer.

## Run

```bash
python main.py            # or: python -m scraper_bot
```

The bot verifies the token, starts the browser, loads `data/state.json`, sends
`🤖 Bot started (v2.0.0): 2/2 site(s) running.` to the alert chats and starts polling
Telegram. Stop it with Ctrl-C; monitors, turbo sessions and the browser shut down
cleanly.

Exit codes: `0` normal or Ctrl-C, `1` unexpected fatal error (the supervisor restarts),
`2` configuration error or rejected token, `3` the browser could not start (run
`playwright install chromium`).

Two instances with the same token cannot poll at once: the second one logs
`getUpdates conflict: another bot instance is polling with this token` and retries.

## Keep it alive

```bash
python rebooter.py
```

The supervisor adopts an already running `main.py` or starts one, restarts it when the
process exits, and restarts it when `data/heartbeat` has not been written for 300 s (a
hung process). Options: `--script`, `--heartbeat`, `--stale-after`, `--interval`. Run
the supervisor itself as a system service (Task Scheduler, systemd) if the machine
reboots unattended.

## Logs

Console by default, `LOG_FILE=path` adds a rotating file (5 MB, 3 backups). Format:
`time LEVEL logger: message`. `httpx`, `httpcore` and `asyncio` are kept at WARNING. The
bot token never appears in logs. Useful lines:

```text
scraper_bot.monitoring.site_monitor: [https://x.io] https://x.io/app: unchanged
scraper_bot.monitoring.site_monitor: [https://x.io] https://x.io unavailable: timeout after 20s
scraper_bot.monitoring.site_monitor: [https://x.io] possible block on https://x.io (HTTP 403); next round in 16s
scraper_bot.chains.contract_validators: Ethereum 0xA0b8… confirmed as a contract on Base
scraper_bot.chains.contract_validators: RPC endpoint Fantom is failing: eth_getCode via rpcapi.fantom.network: HTTP 502
```

## Alerts and what they mean

| Alert | Meaning | What to do |
| --- | --- | --- |
| `🤖 Bot started (v…): N/M site(s) running` | A (re)start. Frequent ones mean the supervisor is restarting a crashing bot; read the log. | – |
| `🔍 Sub-page found: …` | A derived sub-page answered for the first time; it is monitored from now on. | – |
| `‼️ Changes on …` + counts + `- / + / ~` examples | A confirmed change of the page: removed, added, attribute-changed elements. | Look at the page. Mute a noisy site with `/nonotifi`. |
| `🔔 Contract found on …` + network, address, token details, links | A new address on the page has bytecode (and answers ERC-20 calls when required). Token details come from the chain and DexScreener; `No DEX pair listed yet.` means DexScreener answered and has nothing. | The address is monospace: tap to copy. Links go to the explorer, DexScreener and a swap page. |
| a `.txt` document + `⏸️ Monitoring paused: N confirmed contracts found on …` | `contract_pause_threshold` or more new contracts on one page in one round: the addresses are shipped as a file and the site is paused. | `/resume <url>` when you are done. |
| `⚠️ Unverified address on …` | Turbo or unverified mode saw a new address that is not a contract yet; it is re-checked on-chain. | Wait for `✅ Confirmed` or treat as a hint. |
| `✅ Confirmed: 0x… is a contract on …` | A previously unverified address now has a contract. | – |
| `📨 N more unverified address(es) appeared on … (alert cap reached)` | Hourly digest of unverified addresses above `unverified_max_per_hour`. | Lower the noise: `/unverified <url> off` or raise the cap. |
| `🚀 Turbo mode on …` / `🎯 Turbo finished on …: reason` | A turbo session started / ended (time limit, found N contract(s), stopped manually, possible IP block, bot is shutting down) with what it found. | – |
| `🚫 Possible IP block on …` | 401/403/407/429/503, a challenge page or connection resets from the site. The interval doubles per round up to `block_max_backoff`; turbo on that site stops. | Wait. If it persists, the site banned the IP or wants a browser check; slow down with `/set poll_interval_min`. |
| `✅ … answers normally again; normal interval restored.` | The root page answered after a block. | – |
| `🐢 … has not rendered N times in a row (timeout after 20s). Load timeout for this page raised to 40s` | The page keeps timing out; it gets more time. | Check the site by hand; a heavy or dying page. |
| `✅ … renders again; load timeout back to 20s.` | The streak ended. | – |
| `🛑 <title>` + details + hint | An operational failure, at most once per ten minutes per problem: a crashed monitor (restarted after 30 s), a crashed turbo session, a page check that raised, on-chain checks failing on every endpoint, a browser that could not start. | Read the hint; the log has the traceback. |

## Data directory

| File | Safe to delete while stopped? | Effect |
| --- | --- | --- |
| `state.json` | no (you lose the site list) | Back it up; it is the whole configuration you made from Telegram. |
| `snapshots/*.json` | yes | The next round stores a fresh baseline; no alert is sent for the rebuild. Delete one file to reset one page. |
| `known_contracts.txt` | yes | Every contract will be announced again when seen. |
| `rejected_addresses.txt` | yes | Every wallet on the pages is checked again once. |
| `preferences.json` | yes | Live settings go back to `.env`; `/settings reset all` does the same at runtime. |
| `heartbeat` | yes | Rewritten every 15 s. |

Orphaned snapshots (pages of sites that were removed) are pruned on start.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Exit code 2, `Configuration error: …` | A wrong `.env` value; the message names the variable. | Fix the value. |
| Exit code 2, `Telegram rejected TELEGRAM_BOT_TOKEN` | Wrong or revoked token. | New token from @BotFather. |
| Exit code 3, `browser could not start: … Install it with: playwright install chromium` | Chromium missing or an incompatible Playwright. | `playwright install chromium`. |
| `TELEGRAM_CHAT_IDS is empty: alerts will be dropped` at start | No alert chat configured. | Send `/start`, put the id into `.env`, restart. |
| Every page `unavailable: timeout after …` | The site is slow or blocks headless browsers silently. | Wait for the 🐢 alert (the timeout grows), try `HEADLESS=false` to look. |
| `🛑 On-chain address checks are failing` repeatedly | A public RPC endpoint is down or rate-limiting. The log names it. | Replace the endpoint in `.env`, or set it empty to skip that network. |
| Change alerts every round for the same element | A slow ticker (minutes) that the two-render check cannot see as volatile. | The noise memory silences it after `flap_threshold` alerts; raise `confirm_delay` for faster tickers. |
| `getUpdates conflict` | Two bots with one token. | Stop the other instance. |
| A site shows `[STOPPED]` in `/list` | Its monitor crashed and `restart_delay` is off, or the restart was skipped. | `/pause` then `/resume`, or `/remove` and `/add`; the 🛑 alert has the traceback. |

## Upgrading

Pull, then `pip install -r requirements.txt` and `playwright install chromium` (a new
Playwright often needs a new browser build), then `pytest -q` and restart. Compare your
`.env` with `.env.example` for new keys; missing keys use the defaults.

Coming from version 1 (Selenium, `monitored_sites.txt`): on the first start without
`data/state.json` the bot imports `monitored_sites.txt`, `notifications_status.json` and
`found_wallets.txt` from the working directory. Selenium, web3 and solana-py are no
longer needed.
