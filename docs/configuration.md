# Configuration

Every setting is an environment variable. Precedence, highest first:

1. Values changed at runtime with `/set` (stored in `data/preferences.json`, only for the
   knobs marked **live**; `/settings reset <key|all>` removes them).
2. Real environment variables.
3. The `.env` file in the working directory.
4. The built-in default shown below (identical to `.env.example`; a test keeps them in sync).

Put into `.env` only the values you want to differ from the default. A `.env` copied whole
from an older `.env.example` pins that release's defaults. The bot names each key still
set to a default of 2.0 (`SOLANA_RPC_URL`, `RPC_RETRY_DELAY`, `TURBO_RECHECK_INTERVAL`,
`DEEP_SCAN`) or of the builds before it (polling, rendering, RPC and token-detail
timings, tab count, deep-scan budget), in the log at
every start and in one Telegram message per set of such values.

Durations are seconds unless the name says otherwise. Live knobs are changed with
`/set <live key> <value>`; the live key is shown in the last column. Everything else
needs a restart.

## Telegram

| Variable | Default | Meaning | Live key |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | – | **Required.** Token from @BotFather. Rejected token = exit code 2. | – |
| `TELEGRAM_CHAT_IDS` | – | Chats that receive alerts, comma separated. Empty = alerts are dropped with a warning; `/start` tells you your chat id. | – |
| `TELEGRAM_ALLOWED_CHAT_IDS` | empty | Chats allowed to run commands. Empty = the alert chats in `TELEGRAM_CHAT_IDS`. Any other chat only learns its chat id from `/start`. | – |

## Monitoring and rendering

| Variable | Default | Meaning | Live key |
| --- | --- | --- | --- |
| `POLL_INTERVAL_MIN` / `POLL_INTERVAL_MAX` | 4 / 10 | Pause between two rounds of one site, random in this range. A round itself takes about 2 s. `/set poll_interval_min` below 3 s asks for `confirm` (most sites rate-limit); `.env` accepts down to 1 s without asking. | `poll_interval_min`, `poll_interval_max` |
| `MAX_CONCURRENT_PAGES` | 8 | Browser tabs rendering at the same time, shared by all sites and turbo sessions. | – |
| `PAGE_LOAD_TIMEOUT` | 20 | Hard limit for the initial document, and for opening the tab and reading the page once it loaded (a page that stops answering times out too). Stretched per page after repeated timeouts (see `RENDER_FAILURE_THRESHOLD`). | – |
| `NETWORK_IDLE_TIMEOUT` | 2 | Cap on the wait for the page to stop loading. Pages that go idle are not affected (measured: about 0.8 s); pages that keep polling wait the full cap on every render. Token alerts do not wait for it: an address is reported the moment the loading page shows it. 0 = do not wait. | `network_idle_timeout` |
| `SETTLE_DELAY` | 0.25 | Extra pause after network idle so scripts can finish drawing. 0.25 s gave the same result as 0.5 s and 1 s on every measured page. | `settle_delay` |
| `RENDER_FAILURE_THRESHOLD` | 5 | Load timeouts in a row on one page that send one 🐢 alert and double that page's load timeout (up to 3x) until it renders again. 0 = off. | `render_failure_threshold` |
| `CONFIRM_DELAY` | 2 | Seconds between the two renders that confirm a change. Longer delays classify slower tickers as volatile. A render that differs only in elements already seen ticking (a timer, a clock) needs no second render. | `confirm_delay` |
| `LIGHT_INTERVAL` | 1 | Light mode: between renders every live page's HTML and the site's own JSON APIs (the ones a render saw the page call, at most 2 per page) are asked over plain HTTP this often, conditionally (mostly a 304 without a body). A new address there is checked and announced at once; one an API adds before the page shows it comes as "Found in: site API". A site that refuses plain requests is asked with fetch() from a tab kept on it. 0 = off. Below 0.5 s asks for `confirm`. | `light_interval` |
| `RENDER_INTERVAL` | 30 | A page whose HTML carries what its render shows (80% of its texts), and that neither ticks nor polls on its own, is fully rendered only this often, for change detection; the light mode watches it meanwhile, and a new deploy (other scripts in its HTML) renders it at once. Other pages are rendered every round. Minimum 4. | `render_interval` |
| `CONTRACT_PAUSE_THRESHOLD` | 5 | New confirmed contracts on one page in one round that are sent as a `.txt` document and pause the site until `/resume`. | `contract_pause_threshold` |
| `SUB_URL_PATTERNS` | 7 patterns | Sub-pages derived from every root URL; `{domain}` is the host without `www.`. Probed every round, rendered when they exist. A pattern whose page always answers 401/403 keeps its site in block backoff: leave it out. An empty value means the default list. | – |
| `IGNORED_ATTRIBUTES` | nonce, integrity, csrf tokens, ... | HTML attributes never compared. An empty value means the default list. | – |
| `MAX_CHANGE_EXAMPLES` | 5 | Removed / added / changed elements quoted per category in a change alert. 0 = counts only. | `max_change_examples` |
| `ENRICH_TIMEOUT` | 2 | Time budget for the token details edited into a token alert after it went out (name, symbol, decimals, supply, Solana authorities and risky extensions, age, the other networks where the address is a token too, DEX pair). The alert itself never waits for them. Measured 0.05–0.4 s. 0 = links only. | `enrich_timeout` |
| `FLAP_WINDOW_HOURS` / `FLAP_THRESHOLD` | 24 / 2 | An element that alerted `FLAP_THRESHOLD` times within the window is silenced while it keeps changing. 0 hours = off. | `flap_window_hours`, `flap_threshold` |
| `DEEP_SCAN` | false | Also search iframes, shadow DOM and every script or JSON answer the page loaded. No extra requests. Off by default: wallet kits ship dozens of token addresses in their bundles. When on, an address the page's loaded scripts and JSON carried on its first deep round is ignored until the page itself shows it (iframes and shadow DOM count as the page). A single response body over 3 MB is not scanned. | `deep_scan` |
| `DEEP_SCAN_MAX_MB` | 5 | Byte budget per render for the deep scan (5 MB of JS take about 0.8 s to scan). | – |

