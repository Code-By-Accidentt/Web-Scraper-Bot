from pathlib import Path

from scraper_bot.models.addresses import Address, Chain, ConfirmedContract
from scraper_bot.monitoring.alerts import AlertRegistry
from scraper_bot.storage.address_cache import AddressCache

MINT = "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump"
SUI_PKG = "0x" + "ab" * 32


def token(chain: Chain, value: str, network: str = "Base") -> ConfirmedContract:
    return ConfirmedContract(Address(chain, value), network)


def test_the_first_claim_wins() -> None:
    registry = AlertRegistry()
    pepe = token(Chain.ETHEREUM, "0x6982508145454Ce325dDbE47a25d4ec3d2311933")
    assert registry.claim(pepe) and not registry.claim(pepe)
    # an EVM address in another letter case is the same token
    assert not registry.claim(token(Chain.ETHEREUM, pepe.address.value.lower()))


def test_solana_is_case_sensitive_and_sui_is_keyed_by_package() -> None:
    registry = AlertRegistry()
    assert registry.claim(token(Chain.SOLANA, MINT, "Solana"))
    assert registry.claim(token(Chain.SOLANA, MINT.lower(), "Solana"))  # another key entirely
    assert registry.claim(token(Chain.SUI, f"{SUI_PKG}::meme::MEME", "Sui"))
    assert not registry.claim(token(Chain.SUI, SUI_PKG, "Sui"))


def test_the_memory_is_bounded() -> None:
    registry = AlertRegistry(limit=2)
    first, second, third = (token(Chain.ETHEREUM, f"0x{i:040x}") for i in (1, 2, 3))
    for contract in (first, second, third):
        assert registry.claim(contract)
    assert registry.claim(first)  # forgotten: without a known cache nothing else remembers it
    assert not registry.claim(third)


def test_a_claim_records_the_token_as_known(tmp_path: Path) -> None:
    cache = AddressCache(tmp_path / "known.tsv", tmp_path / "rejected.tsv", rejected_ttl=3600)
    cache.load()
    registry = AlertRegistry(cache)
    pepe = token(Chain.ETHEREUM, "0x6982508145454Ce325dDbE47a25d4ec3d2311933", "Ethereum")
    assert not cache.is_known(pepe.address)
    assert registry.claim(pepe)
    assert cache.known_network(pepe.address) == "Ethereum"  # survives a restart from here on
    reloaded = AddressCache(tmp_path / "known.tsv", tmp_path / "rejected.tsv", rejected_ttl=3600)
    reloaded.load()
    assert reloaded.is_known(pepe.address)


def test_a_token_that_cannot_be_written_down_is_still_announced(tmp_path: Path) -> None:
    known = tmp_path / "known"
    known.mkdir()  # a directory where the file should be: every append fails
    cache = AddressCache(known, tmp_path / "r.tsv", rejected_ttl=0)
    registry = AlertRegistry(cache)
    contract = ConfirmedContract(Address(Chain.ETHEREUM, "0x" + "ab" * 20), "Base")
    assert registry.claim(contract)  # the alert goes out
    assert cache.is_known(contract.address) and not registry.claim(contract)


def test_both_forms_of_a_sui_token_become_known(tmp_path: Path) -> None:
    cache = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
    cache.load()
    registry = AlertRegistry(cache)
    package = "0x" + "1" * 64
    coin = ConfirmedContract(Address(Chain.SUI, f"{package}::meme::MEME"), "Sui")
    bare = ConfirmedContract(Address(Chain.SUI, package), "Sui")
    assert registry.claim(coin) and not registry.claim(bare)
    again = AddressCache(tmp_path / "k.tsv", tmp_path / "r.tsv", rejected_ttl=0)
    again.load()
    assert again.is_known(coin.address) and again.is_known(bare.address)  # after a restart
