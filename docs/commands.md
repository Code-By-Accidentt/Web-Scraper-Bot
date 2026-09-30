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
| `/turbo <url> [minutes] [at HH:MM \| in 10m]` | Render the site's root page and its live sub-pages, `turbo_max_pages` pages in all (root included), every `turbo_interval` seconds and announce every new confirmed token at once. An address that is not a token yet is checked every `turbo_recheck_interval` seconds and announced the moment it is deployed; what a page shows on its first good render and already exists is old news. Stops after `turbo_stop_after` confirmed tokens, the timer, `/turbo stop` or a block. `minutes` defaults to `turbo_default_minutes` and is capped at `turbo_max_minutes`. `at 15:30` / `in 10m` (or `2h`, at most `168h`: 7 days) arm it for later; `at` means the server's clock and time zone, which the reply shows together with the time left. |
| `/turbo stop <url>` | End a session or cancel a scheduled one, and end the site's auto-turbo window. |
| `/turbo` | Status of running and scheduled sessions and of the auto-turbo windows. |

Replies: `🚀 Turbo started for …: 4 page(s) every 2s for 30 min.`, `⏰ Turbo for …
scheduled at 2026-09-23 15:30 (UTC+03:00, in 2h 5m) for 30 min. Cancel with /turbo stop …`, `⏹️ Turbo stopped
for …`, `🚀 Turbo is already running (or scheduled) for …`, `❌ Turbo limit reached: at
most 1 site(s) at a time.`, `❌ No turbo session for …`. `/turbo` shows the sessions
like `/status`, with the command that ends each: `🚀 Turbo …: 41 cycles, 164 renders, 0
confirmed, 2 pending, 28m 40s left; stop with /turbo stop …`, `⏰ Turbo …: scheduled at …
for 30 min; cancel with /turbo stop …`. Sessions do not survive a restart.

### Auto-turbo

Nothing to type: when a render shows that a launch is near, the bot speeds the site up
by itself (`auto_turbo`, on by default). What counts:

- a countdown with launch words next to it (launch, TGE, trading, mint, presale, listing,
  opens, starts, CA, …): `Launch in 00:05:30`, `2d 03:04:05`, `02 Days 03 Hours 04 Minutes`,
  `Days 02 Hours 03`, `starts in 5m`. It counts once two renders agree on when it ends and
  that end is less than a day away, so a static text or a clock never does. A countdown
  right after "round", "ends", "reset", "reward", "funding", "live now", "ongoing" and the
  like is left alone (a launch word after that word, as in "Presale live now! Launch in …",
  lifts it), and so is one that starts over after its end (game rounds);
- a launch time with its time zone in the next 24 hours: `Sep 30, 15:00 UTC`,
  `30.09 20:00 UTC+3`, `2026-09-30 15:00 (UTC)`, `2026-09-30T15:00Z`,
  `Trading opens at 16:00 UTC`. A clock without a date counts only while it is still ahead
  today, or with "tomorrow" next to it. A time without a zone depends on the reader and is
  skipped; EST, ET, PST, PT and CET follow their region's daylight saving time (where the
  machine has time zone data), EDT, PDT, CEST and BST are taken as written;
- a countdown target in the page's inline scripts (`new Date("…Z")`, `Date.UTC(…)`, a unix
  or ISO time under a name like `launchTime`, `countdownDate`, `startDate`) or in an
  element attribute (`data-countdown-at="…Z"`);
- a phrase that appears on a page which has not shown it for a day: `CA: soon` / `TBA`,
  `contract address coming`, `launching today` / `now`, `presale is live`,
  `trading is open`. A phrase the page shows from its first render starts nothing, but
  counts as a launch word for a countdown; one that only turns up because the render
  before was still loading does not count either. The page title is not read.

