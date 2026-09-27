"""Address formats of every chain: checks, checksums and canonical forms."""

from __future__ import annotations

import re

import base58
from Crypto.Hash import keccak

from scraper_bot.models.addresses import Address, Chain

# Sui names a coin by its type ``0x<package>::<module>::<NAME>``; a bare 32-byte id may
# be the package that defines it. Ethereum transaction hashes have the same 64-hex
# shape, so bare ids only count on pages that talk about Sui.
MOVE_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

_EVM_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SUI_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_SUI_COIN_TYPE_FULL_RE = re.compile(rf"^0x[0-9a-fA-F]{{1,64}}::{MOVE_IDENT}::{MOVE_IDENT}$")
_HEX_ONLY_RE = re.compile(r"^[0-9a-fA-F]+$")
# Package ids below this value are the Move stdlib, the Sui framework and system
# packages (0x1, 0x2, 0x3, 0xdee9, ...): their coins, e.g. 0x2::sui::SUI, are never a launch.
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
    """``None`` unless ``raw`` is a valid address of ``chain`` (EVM ones come back checksummed)."""
    if chain is Chain.ETHEREUM:
        return Address(chain, to_checksum_address(raw)) if is_evm_address(raw) else None
    if chain is Chain.SOLANA:
        if _HEX_ONLY_RE.match(raw):
            return None  # all-hex text is a slice of some 0x... id, not a base58 key
        if raw.islower() or raw.isupper():
            # base32 (IPFS, Arweave) or a single-case word: a random 32-byte key in base58
            # has both cases (a single-case one is rarer than 1 in 10 billion).
            return None
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
