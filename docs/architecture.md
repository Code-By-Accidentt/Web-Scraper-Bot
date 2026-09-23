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
    urls.py             URL normalisation, sub-page derivation, snapshot file names

  chains/           addresses and blockchains
    address_extractor.py  regexes + syntactic validation (EIP-55, base58, Sui coin types)
    rpc.py                tiny JSON-RPC client
    evm.py / solana.py / sui.py   chain constants and helpers shared below
    contract_validators.py        EVM, Solana and Sui validators + the caching facade
    enrichment.py                 token details for alerts: metadata, DexScreener, links

  monitoring/       the loops
    site_monitor.py     rounds for one root site and its sub-pages, block backoff, timeout streaks
    monitor_manager.py  add / remove / pause / resume, one task per site, crash restart
    turbo.py            TurboManager + TurboSession: render every few seconds, hunt addresses
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
which use only the root modules. The one exception is `chains/contract_validators.py`,
which takes a `Notifier` to report failing RPC endpoints.

## The life of a round

`SiteMonitor.run_once()` is the heart of the bot; every monitored site runs it in its
own asyncio task, then sleeps for a random `poll_interval_min`..`poll_interval_max`.

```text
SiteMonitor.run_once
 │
 ├─ HttpProber.probe(sub-page) x7      HEAD (GET after 404/405/410/501); only 404/410 and
 │                                     DNS or connection failures mean "does not exist"
 │
 ├─ PageChecker.check(page)  for the root and every live sub-page, concurrently
 │   ├─ PlaywrightFetcher.fetch        tab from the shared browser (max MAX_CONCURRENT_PAGES),
 │   │                                 domcontentloaded -> network idle (capped) -> settle;
 │   │                                 deep scan collects frames, shadow roots, script/JSON bodies
 │   ├─ BlockDetector.inspect          a challenge or ban page is never diffed
 │   ├─ AddressExtractor.extract_many  every 0x / base58 / Sui candidate in all collected text
 │   ├─ ContractValidator.validate_many  cache first, then RPC (see below)
 │   ├─ HtmlNormalizer.signature       ordered nodes with own text and cleaned attributes
 │   ├─ snapshot missing?  -> BASELINE (store it)
 │   ├─ identical?         -> UNCHANGED
 │   └─ differs:  sleep confirm_delay, render again
 │        ├─ ChangeDetector.volatile_nodes(first, second)   what moved between two renders
 │        ├─ ChangeDetector.compare(snapshot, second, ignore=volatile)
 │        ├─ NoiseFilter.apply           drop elements that already alerted flap_threshold times
 │        └─ store the second render as the new snapshot  -> CHANGED or UNCHANGED
 │
 ├─ per outcome: announce a new sub-page, contracts (one message each, or a .txt document
 │   plus auto-pause at contract_pause_threshold), the change report (unless muted),
 │   feed candidates to the UnverifiedWatcher, count load timeouts
 ├─ UnverifiedWatcher.recheck          re-validate watched addresses that are due
 └─ update block state (backoff) and /status counters
```

The two heavy synchronous steps, the diffs, run in a worker thread so the other
monitors and the Telegram listener stay responsive on pages with thousands of nodes.

## Rules that shape the design

**A change needs two renders.** Anything that differs between two renders taken
`confirm_delay` seconds apart (timers, counters, rotating banners) is volatile and is
excluded from the report together with its previous value from the snapshot. The
snapshot always advances to the second render, so noise never accumulates. If one
render is much smaller than the other (a truncated load) the round is skipped as
UNSTABLE and the snapshot is kept.

**Slow noise has a memory.** Content that changes slower than the confirm delay would
pass the two-render check every time. `NoiseFilter` counts alerts per element (tag,
attributes and the element's position among look-alikes; attribute-less elements are
keyed by tag and first word) inside `flap_window`; after `flap_threshold` alerts the
element is silenced for as long as it keeps changing. The memory lives in the page's
snapshot file and expires on its own.

