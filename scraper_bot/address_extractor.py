"""Finding candidate contract addresses in raw page HTML."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import ClassVar

import base58
from Crypto.Hash import keccak

from scraper_bot.models import Address, Chain

# All patterns refuse matches glued to other alphanumerics, so fragments of hashes,
# base64 blobs and minified identifiers do not become candidates.
EVM_ADDRESS_RE = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z])")
_BASE58 = "1-9A-HJ-NP-Za-km-z"
SOLANA_ADDRESS_RE = re.compile(rf"(?<![{_BASE58}])[{_BASE58}]{{32,44}}(?![{_BASE58}])")
# Sui names a coin by its type ``0x<package>::<module>::<NAME>``; a bare 32-byte id may
# be the package that defines it. Ethereum transaction hashes have the same 64-hex
# shape, so bare ids only count on pages that talk about Sui.
_MOVE_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
SUI_COIN_TYPE_RE = re.compile(
    rf"(?<![0-9A-Za-z_])0x[0-9a-fA-F]{{1,64}}::{_MOVE_IDENT}::{_MOVE_IDENT}(?![0-9A-Za-z_:])"
)
SUI_ADDRESS_RE = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{64}(?![0-9A-Za-z])")
SUI_CONTEXT_RE = re.compile(r"\bsui\b|suiscan|suivision|cetus|bluefin|turbos|movepump", re.I)

_EVM_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SUI_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_SUI_COIN_TYPE_FULL_RE = re.compile(rf"^0x[0-9a-fA-F]{{1,64}}::{_MOVE_IDENT}::{_MOVE_IDENT}$")
_HEX_ONLY_RE = re.compile(r"^[0-9a-fA-F]+$")
# Package ids below this value are the Move stdlib, the Sui framework and system
# packages (0x1, 0x2, 0x3, 0xdee9, ...): their coins, e.g. 0x2::sui::SUI, are no launch.
_SUI_RESERVED_PACKAGES = 0x10000


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


def is_sui_address(text: str) -> bool:
    """A bare 32-byte Sui id: ``0x`` and 64 hex digits."""
    return bool(_SUI_HEX_RE.match(text))


def is_sui_coin_type(text: str) -> bool:
    """``0x<package>::<module>::<Name>``, the identifier of a Sui coin."""
    return bool(_SUI_COIN_TYPE_FULL_RE.match(text))


def sui_package(value: str) -> str:
    """The package part of a coin type; a bare address is its own package."""
    return value.split("::", 1)[0]


def normalize_sui(raw: str) -> str | None:
    """Canonical Sui form: lower-case 64-hex package, module and name kept as written.

    Short forms are padded (``0x2`` -> ``0x000...002``); reserved packages return None.
    """
    package, separator, rest = raw.partition("::")
    if separator:
        if not is_sui_coin_type(raw):
            return None
    elif not is_sui_address(raw):
        return None
    number = int(package[2:], 16)
    if number < _SUI_RESERVED_PACKAGES:
        return None
    canonical = f"0x{number:064x}"
    return f"{canonical}::{rest}" if separator else canonical


def canonical_address(chain: Chain, raw: str) -> Address | None:
    """Validate ``raw`` for ``chain`` and return it in canonical form."""
    if chain is Chain.ETHEREUM:
        return Address(chain, to_checksum_address(raw)) if is_evm_address(raw) else None
    if chain is Chain.SOLANA:
        if _HEX_ONLY_RE.match(raw):
            return None  # a run of hex digits out of some 0x... id, never a real key
        return Address(chain, raw) if is_solana_address(raw) else None
    if chain is Chain.SUI:
        value = normalize_sui(raw)
        return Address(chain, value) if value else None
    return None


def detect_chain(raw: str) -> Chain | None:
    """Best guess for an address written without any context (legacy files)."""
    if "::" in raw or is_sui_address(raw):
        return Chain.SUI
    if raw.lower().startswith("0x"):
        return Chain.ETHEREUM
    return Chain.SOLANA


class AddressExtractor:
    """Scans text for addresses of the configured chains."""

    _PATTERNS: ClassVar[dict[Chain, tuple[re.Pattern[str], ...]]] = {
        Chain.ETHEREUM: (EVM_ADDRESS_RE,),
        Chain.SOLANA: (SOLANA_ADDRESS_RE,),
        Chain.SUI: (SUI_COIN_TYPE_RE, SUI_ADDRESS_RE),
    }

    def __init__(self, chains: Iterable[Chain] = (Chain.ETHEREUM, Chain.SOLANA, Chain.SUI)) -> None:
        self._chains = tuple(chains)

    def extract(self, text: str) -> list[Address]:
        """Unique, syntactically valid addresses in order of first appearance."""
        return self.extract_many((text,))

    def extract_many(self, texts: Iterable[str]) -> list[Address]:
        """Like :meth:`extract` over several texts (page HTML, frames, scripts, API bodies)."""
        found: dict[Address, None] = {}
        for text in texts:
            sui_context = SUI_CONTEXT_RE.search(text) is not None
            for chain in self._chains:
                for pattern in self._PATTERNS[chain]:
                    if pattern is SUI_ADDRESS_RE and not sui_context:
                        continue
                    for match in pattern.finditer(text):
                        address = canonical_address(chain, match.group(0))
                        if address is not None:
                            found.setdefault(address, None)
        return _drop_packages_of_coins(list(found))


def _drop_packages_of_coins(addresses: list[Address]) -> list[Address]:
    """A page naming a coin type also shows its package: report the coin, not both."""
    packages = {sui_package(a.value) for a in addresses if a.chain is Chain.SUI and "::" in a.value}
    return [
        a
        for a in addresses
        if not (a.chain is Chain.SUI and "::" not in a.value and a.value in packages)
    ]