## Turbo mode and blocking

| Variable | Default | Meaning | Live key |
| --- | --- | --- | --- |
| `TURBO_INTERVAL` | 2 | Seconds from the start of one turbo cycle to the start of the next (minimum 1); a cycle that takes longer starts the next one at once. | `turbo_interval` |
| `TURBO_DEFAULT_MINUTES` / `TURBO_MAX_MINUTES` | 30 / 180 | Session length without an explicit duration, and the cap for any requested duration. | `turbo_default_minutes`, `turbo_max_minutes` |
| `TURBO_STOP_AFTER` | 1 | Confirmed contracts that end a session. 0 = only the timer or `/turbo stop`. | `turbo_stop_after` |
| `TURBO_MAX_SITES` | 1 | Sites in turbo (running or scheduled) at the same time. | `turbo_max_sites` |
| `TURBO_MAX_PAGES` | 4 | Pages rendered per cycle, the root page included. | `turbo_max_pages` |
| `LIGHT_TURBO_INTERVAL` | 0.3 | The light mode's pace while a turbo session runs for the site (minimum 0.1). Below 0.3 s asks for `confirm`. | `light_turbo_interval` |
| `TURBO_RECHECK_INTERVAL` | 1 | Seconds between on-chain checks of a turbo site's addresses that are not a token yet (the pending watch). Elsewhere an address the page names as the CA is checked every second for 15 min, then every 5 s up to an hour, then every 30 s; any other address every 30 s. | `turbo_recheck_interval` |
| `BLOCK_MAX_BACKOFF` | 600 | Longest pause between rounds while a site looks like it blocks the bot. The pause doubles per blocked round up to this value; a longer `Retry-After` from the site is honoured as sent. Meanwhile each round first sends one plain GET to the root page and renders nothing while that is refused (every fourth blocked round renders anyway). | `block_max_backoff` |

## Unverified mode

| Variable | Default | Meaning | Live key |
| --- | --- | --- | --- |
| `UNVERIFIED_RECHECK_MINUTES` | 5 | How often an announced address is re-checked on-chain. | `unverified_recheck_minutes` |
| `UNVERIFIED_WATCH_HOURS` | 24 | How long it is re-checked before the bot gives up on it. | `unverified_watch_hours` |
| `UNVERIFIED_MAX_PER_HOUR` | 10 | Alerts per hour and site; the overflow becomes one digest message. | `unverified_max_per_hour` |

