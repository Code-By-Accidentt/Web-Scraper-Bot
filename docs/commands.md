# Telegram commands

Commands work in the chats listed in `TELEGRAM_ALLOWED_CHAT_IDS` or, when that list is
empty, in the alert chats of `TELEGRAM_CHAT_IDS`. Any other chat only gets its chat id in
answer to `/start` (at most once per ten minutes); its other commands are ignored and
logged, its plain text is ignored silently. Commands are read from messages only (a
channel post is ignored); in an allowed group every member may use them. Commands are
case-insensitive and may be followed by any whitespace, a line break included. A mention
of this bot is fine (`/add@MyBot url`); a command addressed to another bot
(`/pause@OtherBot`) is ignored once the bot knows its own name (from `getMe`, asked again
every minute when the start could not). A bare `example.com` is read as `https://example.com`.
URLs are normalised (lower-case host, default port and fragment dropped, but a route
such as `#/presale` kept), so `/remove Example.com/` finds the site added as
`https://example.com`. Replies use Telegram HTML: commands, addresses and chat ids are
monospace, one tap copies them.

## Sites

| Command | Effect | Reply |
| --- | --- | --- |
| `/add <url>` | Start monitoring a site; its sub-pages are discovered on the first round. | `✅ https://example.com added to monitoring.` When the site shares pages with a monitored one (`x.io` and `www.x.io`, a path of the same domain), a warning follows: both monitors render the shared pages and keep one snapshot of each, so a change there is reported only by the monitor that sees it first. |
| `/remove <url>` | Stop monitoring, end the site's turbo session, forget the addresses it had the pending watch keep, and delete its snapshots (not those of pages another site also renders). | `✅ … removed from monitoring.` |
| `/pause <url>` | Stop the monitor; the flag survives restarts. | `⏸️ … paused. Resume with /resume …` |
| `/resume <url>` | Start it again. A `STOPPED` site gets a fresh monitor. | `▶️ … resumed.`, or `▶️ … was stopped by an error; its monitor is running again.` |
| `/list` | Every site with its alert flag, state and last change. | `➡️ Monitored sites (2):` then one line per site, e.g. `• https://x.io [alerts: ON] [RUNNING] [unverified] ⚠️ BLOCKED? (Cloudflare challenge (HTTP 403)) last change: 2026-09-23 18:40` |
| `/nonotifi <url>` (alias `/mute`) | Mute change alerts for a site; contracts are still reported. | `🔇 Change alerts disabled for …` |
| `/notify <url>` (alias `/unmute`) | Un-mute change alerts. | `🔔 Change alerts enabled for …` |

States in `/list`: `RUNNING`, `PAUSED`, or `STOPPED` (the monitor crashed and the
monitor restarts on its own 30 s after each crash; `/resume <url>` restarts it now).

Errors are spelled out: `‼️ … is already monitored.`, `❌ … is not monitored. See /list.`,
`⏸️ … is already paused.`, `▶️ … is not paused.`, `❌ 'nope' has an invalid host name`,
`❌ 'not a url' is not a URL (expected e.g. https://example.com)`. A command without its
argument answers with its usage line, under the name that was typed (`/mute` answers
`Usage: /mute <url>`).

## Turbo mode

| Command | Effect |
| --- | --- |
| `/turbo <url> [minutes] [at HH:MM \| in 10m]` | Render the site's root page and its live sub-pages, `turbo_max_pages` pages in all (root included), every `turbo_interval` seconds and announce every new confirmed token at once. An address that is not a token yet is checked every `turbo_recheck_interval` seconds and announced the moment it is deployed; what a page shows on its first good render and already exists is old news. Stops after `turbo_stop_after` confirmed tokens, the timer, `/turbo stop` or a block. `minutes` defaults to `turbo_default_minutes` and is capped at `turbo_max_minutes`. `at 15:30` / `in 10m` (or `2h`) arm it for later; `at` means the server's clock and time zone, which the reply shows together with the time left. |
| `/turbo stop <url>` | End a session or cancel a scheduled one. |
| `/turbo` | Status of running and scheduled sessions. |

