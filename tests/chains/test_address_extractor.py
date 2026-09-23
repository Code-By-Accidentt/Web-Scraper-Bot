import pytest

from scraper_bot.chains.address_extractor import (
    AddressExtractor,
    detect_chain,
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


SUI_PKG = "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7"
SUI_COIN = f"{SUI_PKG}::usdc::USDC"


def test_sui_coin_types_are_extracted_and_normalised() -> None:
    shouting = "0x" + SUI_PKG[2:].upper()
    text = f"CA: {shouting}::usdc::USDC, native 0x2::sui::SUI, book 0xdee9::clob::Pool"
    assert AddressExtractor().extract(text) == [Address(Chain.SUI, SUI_COIN)]
    generic = f"Coin<{SUI_COIN}> in 0x2::coin::Coin"
    assert AddressExtractor().extract(generic) == [Address(Chain.SUI, SUI_COIN)]


def test_bare_sui_ids_need_sui_context_and_yield_to_coin_types() -> None:
    tx_hash = "0x" + "9f" * 32
    assert AddressExtractor().extract(f"Tx {tx_hash} confirmed on Etherscan") == []
    assert AddressExtractor().extract(f"Package {tx_hash} is live on Sui") == [
        Address(Chain.SUI, tx_hash)
    ]
    both = f"{SUI_PKG} publishes {SUI_COIN} (Suiscan)"
    assert AddressExtractor().extract(both) == [Address(Chain.SUI, SUI_COIN)]


def test_hex_runs_inside_sui_ids_are_not_solana_candidates() -> None:
    sui_id = "0x0" + "a1b2c3d4e5f6a7b8c9d1e2f3a4b5c6d7e8f9a1b2c3d" + "0" + "ffeeddccbbaa9988776"
    assert len(sui_id) == 66
    found = AddressExtractor().extract(f"Object {sui_id} on Sui")
    assert found == [Address(Chain.SUI, sui_id)]


def test_detect_chain_for_context_free_addresses() -> None:
    assert detect_chain(SUI_COIN) is Chain.SUI
    assert detect_chain(SUI_PKG) is Chain.SUI
    assert detect_chain(USDC_ETH) is Chain.ETHEREUM
    assert detect_chain(USDC_SOL) is Chain.SOLANA


def test_evm_pattern_stops_before_a_sui_type_separator() -> None:
    forty = "0x" + "ab" * 20
    found = AddressExtractor().extract(f"Type {forty}::meme::MEME on Sui")
    assert found == [Address(Chain.SUI, "0x" + "0" * 24 + "ab" * 20 + "::meme::MEME")]
    assert AddressExtractor().extract(f"Wallet {forty} here") == [
        Address(Chain.ETHEREUM, to_checksum_address(forty))
    ]
