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

from scraper_bot.chains.addresses.extractor import (
    SUI_CONTEXT_RE,
    AddressExtractor,
    drop_coin_packages,
)
from scraper_bot.models.addresses import Address
from scraper_bot.models.pages import Signature
from scraper_bot.models.sightings import Sighting, SightingSource

CONTEXT_CHARS = 60
HINT_WINDOW = 32  # characters before the address that decide the hint (as in X Scraper)
_RAW_WINDOW = 400  # characters of raw HTML or script read back for the context

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


# Fields a captcha fills with a fresh random token on every render: never an address.
_CAPTCHA_FIELDS = frozenset({"g-recaptcha-response", "h-captcha-response", "cf-turnstile-response"})
_CAPTCHA_FIELD_RE = re.compile(
    r"<(textarea|input)\b[^>]{0,300}?\bname=[\"'](?:g-recaptcha|h-captcha|cf-turnstile)-response"
    r"[\"'][^>]{0,4000}?>(?:[^<]{0,4000}</textarea>)?",
    re.IGNORECASE,
)


def shown_on_a_challenge(sightings: Iterable[Sighting]) -> list[Sighting]:
    """Return the sightings a page that looks like a challenge shows as its own content.

    That is its text and the links that name a token. Its source and form values are left
    out: challenge scripts keep fresh random tokens there, some shaped like a Solana key.
    """
    return [
        s
        for s in sightings
        if s.source is SightingSource.TEXT or (s.source is SightingSource.LINK and s.hinted)
    ]


def find_sightings(
    extractor: AddressExtractor,
    signature: Signature,
    html: str,
    extras: Sequence[str] = (),
    dom: Sequence[str] = (),
) -> list[Sighting]:
    """Return one sighting per address in order of first appearance, the most telling one kept.

    Text and attributes come from ``signature``, where scripts, styles and comments are
    already gone. An address only in ``html`` or in frames and shadow roots (``dom``) counts
    as page source, one only in the other ``extras`` (deep scan) as a loaded script. A hinted
    sighting beats an unhinted one, then text beats a link; source and scripts count only for
    addresses no element shows.
    """
    # Bare 64-hex ids are Sui only where the text talks about Sui; each script decides alone.
    sui_context = SUI_CONTEXT_RE.search(html) is not None
    if "-response" in html:
        html = _CAPTCHA_FIELD_RE.sub(lambda match: " " * len(match.group(0)), html)
    best: dict[Address, Sighting] = {}
    previous = ""
    for node in signature:
        if any(name == "name" and value in _CAPTCHA_FIELDS for name, value in node.attrs):
            continue
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
    # Before extras, which also hold dom: frames and shadow roots are page source, not scripts.
    _offer_raw(best, extractor, dom, SightingSource.SOURCE, None)
    _offer_raw(best, extractor, extras, SightingSource.SCRIPT, None)
    return drop_coin_packages(list(best.values()), lambda sighting: sighting.address)


def hint_score(before: str) -> int:
    """Return a positive score when the words right before an address name it as a token.

    Only the last ``HINT_WINDOW`` characters count: each token word adds one, each word
    pointing elsewhere (wallet, treasury, an explorer's address page) takes one away.
    """
    window = before[-HINT_WINDOW:]
    return len(_POSITIVE_RE.findall(window)) - len(_NEGATIVE_RE.findall(window))


def _offer_raw(
    best: dict[Address, Sighting],
    extractor: AddressExtractor,
    texts: Iterable[str],
    source: SightingSource,
    sui_context: bool | None,
) -> None:
    """Add a sighting from ``source`` for each address in ``texts`` that has none yet.

    With ``sui_context`` None each text decides for itself whether bare hex ids are Sui.
    """
    for text in texts:
        for address, start in extractor.locate(text, sui_context=sui_context):
            if address not in best:
                # raw markup: "&amp;" and "&quot;" read as the page shows them
                before = html_lib.unescape(text[max(start - _RAW_WINDOW, 0) : start])
                best[address] = _sighting(address, source, before)


def _sighting(address: Address, source: SightingSource, before: str) -> Sighting:
    """Build a sighting whose context is the text before the address, others masked as "…".

    The context keeps the last ``CONTEXT_CHARS`` characters; the hint is scored on it too.
    """
    masked = _SPACES_RE.sub(" ", _ADDRESS_LIKE_RE.sub("…", before)).strip()
    return Sighting(address, source, masked[-CONTEXT_CHARS:], hint_score(masked) > 0)


def _offer(best: dict[Address, Sighting], sighting: Sighting) -> None:
    """Keep ``sighting`` if its address has none yet or it ranks better than the one kept."""
    current = best.get(sighting.address)
    if current is None or _rank(sighting) < _rank(current):
        best[sighting.address] = sighting


def _rank(sighting: Sighting) -> tuple[bool, int]:
    """Return the sort key of a sighting (lower wins): hinted first, then by source."""
    return not sighting.hinted, _RANK[sighting.source]
