# Configuration

Every setting is an environment variable. Precedence, highest first:

1. Values changed at runtime with `/set` (stored in `data/preferences.json`, only for the
   knobs marked **live**; `/settings reset <key|all>` removes them).
2. Real environment variables.
3. The `.env` file in the working directory.
4. The built-in default shown below (identical to `.env.example`; a test keeps them in sync).

Durations are seconds unless the name says otherwise. Live knobs are changed with
`/set <live key> <value>`; the live key is shown in the last column. Everything else
needs a restart.

## Telegram

| Variable | Default | Meaning | Live key |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | – | **Required.** Token from @BotFather. Rejected token = exit code 2. | – |
| `TELEGRAM_CHAT_IDS` | – | Chats that receive alerts, comma separated. Empty = alerts are dropped with a warning; `/start` tells you your chat id. | – |
| `TELEGRAM_ALLOWED_CHAT_IDS` | empty | Chats allowed to run commands. Empty = every chat. Settings and the unverified mode can be changed from these chats, or from `TELEGRAM_CHAT_IDS` when this is empty. | – |

## Monitoring and rendering

| Variable | Default | Meaning | Live key |
| --- | --- | --- | --- |
| `POLL_INTERVAL_MIN` / `POLL_INTERVAL_MAX` | 4 / 10 | Pause between two rounds of one site, random in this range. A round itself takes about 2 s. Values below 3 s ask for `confirm`: most sites rate-limit. | `poll_interval_min`, `poll_interval_max` |
| `MAX_CONCURRENT_PAGES` | 8 | Browser tabs rendering at the same time, shared by all sites and turbo sessions. | – |
| `PAGE_LOAD_TIMEOUT` | 20 | Hard limit for the initial document. Stretched per page after repeated timeouts (see `RENDER_FAILURE_THRESHOLD`). | – |
| `NETWORK_IDLE_TIMEOUT` | 2 | Cap on the wait for the page to stop loading. Pages that go idle are not affected (measured: about 0.8 s); pages that keep polling wait the full cap on every render. 0 = do not wait. | `network_idle_timeout` |
| `SETTLE_DELAY` | 0.25 | Extra pause after network idle so scripts can finish drawing. 0.25 s gave the same result as 0.5 s and 1 s on every measured page. | `settle_delay` |
| `RENDER_FAILURE_THRESHOLD` | 5 | Load timeouts in a row on one page that send one 🐢 alert and double that page's load timeout (up to 3x) until it renders again. 0 = off. | `render_failure_threshold` |
| `CONFIRM_DELAY` | 2 | Seconds between the two renders that confirm a change. Longer delays classify slower tickers as volatile. | `confirm_delay` |
| `CONTRACT_PAUSE_THRESHOLD` | 5 | New confirmed contracts on one page in one round that are sent as a `.txt` document and pause the site until `/resume`. | `contract_pause_threshold` |
| `SUB_URL_PATTERNS` | 7 patterns | Sub-pages derived from every root URL; `{domain}` is the host without `www.`. Probed every round, rendered when they exist. | – |
| `IGNORED_ATTRIBUTES` | nonce, integrity, csrf tokens, ... | HTML attributes never compared. | – |
| `MAX_CHANGE_EXAMPLES` | 5 | Removed / added / changed elements quoted per category in a change alert. 0 = counts only. | `max_change_examples` |
| `ENRICH_TIMEOUT` | 2 | Time budget for token details in a contract alert (name, symbol, decimals, supply, DEX pair). Measured 0.05–0.2 s. 0 = links only. | `enrich_timeout` |
| `FLAP_WINDOW_HOURS` / `FLAP_THRESHOLD` | 24 / 2 | An element that alerted `FLAP_THRESHOLD` times within the window is silenced while it keeps changing. 0 hours = off. | `flap_window_hours`, `flap_threshold` |
| `DEEP_SCAN` | true | Also search iframes, shadow DOM and every script or JSON answer the page loaded. No extra requests. | `deep_scan` |
| `DEEP_SCAN_MAX_MB` | 5 | Byte budget per render for the deep scan (5 MB of JS take about 0.8 s to scan). | – |

## Turbo mode and blocking

