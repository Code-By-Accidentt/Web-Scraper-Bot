import pytest

from scraper_bot.chains.addresses.extractor import AddressExtractor
from scraper_bot.chains.addresses.formats import (
    detect_chain,
    is_evm_address,
    is_solana_address,
    to_checksum_address,
)
from scraper_bot.models.addresses import Address, Chain
from tests.conftest import extract

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
    assert extract(AddressExtractor(), html) == [
        Address(Chain.ETHEREUM, USDC_ETH),
        Address(Chain.SOLANA, USDC_SOL),
    ]


def test_extractor_deduplicates_and_keeps_first_seen_order() -> None:
    html = f"{USDC_SOL} {USDC_ETH} {USDC_ETH.lower()} {USDC_SOL}"
    assert extract(AddressExtractor(), html) == [
        Address(Chain.SOLANA, USDC_SOL),
        Address(Chain.ETHEREUM, USDC_ETH),
    ]


def test_extractor_ignores_fragments_glued_to_other_characters() -> None:
    html = f"abc{USDC_ETH} {USDC_ETH}def x{USDC_SOL} {USDC_SOL}y"
    assert extract(AddressExtractor(), html) == []


def test_extractor_ignores_hashes_and_long_runs() -> None:
    sha = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    long_run = "A" * 50
    hex_hash = "0x" + "ab" * 32  # 64 hex chars: too long for an address
    assert extract(AddressExtractor(), f"{sha} {long_run} {hex_hash}") == []


def test_extractor_can_be_limited_to_one_chain() -> None:
    html = f"{USDC_ETH} {USDC_SOL}"
    assert extract(AddressExtractor(chains=(Chain.SOLANA,)), html) == [
        Address(Chain.SOLANA, USDC_SOL)
    ]
    assert extract(AddressExtractor(chains=(Chain.ETHEREUM,)), html) == [
        Address(Chain.ETHEREUM, USDC_ETH)
    ]


SUI_PKG = "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7"
SUI_COIN = f"{SUI_PKG}::usdc::USDC"


def test_sui_coin_types_are_extracted_and_normalised() -> None:
    shouting = "0x" + SUI_PKG[2:].upper()
    text = f"CA: {shouting}::usdc::USDC, native 0x2::sui::SUI, book 0xdee9::clob::Pool"
    assert extract(AddressExtractor(), text) == [Address(Chain.SUI, SUI_COIN)]
    generic = f"Coin<{SUI_COIN}> in 0x2::coin::Coin"
    assert extract(AddressExtractor(), generic) == [Address(Chain.SUI, SUI_COIN)]


def test_bare_sui_ids_need_sui_context_and_yield_to_coin_types() -> None:
    tx_hash = "0x" + "9f" * 32
    assert extract(AddressExtractor(), f"Tx {tx_hash} confirmed on Etherscan") == []
    assert extract(AddressExtractor(), f"Package {tx_hash} is live on Sui") == [
        Address(Chain.SUI, tx_hash)
    ]
    both = f"{SUI_PKG} publishes {SUI_COIN} (Suiscan)"
    assert extract(AddressExtractor(), both) == [Address(Chain.SUI, SUI_COIN)]


def test_hex_runs_inside_sui_ids_are_not_solana_candidates() -> None:
    sui_id = "0x0" + "a1b2c3d4e5f6a7b8c9d1e2f3a4b5c6d7e8f9a1b2c3d" + "0" + "ffeeddccbbaa9988776"
    assert len(sui_id) == 66
    found = extract(AddressExtractor(), f"Object {sui_id} on Sui")
    assert found == [Address(Chain.SUI, sui_id)]


def test_detect_chain_for_context_free_addresses() -> None:
    assert detect_chain(SUI_COIN) is Chain.SUI
    assert detect_chain(SUI_PKG) is Chain.SUI
    assert detect_chain(USDC_ETH) is Chain.ETHEREUM
    assert detect_chain(USDC_SOL) is Chain.SOLANA


def test_evm_pattern_stops_before_a_sui_type_separator() -> None:
    forty = "0x" + "ab" * 20
    found = extract(AddressExtractor(), f"Type {forty}::meme::MEME on Sui")
    assert found == [Address(Chain.SUI, "0x" + "0" * 24 + "ab" * 20 + "::meme::MEME")]
    assert extract(AddressExtractor(), f"Wallet {forty} here") == [
        Address(Chain.ETHEREUM, to_checksum_address(forty))
    ]


PUMP_MINT = "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAtAAAAAiCAYAAAB2ku6nAAAEVkl"  # carries KGgoAAAA...EVk


def solana(text: str) -> list[tuple[str, int]]:
    return [
        (address.value, offset)
        for address, offset in AddressExtractor().locate(text)
        if address.chain is Chain.SOLANA
    ]


@pytest.mark.parametrize(
    "prefix",
    ["data:image/png;base64,", "DATA:image/png;base64,", "data:image\\/png;base64,"],
)
def test_inline_files_are_not_read_for_addresses(prefix: str) -> None:
    text = f'<img src="{prefix}{PNG}"> CA: {PUMP_MINT}'
    # The payload is blanked, not removed: the real address keeps its offset.
    assert solana(text) == [(PUMP_MINT, text.index(PUMP_MINT))]
    fake = PNG[7:-1]
    assert fake == "KGgoAAAANSUhEUgAAAtAAAAAiCAYAAAB2ku6nAAAEVk"
    assert solana(fake) == [(fake, 0)]  # alone, the run would pass for a mint


def test_single_case_runs_are_not_solana_keys() -> None:
    ipfs = "/api/ipfs/content/bafkreiglu4otsngw67b7ism2friztguep4fxxud2vd32xm6hrve"
    assert is_solana_address("u4otsngw67b7ism2friztguep4fxxud2vd32xm6hrve")  # decodes
    assert solana(ipfs) == []
    assert solana("ABCDEFGHJKLMNPQRSTUVWXYZ23456789ABCDEFGHJKL") == []


@pytest.mark.parametrize(
    "template",
    [
        "https://pump.fun/coin/{}",
        "CA:{}",
        "?address={}&x=1",
        '{{"mint":"{}"}}',
        "buy+{}+now",
        "_{}_",
        "адрес:{}",
    ],
)
def test_a_mint_is_found_in_its_usual_company(template: str) -> None:
    text = template.format(PUMP_MINT)
    assert solana(text) == [(PUMP_MINT, text.index(PUMP_MINT))]


@pytest.mark.parametrize("glued", ["0{}", "{}O", "l{}", "I{}", "x{}"])
def test_a_run_glued_to_other_letters_is_a_fragment(glued: str) -> None:
    assert solana(glued.format(PUMP_MINT)) == []


def test_an_upper_case_data_uri_is_blanked_too() -> None:
    text = f'<img src="data:image/png;BASE64,{PNG}"> CA: {PUMP_MINT}'
    assert solana(text) == [(PUMP_MINT, text.index(PUMP_MINT))]
