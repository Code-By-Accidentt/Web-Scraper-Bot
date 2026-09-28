# Architecture

The bot is one asyncio process that renders web pages in a shared headless Chromium,
compares each page with its stored snapshot, looks for smart-contract addresses in
everything the page loaded, confirms them on-chain and talks to you through Telegram.
This page explains how the pieces fit together and why they are shaped the way they are.

## Package map

```text
scraper_bot/
  app/              starting the bot
    cli.py            main(): settings, logging, SIGTERM handling, exit codes
    application.py    Application: shared HTTP clients, the browser, the Telegram token check
    runtime.py        build_runtime() wires everything, Runtime starts, serves and stops it
    heartbeat.py      the file the supervisor watches
    notices.py        one-time notices: Chromium without its sandbox, outdated .env values

  settings/         configuration
    env.py            Settings: every knob read once from the environment and .env
    defaults.py       built-in defaults and the ones changed since earlier releases
    catalog.py        every live setting: key, range, unit, explanation (PreferenceSpec)
    live.py           Preferences: /set overrides on top of Settings; the Tuning protocol

  models/           plain data types shared by every package
    addresses.py      Chain, Address, AddressKind, ValidationResult, ChainVerdict
    sightings.py      Sighting, Observation, Timeline: where and when an address was seen
    pages.py          Node, Snapshot, ChangeReport, BlockSignal, PageCheckOutcome
    sites.py          SiteState, LastChange: what state.json holds per site

  storage/          what the bot keeps on disk
    files.py          atomic writes
    state.py          state.json (moved aside when damaged)
    snapshots.py      one snapshot file per page
    address_cache.py  known tokens (forever) and rejected addresses (for a TTL)
    outbox.py         token alerts and documents on their way to Telegram (outbox.json)
    legacy.py         import of the version 1 files

  pages/            rendering a page and telling real changes from noise
    fetching/
      result.py         FetchResult, EarlyText and the PageFetcher protocol
      chromium.py       PlaywrightFetcher: one browser, a tab per fetch, deep scan; reads
                        like a person's browser and keeps its cookies (HIDE_AUTOMATION);
                        notes the JSON APIs a page calls; fetch() from tabs kept on sites
      observer.py       the script in every tab that reports address-like text while the
                        page still loads
      request_filter.py requests never needed: prefetches, analytics beacons
      prober.py         HttpProber: does a sub-page exist at all? does a blocking site
                        still refuse?
      blocking.py       recognises blocks (401/403/407/429/503, challenge pages, connection
                        resets) and names the protection; spots captcha widgets
    light/            the light mode
      poller.py         LightPoller: a site's pages and own JSON APIs polled over HTTP between
                        renders; which pages it covers; renders asked for after a new deploy
      transport.py      plain conditional GETs, or fetch() from a tab kept on the site
    changes/
      normalizer.py     rendered HTML -> ordered list of Node(tag, attrs, own text)
      detector.py       ordered diff of two node lists (bounded work), volatile-node exclusion
      volatile.py       memory of elements that tick (timers, clocks): no second render
      noise.py          memory of elements that keep changing (flapping)
    page_checker.py     the per-page pipeline: render -> addresses -> diff -> snapshot
    early.py            EarlyReader: addresses a page shows while it loads, checked at once
    sightings.py        where on a page each address stands and the words in front of it
    urls.py             URL normalisation, sub-page derivation, snapshot file names

  chains/           addresses and blockchains
    networks.py       every network: .env key, default RPC, chain id, explorer and DEX
                      links, optional flag; the alert links
    addresses/
      formats.py        syntactic validation and canonical forms (EIP-55, base58, Sui types)
      extractor.py      finding addresses in text
      stoplist.py       stablecoins, wrapped natives, system addresses: never candidates;
                        the symbols that mark a copy of one (STABLE_SYMBOLS)
    rpc/
      client.py         JSON-RPC client: quick retries, HTTP 429 cool-down, batches
      health.py         which endpoints are failing, since when, and resting ones
      warmup.py         keeps RPC connections warm, checks each EVM endpoint's chain id
    evm/              abi.py (constants, ABI decoding), validator.py (every network at once)
    solana/           accounts.py (account layouts, PDAs), validator.py (mints)
    sui/              objects.py (lookups), validator.py (coins and packages)
    validator.py      ContractValidator: caches verdicts, merges parallel checks, re-checks
    enrichment.py     token details for alerts: metadata, age, DexScreener

  monitoring/       the loops
    site_monitor.py     rounds for one root site and its sub-pages, block backoff, timeout streaks
    monitor_manager.py  add / remove / pause / resume, one task per site, crash restart
    rounds.py           what the monitor and turbo share: pages to render, the paced wait
    alerts.py           AlertRegistry: one alert per token across pages, sites and modes
    pending/            PendingWatch: addresses published before deployment
      entry.py            one watched address and its saved form
      old_news.py         tokens a pausing first check showed but confirmed late: one
                          document per burst
      watch.py            the batched checks, confirmation and pending.json
    turbo/              render every few seconds, hunt tokens
      session.py          TurboSession: one site in a tight loop
      manager.py          TurboManager: start, schedule, list, stop
    unverified.py       UnverifiedWatcher: announce first, confirm on-chain later

  telegram/         what the user sees
    client.py         Bot API over httpx: a send queue per chat, HTML messages, plain-text
                      fallback, documents, polling
    notifier.py       every alert the bot sends on its own initiative
    commands/
      parsing.py        /command text -> name and arguments, /turbo arguments
      help.py           help and usage texts
      handlers.py       BotCommands: what each command does and replies
      listener.py       the long-polling listener
    views/
      token.py          the token part of an alert: name, supply, age, market, links
      status.py         the /status report
      settings.py       the /settings overview and the explanation of one setting
    markup.py         HTML helpers: escape, code, pre, link
    timefmt.py        durations and clock times for people

main.py             `python main.py` (kept for the supervisor and old habits)
rebooter.py         supervisor: restarts the bot when it exits or stops heart-beating
```

