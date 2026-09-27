from pathlib import Path

from scraper_bot.chains.networks import EVM_CHAINS, NETWORK_LINKS, token_links
from scraper_bot.chains.stoplist import EVM_TOKENS
from scraper_bot.models import Address, Chain, ConfirmedContract
from tests.conftest import make_settings

USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ETH_CONTRACT = ConfirmedContract(Address(Chain.ETHEREUM, USDC_ETH), "Ethereum")
SOL_CONTRACT = ConfirmedContract(Address(Chain.SOLANA, USDC_SOL), "Solana")


def test_the_registry_has_no_duplicates() -> None:
    for field in ("name", "env_var", "default_rpc", "chain_id"):
        values = [getattr(chain, field) for chain in EVM_CHAINS]
        assert len(values) == len(set(values)), field
    assert [chain.name for chain in EVM_CHAINS][:2] == ["Ethereum", "Base"]  # verdict order


def test_only_the_young_networks_are_optional() -> None:
    optional = {chain.name: chain.chain_id for chain in EVM_CHAINS if chain.optional}
    assert optional == {"Arc": 5042, "Robinhood Chain": 4663}


def test_every_network_has_links_and_stop_listed_tokens_on_known_networks() -> None:
    assert set(NETWORK_LINKS) == {chain.name for chain in EVM_CHAINS} | {"Solana", "Sui"}
    assert set(EVM_TOKENS) <= {chain.name for chain in EVM_CHAINS}
    assert {"Arc", "Robinhood Chain"} <= set(EVM_TOKENS)


def test_links_per_network() -> None:
    assert token_links(ETH_CONTRACT) == [
        ("Etherscan", f"https://etherscan.io/token/{USDC_ETH}"),
        ("DexScreener", f"https://dexscreener.com/ethereum/{USDC_ETH}"),
        ("Uniswap", f"https://app.uniswap.org/swap?outputCurrency={USDC_ETH}&chain=ethereum"),
    ]
    assert [label for label, _ in token_links(SOL_CONTRACT)] == [
        "Solscan",
        "DexScreener",
        "Jupiter",
    ]
    bsc = ConfirmedContract(ETH_CONTRACT.address, "BSC")
    assert [label for label, _ in token_links(bsc)] == ["BscScan", "DexScreener", "PancakeSwap"]
    fantom = ConfirmedContract(ETH_CONTRACT.address, "Fantom")
    assert [label for label, _ in token_links(fantom)] == ["FtmScan", "DexScreener"]
    assert token_links(ConfirmedContract(ETH_CONTRACT.address, "Nowhere")) == []


def test_links_of_the_new_networks() -> None:
    arc = ConfirmedContract(ETH_CONTRACT.address, "Arc")
    assert token_links(arc) == [
        ("Arc Explorer", f"https://explorer.arc.io/token/{USDC_ETH}"),
        ("DexScreener", f"https://dexscreener.com/arc/{USDC_ETH}"),
    ]
    robinhood = ConfirmedContract(ETH_CONTRACT.address, "Robinhood Chain")
    assert token_links(robinhood) == [
        ("Blockscout", f"https://robinhoodchain.blockscout.com/token/{USDC_ETH}"),
        ("DexScreener", f"https://dexscreener.com/robinhood/{USDC_ETH}"),
    ]


def test_settings_carry_chain_ids_and_the_optional_flag(tmp_path: Path) -> None:
    networks = {net.name: net for net in make_settings(tmp_path).evm_networks}
    assert [net.name for net in networks.values()] == [chain.name for chain in EVM_CHAINS]
    arc = networks["Arc"]
    assert (arc.rpc_url, arc.chain_id, arc.optional) == (
        "https://arc-rpc.publicnode.com",
        5042,
        True,
    )
    assert networks["Robinhood Chain"].rpc_url == "https://robinhood-rpc.publicnode.com"
    assert networks["Ethereum"].chain_id == 1 and not networks["Ethereum"].optional
    trimmed = make_settings(tmp_path, ARC_RPC_URL="", ROBINHOOD_RPC_URL="").evm_networks
    assert [net.name for net in trimmed] == [chain.name for chain in EVM_CHAINS][:8]
