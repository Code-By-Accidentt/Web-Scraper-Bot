"""The token part of an alert: name, supply, age, market and links."""

from __future__ import annotations

from scraper_bot.chains.enrichment import TokenInfo
from scraper_bot.chains.networks import token_links
from scraper_bot.models.addresses import ConfirmedContract
from scraper_bot.models.sightings import Sighting, Timeline
from scraper_bot.telegram.markup import code, escape, link
from scraper_bot.telegram.timefmt import format_duration, format_precise, format_utc_offset

NO_PAIR_YET = "No DEX pair listed yet."
# Every RPC node keeps at least this much history (s). Past that, a node's oldest
# transaction may be days younger than the token, so the age is only a lower bound.
_HISTORY_SURELY_KEPT = 3600.0


def _short(value: float) -> str:
    """Return ``value`` with 2, 1 or 0 decimals by its size, without trailing zeros."""
    if abs(value) >= 100:
        text = f"{value:.0f}"
    elif abs(value) >= 10:
        text = f"{value:.1f}"
    else:
        text = f"{value:.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_amount(value: float) -> str:
    """Return a short amount like ``1.23K``, ``12.4M`` or ``5B``; plain below a thousand."""
    for unit, threshold in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= threshold:
            return f"{_short(value / threshold)}{unit}"
    return _short(value)


def format_usd(value: float) -> str:
    """Return a dollar amount such as ``$1.2M``, ``$3.50`` or ``$0.00042``."""
    if value >= 1000:
        return f"${format_amount(value)}"
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.0001:
        return f"${value:.4g}"
    return f"${value:.2g}"


def found_line(contract: ConfirmedContract, sighting: Sighting | None) -> str | None:
    """Return where on the page the address stood and the text in front of it, or None."""
    if sighting is None:
        return None
    via = sighting.address != contract.address  # the page showed the token's LP pair
    where = f"{'Found via an LP pair in' if via else 'Found in'} {sighting.source.value}"
    return f"{where}: «{escape(sighting.context)}»" if sighting.context else where


def timeline_line(timeline: Timeline, sent_at: float) -> str:
    """Return the timing line of an alert: when the token showed up and when it was sent."""
    when = format_precise
    if timeline.deployed and timeline.on_page_since is not None and timeline.missing_at is not None:
        text = (
            f"on page since {when(timeline.on_page_since)} · deployed between "
            f"{when(timeline.missing_at)} and {when(timeline.found_at)}"
        )
    elif timeline.missing_at is not None:
        text = f"appeared between {when(timeline.missing_at)} and {when(timeline.found_at)}"
    elif timeline.on_page_since is not None:
        text = f"on page since {when(timeline.on_page_since)} · confirmed {when(timeline.found_at)}"
    else:
        text = f"found {when(timeline.found_at)} on the first check of the page"
    return f"⏱ {text} · sent {when(sent_at)} ({format_utc_offset(sent_at)})"


def link_lines(contract: ConfirmedContract, identifier: str | None = None) -> list[str]:
    """Return the line of explorer and market links, or no line when none are known.

    ``identifier`` stands in for the address in the links (the Sui coin type found
    behind a bare package id).
    """
    links = token_links(contract, identifier)
    return ["Links: " + " · ".join(link(label, url) for label, url in links)] if links else []


def _risk_lines(info: TokenInfo) -> list[str]:
    """Return the Solana lines on what the issuer can still do to holders (mint, freeze)."""
    lines: list[str] = []
    if info.mint_authority is not None and info.freeze_authority is not None:
        kept = [
            name
            for name, flag in (("mint", info.mint_authority), ("freeze", info.freeze_authority))
            if flag
        ]
        lines.append(
            f"⚠️ Authority kept: {', '.join(kept)}"
            if kept
            else "Authorities: mint and freeze revoked ✅"
        )
    if info.risky_extensions:
        lines.append("⚠️ Token-2022: " + ", ".join(escape(e) for e in info.risky_extensions))
    return lines


def _age_lines(info: TokenInfo, now: float) -> list[str]:
    """Return the line on how old the token is, or no line when that is unknown."""
    if info.created_at is not None:
        age = max(now - info.created_at, 0.0)
        if age < _HISTORY_SURELY_KEPT:
            return [f"🆕 Created {format_duration(age)} ago"]
        return [f"Created more than {format_duration(age)} ago"]
    if info.busy:
        return ["Active token: 1000+ transactions"]
    if info.deployed_within is not None:
        return [f"🆕 Deployed within the last {format_duration(info.deployed_within)}"]
    if info.deployed_before is not None:
        return [f"Deployed more than {format_duration(info.deployed_before)} ago"]
    return []


def describe_token(info: TokenInfo, now: float) -> list[str]:
    """Return the detail lines of a token alert; unknown parts are left out."""
    lines: list[str] = []
    if info.coin_type:
        lines.append(f"Coin type: {code(info.coin_type)}")
    identity: list[str] = []
    if info.name and info.symbol:
        identity.append(f"{escape(info.name)} ({escape(info.symbol)})")
    elif info.name or info.symbol:
        identity.append(escape(info.name or info.symbol))
    if info.decimals is not None:
        identity.append(f"{info.decimals} decimals")
    if info.supply is not None:
        identity.append(f"supply {format_amount(info.supply)}")
    if identity:
        lines.append("Token: " + " · ".join(identity))
    lines.extend(_risk_lines(info))
    lines.extend(_age_lines(info, now))
    if info.also_on:
        places = (
            f"{escape(network)} ({escape(symbol)})" if symbol else escape(network)
            for network, symbol in info.also_on
        )
        lines.append("Also on: " + ", ".join(places))
    if info.pair_url:
        # No link: the pair URL holds the pair's own address, and an alert names one token.
        market = [escape(f"{info.dex or 'DEX'} {info.pair or ''}".strip())]
        if info.price_usd is not None:
            market.append(f"price {format_usd(info.price_usd)}")
        if info.liquidity_usd is not None:
            market.append(f"liquidity {format_usd(info.liquidity_usd)}")
        if info.fdv_usd is not None:
            market.append(f"FDV {format_usd(info.fdv_usd)}")
        if info.pair_created_at is not None:
            market.append(f"listed {format_duration(now - info.pair_created_at)} ago")
        lines.append("Trading on " + " · ".join(market))
    elif info.market_checked:
        lines.append(NO_PAIR_YET)
    return lines
