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
```

Create `.env` next to `main.py` with `TELEGRAM_BOT_TOKEN` (from @BotFather),
`TELEGRAM_CHAT_IDS` and only the other keys you want to change: `.env.example` lists
every key with its default. A key missing from `.env` takes the built-in default, so a
later release can improve it for you; a copy of the whole example pins every value.
If you do not know your chat id, start the bot with only the token, send it `/start`,
and it answers with the id. Python 3.11 or newer; `requirements.txt` pins the versions
the tests ran with.

### Ubuntu 20.04 server

Playwright 1.63 has no Chromium build for Ubuntu 20.04, so `playwright install
chromium` fails there. Install Google Chrome and point the bot at it:

```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install ./google-chrome-stable_current_amd64.deb   # also adds Google's apt repository
```

and `BROWSER_CHANNEL=chrome` in `.env`. Ubuntu 20.04 ships Python 3.8: install 3.11 or
newer first. The default `BROWSER_ARGS` turn WebGL off, which a server without a GPU
would otherwise draw on the CPU.

Run the bot as its own user, never as root (Chrome's sandbox refuses root):

```bash
sudo adduser --system --group --home /home/scraper scraper
sudo -u scraper -H git clone https://github.com/code-by-accident/Web-Scraper-Bot.git /home/scraper/bot
cd /home/scraper/bot
sudo -u scraper -H python3.12 -m venv .venv
sudo -u scraper -H .venv/bin/pip install -r requirements.txt
sudo -u scraper -H nano .env        # token, chat ids, BROWSER_CHANNEL=chrome
```

## Run

```bash
python main.py            # or: python -m scraper_bot
```

The bot verifies the token, starts the browser, loads `data/state.json`, sends
`🤖 Bot started (v2.1.0): 2/2 site(s) running.` to the alert chats and starts polling
Telegram. Stop it with Ctrl-C or SIGTERM (SIGTERM on Linux and macOS; on Windows it
kills at once); monitors, turbo sessions and the browser shut down cleanly, and token alerts
already on their way are still sent (up to 10 s). A second SIGTERM kills at once.

Exit codes: `0` normal, Ctrl-C or SIGTERM, `1` unexpected fatal error (the supervisor
restarts), `2` configuration error or rejected token, `3` the browser could not start
(run `playwright install chromium`, or use Chrome as described above).

Two instances with the same token cannot poll at once: the second one logs
`getUpdates conflict: another bot instance is polling with this token` and retries.

## Keep it alive

```bash
python rebooter.py
```

The supervisor adopts an already running `main.py` or starts one, restarts it when the
process exits, and restarts it when `data/heartbeat` has not been written for 300 s (a
hung process). Options: `--script`, `--heartbeat` (pass `<DATA_DIR>/heartbeat` when
`DATA_DIR` is not `data`), `--stale-after`, `--interval`. Run
the supervisor itself as a system service (Task Scheduler, systemd) if the machine
reboots unattended. On Linux a SIGTERM to the supervisor stops the bot gracefully (up to
30 s), then the supervisor exits; on Windows stopping the supervisor kills the bot at once.

A systemd service for the layout above, `/etc/systemd/system/scraper-bot.service`:

```ini
[Unit]
Description=Web Scraper Bot
After=network-online.target
Wants=network-online.target

