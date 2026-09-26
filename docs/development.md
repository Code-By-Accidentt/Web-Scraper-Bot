# Development

## Setup

```bash
pip install -r requirements-dev.txt      # runtime deps + pytest, pytest-asyncio, ruff
playwright install chromium              # only for the opt-in browser tests
```

## Checks

```bash
pytest -q                                # ~750 tests, offline, about 9 s
ruff check . && ruff format --check .    # lint (E, F, W, I, B, UP, SIM, RUF) and formatting
SCRAPER_BOT_BROWSER_TESTS=1 pytest -q tests/pages/test_fetcher_browser.py   # real Chromium
```

Tests never touch the network, the real `.env` or `data/`: settings are built with
`tests.conftest.make_settings(tmp_path, ...)`, HTTP goes through `httpx.MockTransport`,
the browser is `FakeFetcher`, Telegram is `FakeTelegram`. `tests/` mirrors the package
layout (`tests/chains`, `tests/pages`, `tests/monitoring`, `tests/telegram`) plus the
root-level tests for `app`, `config`, `preferences`, `storage`, `rebooter`, the guards that
keep the test fakes in line with the real classes (`test_guards.py`) and the end-to-end
scenario.

`tests/test_end_to_end.py` assembles the real bot with `build_runtime()` on fakes and
drives it through Telegram commands: add, sub-page discovery, a confirmed change, muted
alerts, a token alert edited with its details, `/set` plus auto-pause, resume, `/status`,
a turbo session with a token deployed after its address was shown, unverified mode, a
restart with restored state, a 403 block with recovery and a timeout streak. It runs in
well under a second.

Coverage sits at about 97% of lines. The browser path of `pages/fetcher.py` and
`Application.run()` itself are covered only by the opt-in browser tests and real runs.
`pytest-cov` is not a dependency; install it locally if you want the numbers.

## Conventions

- One responsibility per module, classes over globals, every dependency passed in
  through the constructor (keyword-only when there are several).
- Anything that waits or reads the clock takes `sleep=` / `clock=` so tests run instantly.
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
| Browser start | 0.8 s |
| One render | ~0.15 s load + 0.5 s Playwright idle window + `SETTLE_DELAY`; the real sites go idle in ~0.8 s and render in 1.1–1.6 s |
| Page that never goes idle | pays the full `NETWORK_IDLE_TIMEOUT` per render (4 s cap → 4.6 s, 2 s cap → 2.6 s) |
| A round of 4 pages | ~2 s; 7 sub-page probes 0.2 s |
| 12 heavy pages | 6 tabs 2.7 s, 8 tabs 2.6 s, 12 tabs 1.7 s |
| JSON-RPC call, any of the 10 endpoints | 44–72 ms median, 316 ms worst |
| Full validation of a new address | wallet on 8 EVM networks 88 ms, ERC-20 token 122 ms, Solana 55 ms, Sui 47 ms |
| Token details for an alert | 50–300 ms, all fields |
| CPU per changed page | 800 nodes 30 ms, 8 000 nodes 0.4 s, 20 000 nodes 1.6 s (runs in a thread) |
| Address extraction | 1 MB of JS 160 ms, 5 MB 0.8 s |

These numbers set the defaults in `.env.example`; re-measure before changing them.

## How to add things

**A live setting.** Add the field to `Settings` and `from_env()` in `config.py`, the line
in `.env.example` (a test checks that keys and values match the code), the row in
`docs/configuration.md`, a `PreferenceSpec` in `preferences.py` (key, `Settings`
attribute, section, kind, label, description, range, unit, scale, optional caution), the
attribute in the `Tuning` protocol, the field in `tests.conftest.FakeTuning`. Read it
from the tuning object at use time, never cache it.

**A chain.** Add the pattern and canonical form in `chains/address_extractor.py` and a
`Chain` member in `models.py`; a validator class in `chains/contract_validators.py` and a
branch in `ContractValidator._lookup`; explorer / DEX links and metadata lookups in
`chains/enrichment.py`; helpers in a new `chains/<chain>.py` if the RPC surface is big;
tests next to the existing ones with `httpx.MockTransport` responders; `.env.example`
and the docs for the new RPC URL.

**An alert.** A method on `telegram/notifier.py` (escape everything, monospace what
people copy), the same method on `tests.conftest.FakeNotifier` recording the call, a
row in `docs/operations.md`.

**A command.** A handler in `telegram/commands.py` registered in `BotCommands._handlers`,
a line in `COMMAND_HELP`, tests in `tests/telegram/test_commands.py`, a row in
`docs/commands.md`. Replies are HTML; use `usage()` for missing arguments.
