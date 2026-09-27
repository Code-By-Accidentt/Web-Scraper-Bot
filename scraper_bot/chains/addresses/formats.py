"""Address formats of every chain: validity checks, checksums and canonical forms.

A Sui coin is named by its type ``0x<package>::<module>::<NAME>``; a bare 32-byte Sui id
may be the package that defines it.
"""

from __future__ import annotations

import re

import base58
from Crypto.Hash import keccak

from scraper_bot.models.addresses import Address, Chain

MOVE_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

_EVM_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SUI_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_SUI_COIN_TYPE_FULL_RE = re.compile(rf"^0x[0-9a-fA-F]{{1,64}}::{MOVE_IDENT}::{MOVE_IDENT}$")
_HEX_ONLY_RE = re.compile(r"^[0-9a-fA-F]+$")
# Lower ids are stdlib and system packages (0x1, 0x2, 0xdee9...): their coins are no launch.
_SUI_RESERVED_PACKAGES = 0x10000


def to_checksum_address(address: str) -> str:
    """Return the EIP-55 mixed-case form of a ``0x`` address."""
    body = address[2:].lower()
    digest = keccak.new(digest_bits=256, data=body.encode("ascii")).hexdigest()
    return "0x" + "".join(
        ch.upper() if int(digest[i], 16) >= 8 else ch for i, ch in enumerate(body)
    )


def is_evm_address(text: str) -> bool:
    """Return True for ``0x`` plus 40 hex digits.

    Mixed-case input must also carry a valid EIP-55 checksum; all-lower or all-upper
    input passes without one.
    """
    if not _EVM_HEX_RE.match(text):
        return False
    body = text[2:]
    has_lower = any(ch.islower() for ch in body)
    has_upper = any(ch.isupper() for ch in body)
    if has_lower and has_upper:
        return to_checksum_address(text) == text
    return True


def is_solana_address(text: str) -> bool:
    """Return True when ``text`` is base58 and decodes to exactly 32 bytes."""
    try:
        return len(base58.b58decode(text)) == 32
    except ValueError:
        return False


def is_sui_address(text: str) -> bool:
    """Return True for a bare Sui id: ``0x`` plus exactly 64 hex digits."""
    return bool(_SUI_HEX_RE.match(text))


def is_sui_coin_type(text: str) -> bool:
    """Return True for a Sui coin type, ``0x<package>::<module>::<Name>``."""
    return bool(_SUI_COIN_TYPE_FULL_RE.match(text))


def sui_package(value: str) -> str:
    """Return the package part of a Sui coin type; a bare id is its own package."""
    return value.split("::", 1)[0]


def normalize_sui(raw: str) -> str | None:
    """Return the canonical form of a Sui id or coin type, or None if it is not valid.

    The package is padded to lower-case 64-hex (``0x2`` -> ``0x000...002``); module and
    name stay as written. Stdlib and system packages return None: they are never a launch.
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
    """Return ``raw`` as an address of ``chain``, or None if it is not a valid one.

    EVM addresses come back checksummed and Sui ones in canonical form.
    """
    if chain is Chain.ETHEREUM:
        return Address(chain, to_checksum_address(raw)) if is_evm_address(raw) else None
    if chain is Chain.SOLANA:
        if _HEX_ONLY_RE.match(raw):
            return None  # all-hex text is a slice of some 0x... id, not a base58 key
        if raw.islower() or raw.isupper():
            # Base32 (IPFS, Arweave) or a word: a random base58 key is almost never one-case.
            return None
        return Address(chain, raw) if is_solana_address(raw) else None
    if chain is Chain.SUI:
        value = normalize_sui(raw)
        return Address(chain, value) if value else None
    return None


def detect_chain(raw: str) -> Chain | None:
    """Guess the chain of an address written without context (ignore.txt, legacy files).

    Anything that is not Sui or ``0x`` is taken for Solana; the caller still validates it.
    """
    if "::" in raw or is_sui_address(raw):
        return Chain.SUI
    if raw.lower().startswith("0x"):
        return Chain.ETHEREUM
    return Chain.SOLANA
