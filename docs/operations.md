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

Run the bot as its own user, never as root. Chromium starts with its own sandbox, which
refuses root and needs user namespaces; where it cannot start (as root, in a container,
under the AppArmor rules of Ubuntu 23.10 and later) the bot runs the browser without it
and says so once with a 🛑. Google Chrome brings a setuid sandbox helper that may work
where the bundled Chromium's sandbox does not.

```bash
sudo adduser --system --group --home /home/scraper scraper
sudo -u scraper -H git clone https://github.com/code-by-accident/Web-Scraper-Bot.git /home/scraper/bot
cd /home/scraper/bot
sudo -u scraper -H python3.12 -m venv .venv
sudo -u scraper -H .venv/bin/pip install -r requirements.txt
sudo -u scraper -H nano .env        # token, chat ids, BROWSER_CHANNEL=chrome
sudo -u scraper -H chmod 600 .env   # the token: readable by the bot's user alone
```

## Run

```bash
python main.py            # or: python -m scraper_bot
```

The bot verifies the token, starts the browser, loads `data/state.json`, sends
`🤖 Bot started (v2.1.1): 2/2 site(s) running.` to the alert chats and starts polling
Telegram. Stop it with Ctrl-C or SIGTERM (SIGTERM on Linux and macOS; on Windows it
kills at once); monitors, turbo sessions and the browser shut down cleanly, and token alerts
already on their way are still sent (up to 10 s). A second SIGTERM kills at once.

Exit codes: `0` normal, Ctrl-C or SIGTERM, `1` unexpected fatal error, `2` configuration
error or rejected token, `3` the browser could not start (run `playwright install
chromium`, or use Chrome as described above). The supervisor restarts the bot after every
exit; see below for the pauses.

Two instances with the same token cannot poll at once: the second one logs
`getUpdates conflict: another bot instance is polling with this token` and retries.

## Keep it alive

```bash
python rebooter.py
```