Dependencies point downwards: `telegram/` and `monitoring/` use `pages/` and `chains/`,
`pages/` uses `chains/` for extraction and validation (and `chains/rpc/client.py` to read a
`Retry-After` header), and `chains/` knows only the core packages (`models`, `settings`,
`storage`). The loops in `monitoring/` send their alerts through `telegram/notifier.py`;
`telegram/commands/` and `telegram/views/status.py` drive and read the loops.
`settings/env.py` reads the network table in `chains/networks.py` (which imports nothing
of `settings`), and `storage/` uses `pages/urls.py` and `chains/addresses/formats.py` for
file names and addresses. `chains/` reports failing RPC endpoints through a callback that
the `Notifier` provides; it never formats a message.

## The life of a round

`SiteMonitor.run_once()` is the heart of the bot; every monitored site runs it in its
own asyncio task, then sleeps for a random `poll_interval_min`..`poll_interval_max`.

```text
SiteMonitor.run_once
 │
 ├─ while blocked: HttpProber.peek(root)  one GET; still refused -> the round renders
 │                                     nothing (every 4th blocked round renders anyway)
 │
 ├─ HttpProber.probe(sub-page) x7      HEAD (GET after 404/405/410/501); 404/410 or any
 │                                     transport error (DNS, connection, TLS, 10 s timeout)
 │                                     means "does not exist"
 │
 ├─ PageChecker.check(page)  for the root and every live sub-page that is due, concurrently
 │   (a page the light mode covers is due every render_interval, or at once after a deploy)
 │   ├─ PlaywrightFetcher.fetch        tab from the shared browser (max MAX_CONCURRENT_PAGES),
 │   │   │                             domcontentloaded -> network idle (capped) -> settle;
 │   │   │                             prefetches and analytics beacons are dropped; deep scan
 │   │   │                             (off by default) collects frames, shadow roots, script/JSON;
 │   │   │                             every step has a time limit (opening the tab, reading the
 │   │   │                             page and the deep scan: the load timeout; closing it: 5 s)
 │   │   └─ while it loads: the observer reports address-like text -> EarlyReader checks
 │   │                                 it and calls on_observation at once (read=False)
 │   ├─ BlockDetector.inspect          a challenge or ban page is never diffed (a 2xx one
 │   │                                 is still read for the addresses it shows)
 │   ├─ HtmlNormalizer.signature       ordered nodes with own text and cleaned attributes
 │   │                                 (the first 200 of each element)
 │   ├─ find_sightings                 every address with where it stood (text, link, page
 │   │                                 source, loaded script) and the words in front of it;
 │   │                                 script-only addresses from the page's first deep round
 │   │                                 are skipped (Snapshot.deep_seen)
 │   ├─ ContractValidator.validate_many  cache first, then RPC; tokens behind LP pairs too
 │   ├─ on_observation  ─────────────► SiteMonitor announces the tokens NOW (AlertRegistry:
 │   │                                 one alert per token), before any comparison
 │   ├─ snapshot missing?  -> BASELINE (store it)
 │   ├─ identical, or only elements known to tick differ (VolatileShapes)?  -> UNCHANGED
 │   └─ differs:  sleep confirm_delay, render again
 │        ├─ new addresses of the second render -> on_observation again
 │        ├─ ChangeDetector.volatile_nodes(first, second)   what moved between two renders
 │        ├─ ChangeDetector.compare(snapshot, second, ignore=volatile)
 │        ├─ NoiseFilter.apply           drop elements that already alerted flap_threshold times
 │        ├─ VolatileShapes.learn        remember the elements that ticked between the renders
 │        └─ store the second render, without the volatile nodes the old snapshot did not
 │           have, as the new snapshot  -> CHANGED or UNCHANGED
 │
 ├─ per outcome: announce a new sub-page, a captcha widget (once per page and widget),
 │   the change report (unless muted), feed candidates to the UnverifiedWatcher, count
 │   load timeouts
 ├─ UnverifiedWatcher.recheck          re-validate watched addresses that are due
 └─ update block state (backoff) and /status counters

LightPoller.run   one per site, from the end of its first round
 ├─ every light_interval (light_turbo_interval while turbo runs for the site),
 │  nothing while the site blocks:
 │   ├─ each live page: a conditional GET (If-None-Match / If-Modified-Since)
 │   │   ├─ 304 or the same body     -> a read of the page, if the light mode covers it
 │   │   ├─ addresses it never had   -> find_sightings -> validate -> on_observation
 │   │   └─ other scripts than before (a new deploy) -> the monitor renders it now
 │   └─ each adopted API: a GET; addresses it never carried -> validate -> on_observation
 │       (Found in: site API)
 ├─ tries one API of the site the renders saw the page call (at most 2 per page)
 └─ judges coverage: HTML with 80% of the render's texts, no ticking, no own polling
```

