"""Finding candidate contract addresses in raw page HTML."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import ClassVar

import base58
from Crypto.Hash import keccak

from scraper_bot.models import Address, Chain

# Both patterns refuse matches glued to other alphanumerics, so fragments of hashes,
# base64 blobs and minified identifiers do not become candidates.
EVM_ADDRESS_RE = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z])")
_BASE58 = "1-9A-HJ-NP-Za-km-z"
SOLANA_ADDRESS_RE = re.compile(rf"(?<![{_BASE58}])[{_BASE58}]{{32,44}}(?![{_BASE58}])")

_EVM_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def to_checksum_address(address: str) -> str:
    """EIP-55 mixed-case form of a ``0x`` address."""
    body = address[2:].lower()
    digest = keccak.new(digest_bits=256, data=body.encode("ascii")).hexdigest()
    return "0x" + "".join(
        ch.upper() if int(digest[i], 16) >= 8 else ch for i, ch in enumerate(body)
    )


def is_evm_address(text: str) -> bool:
    """Syntactic check; mixed-case input must also carry a valid EIP-55 checksum."""
    if not _EVM_HEX_RE.match(text):
        return False
    body = text[2:]
    has_lower = any(ch.islower() for ch in body)
    has_upper = any(ch.isupper() for ch in body)
    if has_lower and has_upper:
        return to_checksum_address(text) == text
    return True


def is_solana_address(text: str) -> bool:
    """True when ``text`` is base58 and decodes to exactly 32 bytes."""
    try:
        return len(base58.b58decode(text)) == 32
    except ValueError:
        return False


def canonical_address(chain: Chain, raw: str) -> Address | None:
    """Validate ``raw`` for ``chain`` and return it in canonical form."""
    if chain is Chain.ETHEREUM:
        return Address(chain, to_checksum_address(raw)) if is_evm_address(raw) else None
    if chain is Chain.SOLANA:
        return Address(chain, raw) if is_solana_address(raw) else None
    return None


class AddressExtractor:
    """Scans text for addresses of the configured chains."""

    _PATTERNS: ClassVar[dict[Chain, re.Pattern[str]]] = {
        Chain.ETHEREUM: EVM_ADDRESS_RE,
        Chain.SOLANA: SOLANA_ADDRESS_RE,
    }

    def __init__(self, chains: Iterable[Chain] = (Chain.ETHEREUM, Chain.SOLANA)) -> None:
        self._chains = tuple(chains)

    def extract(self, text: str) -> list[Address]:
        """Unique, syntactically valid addresses in order of first appearance."""
        found: dict[Address, None] = {}
        for chain in self._chains:
            for match in self._PATTERNS[chain].finditer(text):
                address = canonical_address(chain, match.group(0))
                if address is not None:
                    found.setdefault(address, None)
        return list(found)