Replies: `🚀 Turbo started for …: 4 page(s) every 2s for 30 min.`, `⏰ Turbo for …
scheduled at 2026-09-23 15:30 (UTC+03:00, in 2h 5m) for 30 min. Cancel with /turbo stop …`, `⏹️ Turbo stopped
for …`, `🚀 Turbo is already running (or scheduled) for …`, `❌ Turbo limit reached: at
most 1 site(s) at a time.`, `❌ No turbo session for …`. Sessions do not survive a
restart.

## Unverified mode

| Command | Effect |
| --- | --- |
| `/unverified <url> on` | Announce every new address on the site at once (wallets and example addresses included) and re-check it on-chain every `unverified_recheck_minutes` for `unverified_watch_hours`; alerts above `unverified_max_per_hour` become one digest. Per site, persisted. |
| `/unverified <url> off` | Back to confirmed contracts only. |
| `/unverified` | Which sites have the mode on. |

## Settings

| Command | Effect |
| --- | --- |
| `/settings` | Every live knob grouped by section with its current value; `*` marks values changed from `.env`. |
| `/settings <key>` | What the knob does, its range, current value and `.env` default. |
| `/set <key> <value>` | Change it; applies at once. Risky values answer with a warning and ask you to repeat the command with `confirm`. |
| `/set <key> <value> confirm` | Apply a risky value. |
| `/settings reset <key\|all>` | Back to the `.env` values; `/settings reset` alone resets every setting, without asking. |

Replies: `✅ poll_interval_min: 4 s → 6 s (applies immediately)`, `⚠️ Below 3 seconds
most sites rate-limit or block the bot's IP. To apply anyway, repeat: /set
poll_interval_min 2 confirm`, `♻️ Restored .env values: …`, `❌ unknown setting 'x';
/settings lists them all`, `❌ poll_interval_min must not exceed poll_interval_max (10)`.

The full list of live keys is in [configuration.md](configuration.md).

## Health

| Command | Effect |
| --- | --- |
| `/status` | Version and uptime, browser state, sites with their last round (duration, pages rendered, rounds), last change, what the light mode polls (`light: 2 page(s) and 1 API(s) every 1 s, 1 rendered every 30 s`, and `through the browser` for a site that refuses plain requests), pages timing out (from their 🐢 alert to their ✅), block state (with the protection when known), RPC endpoints that stopped answering, turbo sessions, addresses waiting to become a token, cache sizes. A site whose monitor crashed shows `⛔ stopped by an error: it restarts on its own, /resume restarts it now` and counts as stopped. |
| `/help` | The command list. |
| `/start` | Your chat id (needed for `TELEGRAM_CHAT_IDS`) plus the command list. |

Example `/status`:

```text
🤖 scraper-bot 2.2.0, up 3h 12m
Browser: connected
⚠️ RPC not answering: Fantom for 2m 10s
Sites: 2 total, 2 running, 0 paused
• https://app.flutch.fun: last round 4s ago (2.0s, 4 page(s), 1187 rounds); last change 2026-09-23 12:01; light: 4 page(s) and 1 API(s) every 1 s
• https://docs.example.io: last round 6s ago (1.3s, 1 page(s), 1190 rounds); 🐢 1 page(s) timing out
🚀 Turbo https://app.flutch.fun: 41 cycles, 0 confirmed, 2 pending, 28m 40s left
⏳ 3 address(es) watched until they become a token
Address cache: 14 known contract(s), 231 rejected
```

## Things every command does

- Unknown commands answer `Unknown command /x. Send /help for the list.`; plain text is
  ignored.
- An internal error answers with the exception type and message and the bot keeps
  running; details are in the log.
- When `TELEGRAM_CHAT_IDS` is empty (commands then come from `TELEGRAM_ALLOWED_CHAT_IDS`),
  the first `/add`, `/list` or `/resume` of each chat after a start appends a warning with
  your chat id, because alerts are being dropped.
- Commands from any other chat are ignored and logged; `/start` from such a chat is
  answered with its chat id only, at most once per ten minutes.
