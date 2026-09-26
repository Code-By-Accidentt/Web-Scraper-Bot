# Architecture

The bot is one asyncio process that renders web pages in a shared headless Chromium,
compares each page with its stored snapshot, looks for smart-contract addresses in
everything the page loaded, confirms them on-chain and talks to you through Telegram.
This page explains how the pieces fit together and why they are shaped the way they are.

## Package map

```text
scraper_bot/
  app.py            composition root: build_runtime() wires everything, Runtime serves it
  config.py         Settings: every knob read once from the environment and .env
  preferences.py    live overrides of the tunable knobs (/settings, /set), the Tuning protocol
  models.py         plain data types shared by every package
  storage.py        state.json, page snapshots, address caches, atomic writes, v1 import

  pages/            rendering a page and telling real changes from noise
    fetcher.py        PlaywrightFetcher (one browser, a tab per fetch, deep scan) + HttpProber
    html_normalizer.py  rendered HTML -> ordered list of Node(tag, attrs, own text)
    change_detector.py  ordered diff of two node lists, volatile-node exclusion
    noise_filter.py     memory of elements that keep changing (flapping)
    blocking.py         recognises 401/403/429/503, WAF challenge pages, connection resets
    page_checker.py     the per-page pipeline: render -> addresses -> diff -> snapshot
    sightings.py        where on a page each address stands and the words in front of it
    urls.py             URL normalisation, sub-page derivation, snapshot file names

  chains/           addresses and blockchains
    address_extractor.py  regexes + syntactic validation (EIP-55, base58, Sui coin types)
    stoplist.py           stablecoins, wrapped natives, system addresses: never candidates
    rpc.py                JSON-RPC client: quick retries, HTTP 429 cool-down, batches
    warmup.py             keeps RPC connections warm, checks each EVM endpoint's chain id
    evm.py / solana.py / sui.py   chain constants and helpers shared below
    contract_validators.py        EVM, Solana and Sui validators + the caching facade
    enrichment.py                 token details for alerts: metadata, DexScreener, links

  monitoring/       the loops
    site_monitor.py     rounds for one root site and its sub-pages, block backoff, timeout streaks
    monitor_manager.py  add / remove / pause / resume, one task per site, crash restart
    alerts.py           AlertRegistry: one alert per token across pages, sites and modes
    pending.py          PendingWatch: addresses published before deployment, checked in batches
    turbo.py            TurboManager + TurboSession: render every few seconds, hunt tokens
    unverified.py       UnverifiedWatcher: announce first, confirm on-chain later

  telegram/         what the user sees
    client.py         Bot API over httpx: HTML messages, plain-text fallback, documents, polling
    commands.py       command parsing and replies, the long-polling listener
    notifier.py       every alert the bot sends on its own initiative
    status.py         the /status report
    markup.py         HTML helpers: escape, code, pre, link
    timefmt.py        durations and clock times for people

main.py             `python main.py` (kept for the supervisor and old habits)
rebooter.py         supervisor: restarts the bot when it exits or stops heart-beating
```

Dependencies point downwards: `telegram/` and `monitoring/` use `pages/` and `chains/`,
and `pages/` uses `chains/` for extraction and validation. Exceptions:
`chains/contract_validators.py` takes a `Notifier` (and `telegram/timefmt.py`) to report
failing RPC endpoints, `storage.py` uses `pages/urls.py` and `chains/address_extractor.py`,
and `preferences.py` uses `telegram/markup.py`.

## The life of a round

`SiteMonitor.run_once()` is the heart of the bot; every monitored site runs it in its
own asyncio task, then sleeps for a random `poll_interval_min`..`poll_interval_max`.