The supervisor adopts a bot of this checkout that is already running (`python main.py`
from any directory, or `python -m scraper_bot` started in this checkout; interpreter
options such as `-X utf8` are fine, the `scraper-bot` console script
is not recognised) or starts `main.py`, restarts it when the process exits, and restarts
it when `data/heartbeat` has not been written for 300 s (a hung process). A bot that ran
for a minute or more is started again at once (within the 5 s check interval). One that
exits sooner is failing at start (a configuration error, a rejected token, a missing
browser) and is restarted after a growing pause: 5 s, 30 s, 1, 2, 5 and 10 minutes, then
every 15 minutes; after exit code 2 or 3 the pauses start at a minute. Every such start
may repeat its 🛑 (the ten-minute limit per problem does not outlive the process), so fix
the cause or stop the service. Options: `--script`, `--heartbeat` (pass
`<DATA_DIR>/heartbeat` when `DATA_DIR` is not `data`), `--stale-after`, `--interval`. Run
the supervisor itself as a system service (Task Scheduler, systemd) if the machine
reboots unattended. On Linux a SIGTERM to the supervisor stops the bot gracefully (up to
30 s), then the supervisor exits. Windows has no such hand-over: Ctrl-C in the console
reaches both processes and the bot shuts down gracefully, but ending only the supervisor
(Task Manager, Task Scheduler) leaves the bot running, and the next supervisor adopts it.

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
scraper_bot.monitoring.site_monitor: [https://x.io] possible block on https://x.io (Cloudflare challenge (HTTP 403)); next round in 16s
scraper_bot.pages.fetching.chromium: Chromium could not start with its sandbox (No usable sandbox! …); running without it
scraper_bot.chains.validator: Ethereum 0x1f9a… confirmed as a token on Base
scraper_bot.chains.rpc.health: RPC endpoint Fantom is failing: eth_getCode via rpcapi.fantom.network: HTTP 502
```

## Alerts and what they mean

| Alert | Meaning | What to do |
| --- | --- | --- |
| `🤖 Bot started (v…): N/M site(s) running` | A (re)start. Frequent ones mean the supervisor is restarting a crashing bot; read the log. | – |
| `⚙️ .env still sets defaults of an earlier release:` + keys | Your `.env` pins values that were the default in an earlier release (it was copied whole from an older `.env.example`). Sent once per set of such values; the log repeats it at every start. | Delete those lines to get the new defaults, or keep them on purpose; restart. |
| `🔍 Sub-page found: …` | A derived sub-page answered for the first time; it is monitored from now on. | – |
| `‼️ Changes on …` + counts + `- / + / ~` examples | A confirmed change of the page: removed, added, attribute-changed elements. | Look at the page. Mute a noisy site with `/nonotifi`. |
| `🔔 Contract found on …` + network, address, `Found in …: «…»`, `⏱ …`, links | A token appeared on the page: an ERC-20 (not an LP pair or vault), an SPL / Token-2022 mint or a Sui coin. It is sent the moment a render shows it, before the page is compared. It waits out Telegram flood limits of up to a minute, and a send that failed before reaching Telegram is tried again with growing pauses (about two minutes in all); one that may have reached Telegram (the answer timed out) is not sent twice. `Found in` says where (page text, link, page source, loaded script, or via an LP pair) and quotes the text in front of the address, other addresses masked. `⏱` says between which two checks it appeared and when the alert was sent (the first attempt). A second or so later the same message is edited to add the token's name and supply, Solana authorities and risky Token-2022 extensions (⚠️), its age (🆕 = a Solana mint created within the hour, or an EVM contract deployed within the last ~90 blocks; «more than …» is a lower bound: Solana nodes keep days of history, EVM nodes about 100 blocks of state), other networks with the same address and the DEX pair. | Read the quoted text: `CA:` next to it is the launch, `Partner`/`Treasury`/a comment is not. ⚠️ lines mean the issuer can still mint, freeze, tax or move holders' tokens. Tap the monospace address to copy. |
| a `.txt` document + `⏸️ Monitoring paused: N confirmed contracts found on …` | `contract_pause_threshold` or more new contracts on one page in one round: the site is paused and the tokens of the render that crossed the threshold come as a file (those of an earlier render of the same round went out as single alerts; N counts them all). The site is paused before either message goes out, and the pause message follows the document. When it says "This was the first check of the page since monitoring started or resumed", the page most likely lists tokens that existed before (after an update that adds a network, for example); they are known now and will not be announced again. | Look at the file, then `/resume <url>`. |
| `⚠️ Unverified address on …` | Only with `/unverified <url> on`: a new address on that site, not a token yet; it is re-checked on-chain. | Wait for `✅ Confirmed` or treat as a hint. |
| `✅ Confirmed: 0x… is a token on …` | A previously unverified address is now a token. | – |
| `📨 N more unverified address(es) appeared on … (alert cap reached)` | Hourly digest of unverified addresses above `unverified_max_per_hour`. | Lower the noise: `/unverified <url> off` or raise the cap. |
| `🚀 Turbo mode on …` / `🎯 Turbo finished on …: reason` | A turbo session started / ended (time limit, found N contract(s), stopped manually, possible IP block, bot is shutting down) with what it found. A session announces confirmed tokens only (`🔔 Contract found`). | – |
| `🔔 Contract found …` with `⏱ on page since … · deployed between … and …` | An address the page showed before its token existed has just been deployed; the pending watch saw it within seconds. | The launch is live now. |
| `🚫 Possible IP block on …` + `Page:`, `Reason:`, `Protection:` | 401/403/407/429/503, a challenge page or connection resets from any page of the site (a sub-page too). `Protection:` names the service when the answer gives it away: Cloudflare challenge, Cloudflare Turnstile, Vercel Security Checkpoint, AWS WAF, DataDome, DDoS-Guard, PerimeterX, Imperva, Sucuri, hCaptcha, reCAPTCHA, or only the CDN in front of the site (Cloudflare, Akamai). The bot does not solve captchas or browser checks: the interval doubles per round up to `block_max_backoff` (a longer `Retry-After` from the site is honoured as sent), and a turbo session on that site ends when one of its own renders is blocked. | Wait. If it persists, the site banned the IP or wants a browser check; slow down with `/set poll_interval_min`. When `Page:` names a sub-page that always answers 401/403 (a login-only `/dashboard`), take its pattern out of `SUB_URL_PATTERNS`. |
| `✅ … answers normally again after 12m 30s; normal interval restored.` | A round without any block signal in which the root page answered. | – |
| `🧩 Captcha on …: the page shows a … widget.` | A page that otherwise loads shows a captcha a person would have to solve: a reCAPTCHA checkbox, hCaptcha or Cloudflare Turnstile (the invisible kinds are not reported). Monitoring goes on, but what the page shows only after solving it stays out of sight. Once per page and widget (the page's other widget, if it alternates, gets its own notice once). | Watch that part of the site by hand if it matters. |
| `🐢 … has not rendered N times in a row (timeout after 20s). Load timeout for this page raised to 40s` | The page keeps timing out; it gets more time. | Check the site by hand; a heavy or dying page. |
| `✅ … renders again; load timeout back to 20s.` | The streak ended. | – |
| `🛑 <title>` + details + hint | An operational failure, at most once per ten minutes per problem and run: a crashed monitor (restarted after 30 s), a crashed turbo session, a page check that raised, a failing pending watch, a damaged `state.json` (moved aside) or a failed save of it, a browser that could not start, Chromium running without its sandbox (once, until a start with the sandbox), an RPC endpoint serving another chain (checked at start). An RPC endpoint silent for five minutes gets one 🛑 per outage, with how long and its last error; another endpoint joining it, or the same one failing again after it answered, gets a new one. | Read the hint; the log has the traceback. |

## Data directory

| File | Safe to delete while stopped? | Effect |
| --- | --- | --- |
| `state.json` | no (you lose the site list) | Back it up; it is the whole configuration you made from Telegram. A damaged one is moved aside at start (`state.json.broken-<time>`, with a 🛑) instead of being overwritten. |
| `state.json.broken-<time>` | yes, once repaired or not needed | A damaged `state.json`: repair it and put it back as `state.json` while the bot is stopped. |
| `snapshots/*.json` | yes | The next round stores a fresh baseline; no alert is sent for the rebuild. Delete one file to reset one page. |
| `known_contracts.txt` | yes | Every contract will be announced again when seen. |
| `rejected_addresses.txt` | yes | Every wallet on the pages is checked again once. Expired entries leave at start and every 1 000 new ones. |
| `evm_networks.txt` | yes | The EVM networks the caches were built with. Without it the next start counts every network as new: cached EVM rejections are forgotten and watched EVM addresses become old news (see Upgrading). |
| `preferences.json` | yes | Live settings go back to `.env`; `/settings reset all` does the same at runtime. |
| `ignore.txt` | yes | Your own stop-list (addresses never announced, one per line, `#` comments); read at start. |
| `pending.json` | yes | Addresses waiting to become a token; after deleting it they are taken up again on the next rounds. |
| `outdated_defaults.txt` | yes | The old `.env` values last reported; after deleting it the next start reports them again. |
| `no_sandbox_notice.txt` | yes | Present while the chat knows that Chromium runs without its sandbox; after deleting it the next such start reports it again. |
| `heartbeat` | yes | Rewritten every 15 s. |

Orphaned snapshots (pages of sites that were removed) are pruned on start, except after
a damaged `state.json`: their sites may come back with the repaired file.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Exit code 2, `Configuration error: …` | A wrong `.env` value; the message names the variable. | Fix the value. |
| Exit code 2, `Telegram rejected TELEGRAM_BOT_TOKEN` | Wrong or revoked token. | New token from @BotFather. |
| Exit code 3, `browser could not start: … Install it with: playwright install chromium` | Chromium missing or an incompatible Playwright. | `playwright install chromium`; on Ubuntu 20.04 install Google Chrome and set `BROWSER_CHANNEL=chrome`. |
| `🚫 Possible IP block …` with `Protection: Vercel Security Checkpoint` or `Cloudflare challenge` | The site's bot protection wants a browser check the headless browser does not pass. | Poll that site less often; the bot only slows down. |
| `🛑 The site list (state.json) was damaged` | A crash of the disk or a hand edit left `state.json` unreadable. It was moved to `state.json.broken-<time>` and the bot started without those sites; their snapshots are kept. | Stop the bot, repair the file (a JSON validator names the line), put it back as `data/state.json`, start again. Or `/add` the sites again. |
| `🛑 Chromium runs without its sandbox` | The sandbox could not start: the bot runs as root, in a container, or where user namespaces are restricted (Ubuntu 23.10+). The browser works, isolated only by the bot's user account. | Run the bot as its own non-root user; try Google Chrome (`BROWSER_CHANNEL=chrome`). `--no-sandbox` in `BROWSER_ARGS` switches the sandbox off on purpose, without a 🛑. |
| A command in a group gets no answer | It was addressed to another bot (`/pause@OtherBot`), or the group is not an allowed chat. | Send it without a mention or with this bot's name. |
| `/add` answers `⚠️ It shares pages with …` | The two sites derive the same pages (`x.io` and `www.x.io`, a path of the same domain, or a subdomain that is also a sub-page pattern). | Keep one of them; otherwise a change on a shared page is reported by only one of the two monitors. |
| `TELEGRAM_CHAT_IDS is empty: alerts will be dropped` at start | No alert chat configured. | Send `/start`, put the id into `.env`, restart. |
| Every page `unavailable: timeout after …` | The site is slow or blocks headless browsers silently. | Wait for the 🐢 alert (the timeout grows), try `HEADLESS=false` to look. |
| `🛑 On-chain checks keep failing` | An RPC endpoint has not answered for five minutes; the message names it, says for how long and quotes its last error (`/status` shows the same from the first minute). Meanwhile every address is re-checked every `RPC_RETRY_DELAY` seconds. When only Arc or Robinhood Chain fails, the other networks keep deciding; an address with no code on them is not cached as rejected but checked again after `RPC_RETRY_DELAY`, and the pending watch keeps it (up to a day, room permitting). | Replace the endpoint in `.env`, or set an EVM URL empty to skip that network. |
| `🛑 An RPC endpoint serves another chain` | A URL in `.env` points at the wrong network (e.g. `BASE_RPC_URL` answers chain 1). | Fix the URL and restart. |
| Change alerts every round for the same element | A slow ticker (minutes) that the two-render check cannot see as volatile. | The noise memory silences it after `flap_threshold` alerts; raise `confirm_delay` for faster tickers. |
| `getUpdates conflict` | Two bots with one token. | Stop the other instance. |
| A site shows `[STOPPED]` in `/list` | Its monitor crashed. It restarts on its own 30 s after each crash; a monitor that crashes again at once stays stopped most of the time. | `/resume <url>` starts a fresh monitor now; the 🛑 alert has the traceback. |

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
  address later. A single response body over 3 MB is not scanned.
- **Headless browser.** Sites see `HeadlessChrome` and `navigator.webdriver`; a strict
  bot shield blocks the bot, which then only slows down. No proxies, no captcha solving.
- **EVM age on fast chains.** "🆕 Deployed within the last …" looks 90 blocks back, as far
  as public nodes keep state. Measured on 2026-09-27 that is 31 min on Fantom, 18 min on
  Ethereum, 4 min on Sonic, 3 min on Base and Optimism, 2 min on Avalanche, 45 s on Arc,
  41 s on BSC, 25 s on Arbitrum and only 9 s on Robinhood Chain, where an older token
  reads "Deployed more than 9s ago".
- **Removed between two renders.** An element that disappears between the two renders
  of a round and stays gone is not reported (one that appears there is, a round later).
- **Tracking parameters.** `utm_*`, click ids (`fbclid`, `gclid`, …), `ref`, `v`, `t`
  and cache busters are stripped from every link before comparing, so a video link that
  changes only its `?v=` is not a change.
- **Sites that share pages** (`x.io` and `www.x.io`, a path or a subdomain of another
  site) render the shared pages twice and keep one snapshot of each: a change there is
  reported by the monitor that sees it first, by none if that site is muted. `/add` warns.
- **Unverified mode** re-checks addresses only while the site's monitor runs; the watch
  window of a paused site keeps running out.
- **Robinhood stock tokens** (TSLA, AAPL, … "• Robinhood Token") are ERC-20 tokens: a page
  that shows one gets one alert for it, like for any other existing token.

## Upgrading

Pull, then `pip install -r requirements-dev.txt` (runtime dependencies plus the test
and lint tools) and
`playwright install chromium` (a new Playwright often needs a new browser build; with
Chrome, `sudo apt install --only-upgrade google-chrome-stable` instead), then `pytest -q`
and restart. Missing
keys in `.env` use the defaults; `⚙️ .env still sets defaults of an earlier release`
at start names the lines that pin an old default.

To 2.1.1 from 2.1:

- Two new networks, on by default: Arc (`ARC_RPC_URL`) and Robinhood Chain
  (`ROBINHOOD_RPC_URL`). They are optional: when their endpoint fails, verdicts come from
  the other networks, and such an address is checked again after `RPC_RETRY_DELAY`
  instead of being cached as rejected. Set a URL empty to skip a network.
- The bot now remembers which EVM networks its caches were built with
  (`data/evm_networks.txt`). On the first start of 2.1.1, and whenever an EVM network is
  added later, it forgets the cached EVM rejections, and the EVM addresses in
  `pending.json` become old news. A page that lists tokens on the new networks
  (flutch.fun on Arc, pons.family on Robinhood Chain) therefore sends one `.txt` document
  and pauses its site on its first round when there are `contract_pause_threshold` (5) or
  more of them; the pause message says it was the page's first check. `/resume` it; those
  tokens are not announced again. Fewer come as single 🔔 alerts saying they were found on
  the first check of the page.
- A token counts as known only once its alert is claimed, so a restart or `/pause` during
  a check can no longer hide it. Token alerts, documents and the pause message wait out
  Telegram flood limits of up to a minute and ride out about two minutes without a
  connection. A failed write of `known_contracts.txt` is logged; the alert still goes out.
- Arc and Robinhood Chain never slow the others down: their answer is awaited half a second
  after the other networks; a token they report later is confirmed at once (by the pending
  watch) or on the next round; an endpoint of theirs that keeps failing is left alone but
  for probes (2 to 30 s apart); and an address checked without them is asked about on them
  alone, in the background. The RPC 🛑 comes once per outage instead of every ten minutes.
- Block alerts name the protection (Cloudflare, Vercel, AWS WAF, DataDome, …) and the
  recovery notice says how long the block lasted; a captcha widget on a page gets one
  🧩 notice. A 2xx page counts as a challenge only by a mitigation header or a title that
  begins like one, and its addresses are read all the same.
- Chromium runs with its sandbox; where it cannot, the bot runs without it and sends one
  🛑 (see Troubleshooting).
- Change detection: a change (an addition or a removal) that happens between the two
  renders of a round is reported by the next round; a static text that turns into a
  countdown is a change; counters in elements without attributes are silenced like any
  other; a page swapped out wholesale (an interstitial) no longer silences all its
  elements for a day; pages with thousands of rows are compared much faster. An element
  without attributes that was silenced before may alert up to `flap_threshold` times again.
- Solana: base64 images, IPFS and Arweave ids, fragments glued to other letters and the
  tokens of captcha answers are no longer taken for addresses; a page that looks like a
  challenge counts only its text and token links.
- URLs: a route after `#/` or `#!/` is kept (`https://app.io/#/presale` is a page of its
  own; the bare `#/` is the site itself); a default port (`:443`, `:80`) is dropped, and a
  site stored with one is stored without it at the first start. A site that 2.1 stored
  without its route is found by `/remove https://app.io/#/presale` too.
- `rebooter.py` pauses before restarting a bot that fails at start and also adopts one
  started with `python -m scraper_bot` or with interpreter options (`-X utf8`). A damaged
  `state.json` is moved aside with a 🛑, the readable sites are saved at once, and while
  the damaged copy is there, snapshots are not pruned. A configuration error never prints
  more of an RPC URL than its host.
- Commands: `/status` counts crashed monitors as stopped, `/add` warns when a site shares
  pages with another, `/remove` also ends the site's turbo session, commands addressed to
  another bot are ignored, long change reports
  keep their formatting, `/turbo … at HH:MM` shows the time zone and the time left,
  `/resume` also restarts a site shown as `[STOPPED]`, `/mute` and `/unmute` answer with
  their own usage line, the empty `TELEGRAM_CHAT_IDS` warning comes once per chat, and the
  🛑 about RPC endpoints quotes each endpoint's last error.
- No new dependency and no `.env` line to change. 2.1 runs on the data directory again
  after a rollback (it ignores the new files and fields); before returning to 2.1.1 after
  running 2.1 again, delete `data/evm_networks.txt`.

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
