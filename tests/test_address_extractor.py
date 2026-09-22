import pytest

from scraper_bot.address_extractor import (
    AddressExtractor,
    is_evm_address,
    is_solana_address,
    to_checksum_address,
)
from scraper_bot.models import Address, Chain

USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


@pytest.mark.parametrize(
    "checksummed",
    [
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
        "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
    ],
)
def test_eip55_checksum_vectors(checksummed: str) -> None:
    assert to_checksum_address(checksummed.lower()) == checksummed
    assert is_evm_address(checksummed)


def test_mixed_case_with_wrong_checksum_is_rejected() -> None:
    wrong = USDC_ETH[:-1] + ("8" if USDC_ETH[-1] != "8" else "9")
    assert not is_evm_address(wrong.swapcase().replace("0X", "0x"))
    assert is_evm_address(USDC_ETH.lower())
    assert is_evm_address("0x" + USDC_ETH[2:].upper())


def test_solana_address_validation() -> None:
    assert is_solana_address(USDC_SOL)
    assert is_solana_address("1" * 32)  # system program
    assert not is_solana_address("0OIl" * 11)  # characters outside base58
    assert not is_solana_address("abc")


def test_extractor_finds_both_chains_and_canonicalises() -> None:
    html = f"<p>token: {USDC_ETH.lower()}</p><span>{USDC_SOL}</span>"
    assert AddressExtractor().extract(html) == [
        Address(Chain.ETHEREUM, USDC_ETH),
        Address(Chain.SOLANA, USDC_SOL),
    ]


def test_extractor_deduplicates_and_keeps_first_seen_order() -> None:
    html = f"{USDC_SOL} {USDC_ETH} {USDC_ETH.lower()} {USDC_SOL}"
    assert AddressExtractor().extract(html) == [
        Address(Chain.ETHEREUM, USDC_ETH),
        Address(Chain.SOLANA, USDC_SOL),
    ]


def test_extractor_ignores_fragments_glued_to_other_characters() -> None:
    html = f"abc{USDC_ETH} {USDC_ETH}def x{USDC_SOL} {USDC_SOL}y"
    assert AddressExtractor().extract(html) == []


def test_extractor_ignores_hashes_and_long_runs() -> None:
    sha = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    long_run = "A" * 50
    hex_hash = "0x" + "ab" * 32  # 64 hex chars: too long for an address
    assert AddressExtractor().extract(f"{sha} {long_run} {hex_hash}") == []


def test_extractor_can_be_limited_to_one_chain() -> None:
    html = f"{USDC_ETH} {USDC_SOL}"
    assert AddressExtractor(chains=(Chain.SOLANA,)).extract(html) == [
        Address(Chain.SOLANA, USDC_SOL)
    ]
    assert AddressExtractor(chains=(Chain.ETHEREUM,)).extract(html) == [
        Address(Chain.ETHEREUM, USDC_ETH)
    ]


def test_extract_many_deduplicates_across_texts() -> None:
    texts = (f"<p>{USDC_ETH}</p>", f"var a = '{USDC_ETH.lower()}'; var b = '{USDC_SOL}';", "")
    assert AddressExtractor().extract_many(texts) == [
        Address(Chain.ETHEREUM, USDC_ETH),
        Address(Chain.SOLANA, USDC_SOL),
    ]
