"""Help and usage texts of the bot commands."""

from __future__ import annotations

from scraper_bot.telegram.markup import code, escape

COMMAND_HELP: tuple[tuple[str, str], ...] = (
    ("/add <url>", "start monitoring a site"),
    ("/remove <url>", "stop monitoring a site"),
    ("/pause <url>", "pause monitoring"),
    ("/resume <url>", "resume monitoring"),
    ("/list", "monitored sites with their status"),
    ("/nonotifi <url>", "mute change alerts for a site"),
    ("/notify <url>", "un-mute change alerts for a site"),
    (
        "/turbo <url> [minutes] [at HH:MM | in 10m]",
        "render every few seconds until its token appears",
    ),
    ("/turbo stop <url>", "end turbo mode; /turbo alone shows turbo status"),
    ("/unverified <url> on|off", "announce every new address at once, confirm later"),
    ("/settings [key]", "live settings with explanations; /set <key> <value> changes one"),
    ("/status", "bot health, last round per site, cache sizes"),
    ("/help", "this message"),
)
HELP_TEXT = "Available commands:\n" + "\n".join(
    f"{code(usage)} — {escape(description)}" for usage, description in COMMAND_HELP
)
TURBO_USAGE = "Usage:\n" + "\n".join(
    code(line)
    for line in ("/turbo <url> [minutes] [at HH:MM | in 10m]", "/turbo stop <url>", "/turbo")
)
SETTINGS_USAGE = "Usage:\n" + "\n".join(
    f"{code(usage)} — {escape(description)}"
    for usage, description in (
        ("/settings", "all settings with live values"),
        ("/settings <key>", "what a setting does, its range and default"),
        ("/set <key> <value>", "change it (add 'confirm' for risky values)"),
        ("/settings reset <key|all>", "back to the .env value"),
    )
)


def usage(command: str) -> str:
    return f"Usage: {code(command)}"
