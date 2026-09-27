"""Finding addresses in page text."""

from __future__ import annotations

import re
from collections.abc import Callable, Container, Iterable
from typing import ClassVar, TypeVar

from scraper_bot.chains.addresses.formats import MOVE_IDENT, canonical_address, sui_package
from scraper_bot.models.addresses import Address, Chain

# No match may be glued to other alphanumerics: those are hash fragments or minified names.
# ``::`` after 40 hex digits makes a short Sui coin type, not an EVM address.
EVM_ADDRESS_RE = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z]|::)")
_BASE58 = "1-9A-HJ-NP-Za-km-z"
SOLANA_ADDRESS_RE = re.compile(rf"(?<![0-9A-Za-z])[{_BASE58}]{{32,44}}(?![0-9A-Za-z])")
# Base64 data: URIs (also JSON-escaped, image\/png) are full of base58-looking runs.
_DATA_URI_RE = re.compile(
    r"data:[\w.+-]+\\?/[\w.+-]+(?:;[\w.+-]+=[^;,\s\"'<>]*)*;base64,[A-Za-z0-9+/=\\_-]*",
    re.IGNORECASE,
)
SUI_COIN_TYPE_RE = re.compile(
    rf"(?<![0-9A-Za-z_])0x[0-9a-fA-F]{{1,64}}::{MOVE_IDENT}::{MOVE_IDENT}(?![0-9A-Za-z_:])"
)
# Ethereum tx hashes are 64 hex too, so bare Sui ids count only on pages that mention Sui.
SUI_ADDRESS_RE = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{64}(?![0-9A-Za-z])")
SUI_CONTEXT_RE = re.compile(r"\bsui\b|suiscan|suivision|cetus|bluefin|turbos|movepump", re.I)

# The shortest address is a Sui coin type such as 0x10000::a::B.
_SHORTEST_ADDRESS = 13


class AddressExtractor:
    """Finds addresses of the chosen chains in text, leaving out those on the stop-list."""

    _PATTERNS: ClassVar[dict[Chain, tuple[re.Pattern[str], ...]]] = {
        Chain.ETHEREUM: (EVM_ADDRESS_RE,),
        Chain.SOLANA: (SOLANA_ADDRESS_RE,),
        Chain.SUI: (SUI_COIN_TYPE_RE, SUI_ADDRESS_RE),
    }

    def __init__(
        self,
        chains: Iterable[Chain] = (Chain.ETHEREUM, Chain.SOLANA, Chain.SUI),
        *,
        stoplist: Container[Address] = (),
    ) -> None:
        """Look only for addresses of ``chains`` and skip those in ``stoplist``."""
        self._chains = tuple(chains)
        self._stoplist = stoplist

    def locate(self, text: str, *, sui_context: bool | None = None) -> list[tuple[Address, int]]:
        """Return every valid address in ``text`` with its start offset, in text order.

        ``sui_context`` says whether the page talks about Sui: bare 64-hex Sui ids count
        only then. When it is None, ``text`` itself decides.
        """
        if len(text) < _SHORTEST_ADDRESS:
            return []
        if sui_context is None:
            sui_context = SUI_CONTEXT_RE.search(text) is not None
        if "base64," in text or "BASE64," in text or "Base64," in text:
            # Blanked out, not cut out: offsets and the context around matches stay the same.
            text = _DATA_URI_RE.sub(lambda match: " " * len(match.group(0)), text)
        found: list[tuple[Address, int]] = []
        for chain in self._chains:
            for pattern in self._PATTERNS[chain]:
                if pattern is SUI_ADDRESS_RE and not sui_context:
                    continue
                for match in pattern.finditer(text):
                    address = canonical_address(chain, match.group(0))
                    if address is not None and address not in self._stoplist:
                        found.append((address, match.start()))
        found.sort(key=lambda item: item[1])
        return found


T = TypeVar("T")


def drop_coin_packages(items: list[T], address: Callable[[T], Address]) -> list[T]:
    """Return ``items`` without the bare Sui packages whose coin type is among them too.

    A page that names a coin type often shows its package as well; only the coin is
    reported. ``address`` gets the address out of an item.
    """
    coins = [a for a in map(address, items) if a.chain is Chain.SUI and "::" in a.value]
    packages = {sui_package(a.value) for a in coins}
    return [
        item
        for item in items
        if not (
            (found := address(item)).chain is Chain.SUI
            and "::" not in found.value
            and found.value in packages
        )
    ]