From `auto_turbo_before_minutes` (10) before the launch time to `auto_turbo_after_minutes`
(60) after it, or until the site's first token (`turbo_stop_after`), a round of the site
starts every `turbo_interval` (2 s) instead of every 4-10 s, its light mode polls every
`light_turbo_interval` (0.3 s) and its addresses that are not a token yet are checked every
`turbo_recheck_interval` (1 s). No second render loop is started. A site has one window at
a time; an earlier launch time takes over a window that has not begun yet. A launch time
that moves moves the window (one more `⏰` message). After a window ended or was stopped,
the same launch time (within a minute), the same text showing the same clock the next day
and the same phrase on the same page plan no window for a day; a launch time that moved
since, or a countdown to the next sale, gets a new window. At most
`auto_turbo_max_sites` (3) sites have a window at once; one more gets a message only.

Messages: `⏰ https://x.io shows a countdown “Launch in 00:05:30”: launch at 15:00:12
(UTC+00:00, in 5m 30s). Auto-turbo for https://x.io: checking faster now until 16:00:12, or
until its first token. Cancel with /turbo stop https://x.io`; when the page moves the time:
`⏰ The launch time for https://x.io changed from 15:00 to 16:00 (UTC+00:00, in 1h 5m):
https://x.io shows a launch time “Trading opens at 16:00 UTC”.`; and at the end,
`🎯 Auto-turbo finished on https://x.io: found 1 confirmed contract(s) / the window is over
/ stopped manually. Checked faster for 12m 30s, 1 confirmed contract(s).` A window stopped
before it began ends without a summary. `/turbo` and `/status` show each window as
`⏰ Auto-turbo https://x.io: launch at 15:00 on https://x.io/presale; from … until …` (or
`on until … (… left)` while it runs). Windows do not survive a restart; the page plans them
again.

## Unverified mode

| Command | Effect |
| --- | --- |
| `/unverified <url> on` | Announce every new address on the site at once (wallets and example addresses included) and re-check it on-chain every `unverified_recheck_minutes` for `unverified_watch_hours` (at most 25 per round, those checked longest ago first); alerts above `unverified_max_per_hour` become one digest. Per site, persisted. |
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
When `data/preferences.json` cannot be written, the change still applies and the reply
says `(applied now, not saved (…), lasts until a restart)` (after a reset: `⚠️ Applied
now, not saved (…), lasts until a restart.`).

The full list of live keys is in [configuration.md](configuration.md).

## Health

| Command | Effect |
| --- | --- |
| `/status` | Version and uptime, browser state, sites with their last round (duration, pages rendered, rounds), last change, what the light mode polls (`light: 2 page(s) and 1 API(s) every 1 s, 1 rendered every 30 s`, and `through the browser` for a site that refuses plain requests), pages timing out (from their 🐢 alert to their ✅), block state (with the protection when known), a server that seems down (`⚠️ server down? HTTP 503 since 14:02`: a bare 503 or a refused connection, which sends no alert), RPC endpoints that stopped answering, turbo sessions and auto-turbo windows, addresses waiting to become a token, cache sizes. A site whose monitor crashed shows `⛔ stopped by an error: it restarts on its own, /resume restarts it now` and counts as stopped. |
| `/help` | The command list. |
| `/start` | Your chat id (needed for `TELEGRAM_CHAT_IDS`) plus the command list. |

Example `/status`:

```text
🤖 scraper-bot 2.3.1, up 3h 12m
Browser: connected
⚠️ RPC not answering: Fantom for 2m 10s
Sites: 2 total, 2 running, 0 paused
• https://app.flutch.fun: last round 4s ago (2.0s, 4 page(s), 1187 rounds); last change 2026-09-23 12:01; light: 4 page(s) and 1 API(s) every 1 s
• https://docs.example.io: last round 6s ago (1.3s, 1 page(s), 1190 rounds); 🐢 1 page(s) timing out
🚀 Turbo https://app.flutch.fun: 41 cycles, 164 renders, 0 confirmed, 2 pending, 28m 40s left
⏰ Auto-turbo https://docs.example.io: countdown “Launch in 02:10:00”; from 2026-09-30 14:50 (UTC+00:00, in 2h) until 16:00
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