| Variable | Default | Meaning | Live key |
| --- | --- | --- | --- |
| `TURBO_INTERVAL` | 2 | Seconds between full renders in a turbo session (minimum 1). A render takes about 1 s on top. | `turbo_interval` |
| `TURBO_DEFAULT_MINUTES` / `TURBO_MAX_MINUTES` | 30 / 180 | Session length without an explicit duration, and the cap for any requested duration. | `turbo_default_minutes`, `turbo_max_minutes` |
| `TURBO_STOP_AFTER` | 1 | Confirmed contracts that end a session. 0 = only the timer or `/turbo stop`. | `turbo_stop_after` |
| `TURBO_MAX_SITES` | 1 | Sites in turbo (running or scheduled) at the same time. | `turbo_max_sites` |
| `TURBO_MAX_PAGES` | 4 | Pages rendered per cycle, the root page included. | `turbo_max_pages` |
| `TURBO_RECHECK_INTERVAL` | 5 | Seconds between on-chain re-checks of the addresses a session watches. | `turbo_recheck_interval` |
| `BLOCK_MAX_BACKOFF` | 600 | Longest pause between rounds while a site looks like it blocks the bot. The pause doubles per blocked round up to this value and honours `Retry-After`. | `block_max_backoff` |

## Unverified mode

| Variable | Default | Meaning | Live key |
| --- | --- | --- | --- |
| `UNVERIFIED_RECHECK_MINUTES` | 5 | How often an announced address is re-checked on-chain. | `unverified_recheck_minutes` |
| `UNVERIFIED_WATCH_HOURS` | 24 | How long it is re-checked before the bot gives up on it. | `unverified_watch_hours` |
| `UNVERIFIED_MAX_PER_HOUR` | 10 | Alerts per hour and site; the overflow becomes one digest message. | `unverified_max_per_hour` |

## Browser

| Variable | Default | Meaning |
| --- | --- | --- |
| `BROWSER_CHANNEL` | chromium | `chromium` (bundled, `playwright install chromium` once) or an installed `chrome` / `msedge`. |
| `HEADLESS` | true | Set to false to watch the browser. |
| `BLOCKED_RESOURCE_TYPES` | image,media,font | Resource types the browser never downloads. |

## Blockchain RPC

| Variable | Default | Meaning | Live key |
| --- | --- | --- | --- |
| `ETH_RPC_URL`, `BASE_RPC_URL`, `ARB_RPC_URL`, `OPT_RPC_URL`, `BSC_RPC_URL`, `FANTOM_RPC_URL`, `SONIC_RPC_URL`, `AVAX_RPC_URL` | public nodes | EVM endpoints checked in parallel for every `0x` address. An empty value skips that network; at least one must remain. | – |
| `SOLANA_RPC_URL` | api.mainnet-beta.solana.com | `getAccountInfo` for base58 addresses: programs and SPL / Token-2022 mints count. | – |
| `SUI_RPC_URL` | sui-rpc.publicnode.com | Coin metadata for `0x…::module::NAME` coin types; a bare 64-hex id must be a package that defines a coin. | – |
| `REQUIRE_ERC20` | true | An EVM contract counts only if it answers `totalSupply`, `symbol` or `decimals`. Off = any bytecode counts (routers, multisigs, proxies too). Turning it off asks for `confirm`. | `require_erc20` |
| `RPC_TIMEOUT` | 5 | Per JSON-RPC call. Every public node answered in 0.05–0.3 s when measured. | – |
| `MAX_CONCURRENT_RPC` | 8 | Address checks in flight at once (each check is 8–11 requests). | – |
| `REJECTED_TTL_HOURS` | 6 | How long an address that had no contract stays rejected before it is checked again. 0 = never (asks for `confirm`). | `rejected_ttl_hours` |
| `RPC_RETRY_DELAY` | 600 | Seconds before an address whose check failed on some endpoint is retried. | – |

## Misc

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATA_DIR` | data | Where state, snapshots, caches and the heartbeat live. |
| `LOG_LEVEL` | INFO | Python logging level. |
| `LOG_FILE` | empty | Optional rotating log file (5 MB, 3 backups). Empty = console only. |
| `HEARTBEAT_INTERVAL` | 15 | Seconds between heartbeat writes; `rebooter.py` restarts the bot after 300 s without one. |

## Validation at start

Wrong values stop the bot with exit code 2 and a message naming the variable:
non-numeric numbers, values below their minimum, booleans that are not one of
`1/true/yes/on/0/false/no/off`, `POLL_INTERVAL_MAX` below `POLL_INTERVAL_MIN`, no EVM
endpoint left, a chat id list that is not integers. Lists tolerate brackets, quotes,
semicolons and spaces.

## Changing settings from Telegram

`/settings` shows every live knob with its current value (`*` marks values that differ
from `.env`), `/settings <key>` explains one knob with its range and default, `/set <key>
<value>` changes it at once, `/settings reset <key|all>` goes back to `.env`. Values are
type- and range-checked, related pairs stay consistent (`poll_interval_min` never above
`poll_interval_max`, `turbo_default_minutes` never above `turbo_max_minutes`), and risky
values (polling below 3 s, `require_erc20 off`, `deep_scan off`, a rejected TTL of 0,
`turbo_interval` below 2 s, more than one turbo site, more than 8 turbo pages, a block
backoff below 60 s) must be repeated with `confirm`.