```text
SiteMonitor.run_once
 │
 ├─ HttpProber.probe(sub-page) x7      HEAD (GET after 404/405/410/501); 404/410 or any
 │                                     transport error (DNS, connection, TLS, 10 s timeout)
 │                                     means "does not exist"
 │
 ├─ PageChecker.check(page)  for the root and every live sub-page, concurrently
 │   ├─ PlaywrightFetcher.fetch        tab from the shared browser (max MAX_CONCURRENT_PAGES),
 │   │                                 domcontentloaded -> network idle (capped) -> settle;
 │   │                                 prefetches and analytics beacons are dropped; deep scan
 │   │                                 (off by default) collects frames, shadow roots, script/JSON
 │   ├─ BlockDetector.inspect          a challenge or ban page is never diffed
 │   ├─ HtmlNormalizer.signature       ordered nodes with own text and cleaned attributes
 │   ├─ find_sightings                 every address with where it stood (text, link, page
 │   │                                 source, loaded script) and the words in front of it;
 │   │                                 script-only addresses from the page's first deep round
 │   │                                 are skipped (Snapshot.deep_seen)
 │   ├─ ContractValidator.validate_many  cache first, then RPC; tokens behind LP pairs too
 │   ├─ on_observation  ─────────────► SiteMonitor announces the tokens NOW (AlertRegistry:
 │   │                                 one alert per token), before any comparison
 │   ├─ snapshot missing?  -> BASELINE (store it)
 │   ├─ identical?         -> UNCHANGED
 │   └─ differs:  sleep confirm_delay, render again
 │        ├─ new addresses of the second render -> on_observation again
 │        ├─ ChangeDetector.volatile_nodes(first, second)   what moved between two renders
 │        ├─ ChangeDetector.compare(snapshot, second, ignore=volatile)
 │        ├─ NoiseFilter.apply           drop elements that already alerted flap_threshold times
 │        └─ store the second render as the new snapshot  -> CHANGED or UNCHANGED
 │
 ├─ per outcome: announce a new sub-page, the change report (unless muted), feed
 │   candidates to the UnverifiedWatcher, count load timeouts
 ├─ UnverifiedWatcher.recheck          re-validate watched addresses that are due
 └─ update block state (backoff) and /status counters
```

A token alert goes out from a task the `Notifier` owns, the moment a render shows the
token: a later cancellation of the round cannot lose it (its token is already in the
known cache) and a Telegram rate limit cannot hold up the page. The token's details
(name, supply, Solana authorities and Token-2022 flags, age, other networks, DEX pair)
follow as an edit of the same message. From `contract_pause_threshold` tokens on one page
in one round (both renders) they go out as a `.txt` document and the site pauses.

The heavy synchronous steps (parsing, finding addresses, the diffs) run in worker
threads, each with its own lxml parser, so the other monitors and the Telegram listener
stay responsive on pages with thousands of nodes.

## Rules that shape the design

**A change needs two renders.** Anything that differs between two renders taken
`confirm_delay` seconds apart (timers, counters, rotating banners) is volatile and is
excluded from the report together with its previous value from the snapshot. The
snapshot always advances to the second render, so noise never accumulates. If one
render is much smaller than the other (a truncated load) the page is skipped for this
round as UNSTABLE and the snapshot is kept.

**Slow noise has a memory.** Content that changes slower than the confirm delay would
pass the two-render check every time. `NoiseFilter` counts alerts per element (tag,
attributes and the element's position among look-alikes; attribute-less elements are
keyed by tag and first word) inside `flap_window`; after `flap_threshold` alerts the
element is silenced for as long as it keeps changing. The memory lives in the page's
snapshot file and expires on its own.

**Well-known addresses never become candidates.** `StopList` (`chains/stoplist.py`) holds
stablecoins, wrapped natives, bridged BTC/ETH, liquid-staking tokens (stETH, cbETH,
jitoSOL, mSOL, ...) and a few chain tokens (ARB) on every configured chain (each one
checked on-chain), the OP-stack predeploys, every EVM address below `0x10000`, the burn
address and the native-coin placeholder, plus the operator's own `data/ignore.txt`. The
extractor drops them, so wallet-connect bundles cannot flood a first round.