A token alert goes out from a task the `Notifier` owns, the moment the page shows the
token: usually while the page still loads (see "Addresses are read while the page loads"
below), else when its render is read. Claiming it in the `AlertRegistry` is what writes
the token into the known cache,
so a round cancelled during the check (or a restart) cannot hide a token that was never
announced, and neither a later cancellation nor a stop can lose the alert: it is kept in
`data/outbox.json` until every chat got it (see the rules below). In each chat's Telegram
queue only command replies overtake it, and it waits out flood limits and outages of up
to about ten minutes (see Concurrency and resources), so a rate limit never holds up the
page. The ⏸️ pause notice and the turbo summary follow the alerts they belong to from
tasks of their own; the monitor pauses the site first. The token's details
(name, supply, Solana authorities and Token-2022 flags, age, the other networks where the
address is a token too, with their symbols, DEX pair) follow as an edit of the same message. When one page reaches `contract_pause_threshold`
tokens in one round (both renders), the render that crosses it sends its tokens as a
`.txt` document and the site pauses (tokens of an earlier render of that round went out
one by one); when that happens on the monitor's first round, the pause message says the
tokens are most likely ones the page already listed (a page full of tokens on a newly
supported network, for example). The addresses that first round left open (no verdict at
all, or an EVM rejection that a late Arc or Robinhood Chain answer may overturn; Solana
and Sui rejections are final) are remembered for a day, at most 1 000 per site (`old_news`
in `pending.json`). One of them that turns out a token later is old news too: it gets no
alert of its own, counts toward no pause and is not reported to turbo; each burst of them
is claimed and sent as one more `.txt` once 10 s pass without another:
`📄 N more contract(s) that were already on <site> at the first check (confirmed late)`.
A token on a network where the pending watch saw the address empty is a deployment and
gets its own 🔔.

