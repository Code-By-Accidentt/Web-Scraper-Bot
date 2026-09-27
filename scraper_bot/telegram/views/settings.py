"""How /settings shows the live knobs (Telegram HTML)."""

from __future__ import annotations

from scraper_bot.settings.catalog import PREFERENCES, SECTIONS
from scraper_bot.settings.live import Preferences
from scraper_bot.telegram.markup import code, escape


def settings_overview(prefs: Preferences) -> str:
    """Every live knob by section, with its current value; ``*`` marks a /set override."""
    lines = ["⚙️ Settings (live values; * = changed from .env)"]
    for section, title in SECTIONS.items():
        lines.append(f"\n{title}")
        for spec in PREFERENCES:
            if spec.section != section:
                continue
            mark = "*" if prefs.is_overridden(spec) else ""
            lines.append(f"• {code(spec.key)} = {escape(spec.format(prefs.current(spec)))}{mark}")
    lines.append(
        f"\n{code('/settings <key>')} explains one setting, "
        f"{code('/set <key> <value>')} changes it, "
        f"{code('/settings reset <key|all>')} restores the .env value."
    )
    return "\n".join(lines)


def describe_setting(prefs: Preferences, key: str) -> str:
    """What one knob does, its range, current value and .env default."""
    spec = prefs.spec(key)
    lines = [f"⚙️ {code(spec.key)}: {escape(spec.label)}", escape(spec.description)]
    if spec.kind == "bool":
        lines.append("Values: on / off")
    else:
        unit = f" {spec.unit}" if spec.unit else ""
        lines.append(f"Range: {spec.minimum:g} to {spec.maximum:g}{unit}")
    lines.append(f"Current: {escape(spec.format(prefs.current(spec)))}")
    lines.append(f"Default from .env: {escape(spec.format(prefs.default(spec)))}")
    if spec.caution is not None:
        lines.append("Risky values ask you to repeat the command with 'confirm'.")
    lines.append(f"Change: {code(f'/set {spec.key} <value>')}")
    return "\n".join(lines)
