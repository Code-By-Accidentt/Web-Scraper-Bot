"""Addresses and what the blockchains say about them."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum


class Chain(StrEnum):
    """The blockchain families the bot understands."""

    ETHEREUM = "Ethereum"
    SOLANA = "Solana"
    SUI = "Sui"


@dataclass(frozen=True, slots=True)
class Address:
    """A syntactically valid, canonical address (checksummed for EVM)."""

    chain: Chain
    value: str

    def __str__(self) -> str:
        """Return the bare address, without the chain."""
        return self.value


@dataclass(frozen=True, slots=True)
class ConfirmedContract:
    """An address an RPC node confirmed to be a contract, an SPL mint or a Sui coin/package.

    The coin type found behind a bare Sui package rides along for the alert's links and
    details; it is not part of the contract's identity.
    """

    address: Address
    network: str
    coin_type: str | None = field(default=None, compare=False)


class ValidationStatus(Enum):
    """The outcome of checking one address.

    The ALREADY_* statuses come from the bot's memory, without a new RPC call. UNAVAILABLE
    means no verdict: an endpoint failed (for EVM a required network, or every network, and
    none found a token), and nothing was cached.
    """

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    ALREADY_KNOWN = "already_known"
    ALREADY_REJECTED = "already_rejected"
    UNAVAILABLE = "unavailable"


class AddressKind(Enum):
    """What the chain says an address is, in more detail than "a token or not"."""

    TOKEN = "token"  # ERC-20, SPL / Token-2022 mint or Sui coin, the only kind we announce
    NO_CODE = "no_code"  # EVM: no code on networks that answered, so a wallet or not deployed yet
    WALLET = "wallet"  # EVM: an EIP-7702 delegated wallet (its "code" is 0xef0100 + address)
    PAIR = "pair"  # an LP pair or pool, with the tokens it holds in ``resolved``
    VAULT = "vault"  # ERC-4626 vault shares: a token, but never a launch
    STABLE_COPY = "stable_copy"  # a token copying a stablecoin's or wrapped coin's symbol
    NOT_TOKEN = "not_token"  # a contract, program or account that is not a token
    MISSING = "missing"  # Solana: no account at this address yet
    NOT_YET_MINTED = "not_yet_minted"  # Solana: a decimals-0 mint with nothing minted yet
    NFT = "nft"  # Solana: a decimals-0 mint with a supply of exactly one
    UNKNOWN = "unknown"  # we only have a cached verdict, or none at all


# Kinds that may still become a token later, so the address is worth watching.
MAY_APPEAR = frozenset({AddressKind.NO_CODE, AddressKind.MISSING, AddressKind.NOT_YET_MINTED})


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The verdict on one address: its status, the network and what kind of address it is."""

    address: Address
    status: ValidationStatus
    network: str | None = None
    kind: AddressKind = AddressKind.UNKNOWN
    resolved: tuple[Address, ...] = ()  # PAIR: its tokens (stop-listed ones left out)
    via: Address | None = None  # the LP pair we found this token through
    coin_type: str | None = None  # Sui: the coin found behind a bare package id

    @property
    def contract(self) -> ConfirmedContract | None:
        """The confirmed contract, or None unless the status is CONFIRMED with a network."""
        if self.status is ValidationStatus.CONFIRMED and self.network:
            return ConfirmedContract(self.address, self.network, self.coin_type)
        return None


LABEL_LIMIT = 64  # how many characters of a token name or symbol we keep


def clean_label(text: str) -> str | None:
    """Return a token name or symbol fit to show, or None when nothing is left.

    NUL characters and runs of whitespace become single spaces, and the text is cut to 64
    characters.
    """
    cleaned = " ".join(text.replace("\x00", " ").split())
    return cleaned[:LABEL_LIMIT] or None


def address_key(address: Address) -> str:
    """Return the ``Chain:value`` key used for an address in state.json and pending.json."""
    return f"{address.chain.value}:{address.value}"


def parse_address_key(key: str) -> Address | None:
    """Turn a ``Chain:value`` key back into an address, or None when the key is malformed.

    Only the chain name is checked; the value is taken as it is.
    """
    chain, _, value = key.partition(":")
    try:
        return Address(Chain(chain), value) if value else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ChainVerdict:
    """What one chain says about an address."""

    kind: AddressKind
    network: str | None = None  # where the token, LP pair or other contract was found
    sides: tuple[str, ...] = ()  # PAIR: the tokens it holds, stop-listed ones left out
    # optional networks that didn't answer (failed, slow or resting), so the verdict may change
    missing: frozenset[str] = frozenset()
    symbol: str | None = None  # STABLE_COPY: the symbol it copies, e.g. "USDC.e"
    coin_type: str | None = None  # Sui: the coin found behind a bare package id

    @property
    def partial(self) -> bool:
        """True when some optional networks did not answer, so the verdict is not final."""
        return bool(self.missing)