[Service]
User=scraper
WorkingDirectory=/home/scraper/bot
ExecStart=/home/scraper/bot/.venv/bin/python rebooter.py
Restart=always
RestartSec=10
# Only rebooter.py gets the SIGTERM: it stops the bot, which sends its alerts first.
KillMode=mixed
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now scraper-bot
journalctl -u scraper-bot -f        # the log
sudo systemctl stop scraper-bot     # graceful: alerts on their way still go out
```

## Logs

Console by default, `LOG_FILE=path` adds a rotating file (5 MB, 3 backups). Format:
`time LEVEL logger: message`. `httpx`, `httpcore`, `hpack`, `h2` and `asyncio` are kept at
WARNING (`hpack` would log request paths, which hold the bot token). The
bot token never appears in logs. Useful lines:

```text
scraper_bot.monitoring.site_monitor: [https://x.io] https://x.io/app: unchanged
scraper_bot.monitoring.site_monitor: [https://x.io] https://x.io unavailable: timeout after 20s
scraper_bot.monitoring.site_monitor: [https://x.io] possible block on https://x.io (HTTP 403); next round in 16s
scraper_bot.chains.contract_validators: Ethereum 0x1f9a… confirmed as a token on Base
scraper_bot.chains.contract_validators: RPC endpoint Fantom is failing: eth_getCode via rpcapi.fantom.network: HTTP 502
```

## Alerts and what they mean

| Alert | Meaning | What to do |
| --- | --- | --- |
| `🤖 Bot started (v…): N/M site(s) running` | A (re)start. Frequent ones mean the supervisor is restarting a crashing bot; read the log. | – |
| `⚙️ .env still sets defaults of an earlier release:` + keys | Your `.env` pins values that were the default in an earlier release (it was copied whole from an older `.env.example`). Sent once per set of such values; the log repeats it at every start. | Delete those lines to get the new defaults, or keep them on purpose; restart. |
| `🔍 Sub-page found: …` | A derived sub-page answered for the first time; it is monitored from now on. | – |
| `‼️ Changes on …` + counts + `- / + / ~` examples | A confirmed change of the page: removed, added, attribute-changed elements. | Look at the page. Mute a noisy site with `/nonotifi`. |
| `🔔 Contract found on …` + network, address, `Found in …: «…»`, `⏱ …`, links | A token appeared on the page: an ERC-20 (not an LP pair or vault), an SPL / Token-2022 mint or a Sui coin. It is sent the moment a render shows it, before the page is compared. `Found in` says where (page text, link, page source, loaded script, or via an LP pair) and quotes the text in front of the address, other addresses masked. `⏱` says between which two checks it appeared and when the alert was sent (the first attempt; a Telegram rate limit can delay it by seconds). A second or so later the same message is edited to add the token's name and supply, Solana authorities and risky Token-2022 extensions (⚠️), its age (🆕 = a Solana mint created within the hour, or an EVM contract deployed within the last ~90 blocks; «more than …» is a lower bound: Solana nodes keep days of history, EVM nodes about 100 blocks of state), other networks with the same address and the DEX pair. | Read the quoted text: `CA:` next to it is the launch, `Partner`/`Treasury`/a comment is not. ⚠️ lines mean the issuer can still mint, freeze, tax or move holders' tokens. Tap the monospace address to copy. |
| a `.txt` document + `⏸️ Monitoring paused: N confirmed contracts found on …` | `contract_pause_threshold` or more new contracts on one page in one round: the addresses are shipped as a file and the site is paused. | `/resume <url>` when you are done. |
| `⚠️ Unverified address on …` | Only with `/unverified <url> on`: a new address on that site, not a token yet; it is re-checked on-chain. | Wait for `✅ Confirmed` or treat as a hint. |
| `✅ Confirmed: 0x… is a token on …` | A previously unverified address is now a token. | – |
| `📨 N more unverified address(es) appeared on … (alert cap reached)` | Hourly digest of unverified addresses above `unverified_max_per_hour`. | Lower the noise: `/unverified <url> off` or raise the cap. |
| `🚀 Turbo mode on …` / `🎯 Turbo finished on …: reason` | A turbo session started / ended (time limit, found N contract(s), stopped manually, possible IP block, bot is shutting down) with what it found. A session announces confirmed tokens only (`🔔 Contract found`). | – |
| `🔔 Contract found …` with `⏱ on page since … · deployed between … and …` | An address the page showed before its token existed has just been deployed; the pending watch saw it within seconds. | The launch is live now. |
| `🚫 Possible IP block on …` | 401/403/407/429/503, a challenge page or connection resets from the site. The interval doubles per round up to `block_max_backoff`; a turbo session on that site ends when one of its own renders is blocked. | Wait. If it persists, the site banned the IP or wants a browser check; slow down with `/set poll_interval_min`. |
| `✅ … answers normally again; normal interval restored.` | The root page answered after a block. | – |
| `🐢 … has not rendered N times in a row (timeout after 20s). Load timeout for this page raised to 40s` | The page keeps timing out; it gets more time. | Check the site by hand; a heavy or dying page. |
| `✅ … renders again; load timeout back to 20s.` | The streak ended. | – |
| `🛑 <title>` + details + hint | An operational failure, at most once per ten minutes per problem: a crashed monitor (restarted after 30 s), a crashed turbo session, a page check that raised, an RPC endpoint silent for five minutes, an RPC endpoint serving another chain (checked at start), a browser that could not start. | Read the hint; the log has the traceback. |

## Data directory

| File | Safe to delete while stopped? | Effect |
| --- | --- | --- |
| `state.json` | no (you lose the site list) | Back it up; it is the whole configuration you made from Telegram. |
| `snapshots/*.json` | yes | The next round stores a fresh baseline; no alert is sent for the rebuild. Delete one file to reset one page. |
| `known_contracts.txt` | yes | Every contract will be announced again when seen. |
| `rejected_addresses.txt` | yes | Every wallet on the pages is checked again once. |
| `preferences.json` | yes | Live settings go back to `.env`; `/settings reset all` does the same at runtime. |
| `ignore.txt` | yes | Your own stop-list (addresses never announced, one per line, `#` comments); read at start. |
| `pending.json` | yes | Addresses waiting to become a token; after deleting it they are taken up again on the next rounds. |
| `outdated_defaults.txt` | yes | The old `.env` values last reported; after deleting it the next start reports them again. |
| `heartbeat` | yes | Rewritten every 15 s. |

Orphaned snapshots (pages of sites that were removed) are pruned on start.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Exit code 2, `Configuration error: …` | A wrong `.env` value; the message names the variable. | Fix the value. |
| Exit code 2, `Telegram rejected TELEGRAM_BOT_TOKEN` | Wrong or revoked token. | New token from @BotFather. |
| Exit code 3, `browser could not start: … Install it with: playwright install chromium` | Chromium missing or an incompatible Playwright. | `playwright install chromium`; on Ubuntu 20.04 install Google Chrome and set `BROWSER_CHANNEL=chrome`. |
| `🚫 Possible IP block … (vercel security checkpoint)` or `(x-vercel-mitigated)` | Vercel's bot protection wants a browser check the headless browser does not pass. | Poll that site less often; the bot only slows down. |
| `TELEGRAM_CHAT_IDS is empty: alerts will be dropped` at start | No alert chat configured. | Send `/start`, put the id into `.env`, restart. |
| Every page `unavailable: timeout after …` | The site is slow or blocks headless browsers silently. | Wait for the 🐢 alert (the timeout grows), try `HEADLESS=false` to look. |
| `🛑 On-chain checks keep failing` | An RPC endpoint has not answered for five minutes; the message names it and says for how long (`/status` shows the same from the first minute). Meanwhile every address is re-checked every `RPC_RETRY_DELAY` seconds. | Replace the endpoint in `.env`, or set an EVM URL empty to skip that network. |
| `🛑 An RPC endpoint serves another chain` | A URL in `.env` points at the wrong network (e.g. `BASE_RPC_URL` answers chain 1). | Fix the URL and restart. |
| Change alerts every round for the same element | A slow ticker (minutes) that the two-render check cannot see as volatile. | The noise memory silences it after `flap_threshold` alerts; raise `confirm_delay` for faster tickers. |
| `getUpdates conflict` | Two bots with one token. | Stop the other instance. |
| A site shows `[STOPPED]` in `/list` | Its monitor crashed and the automatic restart (30 s later) failed or was skipped. | `/pause` then `/resume`, or `/remove` and `/add`; the 🛑 alert has the traceback. |

## Known gaps

- **Proxies initialised later.** A token deployed behind a proxy whose implementation is
  set in a later transaction answers neither `totalSupply` nor `decimals` at first; it
  is rejected and checked again only after `rejected_ttl_hours`.
- **Solana pools.** An EVM LP pair leads to its token; a Raydium or Meteora pool address
  on a page does not lead to its mint.
- **EVM honeypots and taxes** are not checked. For Solana the alert shows mint and freeze
  authorities and risky Token-2022 extensions.
- **Deep scan** (off by default) ignores what a page's scripts carried on its first deep
  round; a bundle cut off by `DEEP_SCAN_MAX_MB` on that round can still yield an old
  address later.
- **Headless browser.** Sites see `HeadlessChrome` and `navigator.webdriver`; a strict
  bot shield blocks the bot, which then only slows down. No proxies, no captcha solving.

## Upgrading

Pull, then `pip install -r requirements-dev.txt` (runtime dependencies plus the test
and lint tools) and
`playwright install chromium` (a new Playwright often needs a new browser build; with
Chrome, `sudo apt install --only-upgrade google-chrome-stable` instead), then `pytest -q`
and restart. Missing
keys in `.env` use the defaults; `⚙️ .env still sets defaults of an earlier release`
at start names the lines that pin an old default.

To 2.1 from 2.0:

- Commands are taken only from `TELEGRAM_ALLOWED_CHAT_IDS` or, when it is empty, from
  the alert chats in `TELEGRAM_CHAT_IDS`; other chats get their chat id on `/start`.
- Token alerts go out before the page is compared, say where the address stood and
  when it appeared, and get their details by an edit a second later; `ENRICH_TIMEOUT`
  no longer delays an alert, so there is no need to set it to 0.
- LP pairs, vaults, NFTs, EIP-7702 wallets, Solana programs, stablecoins and wrapped
  natives are no longer announced; an LP pair leads to its token.
- Addresses published before their token exists are watched in `data/pending.json`;
  turbo announces confirmed tokens only.
- `/status` names RPC endpoints that stopped answering; `LOG_LEVEL` must be a level name
  and numbers in `.env` must be finite, or the bot does not start (exit code 2).
- New defaults: `DEEP_SCAN=false`, `SOLANA_RPC_URL=https://solana-rpc.publicnode.com`,
  `RPC_RETRY_DELAY=10`, `TURBO_RECHECK_INTERVAL=1`, `BROWSER_ARGS` without 3D. New
  keys: `HTTP2` (on), `BROWSER_ARGS`. New dependency: `h2`; `requirements.txt` now pins
  exact versions.
- SIGTERM (Linux, macOS) stops the bot gracefully and `rebooter.py` passes it on; give an
  existing systemd unit `KillMode=mixed` and `TimeoutStopSec=45` (see above).
- Optional `data/ignore.txt`: your own stop-list. At start every EVM endpoint's chain id
  is checked. An empty `SOLANA_RPC_URL`, `SUI_RPC_URL` or `DATA_DIR` now means the default.
- 2.0 can still read the data directory of 2.1 (it ignores `pending.json` and the new
  snapshot field), so a rollback needs no cleanup.

Coming from version 1 (Selenium, `monitored_sites.txt`): on the first start without
`data/state.json` the bot imports `monitored_sites.txt`, `notifications_status.json` and
`found_wallets.txt` from the working directory. Selenium, web3 and solana-py are no
longer needed.
