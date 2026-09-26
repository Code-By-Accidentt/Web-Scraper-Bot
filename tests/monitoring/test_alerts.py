from scraper_bot.models import Address, Chain
from scraper_bot.monitoring.alerts import AlertRegistry

MINT = "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump"
SUI_PKG = "0x" + "ab" * 32


def test_the_first_claim_wins() -> None:
    registry = AlertRegistry()
    token = Address(Chain.ETHEREUM, "0x6982508145454Ce325dDbE47a25d4ec3d2311933")
    assert registry.claim(token) and not registry.claim(token)
    assert registry.claimed(token)
    # an EVM address in another letter case is the same token
    assert not registry.claim(Address(Chain.ETHEREUM, token.value.lower()))


def test_solana_is_case_sensitive_and_sui_is_keyed_by_package() -> None:
    registry = AlertRegistry()
    assert registry.claim(Address(Chain.SOLANA, MINT))
    assert registry.claim(Address(Chain.SOLANA, MINT.lower()))  # a different key entirely
    assert registry.claim(Address(Chain.SUI, f"{SUI_PKG}::meme::MEME"))
    assert not registry.claim(Address(Chain.SUI, SUI_PKG))


def test_the_memory_is_bounded() -> None:
    registry = AlertRegistry(limit=2)
    first, second, third = (Address(Chain.ETHEREUM, f"0x{i:040x}") for i in (1, 2, 3))
    for address in (first, second, third):
        assert registry.claim(address)
    assert not registry.claimed(first) and registry.claimed(third)
