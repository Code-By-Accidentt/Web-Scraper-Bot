"""Every network the bot checks: explorer and DEX links, and for the EVM networks their
``.env`` key, RPC default, chain id and whether they are optional (the Solana and Sui RPC
defaults live in :mod:`scraper_bot.config`).

Adding an EVM network takes one :class:`EvmChain` entry here, its stablecoins and wrapped
tokens in :mod:`scraper_bot.chains.stoplist` and its line in ``.env.example`` (a test
requires it). The settings, the warm-up, the validators and the alert links all read this
table.
"""

from __future__ import annotations

from dataclasses import dataclass

from scraper_bot.chains.address_extractor import is_sui_coin_type
from scraper_bot.models import Chain, ConfirmedContract


@dataclass(frozen=True, slots=True)
class NetworkLinks:
    explorer_name: str
    explorer: str  # token page, ``{address}`` placeholder
    dex_chain: str  # DexScreener chain id
    swap_name: str | None = None
    swap: str | None = None


@dataclass(frozen=True, slots=True)
class EvmChain:
    """One EVM network on which 20-byte ``0x`` addresses are checked."""

    name: str  # shown in alerts, logs and /status; the key of every per-network table
    env_var: str  # the .env key of its RPC URL
    default_rpc: str
    chain_id: int  # what eth_chainId answers; checked at start
    links: NetworkLinks
    # When an optional network's endpoint fails, the answers of the others still give a
    # verdict; the pending watch keeps asking it about addresses that have no code yet.
    optional: bool = False


def _uniswap(chain: str) -> str:
    return f"https://app.uniswap.org/swap?outputCurrency={{address}}&chain={chain}"


EVM_CHAINS: tuple[EvmChain, ...] = (
    EvmChain(
        "Ethereum",
        "ETH_RPC_URL",
        "https://ethereum-rpc.publicnode.com",
        1,
        NetworkLinks(
            "Etherscan",
            "https://etherscan.io/token/{address}",
            "ethereum",
            "Uniswap",
            _uniswap("ethereum"),
        ),
    ),
    EvmChain(
        "Base",
        "BASE_RPC_URL",
        "https://base-rpc.publicnode.com",
        8453,
        NetworkLinks(
            "Basescan", "https://basescan.org/token/{address}", "base", "Uniswap", _uniswap("base")
        ),
    ),
    EvmChain(
        "Arbitrum",
        "ARB_RPC_URL",
        "https://arbitrum-one-rpc.publicnode.com",
        42161,
        NetworkLinks(
            "Arbiscan",
            "https://arbiscan.io/token/{address}",
            "arbitrum",
            "Uniswap",
            _uniswap("arbitrum"),
        ),
    ),
    EvmChain(
        "Optimism",
        "OPT_RPC_URL",
        "https://optimism-rpc.publicnode.com",
        10,
        NetworkLinks(
            "Etherscan",
            "https://optimistic.etherscan.io/token/{address}",
            "optimism",
            "Uniswap",
            _uniswap("optimism"),
        ),
    ),
    EvmChain(
        "BSC",
        "BSC_RPC_URL",
        "https://bsc-rpc.publicnode.com",
        56,
        NetworkLinks(
            "BscScan",
            "https://bscscan.com/token/{address}",
            "bsc",
            "PancakeSwap",
            "https://pancakeswap.finance/swap?outputCurrency={address}",
        ),
    ),
    EvmChain(
        "Fantom",
        "FANTOM_RPC_URL",
        "https://rpcapi.fantom.network",
        250,
        NetworkLinks("FtmScan", "https://ftmscan.com/token/{address}", "fantom"),
    ),
    EvmChain(
        "Sonic",
        "SONIC_RPC_URL",
        "https://rpc.soniclabs.com",
        146,
        NetworkLinks("SonicScan", "https://sonicscan.org/token/{address}", "sonic"),
    ),
    EvmChain(
        "Avalanche",
        "AVAX_RPC_URL",
        "https://avalanche-c-chain-rpc.publicnode.com",
        43114,
        NetworkLinks(
            "Snowtrace",
            "https://snowtrace.io/token/{address}",
            "avalanche",
            "Uniswap",
            _uniswap("avalanche"),
        ),
    ),
    # Circle's chain (mainnet since 2026-09-16), USDC is its gas; flutch.fun launches here.
    # The official rpc.mainnet.arc.io answered 403 to keyless calls.
    EvmChain(
        "Arc",
        "ARC_RPC_URL",
        "https://arc-rpc.publicnode.com",
        5042,
        NetworkLinks("Arc Explorer", "https://explorer.arc.io/token/{address}", "arc"),
        optional=True,
    ),
    # Robinhood's Arbitrum Orbit L2; pons.family and bag.fun launch here.
    EvmChain(
        "Robinhood Chain",
        "ROBINHOOD_RPC_URL",
        "https://robinhood-rpc.publicnode.com",
        4663,
        NetworkLinks(
            "Blockscout", "https://robinhoodchain.blockscout.com/token/{address}", "robinhood"
        ),
        optional=True,
    ),
)

NETWORK_LINKS: dict[str, NetworkLinks] = {
    **{chain.name: chain.links for chain in EVM_CHAINS},
    "Solana": NetworkLinks(
        "Solscan",
        "https://solscan.io/token/{address}",
        "solana",
        "Jupiter",
        "https://jup.ag/swap/SOL-{address}",
    ),
    "Sui": NetworkLinks("Suiscan", "https://suiscan.xyz/mainnet/coin/{address}", "sui"),
}


def token_links(
    contract: ConfirmedContract, identifier: str | None = None
) -> list[tuple[str, str]]:
    """(label, url) pairs: explorer, DexScreener and, where known, a swap page.

    Every URL carries only the token's own identifier, never a pair address, so a bot
    that parses the chat for addresses cannot pick up a second contract.
    """
    links = NETWORK_LINKS.get(contract.network)
    if links is None:
        return []
    address = identifier or contract.address.value
    if contract.address.chain is Chain.SUI and not is_sui_coin_type(address):
        return [("Suiscan", f"https://suiscan.xyz/mainnet/object/{address}")]
    out = [
        (links.explorer_name, links.explorer.format(address=address)),
        ("DexScreener", f"https://dexscreener.com/{links.dex_chain}/{address}"),
    ]
    if links.swap and links.swap_name:
        out.append((links.swap_name, links.swap.format(address=address)))
    return out
