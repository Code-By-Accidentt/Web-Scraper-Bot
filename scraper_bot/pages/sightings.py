"""Where on a page each address sits, and what the page says right in front of it.

An alert quotes that context ("CA: …", "Treasury: …", a Basescan link) so a person can
tell the launch from a partner token or an address posted in a comment. The same words
decide whether an address that is not on-chain yet is worth watching closely.
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Iterable, Sequence
from urllib.parse import unquote

from scraper_bot.chains.address_extractor import SUI_CONTEXT_RE, AddressExtractor, sui_package
from scraper_bot.models import Address, Chain, Sighting, SightingSource, Signature

CONTEXT_CHARS = 60
HINT_WINDOW = 32  # characters before the address that decide the hint (as in X Scraper)
_RAW_WINDOW = 400  # characters of raw HTML or script read back for the context

# Words that point at a token's own address, and words that point elsewhere.
_POSITIVE_RE = re.compile(
    r"\b(?:ca|contract|mint|token)\b|/coin/|/token/|pump\.fun/|dexscreener\.com/|birdeye\.so/"
    r"|gmgn\.ai/",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"\b(?:wallets?|devs?|deployer|creator|treasury|vault|multisig|donate|tips?|funds?|payout)\b"
    r"|/address/|/account/",
    re.IGNORECASE,
)
# Other addresses in a context are masked: an alert names exactly one address.
_ADDRESS_LIKE_RE = re.compile(r"0x[0-9a-fA-F]{8,}(?:::\w+::\w+)?|[1-9A-HJ-NP-Za-km-z]{32,44}")
_SPACES_RE = re.compile(r"\s+")
_RANK = {
    SightingSource.TEXT: 0,
    SightingSource.LINK: 1,
    SightingSource.SOURCE: 2,
    SightingSource.SCRIPT: 3,
}


def find_sightings(
    extractor: AddressExtractor,
    signature: Signature,
    html: str,
    extras: Sequence[str] = (),
    dom: Sequence[str] = (),
) -> list[Sighting]:
    """One sighting per address in order of first appearance, the most telling one kept.

    Visible text and attributes come from ``signature`` (scripts, styles and comments are
    already gone there). What is left only in ``html``, frames or shadow roots (``dom``) is
    page source; what is only in the other ``extras`` (deep scan) is a loaded script. Among
    text and attribute sightings a hinted one beats an unhinted one, then text beats link.
    Page source and loaded scripts count only for addresses no element shows.
    """
    # Bare 64-hex ids count as Sui only where the text itself talks about Sui: the page
    # decides for its elements and source, each loaded script for itself.
    sui_context = SUI_CONTEXT_RE.search(html) is not None
    best: dict[Address, Sighting] = {}
    previous = ""
    for node in signature:
        for address, start in extractor.locate(node.text, sui_context=sui_context):
            _offer(best, _sighting(address, SightingSource.TEXT, f"{previous} {node.text[:start]}"))
        for name, value in node.attrs:
            # Query strings arrive percent-encoded ("0x..%3A%3Acoin%3A%3ACOIN" for a coin type).
            value = unquote(value)
            for address, start in extractor.locate(value, sui_context=sui_context):
                _offer(best, _sighting(address, SightingSource.LINK, f"{name}={value[:start]}"))
        if node.text:
            previous = node.text
    _offer_raw(best, extractor, (html,), SightingSource.SOURCE, sui_context)
    # Frames and shadow roots (``dom``, also among ``extras``) are page content, not scripts.
    _offer_raw(best, extractor, dom, SightingSource.SOURCE, None)
    _offer_raw(best, extractor, extras, SightingSource.SCRIPT, None)
    return _without_packages_of_coins(list(best.values()))


def hint_score(before: str) -> int:
    """Positive when the words right before an address name it as a token."""
    window = before[-HINT_WINDOW:]
    return len(_POSITIVE_RE.findall(window)) - len(_NEGATIVE_RE.findall(window))


def _offer_raw(
    best: dict[Address, Sighting],
    extractor: AddressExtractor,
    texts: Iterable[str],
    source: SightingSource,
    sui_context: bool | None,
) -> None:
    for text in texts:
        for address, start in extractor.locate(text, sui_context=sui_context):
            if address not in best:
                # raw markup: "&amp;" and "&quot;" read as the page shows them
                before = html_lib.unescape(text[max(start - _RAW_WINDOW, 0) : start])
                best[address] = _sighting(address, source, before)


def _sighting(address: Address, source: SightingSource, before: str) -> Sighting:
    masked = _SPACES_RE.sub(" ", _ADDRESS_LIKE_RE.sub("…", before)).strip()
    return Sighting(address, source, masked[-CONTEXT_CHARS:], hint_score(masked) > 0)


def _offer(best: dict[Address, Sighting], sighting: Sighting) -> None:
    current = best.get(sighting.address)
    if current is None or _rank(sighting) < _rank(current):
        best[sighting.address] = sighting


def _rank(sighting: Sighting) -> tuple[bool, int]:
    return not sighting.hinted, _RANK[sighting.source]


def _without_packages_of_coins(sightings: list[Sighting]) -> list[Sighting]:
    """A page naming a Sui coin type also shows its package: keep the coin only."""
    coins = {
        sui_package(s.address.value)
        for s in sightings
        if s.address.chain is Chain.SUI and "::" in s.address.value
    }
    return [
        s
        for s in sightings
        if not (
            s.address.chain is Chain.SUI
            and "::" not in s.address.value
            and s.address.value in coins
        )
    ]
