"""Addresses that are never a new launch: stablecoins, wrapped natives, system contracts.

App pages load wallet-connect bundles and swap widgets full of these. Without this list,
the first round on such a site would announce a dozen "contracts" and auto-pause it.
Every token below was checked on-chain (its ``symbol()`` or mint account) on 2026-09-24,
those added in 2.1.3 on 2026-09-28; those on Arc and Robinhood Chain were taken from the
chains' own docs on 2026-09-27. Copies of such tokens elsewhere are known by their symbol
(``STABLE_SYMBOLS``).
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from scraper_bot.chains.addresses.formats import canonical_address, detect_chain, sui_package
from scraper_bot.models.addresses import Address, Chain

log = logging.getLogger(__name__)

# (symbol, address) per network. The symbol is for the reader, and lookups ignore letter case.
EVM_TOKENS: dict[str, tuple[tuple[str, str], ...]] = {
    "Ethereum": (
        ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
        ("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7"),
        ("DAI", "0x6B175474E89094C44Da98b954EedeAC495271d0F"),
        ("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
        ("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"),
        ("cbBTC", "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"),
        ("USDe", "0x4c9EDD5852cd905f086C759E8383e09bff1E68B3"),
        ("stETH", "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"),
        ("wstETH", "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"),
        ("PYUSD", "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8"),
        ("FDUSD", "0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409"),
        ("USDS", "0xdC035D45d973E3EC169d2276DDab16f1e407384F"),
        ("EURC", "0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c"),
        ("rETH", "0xae78736Cd615f374D3085123A210448E74Fc6393"),
        ("weETH", "0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee"),
        ("RLUSD", "0x8292Bb45bf1Ee4d140127049757C2E0fF06317eD"),
    ),
    "Base": (
        ("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
        ("USDbC", "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA"),
        ("DAI", "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb"),
        ("cbETH", "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22"),
        ("USDT", "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2"),
        ("wstETH", "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452"),
        ("EURC", "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42"),
        ("USDe", "0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34"),
    ),
    "Arbitrum": (
        ("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
        ("USDC.e", "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8"),
        ("USDT0", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"),
        ("WETH", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"),
        ("WBTC", "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"),
        ("DAI", "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"),
        ("ARB", "0x912CE59144191C1204E64559FE8253a0e49E6548"),
        ("wstETH", "0x5979D7b546E38E414F7E9822514be443A4800529"),
        ("USDe", "0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34"),
    ),
    "Optimism": (
        ("USDC", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"),
        ("USDC.e", "0x7F5c764cBc14f9669B88837ca1490cCa17c31607"),
        ("USDT", "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58"),
        ("WBTC", "0x68f180fcCe6836688e9084f035309E29Bf0A2095"),
        ("wstETH", "0x1F32b1c2345538c0c6f582fCB022739c4A194Ebb"),
    ),
    "BSC": (
        ("USDT", "0x55d398326f99059fF775485246999027B3197955"),
        ("USDC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"),
        ("BUSD", "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56"),
        ("WBNB", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
        ("BTCB", "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c"),
        ("ETH", "0x2170Ed0880ac9A755fd29B2688956BD959F933F8"),
        ("DAI", "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3"),
        ("USD1", "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d"),
        ("slisBNB", "0xB0b84D294e0C75A6abe60171b70edEb2EFd14A1B"),
    ),
    "Avalanche": (
        ("USDC", "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E"),
        ("USDt", "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7"),
        ("WAVAX", "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"),
        ("WETH.e", "0x49D5c2BdFfac6CE2BFdB6640F4F80f226bc10bAB"),
        ("WBTC.e", "0x50b7545627a5162F82A992c33b87aDc75187B218"),
        ("USDC.e", "0xA7D7079b0FEaD91F3e65f86E8915Cb59c1a4C664"),
        ("DAI.e", "0xd586E7F844cEa2F87f50152665BCbc2C279D8d70"),
        ("BTC.b", "0x152b9d0FdC40C096757F570A51E494bd4b943E50"),
        ("sAVAX", "0x2b2C81e08f1Af8835a78Bb2A90AE924ACE0eA4bE"),
    ),
    "Sonic": (
        ("wS", "0x039e2fB66102314Ce7b64Ce5Ce3E5183bc94aD38"),
        ("USDC.e", "0x29219dd400f2Bf60E5a23d13Be72B486D4038894"),
        ("WETH", "0x50c42dEAcD8Fc9773493ED674b675bE577f2634b"),
        ("stS", "0xE5DA20F15420aD15DE0fa650600aFc998bbE3955"),
    ),
    "Fantom": (
        ("WFTM", "0x21be370D5312f44cB42ce377BC9b8a0cEF1A4C83"),
        ("USDC", "0x04068DA6C83AFCFA0e13ba15A6696662335D5B75"),
        ("fUSDT", "0x049d68029688eAbF473097a2fC38ef61633A3C7A"),
        ("DAI", "0x8D11eC38a3EB5E956B052f67Da8Bdc9bef8Abf3E"),
        ("WETH", "0x74b23882a30290451A17c44f4F05243b6b58C76d"),
        ("WBTC", "0x321162Cd933E2Be498Cd2267a90534A804051b11"),
    ),
    "Arc": (
        ("USDC", "0x3600000000000000000000000000000000000000"),  # the gas token as an ERC-20
        ("EURC", "0xbEf5f6d51CB62b58e6A8f77868681825C6fe21c1"),
        ("USYC", "0x8a5D989Bbb96929F689B0200f435f53dA42bF490"),
        ("cirBTC", "0x171A4217b86A807A64eB94757Db6849fb4bDbAA0"),
        ("WETH", "0x128cC466B61f542da60c70e3aA11c10e19B84EDB"),
    ),
    "Robinhood Chain": (
        ("WETH", "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"),
        ("USDG", "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"),
    ),
}

# The burn address and the "native coin" placeholder that aggregator UIs use.
EVM_SENTINELS = (
    "0x000000000000000000000000000000000000dEaD",
    "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
)
# Addresses under this limit are the zero address, precompiles and system contracts
# (BSC 0x1000-0x2005, Arbitrum 0x64-0xff).
EVM_SYSTEM_LIMIT = 0x10000
# OP-stack predeploys (Base, Optimism): WETH 0x4200...0006, GasPriceOracle ...000F, OP ...0042.
OP_STACK_PREDEPLOYS = range(0x42 << 152, (0x42 << 152) + 0x800)

SOLANA_MINTS: tuple[tuple[str, str], ...] = (
    ("wSOL", "So11111111111111111111111111111111111111112"),
    ("USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
    ("USDT", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"),
    ("PYUSD", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"),
    ("USD1", "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"),
    ("jitoSOL", "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"),
    ("mSOL", "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So"),
    ("bSOL", "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1"),
    ("JupSOL", "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v"),
    ("INF", "5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2X4TxVusJm"),
)

# Sui coins match by package, so both the coin type and the bare package id are caught.
SUI_PACKAGES: tuple[tuple[str, str], ...] = (
    ("USDC", "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7"),
    ("USDT", "0x375f70cf2ae4c00bf37117d0c85a2c71545e6ee05c4a5c7d282cd66a4504b068"),
)

# Stablecoins plus wrapped, native and staked coins. A token with one of these symbols that
# isn't on the lists above is a copy (bridged, plumbing or a scam).
STABLE_SYMBOLS: tuple[str, ...] = (
    *("USDC", "USDT", "USDC.e", "USDbC", "USDT.e", "USDT0", "EURC", "DAI", "USDe", "PYUSD"),
    *("FDUSD", "USDS", "USD1", "USDG", "RLUSD", "TUSD", "FRAX", "LUSD", "GHO", "crvUSD"),
    *("BUSD", "DAI.e"),
    *("WETH", "WBTC", "cbBTC", "WBNB", "WAVAX", "WFTM", "WSOL", "WETH.e", "WBTC.e"),
    *("BTCB", "BTC.b"),
    *("ETH", "BTC", "BNB", "SOL", "AVAX"),
    *("stETH", "wstETH", "rETH", "weETH", "cbETH", "sAVAX", "stS", "slisBNB"),
    *("jitoSOL", "mSOL", "bSOL", "JupSOL"),
)

# Each group is a Latin letter, then the Cyrillic and Greek letters (and Tether's ₮) that
# look like it, as in "USDС" with a Cyrillic С.
_LOOKALIKE_GROUPS = (
    "AАΑ BВΒ CС EЕΕ HНҺΗ IІΙӀ JЈ KКΚ MМΜ NΝ OОΟ PРΡ SЅ TТΤ₮ XХΧ YУҮΥ ZΖ "
    "aаα bв cс dԁ eе hһн iіι jј kкκ lӏ mм oоο pрρ sѕ tтτ uυ vν xхχ yуү"
)
_LOOKALIKES = str.maketrans(
    {look: group[0] for group in _LOOKALIKE_GROUPS.split() for look in group[1:]}
)


def _symbol_key(symbol: str) -> str:
    """Return a token symbol in the form in which symbols are compared.

    Invisible characters go, NFKC turns fullwidth and styled letters (ＵＳＤＣ, 𝐔𝐒𝐃𝐂) into
    plain ones, look-alike letters become Latin, and the case is folded.
    """
    visible = "".join(char for char in symbol if unicodedata.category(char) != "Cf")
    return unicodedata.normalize("NFKC", visible).translate(_LOOKALIKES).casefold().strip()


_STABLE_KEYS = frozenset(_symbol_key(symbol) for symbol in STABLE_SYMBOLS)


def is_stable_symbol(symbol: str | None) -> bool:
    """Return True when ``symbol`` reads as one of ``STABLE_SYMBOLS``, look-alikes included.

    Only the whole symbol counts, so "USDCAT" or "PEPEUSDC" do not; None never does.
    """
    return symbol is not None and _symbol_key(symbol) in _STABLE_KEYS


class StopList:
    """The addresses never reported: the built-in ones plus the operator's ``data/ignore.txt``.

    EVM addresses match in any letter case, and a Sui coin matches by its package.
    """

    def __init__(self, extra: Iterable[Address] = ()) -> None:
        """Start from the built-in lists and add the ``extra`` addresses."""
        self._evm = {address.lower() for pairs in EVM_TOKENS.values() for _, address in pairs}
        self._evm.update(address.lower() for address in EVM_SENTINELS)
        self._solana = {mint for _, mint in SOLANA_MINTS}
        self._sui = {package for _, package in SUI_PACKAGES}
        for address in extra:
            self.add(address)

    @classmethod
    def load(cls, path: Path) -> StopList:
        """Return the built-in list plus the addresses in ``path``.

        The file has one address per line, and ``#`` starts a comment. Bad lines are logged
        and skipped; if the file is missing or unreadable, only the built-in list applies.
        """
        extra: list[Address] = []
        if path.is_file():
            try:
                # utf-8-sig drops Notepad's BOM, and a file in another encoding still loads.
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError as exc:
                log.warning("%s is unreadable (%s); only the built-in stop-list applies", path, exc)
                return cls()
            for number, line in enumerate(text.splitlines(), 1):
                raw = line.split("#", 1)[0].strip()
                if not raw:
                    continue
                address = canonical_address(detect_chain(raw), raw)
                if address is None:
                    log.warning("%s:%d: %r is not an address; ignored", path.name, number, raw)
                    continue
                extra.append(address)
            log.info("%d address(es) from %s added to the stop-list", len(extra), path.name)
        return cls(extra)

    def add(self, address: Address) -> None:
        """Add one address to the list; a Sui coin adds its whole package."""
        if address.chain is Chain.ETHEREUM:
            self._evm.add(address.value.lower())
        elif address.chain is Chain.SOLANA:
            self._solana.add(address.value)
        else:
            self._sui.add(sui_package(address.value))

    def __contains__(self, address: object) -> bool:
        """Return True when ``address`` is on the list; anything but an Address is not."""
        if not isinstance(address, Address):
            return False
        if address.chain is Chain.ETHEREUM:
            return self.contains_evm(address.value)
        if address.chain is Chain.SOLANA:
            return address.value in self._solana
        return sui_package(address.value) in self._sui

    def contains_evm(self, value: str) -> bool:
        """Return True when the ``0x`` address ``value``, in any letter case, is on the list.

        Addresses below ``EVM_SYSTEM_LIMIT`` and the OP-stack predeploys always are.
        """
        lowered = value.lower()
        if lowered in self._evm:
            return True
        number = int(lowered, 16)
        return number < EVM_SYSTEM_LIMIT or number in OP_STACK_PREDEPLOYS