**An address is checked once.** `ContractValidator` remembers verdicts: confirmed
tokens forever (`known_contracts.txt`), rejected addresses for `rejected_ttl`
(`rejected_addresses.txt`), addresses whose RPC check failed for `rpc_retry_delay`.
Concurrent checks of the same address share one lookup. Every verdict carries a
`kind`: an EVM address is a token when a network returns bytecode and, with
`require_erc20`, answers `totalSupply()` and `decimals()` without being an LP pair
(`token0`/`token1`, whose non-quote tokens are checked instead) or an ERC-4626 vault
(`asset()`); `0xef0100…` code is an EIP-7702 wallet. A Solana address counts only as an
SPL / Token-2022 mint read at `confirmed` commitment (a node's default, `finalized`,
trails by about 13 s). A transport failure or rate limit on any network means "no
verdict" rather than a cached rejection, because a wrong rejection would hide a real
launch for hours.

**Addresses published before deployment are watched, not forgotten.** Whatever a check
finds that may still become a token (no bytecode anywhere, no Solana account yet, a
decimals-0 mint with nothing minted, or no verdict) goes to `PendingWatch`. It checks in
batches (one `eth_getCode` batch per network, one `getMultipleAccounts` per 100 Solana
accounts): every second for a quarter of an hour when the page names the address as the
token (CA, contract, mint, ...), then every 5 s up to an hour and every 30 s up to a day;
other addresses every 30 s. While its site is in turbo, every address of the site is
checked at least every `turbo_recheck_interval`. Wallets leave at once: an EVM address
that has sent a transaction or delegates via EIP-7702 has a key and never becomes a
contract, and a Solana address holding an account that is not a mint never becomes one.
When the address turns into a token it is validated like any other and announced with
"deployed between" in its ⏱ line (only when a check saw the network empty before; the
bytecode is then validated on the networks that show it, outside the queue of checks).
An address shown by several sites is checked while any of them runs or is in turbo, and
a turbo session on any of them hears about the token. An address given up on after a
day comes back when a page names it as the CA or turbo sees it; wallets stay out for a
week. Sui is not watched: a package id cannot be known before it is published.

**Turbo announces confirmed tokens only.** What a page shows on its first good render
(the first cycle, or later for a page that failed to load at first) is old news if it
exists already; what does not exist yet goes to the pending watch, and turns into news
only on a network where a check saw it absent. A token the regular monitor sees appear
between two of its renders, or on a sub-page that goes live, counts for the session too. Later addresses are
checked at once: a token is announced, anything that may still become one goes to the
watch at the turbo pace (`turbo_recheck_interval`). `/unverified <url> on` remains the one
place where addresses are announced before they are tokens; it validates with
`negative_cache=False`, so earlier rejections are ignored, and its `✅ Confirmed` claims
the `AlertRegistry` like every token alert.

**Blocks slow the bot down, nothing else.** A block signal (HTTP 401/403/407/429/503,
a WAF challenge title such as "Just a moment" or "Vercel Security Checkpoint",
`cf-mitigated: challenge`, `x-vercel-mitigated`, connection resets) doubles the pause
between rounds up to `block_max_backoff`, honours `Retry-After`, sends one warning and
one recovery notice. There is no proxy rotation and no captcha handling by design.

**Pages that keep timing out get more time, once.** After `render_failure_threshold`
load timeouts in a row on one page you get one 🐢 alert and the page's load timeout is
stretched (x2, then x3); a successful render resets it and says so.

## Persistent state

| File | Written by | Content |
| --- | --- | --- |
| `data/state.json` | `StateStore` | every site: url, paused, notifications flag, announced sub-pages, last change, unverified mode and its memory |
| `data/snapshots/<host_path>-<hash>.json` | `SnapshotStore` | one per page: the last accepted node list, the noise memory and, once deep scan has seen the page, the addresses its scripts carried then (`deep_seen`) |
| `data/known_contracts.txt` | `AddressCache` | confirmed contracts, tab separated, append-only |
| `data/rejected_addresses.txt` | `AddressCache` | rejected addresses with a timestamp, compacted on start |
| `data/preferences.json` | `Preferences` | live overrides made with `/set` |
| `data/ignore.txt` | you | optional: addresses never to announce, one per line, `#` comments |
| `data/pending.json` | `PendingWatch` | addresses waiting to become a token (sites, networks seen empty), and dropped ones remembered for a week (wallets and non-tokens refused; expired ones refused unless named as the CA or seen by turbo) |
| `data/outdated_defaults.txt` | `Application` | the old `.env` values last reported in Telegram, so a restart does not report them again |
| `data/heartbeat` | `Heartbeat` | epoch seconds, every `HEARTBEAT_INTERVAL`, read by `rebooter.py` |