## Browser

| Variable | Default | Meaning |
| --- | --- | --- |
| `BROWSER_CHANNEL` | chromium | `chromium` (bundled, `playwright install chromium` once) or an installed `chrome` / `msedge`. `bundled` runs Playwright's old headless shell, whose client hints still say HeadlessChrome. |
| `HEADLESS` | true | Set to false to watch the browser. |
| `HIDE_AUTOMATION` | true | Read pages like a browser a person runs: the User-Agent without "Headless" (version and platform stay the browser's; the HTTP clients send the same one), `navigator.webdriver` false, one language (`en-US`), and cookies and local storage kept in `data/browser_state.json` across the hourly browser restart and bot restarts, so a Cloudflare or Vercel check the browser passed on its own is not asked again. No captcha solving, no proxies. `false` = the browser's own User-Agent and flags, cookies lost at every restart. |
| `BLOCKED_RESOURCE_TYPES` | image,media,font | Resource types the browser never downloads (an empty value means the default list). Next.js prefetches (`Next-Router-Prefetch`, `Next-Router-Segment-Prefetch`, `(Sec-)Purpose: prefetch`, and `_rsc=` fetches of other pages, which links with `prefetch={true}` send without a header; the page's own `_rsc=` fetches pass) and analytics beacons (Google Analytics and Tag Manager, Vercel Insights, Cloudflare Web Analytics, Segment, Mixpanel, Hotjar, Clarity, PostHog, Sentry) are always dropped; Vercel's checkpoint (`/.well-known/vercel/`) always passes. |
| `BROWSER_ARGS` | `--disable-3d-apis --disable-webgl --disable-webgl2` | Extra Chromium switches, split like a shell does (`'--x=a,b'` stays whole). The default turns 3D off: without a GPU, WebGL is drawn by the CPU. An empty value passes none. Chromium runs with its sandbox unless `--no-sandbox` is among them; where the sandbox cannot start the bot runs without it and sends one 🛑. |

## Blockchain RPC

| Variable | Default | Meaning | Live key |
| --- | --- | --- | --- |
| `ETH_RPC_URL`, `BASE_RPC_URL`, `ARB_RPC_URL`, `OPT_RPC_URL`, `BSC_RPC_URL`, `FANTOM_RPC_URL`, `SONIC_RPC_URL`, `AVAX_RPC_URL` | public nodes | EVM endpoints checked in parallel for every `0x` address. An empty value skips that network; at least one EVM network must remain. At start each endpoint's `eth_chainId` is checked. | – |
| `ARC_RPC_URL`, `ROBINHOOD_RPC_URL` | arc-rpc.publicnode.com, robinhood-rpc.publicnode.com | Arc (Circle, chain 5042) and Robinhood Chain (chain 4663), checked like the others but **optional**: when their endpoint fails, the other networks' answers still decide, and an address that has no code elsewhere is re-checked there by the pending watch. The official `rpc.mainnet.arc.io` refuses calls without an API key. An empty value skips the network. | – |
| `SOLANA_RPC_URL` | solana-rpc.publicnode.com | One `getMultipleAccounts` (at `confirmed` commitment) for a base58 address and its Metaplex metadata account: SPL / Token-2022 mints count; programs, wallets and NFTs (decimals 0, supply 1) do not. | – |
| `SUI_RPC_URL` | sui-rpc.publicnode.com | Coin metadata for `0x…::module::NAME` coin types; a bare 64-hex id must be a package that defines a coin. | – |
| `REQUIRE_ERC20` | true | An EVM contract counts only if it answers `totalSupply` and `decimals` and is neither an LP pair or pool (its non-quote token is checked instead) nor an ERC-4626 vault. Off = any bytecode counts (routers, multisigs, proxies too). EIP-7702 wallets never count. Turning it off asks for `confirm`. | `require_erc20` |
| `SKIP_STABLE_COPIES` | true | A token whose symbol reads as a stablecoin's or a wrapped, native or staked coin's (USDC, USDT, USDC.e, EURC, DAI, WETH, WBTC, stETH, ... 50 in all) at an address not on the stop-list is a copy (bridged, plumbing or a scam) and is never announced. Only the whole symbol counts, after folding case, styled letters, invisible characters, Cyrillic and Greek look-alikes and ₮ (`USDС` matches, `USDCAT` does not); an unreadable symbol counts as none. Judged per network: the same address with another symbol elsewhere is announced there. A copy everywhere is rejected and cached like any non-token. On EVM and Sui only with `REQUIRE_ERC20` on. Turning it off asks for `confirm`. | `skip_stable_copies` |
| `RPC_TIMEOUT` | 5 | Per JSON-RPC call, retries included: HTTP 429 (honouring `Retry-After`), 500/502/503/504, refused or reset connections, HTTP/2 protocol errors and rate-limit errors are retried after 0.25 s and 1 s; a timeout is not. Every public node answered in 0.05–0.3 s when measured. | – |
| `MAX_CONCURRENT_RPC` | 8 | Address checks in flight at once (each is one `eth_getCode` per network plus, where code exists, one batch: the bytecode again and six `eth_call` probes). | – |
| `HTTP2` | true | RPC and Telegram over HTTP/2, so the parallel calls of a check share one warm connection. Sub-page probes always use HTTP/1.1. `false` = HTTP/1.1 everywhere. | – |
| `REJECTED_TTL_HOURS` | 6 | How long an address that had no contract stays rejected before it is checked again. 0 = never (asks for `confirm`). | `rejected_ttl_hours` |
| `RPC_RETRY_DELAY` | 10 | Seconds before an address is checked again when its check failed on a required network (or on every network). When only an optional network did not answer, that network alone is asked again after this delay, in the background (such a rejection is not cached until it answers). An endpoint that stays silent for 5 minutes (an optional one too) is reported with one 🛑 per outage. | – |

## Misc

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATA_DIR` | data | Where state, snapshots, caches and the heartbeat live. With another directory start the supervisor with `--heartbeat <DATA_DIR>/heartbeat`. |
| `LOG_LEVEL` | INFO | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `LOG_FILE` | empty | Optional rotating log file (5 MB, 3 backups). Empty = console only. |
| `HEARTBEAT_INTERVAL` | 15 | Seconds between heartbeat writes; `rebooter.py` restarts the bot after 300 s without one. |

## Validation at start

Wrong values stop the bot with exit code 2 and a message naming the variable:
non-numeric or non-finite (`nan`, `inf`) numbers, values below their minimum, an unknown
`LOG_LEVEL`, booleans that are not one of
`1/true/yes/on/0/false/no/off`, `POLL_INTERVAL_MAX` below `POLL_INTERVAL_MIN`, no EVM
endpoint left, an RPC URL that is not `http(s)://` with a host (a typo such as `:8545x`),
a chat id list that is not integers, a `BROWSER_ARGS` value with unbalanced quotes.
Lists tolerate brackets, quotes,
semicolons and spaces. An empty `SOLANA_RPC_URL`, `SUI_RPC_URL`, `DATA_DIR` or `LOG_LEVEL`
means the default (only an empty EVM URL skips its network); in `BROWSER_ARGS` on Windows a
backslash is part of a path, not an escape.

## Changing settings from Telegram

`/settings` shows every live knob with its current value (`*` marks values that differ
from `.env`), `/settings <key>` explains one knob with its range and default, `/set <key>
<value>` changes it at once, `/settings reset <key|all>` goes back to `.env`
(`/settings reset` alone resets all). Values are
type- and range-checked, related pairs stay consistent (`poll_interval_min` never above
`poll_interval_max`, `turbo_default_minutes` never above `turbo_max_minutes`), and risky
values (polling below 3 s, light polls below 0.5 s or in turbo below 0.3 s, `require_erc20 off`, `skip_stable_copies off`, `deep_scan on`, a rejected TTL of 0,
`flap_window_hours 0`,
`turbo_interval` below 2 s, more than one turbo site, more than 8 turbo pages, a block
backoff below 60 s) must be repeated with `confirm`.