**An address is checked once.** `ContractValidator` remembers verdicts: confirmed
contracts forever (`known_contracts.txt`), rejected addresses for `rejected_ttl`
(`rejected_addresses.txt`), addresses whose RPC check failed for `rpc_retry_delay`.
Concurrent checks of the same address share one lookup. An EVM address is a contract
when any configured network returns bytecode; with `require_erc20` it must also answer
one ERC-20 call. A transport failure or rate limit on any network means "no verdict"
rather than a cached rejection, because a wrong rejection would hide a real launch for
hours.

**Turbo and unverified mode bypass the negative cache.** Both exist for the one case
the regular monitor cannot catch: an address published before its contract is
deployed. They validate with `negative_cache=False`, so earlier rejections are ignored
and new ones are not remembered.

**Blocks slow the bot down, nothing else.** A block signal (HTTP 401/403/407/429/503,
a WAF challenge title, `cf-mitigated: challenge`, connection resets) doubles the pause
between rounds up to `block_max_backoff`, honours `Retry-After`, sends one warning and
one recovery notice. There is no proxy rotation and no captcha handling by design.

**Pages that keep timing out get more time, once.** After `render_failure_threshold`
load timeouts in a row on one page you get one 🐢 alert and the page's load timeout is
stretched (x2, then x3); a successful render resets it and says so.

## Persistent state

| File | Written by | Content |
| --- | --- | --- |
| `data/state.json` | `StateStore` | every site: url, paused, notifications flag, announced sub-pages, last change, unverified mode and its memory |
| `data/snapshots/<host_path>-<hash>.json` | `SnapshotStore` | one per page: the last accepted node list and the noise memory |
| `data/known_contracts.txt` | `AddressCache` | confirmed contracts, tab separated, append-only |
| `data/rejected_addresses.txt` | `AddressCache` | rejected addresses with a timestamp, compacted on start |
| `data/preferences.json` | `Preferences` | live overrides made with `/set` |
| `data/heartbeat` | `Heartbeat` | epoch seconds, every `HEARTBEAT_INTERVAL`, read by `rebooter.py` |

Every write goes through `atomic_write_text` (write to a temp file, then replace), so a
crash never leaves a half-written file.

## Concurrency and resources

- One event loop. One Chromium for the whole process, relaunched transparently if it
  crashes; a tab per fetch; at most `MAX_CONCURRENT_PAGES` renders at once.
- At most `MAX_CONCURRENT_RPC` address checks in flight; one httpx client with a
  64-connection pool is shared by Telegram, RPC, DexScreener and the probes.
- Per site one monitor task; turbo sessions and their timers are separate tasks;
  fire-and-forget notifications from task callbacks go through `Notifier.report_later`.
- Shutdown order: Telegram listener and heartbeat, turbo sessions, monitors, browser,
  HTTP client. A cancelled command (Ctrl-C during `/remove`) is never swallowed.

## Configuration layering

`Settings` (`config.py`) is built once from the environment and `.env`. `Preferences`
(`preferences.py`) layers validated overrides from `/set` on top and exposes the
effective values under the same attribute names. Everything that may change at runtime
reads a `Tuning` (a Protocol) at use time, so a `/set` applies on the next round without
a restart. Knobs that need a restart (browser, concurrency, RPC endpoints, paths) live
only in `Settings`.

## Seams for tests

The whole bot can be assembled on fakes: `build_runtime()` takes the HTTP client, the
Telegram client, the notifier and the page renderer (`Browser` protocol) as arguments,
and `SiteMonitor` / `TurboSession` / `TurboManager` / `PageChecker` accept injected
clocks and sleeps. `tests/conftest.py` holds the fakes (`FakeFetcher`, `FakeTelegram`,
`FakeValidator`, `FakeNotifier`, ...), and `tests/test_end_to_end.py` drives the wired
bot through Telegram commands with `httpx.MockTransport` answering JSON-RPC.