State, snapshots, preferences and the pending watch are written through
`atomic_write_text` (write to a temp file, then replace), so a crash never leaves a
half-written file; the address caches are append-only.

## Concurrency and resources

- One event loop. One Chromium for the whole process, relaunched transparently if it
  crashes; a tab per fetch; at most `MAX_CONCURRENT_PAGES` renders at once.
- At most `MAX_CONCURRENT_RPC` address checks in flight. An EVM check asks every network
  at once and answers as soon as one of them finds a token (or an LP pair); the slower
  requests finish in the background (at most 64 of them) instead of being cancelled,
  because cancelling would drop a warm connection.
- Two httpx clients: one for RPC, Telegram and DexScreener (HTTP/2, idle connections kept
  60 s, `ConnectionWarmer` calls every RPC endpoint (EVM, Solana, Sui) every 30 s and
  checks each EVM endpoint's
  chain id at start), one plain HTTP/1.1 client for the sub-page probes, so an HTTP/2
  protocol error can never make a live page look absent.
- `JsonRpcClient` retries quick failures (HTTP 429 with `Retry-After`, 500/502/503/504,
  refused or reset connections, HTTP/2 protocol errors, rate-limit errors inside a 200,
  also inside a batch answer) within `RPC_TIMEOUT`, a hard deadline; after a 429 every
  client of the HTTP pool leaves that host alone; batches carry up to 50 calls, and a node
  that refuses a batch gets single calls for ten minutes (the token probe then asks about
  one block number, so that nodes behind a load balancer agree). `EndpointHealth` remembers since when an endpoint fails; one 🛑
  goes out only after five minutes of failures (the latest within the last minute), and
  `/status` lists endpoints failing for a minute or more.
- Per site one monitor task; turbo sessions and their timers are separate tasks;
  fire-and-forget notifications from task callbacks go through `Notifier.report_later`.
- Shutdown (Ctrl-C or SIGTERM, even during the start) order: Telegram listener,
  heartbeat and warm-up, the pending watch saved, turbo sessions and monitors (8 s each
  at most, their summaries included), the pending watch stopped and saved again, alerts
  and documents on their way (up to 10 s), RPC checks still running, browser, HTTP
  clients: within the 30 s the supervisor grants. A cancelled command (Ctrl-C during
  `/remove`) is never swallowed.

## Configuration layering

`Settings` (`config.py`) is built once from the environment and `.env`. `Preferences`
(`preferences.py`) layers validated overrides from `/set` on top and exposes the
effective values under the same attribute names. Everything that may change at runtime
reads a `Tuning` (a Protocol) at use time, so a `/set` applies at once (a running round
picks it up at its next use) without a restart. Knobs that need a restart (browser, concurrency, RPC endpoints, paths) live
only in `Settings`.

## Seams for tests

The whole bot can be assembled on fakes: `build_runtime()` takes the HTTP client, the
Telegram client, the notifier and the page renderer (`Browser` protocol) as arguments,
and `SiteMonitor` (sleep, random generator), `TurboSession` / `TurboManager` /
`PageChecker` / `PendingWatch` (clock and sleep) accept injected timing. `tests/conftest.py` holds the fakes (`FakeFetcher`, `FakeTelegram`,
`FakeValidator`, `FakeNotifier`, ...), and `tests/test_end_to_end.py` drives the wired
bot through Telegram commands with `httpx.MockTransport` answering JSON-RPC.