The heavy synchronous steps (parsing, finding addresses, the diffs) run in worker
threads, each with its own lxml parser, so the other monitors and the Telegram listener
stay responsive on pages with thousands of nodes. The ordered diff is `SequenceMatcher`
under a work budget: the common start and end of the two lists are matched first where
that lines up more nodes, a part too big for the budget is split at the nodes found exactly
once on each side (patience diff), and a part without such anchors is compared as a bag
of nodes. Its cost grows with the page size, not with its square, also on pages full of
identical rows.

## Rules that shape the design

**A change needs two renders.** Anything that differs between two renders taken
`confirm_delay` seconds apart (timers, counters, rotating banners) is volatile and is
excluded from the report together with its previous value from the snapshot: an old node
with the same text (only its attributes differ) or with the same attributes and a text
of the same shape (numbers masked, so `00:05:30` and `00:05:28` match). "Presale is live"
turning into a ticking "Launch in 00:05:30" is therefore a change, not a previous value.
The snapshot keeps only confirmed content: the second render without the volatile nodes
the old snapshot did not have, plus the old nodes that vanished only between the two
renders. Noise never accumulates, and a real change (an addition or a removal) that
happened between the renders is reported by the next round, if it lasts. If one render
is much smaller than the other (a truncated load) the page is skipped for this round as
UNSTABLE and the snapshot is kept. So is a round whose confirming render has under 60% of
the stored snapshot's nodes (a loading skeleton, a half-loaded list); the third such round
in a row is compared as usual, since the page then really changed. Tokens are looked for
in those renders all the same.

**A timer costs one render.** On a page with a ticking timer or a live clock every render
differs from the snapshot, which would cost a second render on every check. So a round
remembers in the snapshot (`volatile`) the elements that ticked between its two renders:
their shape (tag, attributes and text with numbers masked), how many elements of that
shape the page shows and which of them ticked (their position among those elements). A
later render that differs from the snapshot only in those elements, in the same order,
with the same count, is UNCHANGED without a second render and without rewriting the
snapshot. Anything else gets the second render as before: another shape ("Launch in
00:05:30" turning into "CA: 0x…"), a new row shaped like a ticking one, a still element
shaped like a ticking one that changes, a reorder. An element seen ticking is remembered
for a day (at most 200 shapes per page); a shape does not count as ticking when what went
between the renders was in the stored snapshot (that is a change landing between the
renders). A timer that stops, with nothing else changing, is therefore reported up to a
day late; its page then renders twice once a day to learn its elements again.

**Slow noise has a memory.** Content that changes slower than the confirm delay would
pass the two-render check every time. `NoiseFilter` counts alerts per element (tag,
attributes and the element's position among look-alikes; attribute-less elements by
tag, the first word of their text with numbers masked, and their position among such
elements) inside `flap_window`; a report that swaps out most of a page (an interstitial)
counts once for the page as a whole, not for each element; after `flap_threshold` alerts the
element is silenced for as long as it keeps changing. The memory lives in the page's
snapshot file and expires on its own.

**Well-known addresses never become candidates.** `StopList` (`chains/addresses/stoplist.py`) holds
stablecoins, wrapped natives, bridged BTC/ETH, liquid-staking tokens (stETH, cbETH,
jitoSOL, mSOL, ...) and a few chain tokens (ARB) on every configured chain (each one
checked on-chain), the OP-stack predeploys, every EVM address below `0x10000`, the burn
address and the native-coin placeholder, plus the operator's own `data/ignore.txt`. The
extractor drops them, so wallet-connect bundles cannot flood a first round. Copies of
such tokens at other addresses (bridged, plumbing or scams) are known by their symbol
instead: with `skip_stable_copies`, a token whose `symbol()` (Solana: Metaplex or
Token-2022 metadata; Sui: coin metadata) reads as one of `STABLE_SYMBOLS` (USDC, USDT,
USDC.e, EURC, DAI, WETH, WBTC, stETH, ... 50 in all) is a copy on that network, not a
launch. Only the whole symbol counts, after folding case, styled and fullwidth letters,
invisible characters, Cyrillic and Greek look-alikes and Tether's ₮ (`USDС` with a Cyrillic
С matches, `USDCAT` does not); a symbol that cannot be read counts as none. A Solana
candidate must also look like a key: a base58 run of 32 to 44 characters not glued to
other letters or digits, with both upper- and lower-case letters (base32 ids of IPFS or
Arweave are single-case), outside inline `data:` files, whose base64 is full of
base58-looking runs.

