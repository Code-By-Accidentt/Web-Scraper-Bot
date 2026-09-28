# Development

## Setup

```bash
pip install -r requirements-dev.txt      # runtime deps + pytest, pytest-asyncio, ruff
playwright install chromium              # only for the opt-in browser tests
```

## Checks

```bash
pytest -q                                # ~970 tests, offline, about 14 s
ruff check . && ruff format --check .    # lint (E, F, W, I, B, UP, SIM, RUF) and formatting
SCRAPER_BOT_BROWSER_TESTS=1 pytest -q tests/pages/fetching/test_fetcher_browser.py   # real Chromium
```

The browser tests use the browser `BROWSER_CHANNEL` names in the environment (Playwright's
Chromium when it is unset); on Ubuntu 20.04, which has no Playwright Chromium, run them with
`BROWSER_CHANNEL=chrome`.

Tests never touch the network, the real `.env` or `data/`: settings are built with
`tests.conftest.make_settings(tmp_path, ...)`, HTTP goes through `httpx.MockTransport`,
the browser is `FakeFetcher`, Telegram is `FakeTelegram`. `tests/` mirrors the package
layout (`tests/app`, `tests/settings`, `tests/storage`, `tests/pages`, `tests/chains`,
`tests/monitoring`, `tests/telegram`); only three files sit at its root: the supervisor's
tests (`test_rebooter.py`), the guards that keep the test fakes in line with the real
classes (`test_guards.py`) and the end-to-end scenario (`test_end_to_end.py`).

`tests/test_end_to_end.py` assembles the real bot with `build_runtime()` on fakes and
drives it through Telegram commands: add, sub-page discovery, a confirmed change, muted
alerts, a token alert edited with its details, `/set` plus auto-pause, resume, `/status`,
a turbo session with a token deployed after its address was shown, unverified mode, a
restart with restored state, a 403 block with recovery and a timeout streak. It runs in
well under a second.

Coverage sits at about 97% of lines. The browser path of `pages/fetching/chromium.py` and
`Application.run()` itself are covered only by the opt-in browser tests (local pages in a
real Chromium: the deep scan, a load timeout, a page that stops answering, tabs a page
opens, a killed Playwright driver, the scheduled restart, addresses heard while a page
loads, the hidden automation marks and cookies kept across a restart) and real runs.
`pytest-cov` is not a dependency; install it locally if you want the numbers.

## Conventions

- One responsibility per module, classes over globals, every dependency passed in
  through the constructor (keyword-only when there are several).
- Anything that waits or reads the clock takes `sleep=` / `clock=` so tests run instantly.
- A test never depends on wall-clock timing: it waits for a condition or uses those
  seams, so a busy machine cannot make it fail.
- Fakes over mocks: extend the fakes in `tests/conftest.py` rather than patching internals.
- All Telegram text goes through `telegram/markup.py` (`escape`, `code`, `pre`, `link`);
  never build HTML by hand.
- Comments explain *why*; the code says what. Section banners `# ── name ──` only in the
  long files, as a table of contents.
- Line length 100, `from __future__ import annotations` everywhere in `scraper_bot/` and
  `rebooter.py`, Python 3.11+.

## Measured numbers (2026-09, 16-core machine, real sites and public nodes)

| What | Result |
| --- | --- |
| Browser start | 0.54 s |
| One render | ~0.15 s load + 0.5 s Playwright idle window + `SETTLE_DELAY`; the real sites go idle in ~0.8 s and render in 1.1–1.6 s |
| Page that never goes idle | pays the full `NETWORK_IDLE_TIMEOUT` per render (4 s cap → 4.6 s, 2 s cap → 2.4 s) |
| A round of 4 pages | ~2 s; 7 sub-page probes 0.2 s |
| 12 heavy pages | 6 tabs 2.7 s, 8 tabs 2.6 s, 12 tabs 1.7 s |
| JSON-RPC call, any of 2.1's 10 endpoints (8 EVM, Solana, Sui) | 44–72 ms median, 316 ms worst |
| Full validation of a new address | wallet on 8 EVM networks 88 ms, ERC-20 token 122 ms, Solana 55 ms, Sui 47 ms |
| The same with 10 EVM networks (2026-09-27, warm connections) | wallet 128 ms, ERC-20 on Ethereum 149 ms, on Arc 118 ms, on Robinhood Chain 128 ms, Solana 82 ms; Arc and Robinhood publicnode answer in ~50 ms and took 40 parallel calls without a 429 |
| Token details for an alert | 50–300 ms, all fields |
| CPU per changed page | 800 nodes 17 ms, 8 000 nodes 0.13 s, 20 000 nodes 0.34 s (runs in a thread) |
| Address extraction | 1 MB of JS 112 ms, 5 MB 0.57 s |
| Address reported while the page loads (2.1.3, local page, 20 loads, instant validator) | 17 ms p50 / 21 ms p95 after the document was sent (the full read: 0.8 s); 23 / 29 ms after a script showed it on a page that keeps polling (the full read: 2.2 s); the same in Chromium and Chrome |
| Timer fast path (2.1.3) | 2 ms on 900 nodes, 13–28 ms on 12 000 nodes, in a thread |

These numbers set the defaults in `.env.example`; re-measure before changing them.

## How to add things

**A live setting.** Add the field to `Settings` and `from_env()` in `settings/env.py`, the line
in `.env.example` (a test checks that keys and values match the code), the row in
`docs/configuration.md`, a `PreferenceSpec` in `settings/catalog.py` (key, `Settings`
attribute, section, kind, label, description, range, unit, scale, optional caution), the
attribute in the `Tuning` protocol, the field in `tests.conftest.FakeTuning`. Read it
from the tuning object at use time, never cache it.

**An EVM network.** One `EvmChain` entry in `chains/networks.py` (name, `.env` key,
default RPC, chain id, explorer and DexScreener links, `optional` for a young chain whose
public nodes may fail), its stablecoins and wrapped tokens in `chains/addresses/stoplist.py`
(check each `symbol()` on-chain), the line in `.env.example` and the row in
`docs/configuration.md`. Settings, warm-up, chain-id check, validation, pending watch and
alert links pick it up from the table; `tests/chains/test_networks.py` keeps the table
consistent.

**A chain family.** Add the canonical form in `chains/addresses/formats.py`, the pattern
in `chains/addresses/extractor.py` and a `Chain` member in `models/addresses.py`; a new
package `chains/<chain>/` with a `validator.py` (and helpers next to it when the RPC
surface is big), plus a branch in `ContractValidator._lookup` (`chains/validator.py`);
links in `chains/networks.py` and metadata lookups in `chains/enrichment.py`; tests next to the existing ones with `httpx.MockTransport` responders;
`.env.example` and the docs for the new RPC URL.

**An alert.** A method on `telegram/notifier.py` (escape everything, monospace what
people copy), the same method on `tests.conftest.FakeNotifier` recording the call, a
row in `docs/operations.md`.

**A command.** A handler in `telegram/commands/handlers.py` registered in
`BotCommands._handlers`, a line in `COMMAND_HELP` (`telegram/commands/help.py`), tests in `tests/telegram/commands/test_commands.py`, a row in
`docs/commands.md`. Replies are HTML; use `usage()` for missing arguments.
