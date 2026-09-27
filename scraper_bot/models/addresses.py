"""Addresses and what the blockchains say about them."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum


class Chain(StrEnum):
    ETHEREUM = "Ethereum"
    SOLANA = "Solana"
    SUI = "Sui"


@dataclass(frozen=True, slots=True)
class Address:
    """A syntactically valid, canonical address (checksummed for EVM)."""

    chain: Chain
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ConfirmedContract:
    """An address an RPC node confirmed to be a contract, an SPL mint or a Sui coin/package."""

    address: Address
    network: str


class ValidationStatus(Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ALREADY_KNOWN = "already_known"
    ALREADY_REJECTED = "already_rejected"
    # no verdict: an endpoint failed (EVM: a required network, or every network, and none
    # found a token); nothing was cached
    UNAVAILABLE = "unavailable"


class AddressKind(Enum):
    """What the chain says an address is (beyond "a token or not")."""

    TOKEN = "token"  # ERC-20, SPL / Token-2022 mint or Sui coin: the only kind announced
    NO_CODE = "no_code"  # EVM: no bytecode on the networks that answered: a wallet or not yet
    WALLET = "wallet"  # EVM: an EIP-7702 delegated wallet (its "code" is 0xef0100 + address)
    PAIR = "pair"  # an LP pair or pool; the tokens it holds are in ``resolved``
    VAULT = "vault"  # ERC-4626 vault shares: a token, but never a launch
    NOT_TOKEN = "not_token"  # a contract, program or account that is not a token
    MISSING = "missing"  # Solana: no account at this address yet
    NOT_YET_MINTED = "not_yet_minted"  # Solana: a decimals-0 mint with nothing minted yet
    NFT = "nft"  # Solana: a decimals-0 mint with a supply of exactly one
    UNKNOWN = "unknown"  # a cached verdict or no verdict at all


# Kinds that may still turn into a token later: the address is worth watching.
MAY_APPEAR = frozenset({AddressKind.NO_CODE, AddressKind.MISSING, AddressKind.NOT_YET_MINTED})


@dataclass(frozen=True, slots=True)
class ValidationResult:
    address: Address
    status: ValidationStatus
    network: str | None = None
    kind: AddressKind = AddressKind.UNKNOWN
    resolved: tuple[Address, ...] = ()  # PAIR: its tokens (stop-listed ones left out)
    via: Address | None = None  # this token was found through that LP pair

    @property
    def contract(self) -> ConfirmedContract | None:
        if self.status is ValidationStatus.CONFIRMED and self.network:
            return ConfirmedContract(self.address, self.network)
        return None


LABEL_LIMIT = 64  # characters kept of a token name or symbol


def clean_label(text: str) -> str | None:
    """A token name or symbol fit to show: NULs and runs of spaces gone, cut to 64 chars."""
    cleaned = " ".join(text.replace("\x00", " ").split())
    return cleaned[:LABEL_LIMIT] or None


def address_key(address: Address) -> str:
    """``Chain:value``, how addresses are keyed in state.json and pending.json."""
    return f"{address.chain.value}:{address.value}"


def parse_address_key(key: str) -> Address | None:
    chain, _, value = key.partition(":")
    try:
        return Address(Chain(chain), value) if value else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ChainVerdict:
    """What one chain says about an address."""

    kind: AddressKind
    network: str | None = None  # where the token (or the LP pair) lives
    sides: tuple[str, ...] = ()  # PAIR: the tokens it holds, stop-listed ones left out
    # optional networks that did not answer (failed, too slow, resting): it may change there
    missing: frozenset[str] = frozenset()

    @property
    def partial(self) -> bool:
        return bool(self.missing)
