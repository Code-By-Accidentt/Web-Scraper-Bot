from pathlib import Path

import pytest

from scraper_bot.chains.address_extractor import (
    AddressExtractor,
    canonical_address,
    is_evm_address,
    is_solana_address,
    to_checksum_address,
)
from scraper_bot.chains.stoplist import (
    EVM_SENTINELS,
    EVM_TOKENS,
    SOLANA_MINTS,
    SUI_PACKAGES,
    StopList,
)
from scraper_bot.models import Address, Chain

MEME = "0x6982508145454Ce325dDbE47a25d4ec3d2311933"
SUI_USDC_PACKAGE = SUI_PACKAGES[0][1]


def evm(value: str) -> Address:
    return Address(Chain.ETHEREUM, to_checksum_address(value))


def test_the_built_in_lists_hold_valid_canonical_addresses() -> None:
    for pairs in EVM_TOKENS.values():
        for symbol, address in pairs:
            assert is_evm_address(address) and to_checksum_address(address) == address, symbol
    for address in EVM_SENTINELS:
        assert is_evm_address(address)
    for symbol, mint in SOLANA_MINTS:
        assert is_solana_address(mint), symbol
    for symbol, package in SUI_PACKAGES:
        assert canonical_address(Chain.SUI, package) == Address(Chain.SUI, package), symbol


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "USDC on Ethereum"),
        ("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDC on Base, any letter case"),
        ("0x4200000000000000000000000000000000000006", "WETH predeploy on OP stacks"),
        ("0x420000000000000000000000000000000000000F", "GasPriceOracle predeploy"),
        ("0x00000000000000000000000000000000000007FF", "a system contract below 0x10000"),
        ("0x0000000000000000000000000000000000000000", "the zero address"),
        ("0x000000000000000000000000000000000000dEaD", "the burn address"),
        ("0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE", "the native-coin placeholder"),
    ],
)
def test_well_known_evm_addresses_are_stopped(value: str, why: str) -> None:
    assert evm(value) in StopList(), why


@pytest.mark.parametrize(
    "value",
    [
        MEME,
        "0x0000000000000000000000000000000000010000",  # first address past the system range
        "0x4200000000000000000000000000000000000800",  # first address past the predeploys
    ],
)
def test_other_evm_addresses_pass(value: str) -> None:
    assert evm(value) not in StopList()


def test_solana_and_sui_entries() -> None:
    stop = StopList()
    assert Address(Chain.SOLANA, "So11111111111111111111111111111111111111112") in stop
    assert Address(Chain.SOLANA, "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump") not in stop
    # Sui coins are matched by package: the coin type and the bare package both hit.
    assert Address(Chain.SUI, f"{SUI_USDC_PACKAGE}::usdc::USDC") in stop
    assert Address(Chain.SUI, SUI_USDC_PACKAGE) in stop
    assert Address(Chain.SUI, "0x" + "ab" * 32) not in stop
    assert "not an address" not in stop


def test_the_operator_list_adds_to_the_built_in_one(tmp_path: Path) -> None:
    path = tmp_path / "ignore.txt"
    path.write_text(
        f"# partner tokens\n{MEME.lower()}  # shown in the footer\n\nnot-an-address\n"
        "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU\n",
        encoding="utf-8",
    )
    stop = StopList.load(path)
    assert evm(MEME) in stop
    assert Address(Chain.SOLANA, "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU") in stop
    assert evm("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48") in stop  # built-ins stay
    assert evm(MEME) not in StopList.load(tmp_path / "missing.txt")


def test_the_operator_list_survives_a_bom_and_other_encodings(tmp_path: Path) -> None:
    bom = tmp_path / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbf" + MEME.encode() + b"\n")  # Notepad's UTF-8 with BOM
    assert evm(MEME) in StopList.load(bom)
    cyrillic = tmp_path / "cp1251.txt"
    cyrillic.write_bytes("# мем токен\n".encode("cp1251") + MEME.encode() + b"\n")
    assert evm(MEME) in StopList.load(cyrillic)


def test_the_extractor_never_returns_stopped_addresses() -> None:
    text = f"USDC 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 and the launch {MEME}"
    assert AddressExtractor(stoplist=StopList()).extract(text) == [evm(MEME)]
    assert len(AddressExtractor().extract(text)) == 2  # without a stop-list nothing is dropped


def test_an_unreadable_operator_list_leaves_the_built_in_one(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "ignore.txt"
    path.write_text(MEME, encoding="utf-8")

    def refuse(*_args: object, **_kwargs: object) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", refuse)
    stop = StopList.load(path)
    assert evm(MEME) not in stop
    assert evm("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48") in stop