**An address is checked once.** `ContractValidator` remembers verdicts: confirmed
tokens forever (`known_contracts.txt`, written when the token's alert is claimed),
rejected addresses for `rejected_ttl` (`rejected_addresses.txt`; expired entries leave at
start and every 1 000 rejections), addresses whose RPC check failed for `rpc_retry_delay`.
Concurrent checks of the same address share one lookup. Every verdict carries a
`kind`: an EVM address is a token when a network returns bytecode and, with
`require_erc20`, answers `totalSupply()` and `decimals()` without being an LP pair
(`token0`/`token1`, whose non-quote tokens are checked instead) or an ERC-4626 vault
(`asset()`); `0xef0100…` code is an EIP-7702 wallet. `symbol()` travels in the same batch
and only tells a copy of a stablecoin or wrapped coin (`STABLE_COPY`), per network: the
check keeps waiting for the other networks, so an address that is a copy on one network
and a real token on another (`0xF181…`: "USDC.e" on Sonic, LIMP on Ethereum) is announced
on the network where it is a token; a copy everywhere is rejected like any non-token. A
Solana address counts only as an SPL / Token-2022 mint read at `confirmed` commitment (a
node's default, `finalized`, trails by about 13 s), fetched in one `getMultipleAccounts`
call together with its Metaplex metadata account. A transport failure or rate limit on any network means "no
verdict" rather than a cached rejection, because a wrong rejection would hide a real
launch for hours. The exception are the optional networks, Arc and Robinhood Chain (young
chains with few public nodes): an EVM check waits for them only half a second
(`OPTIONAL_GRACE`) after the required networks answered, and when they fail or lag, the
other networks' answers give the verdict, which names the networks missing from it. A
late answer is not thrown away: a token found there is kept and the next look at the
address (the pending watch at its next tick, or the monitor's next round) is confirmed
from it at once; "nothing here" completes the rejection. Otherwise only the missing
networks are asked again after `rpc_retry_delay`, in the background (two at a time): the
page showing the address never waits for that, and a token found then is confirmed at
the address's next check. An optional endpoint that fails twice in a row rests: checks
leave it out without waiting, but for probes (at once, then 2, 4, 8, 16 and 30 s apart),
whose answer ends the rest. The pending watch never rests an endpoint: it sends one batch
at a time to each, and a late answer there counts too (code: confirmed at the next tick;
none: seen absent as of the tick).
The network set the caches were built with is
kept in `data/evm_networks.txt`; when a network is added, cached EVM rejections are
forgotten and watched EVM addresses become baselines (old news if found there).

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
An address shown by several sites is checked while any of them runs or is in turbo
(and after a session on a URL that is not monitored, since the launch may come later;
`/remove` ends the site's session and forgets its addresses), and a turbo session on any
of them hears about the token. EVM and Solana addresses are checked and confirmed by
loops and queues of their own, so a hanging EVM node never delays a Solana mint. The
watch holds up to 100
addresses per site and 1 000 in all; when full, an urgent address (named as the CA, or
seen in turbo) takes the place of the oldest non-urgent one (only leaving that site's
list when others show it too) and is refused when every watched address is urgent;
other offers wait until there is room again. An address
given up on after a day comes back when a page
names it as the CA or turbo sees it; wallets stay out for a week (the newest 20 000
dropped addresses are remembered). Sui is not watched: a package id cannot be known
before it is published.

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

**Blocks slow the bot down, nothing else.** A block signal from any page of a site
(HTTP 401/403/407/429/503, a mitigation header such as `cf-mitigated: challenge`,
`x-vercel-mitigated` or `x-amzn-waf-action`, a page whose title begins like a challenge
such as "Just a moment" or "Vercel Security Checkpoint", connection resets) doubles the
pause between rounds up to `block_max_backoff` and honours a longer `Retry-After` as
sent. It sends one warning, which names the protection when the answer gives it away
(Cloudflare challenge or Turnstile, Vercel, AWS WAF, DataDome, DDoS-Guard, PerimeterX,
Imperva, Sucuri, hCaptcha, reCAPTCHA, or the CDN in front), and one recovery notice
after a round without block signals in which the root page answered. A 2xx page counts
as a challenge by a mitigation header, a title that begins like one, or a title part that
is nothing but a challenge phrase ("Security Captcha", "site | Access Denied", not a lone
"Verification"); a title that merely mentions a captcha is no block. Such a page is still read for what it shows
(its text and token links, not its source or form values, where challenge scripts keep
random tokens), but it is not compared, and its render does not count as a read of the
page for the ⏱ line. A captcha widget on a page that loads normally (a reCAPTCHA
checkbox, hCaptcha, Turnstile; not the invisible kinds) is reported once per page and
widget. While a site blocks, each round first asks its root page with one plain GET (the
first 64 KB): if that is still refused, the round renders nothing and counts as blocked,
so the bot does not prolong a ban with full page loads; every fourth blocked round
renders anyway, since the browser may pass where the HTTP client does not, and anything
but a refusal (the page, a 404, a network error) lets the round render. There is no
proxy rotation and no captcha solving by design.

**The browser reads like a person's.** With `HIDE_AUTOMATION` (on by default) Chromium
starts with the `AutomationControlled` blink feature disabled (`navigator.webdriver` is
false), its context sends the browser's own User-Agent with "HeadlessChrome" read as
"Chrome" (version and platform stay the browser's), speaks `en-US` (`Accept-Language:
en-US,en;q=0.9`), and both HTTP clients send the same User-Agent and language once the
browser has started. Cookies and local storage live in the one shared context and are
saved to `data/browser_state.json` every 5 minutes, before the hourly Playwright restart
and at shutdown, and restored at every launch: a Cloudflare or Vercel check that the
browser passed on its own is not asked again after a restart (a state over 2 MB keeps
only its cookies; an unreadable file is moved aside as `browser_state.json.bad`). This
makes nothing pass that a person's browser would not: no captcha solving, no proxies,
and a data-centre IP stays one. The client hints (`Sec-CH-UA`) name Chrome or Chromium
in the `chrome` and `chromium` channels; the old headless shell (`BROWSER_CHANNEL=bundled`)
still names HeadlessChrome there.

**The light mode watches between renders.** A render costs dozens of requests and a
second of CPU, so the monitor also asks every live page over plain HTTP each
`light_interval` (1 s; `light_turbo_interval`, 0.3 s, while a turbo session runs for the
site), conditionally, so an unchanged page is a 304 without a body. A page whose HTML
gains an address hands it to the usual pipeline at once (checked on-chain, one alert per
token, the pending watch). The site's own JSON APIs that a render saw the page call
(GET, answered in JSON, on the site's domain or a subdomain; price feeds and other
sites are never asked) are tried once: one that holds addresses or names a CA (`"ca":`,
`"contract":`, `"mint":`) is polled too, at most two per page and six per site. What it
carries when adopted is its baseline; an address it later adds is announced even before
the page shows it, as "Found in: site API". A page whose HTML carries at least 80% of its
render's texts, and that neither ticks (a timer, see above) nor calls its API again and
again on its own, is covered: the monitor then renders it only every `render_interval`
(30 s), for change detection; every other page (a single-page app, a page that reveals
its CA from a script) keeps its render every round, and gets the polls on top. A page
whose HTML starts loading other scripts (a new deploy of a single-page app) is rendered
at once (at most every 10 s; a list that changes on every request is ignored). A site
that refuses the plain client (a block signal) while the renders pass is asked with
fetch() from a tab kept on its origin (`PlaywrightFetcher.tab_fetch`, opened on the
origin's robots.txt, at most eight such tabs): the requests then carry the browser's
cookies, headers and TLS. Refused there too, the polls rest a minute; while the site
blocks the bot they stop. `light_interval 0` turns the light mode off.

**Addresses are read while the page loads.** Every tab runs a small observer before the
page's own scripts (`pages/fetching/observer.py`): a `MutationObserver` on the document
that reports each new visible text or attribute value that may hold an address (script
and style texts aside, at most 400 pieces per document, frames left to deep scan) through
a Playwright binding. `EarlyReader` extracts the addresses with the usual rules, keeps
what a person sees (text, and links that name a token, as on a challenge page), checks
them and reports them to the monitor at once (`read=False`, so no ⏱ window closes yet).
An address the HTML carries is therefore announced a few milliseconds after the document
arrives, and one a script shows as soon as it is in the DOM, instead of after network
idle and the settle delay (0.8 s on a quiet page, the whole network-idle cap on a page
that keeps polling). The render then goes on as before for the comparison; the tokens
already claimed are not announced twice. Each fetch opens a fresh tab, so the observer
never sees an older document.

**Pages that keep timing out get more time, once.** After `render_failure_threshold`
load timeouts in a row on one page you get one 🐢 alert and the page's load timeout is
stretched (x2, then x3); a successful render resets it and says so. A page that stops
answering once it has loaded (a script stuck in an endless loop: "page did not respond
within 20s") counts as a load timeout, and so does a browser that does not open a tab in
time ("browser did not open a tab within 20s").

**A token alert survives a restart.** Token alerts, `✅ Confirmed` alerts and token
`.txt` documents are written to `data/outbox.json` (atomically) before they are sent, one
entry each with the chats it is owed to; a chat is struck off once its send is over:
sent, refused, or given up after about ten minutes of failures. The next start sends what
is left to the chats still configured, right after the 🤖 message, token details
included. After a stop or crash just after Telegram accepted a message but before the
outbox noted it, the next start sends that message once more (rare, and accepted). A
damaged outbox is moved aside as `outbox.json.broken-<time>` with a 🛑; when the outbox
cannot be saved, the bot logs the error and sends a 🛑, and the alerts go out all the same.

## Persistent state

| File | Written by | Content |
| --- | --- | --- |
| `data/state.json` | `StateStore` | every site: url, paused, notifications flag, announced sub-pages, last change, unverified mode and its memory, captcha widgets reported per page; a damaged file is moved aside as `state.json.broken-<time>` |
| `data/snapshots/<host_path>-<hash>.json` | `SnapshotStore` | one per page: the last accepted node list, the noise memory, the elements seen ticking (`volatile`, for a day) and, once deep scan has seen the page, the addresses its scripts carried then (`deep_seen`) |
| `data/browser_state.json` | `PlaywrightFetcher` | with `HIDE_AUTOMATION`: the browser's cookies and local storage, restored at every launch; an unreadable one is moved aside as `browser_state.json.bad` |
| `data/known_contracts.txt` | `AddressCache` | confirmed contracts, tab separated, append-only |
| `data/rejected_addresses.txt` | `AddressCache` | rejected addresses with a timestamp, compacted on start |
| `data/preferences.json` | `Preferences` | live overrides made with `/set` |
| `data/ignore.txt` | you | optional: addresses never to announce, one per line, `#` comments |
| `data/pending.json` | `PendingWatch` | addresses waiting to become a token (sites, networks seen empty), dropped ones remembered for a week (wallets and non-tokens refused; expired ones refused unless named as the CA or seen by turbo), and the addresses a pausing first check left open (`old_news`, for a day) |
| `data/outbox.json` | `Outbox` (for the `Notifier`) | token alerts, `✅ Confirmed` alerts and token documents not yet delivered to every chat, with the chats still owed; a damaged file is moved aside as `outbox.json.broken-<time>` |
| `data/outdated_defaults.txt` | `Application` | the old `.env` values last reported in Telegram, so a restart does not report them again |
| `data/evm_networks.txt` | `Runtime` | the EVM networks the caches were built with, written once the bot runs |
| `data/no_sandbox_notice.txt` | `Application` | written once a chat received the 🛑 that Chromium runs without its sandbox; removed by a start with the sandbox |
| `data/heartbeat` | `Heartbeat` | epoch seconds, every `HEARTBEAT_INTERVAL`, read by `rebooter.py` |

State, snapshots, preferences, the pending watch, the outbox and the browser state are written through
`atomic_write_text` (write to a temp file, then replace), so a crash never leaves a
half-written file; the address caches are appended to line by line, and
`rejected_addresses.txt` is rewritten through it when compacted.

## Concurrency and resources

- One event loop. One Chromium for the whole process, started with its sandbox where
  that works (without it, and one 🛑, where it does not), relaunched transparently if it
  crashes (only by the first tab that notices); when the Playwright driver itself dies
  ("Connection closed while reading from the driver"), driver, browser and context start
  again and the fetch is tried once more. Between renders, after an hour or 10 000
  renders, whichever comes first, Playwright and the browser start afresh, because
  Playwright keeps a little memory for every call (6 to 14 KB per render, measured): the
  renders in flight get up to 30 s to finish, the cache starts empty, and the cookies
  carry over through `browser_state.json` (with `HIDE_AUTOMATION`; without, they start
  empty too). A tab
  per fetch, at most `MAX_CONCURRENT_PAGES` renders at once; a tab that a page opens itself
  (`window.open`, `target=_blank`) is closed at once, downloads are refused, and closing a
  tab, the browser or the driver gets 5 s.
- At most `MAX_CONCURRENT_RPC` address checks in flight. An EVM check asks every network
  at once and answers as soon as one of them finds a token (or an LP pair); the slower
  requests finish in the background (at most 64 of them) instead of being cancelled,
  because cancelling would drop a warm connection.
- Two httpx clients: one for RPC, Telegram and DexScreener (HTTP/2, idle connections kept
  60 s, `ConnectionWarmer` calls every RPC endpoint (EVM, Solana, Sui) every 30 s and
  checks each EVM endpoint's
  chain id at start), one plain HTTP/1.1 client for the sub-page probes and the light
  request of a blocked site, so an HTTP/2 protocol error can never make a live page look
  absent. With `HIDE_AUTOMATION` both send the browser's User-Agent and language.
- `JsonRpcClient` retries quick failures (HTTP 429 with `Retry-After`, 500/502/503/504,
  refused or reset connections, HTTP/2 protocol errors, rate-limit errors inside a 200,
  also inside a batch answer) within `RPC_TIMEOUT`, a hard deadline; after a 429 every
  client of the HTTP pool leaves that host alone; batches carry up to 50 calls, and a node
  that refuses a batch gets single calls for ten minutes (the token probe then asks about
  one block number, so that nodes behind a load balancer agree). `EndpointHealth` remembers
  since when an endpoint fails and its latest error; one 🛑 goes out only after five
  minutes of failures (the latest within the last minute), optional networks included,
  and `/status` lists endpoints failing for a minute or more.
- `TelegramClient` sends every request to a chat from that chat's queue: a chat that was
  quiet takes up to 5 at once, then about one a second, and at most 30 a second go out in
  all; command replies first, then token alerts, their details edits and documents, then
  everything else (change reports, notices). A 429 pauses the whole chat, which then
  resumes at one a second, and only the request at the head of the queue tries again.
  Token alerts, documents and notices wait out any flood limit, up to about ten minutes
  of waiting; a command reply gets three tries and is dropped on a flood wait over 10 s.
  A request that never reached Telegram is sent again (three tries, 2 s apart); patient
  sends (token alerts, documents, the ⏸️ pause message, the RPC 🛑) keep trying for about
  ten minutes, the pause doubling from 2 to 30 s, and retry a 5xx answer too. A request
  whose answer timed out may have arrived and is never resent. A notice holds up its
  caller for 2 s at most (a 🛑 for 10 s) and stays queued; the details edit of an alert
  waits only for the token alerts queued before it.
- Per site one monitor task and one light-mode task (started after its first round);
  the light polls of a site run concurrently, every page and API once per interval,
  through the HTTP/1.1 probe client (keep-alive) or the site's kept tab; turbo sessions and their timers are separate tasks;
  fire-and-forget notifications from task callbacks go through `Notifier.report_later`.
- Shutdown (Ctrl-C or SIGTERM, even during the start) order: Telegram listener,
  heartbeat and warm-up, the pending watch saved, turbo sessions and monitors (8 s each
  at most, their summaries included), the pending watch stopped and saved again, alerts
  and documents on their way (up to 10 s; token alerts and documents not out by then stay
  in the outbox for the next start), RPC checks still running, browser, HTTP clients:
  within the 30 s the supervisor grants. `/remove` and `/pause` wait at most 10 s for the
  site's monitor to end; one stuck in a browser call is left to finish on its own, so the
  command still gets its answer. A cancelled command (Ctrl-C during `/remove`) is never
  swallowed.

## Configuration layering

`Settings` (`settings/env.py`) is built once from the environment and `.env`. `Preferences`
(`settings/live.py`) layers validated overrides from `/set` on top and exposes the
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
