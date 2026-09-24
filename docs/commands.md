# Telegram commands

Commands work in any chat the bot is in (or only in `TELEGRAM_ALLOWED_CHAT_IDS` when
that is set). They are case-insensitive, a bot mention is fine (`/add@MyBot url`), and a
bare `example.com` is read as `https://example.com`. URLs are normalised (lower-case
host, fragment dropped), so `/remove Example.com/` finds the site added as
`https://example.com`. Replies use Telegram HTML: commands, addresses and chat ids are
monospace, one tap copies them.

## Sites

| Command | Effect | Reply |
| --- | --- | --- |
| `/add <url>` | Start monitoring a site; its sub-pages are discovered on the first round. | `✅ https://example.com added to monitoring.` |
| `/remove <url>` | Stop monitoring and delete the site's snapshots. | `✅ … removed from monitoring.` |
| `/pause <url>` | Stop the monitor; the flag survives restarts. | `⏸️ … paused. Resume with /resume …` |
| `/resume <url>` | Start it again. | `▶️ … resumed.` |
| `/list` | Every site with its alert flag, state and last change. | `➡️ Monitored sites (2):` then one line per site, e.g. `• https://x.io [alerts: ON] [RUNNING] [unverified] ⚠️ BLOCKED? (HTTP 403) last change: 2026-09-23 18:40` |
| `/nonotifi <url>` (alias `/mute`) | Mute change alerts for a site; contracts are still reported. | `🔇 Change alerts disabled for …` |
| `/notify <url>` (alias `/unmute`) | Un-mute change alerts. | `🔔 Change alerts enabled for …` |

States in `/list`: `RUNNING`, `PAUSED`, or `STOPPED` (the monitor crashed and did not
restart; `/pause` then `/resume`, or `/remove` and `/add`, brings it back).

Errors are spelled out: `‼️ … is already monitored.`, `❌ … is not monitored. See /list.`,
`⏸️ … is already paused.`, `▶️ … is not paused.`, `❌ 'nope' is not a URL (expected e.g.
https://example.com)`. A command without its argument answers with its usage line.

## Turbo mode

| Command | Effect |
| --- | --- |
| `/turbo <url> [minutes] [at HH:MM \| in 10m]` | Render the site's root page and up to `turbo_max_pages` live sub-pages every `turbo_interval` seconds, announce every new address at once as *unverified*, re-check it on-chain until confirmed, stop after `turbo_stop_after` confirmed contracts, the timer, `/turbo stop` or a block. `minutes` defaults to `turbo_default_minutes` and is capped at `turbo_max_minutes`. `at 15:30` / `in 10m` (or `2h`) arm it for later. |
| `/turbo stop <url>` | End a session or cancel a scheduled one. |
| `/turbo` | Status of running and scheduled sessions. |

Replies: `🚀 Turbo started for … : 4 page(s) every 2s for 30 min.`, `⏰ Turbo for …
scheduled at 2026-09-23 15:30 for 30 min. Cancel with /turbo stop …`, `⏹️ Turbo stopped
for …`, `🚀 Turbo is already running (or scheduled) for …`, `❌ Turbo limit reached: at
most 1 site(s) at a time.`, `❌ No turbo session for …`. Sessions do not survive a
restart.

## Unverified mode

| Command | Effect |
| --- | --- |
| `/unverified <url> on` | Announce every new address on the site at once (wallets and example addresses included) and re-check it on-chain every `unverified_recheck_minutes` for `unverified_watch_hours`; alerts above `unverified_max_per_hour` become one digest. Per site, persisted. |
| `/unverified <url> off` | Back to confirmed contracts only. |
| `/unverified` | Which sites have the mode on. |

Switching the mode needs a settings-capable chat (see below).

## Settings

| Command | Effect |
| --- | --- |
| `/settings` | Every live knob grouped by section with its current value; `*` marks values changed from `.env`. |
| `/settings <key>` | What the knob does, its range, current value and `.env` default. |
| `/set <key> <value>` | Change it; applies on the next round. Risky values answer with a warning and ask you to repeat the command with `confirm`. |
| `/set <key> <value> confirm` | Apply a risky value. |
| `/settings reset <key\|all>` | Back to the `.env` values. |

Replies: `✅ poll_interval_min: 4 s → 6 s (applies immediately)`, `⚠️ Below 3 seconds
most sites rate-limit or block the bot's IP. To apply anyway, repeat: /set
poll_interval_min 2 confirm`, `♻️ Restored .env values: …`, `❌ unknown setting 'x';
/settings lists them all`, `❌ poll_interval_min must not exceed poll_interval_max (10)`.

Only chats in `TELEGRAM_ALLOWED_CHAT_IDS` (or, when that list is empty, in
`TELEGRAM_CHAT_IDS`) may change settings; everyone may read them. Other chats get
`🔒 Only chats listed in TELEGRAM_ALLOWED_CHAT_IDS (or TELEGRAM_CHAT_IDS) can change
settings.` The full list of live keys is in [configuration.md](configuration.md).

## Health

| Command | Effect |
| --- | --- |
| `/status` | Version and uptime, browser state, sites with their last round (duration, pages, rounds), last change, pages timing out, block state, turbo sessions, cache sizes. |
| `/help` | The command list. |
| `/start` | Your chat id (needed for `TELEGRAM_CHAT_IDS`) plus the command list. |

Example `/status`:

```text
🤖 scraper-bot 2.0.0, up 3h 12m
Browser: connected
Sites: 2 total, 2 running, 0 paused
• https://app.flutch.fun: last round 4s ago (2.0s, 4 page(s), 1187 rounds); last change 2026-09-23 12:01
• https://docs.example.io: last round 6s ago (1.3s, 1 page(s), 1190 rounds); 🐢 1 page(s) timing out
🚀 Turbo https://app.flutch.fun: 41 cycles, 0 confirmed, 2 unverified, 28m 40s left
Address cache: 14 known contract(s), 231 rejected
```

## Things every command does

- Unknown commands answer `Unknown command /x. Send /help for the list.`; plain text is
  ignored.
- An internal error answers with the exception type and message and the bot keeps
  running; details are in the log.
- When `TELEGRAM_CHAT_IDS` is empty, `/add`, `/list` and `/resume` append a warning with
  your chat id, because alerts are being dropped.
- Commands from chats outside `TELEGRAM_ALLOWED_CHAT_IDS` are ignored silently and logged.
