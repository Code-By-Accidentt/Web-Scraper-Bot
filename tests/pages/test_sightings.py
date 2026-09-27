import pytest

from scraper_bot.chains.addresses.extractor import AddressExtractor
from scraper_bot.chains.addresses.formats import to_checksum_address
from scraper_bot.chains.addresses.stoplist import StopList
from scraper_bot.models.addresses import Address, Chain
from scraper_bot.models.sightings import Sighting, SightingSource
from scraper_bot.pages.changes.normalizer import HtmlNormalizer
from scraper_bot.pages.sightings import CONTEXT_CHARS, find_sightings, hint_score

TOKEN = "0x6982508145454Ce325dDbE47a25d4ec3d2311933"
OTHER = "0x" + "7a" * 20
MINT = "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump"
SUI_PKG = "0x" + "ab" * 32


def sightings(html: str, extras: tuple[str, ...] = (), stoplist: StopList | None = None):
    extractor = AddressExtractor(stoplist=stoplist or ())
    return find_sightings(extractor, HtmlNormalizer().signature(html), html, extras)


def evm(value: str) -> Address:
    return Address(Chain.ETHEREUM, to_checksum_address(value))


def test_text_links_page_source_and_loaded_scripts_are_told_apart() -> None:
    html = (
        "<html><head><script>window.cfg={token:'" + OTHER + "'}</script></head><body>"
        f"<p>Contract address: {TOKEN}</p>"
        f'<a href="https://basescan.org/token/{MINT}">scan</a>'
        "</body></html>"
    )
    found = {s.address.value: s for s in sightings(html, extras=("x" * 10 + " 0x" + "5c" * 20,))}
    assert found[TOKEN].source is SightingSource.TEXT
    assert found[TOKEN].context == "Contract address:" and found[TOKEN].hinted
    assert found[MINT].source is SightingSource.LINK
    assert found[MINT].context == "href=https://basescan.org/token/" and found[MINT].hinted
    assert found[to_checksum_address(OTHER)].source is SightingSource.SOURCE
    assert found[to_checksum_address("0x" + "5c" * 20)].source is SightingSource.SCRIPT


def test_the_label_in_a_neighbouring_element_counts_as_context() -> None:
    html = f"<html><body><span>CA:</span><span>{TOKEN}</span></body></html>"
    [found] = sightings(html)
    assert found.context == "CA:" and found.hinted


def test_other_addresses_in_the_context_are_masked() -> None:
    html = f"<html><body><p>Old token {OTHER} is gone, new token: {TOKEN}</p></body></html>"
    found = {s.address: s for s in sightings(html)}
    context = found[evm(TOKEN)].context
    assert OTHER not in context and OTHER.lower() not in context.lower()
    assert "…" in context and context.endswith("new token:")


def test_the_context_is_capped() -> None:
    html = f"<html><body><p>{'word ' * 40}CA {TOKEN}</p></body></html>"
    [found] = sightings(html)
    assert len(found.context) == CONTEXT_CHARS and found.context.endswith("CA")


@pytest.mark.parametrize(
    ("before", "score"),
    [
        ("CA: ", 1),
        ("Contract ", 1),
        ("https://pump.fun/coin/", 1),
        ("https://dexscreener.com/base/", 1),
        ("Dev wallet: ", -2),
        ("Treasury multisig ", -2),
        ("https://etherscan.io/address/", -1),
        ("Decade of ", 0),  # "ca" only as a word
        ("Tipping point ", 0),
    ],
)
def test_hint_words(before: str, score: int) -> None:
    assert hint_score(before) == score


def test_a_hinted_sighting_beats_an_earlier_unhinted_one() -> None:
    html = f"<html><body><p>Partner {TOKEN}</p><p>Official CA: {TOKEN}</p></body></html>"
    [found] = sightings(html)
    assert found.hinted and found.context.endswith("Official CA:")


def test_stop_listed_addresses_never_become_sightings() -> None:
    html = "<html><body><p>Pay with 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48</p></body></html>"
    assert sightings(html, stoplist=StopList()) == []


def test_a_coin_type_hides_its_bare_package() -> None:
    html = (
        f"<html><body><p>Sui coin {SUI_PKG}::meme::MEME</p><p>package {SUI_PKG}</p></body></html>"
    )
    found = sightings(html)
    assert [s.address.value for s in found] == [f"{SUI_PKG}::meme::MEME"]


def test_only_a_text_that_talks_about_sui_makes_bare_ids_sui() -> None:
    page = f"<html><body><p>Deploy tx: {SUI_PKG}</p></body></html>"
    bundle = "const chains = ['eth', 'sui'];"  # a loaded script that mentions Sui
    assert sightings(page, extras=(bundle,)) == []  # the page itself says nothing of Sui
    assert [s.address.chain for s in sightings(page, extras=(bundle + SUI_PKG,))] == [
        Chain.SUI
    ]  # the script that talks about Sui carries it


def test_raw_contexts_read_as_the_page_shows_them() -> None:
    html = f'<html><head><meta content="Buy &amp; hold &quot;CA&quot; {TOKEN}"></head></html>'
    [found] = sightings(html)
    assert found.source is SightingSource.SOURCE
    assert found.context.endswith('Buy & hold "CA"')


def test_a_sui_coin_type_in_a_link_query_stays_a_link() -> None:
    coin = f"{SUI_PKG}::coin::COIN"
    html = f'<html><body><a href="https://app.cetus.zone/swap?to={coin}&utm_source=x">buy</a></body></html>'
    [found] = sightings(html)
    assert found.source is SightingSource.LINK and found.address.value.endswith("::coin::COIN")


def test_captcha_response_tokens_are_never_addresses() -> None:
    from scraper_bot.pages.changes.normalizer import HtmlNormalizer

    token = "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump"  # shaped like a mint
    html = (
        "<html><body><form>"
        f"<textarea name='g-recaptcha-response'>{token}</textarea>"
        f"<input type='hidden' name='cf-turnstile-response' value='{token}'>"
        "</form></body></html>"
    )
    signature = HtmlNormalizer().signature(html)
    assert find_sightings(AddressExtractor(), signature, html) == []


def test_a_challenge_page_counts_only_its_text_and_token_links() -> None:
    from scraper_bot.pages.sightings import shown_on_a_challenge

    mint = Address(Chain.SOLANA, "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump")
    sightings = [
        Sighting(mint, SightingSource.SOURCE, "window._cf_chl_opt"),
        Sighting(mint, SightingSource.LINK, "value=", False),
        Sighting(mint, SightingSource.LINK, "href=https://pump.fun/coin/", True),
        Sighting(mint, SightingSource.TEXT, "CA:"),
    ]
    kept = shown_on_a_challenge(sightings)
    assert [s.source for s in kept] == [SightingSource.LINK, SightingSource.TEXT]
