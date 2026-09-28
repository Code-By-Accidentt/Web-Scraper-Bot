import json

from scraper_bot.chains.addresses.extractor import AddressExtractor
from scraper_bot.models.addresses import Address, Chain
from scraper_bot.models.pages import PageCheckOutcome, PageStatus
from scraper_bot.models.sightings import Observation, SightingSource
from scraper_bot.pages.changes.normalizer import HtmlNormalizer
from scraper_bot.pages.fetching.result import FetchResult
from scraper_bot.pages.light.poller import LightPoller
from tests.conftest import FakeTuning, FakeValidator

URL = "https://x.io"
BRETT = "0x532f27101965dd16442E59d40670FaF5eBB142E4"
PARTNER = "0x" + "ab" * 20


def html(body: str = "", script: str = "/app-1.js") -> str:
    lines = "".join(f"<p>Line {i} of the launch page</p>" for i in range(12))
    return f"<html><head><script src='{script}'></script></head><body>{lines}{body}</body></html>"


def answer(url: str, text: str, **headers: str) -> FetchResult:
    return FetchResult(url, 200, text, headers=headers)


class FakeTransport:
    """Scripted answers per URL; the last one repeats. Records (url, etag) of every call."""

    def __init__(self, answers: dict[str, list[FetchResult]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str | None]] = []

    async def get(
        self, url: str, *, origin: str, etag: str | None = None, modified: str | None = None
    ) -> FetchResult:
        self.calls.append((url, etag))
        queue = self.answers.get(url)
        if not queue:
            return FetchResult(url, 404, "")
        return queue.pop(0) if len(queue) > 1 else queue[0]


def poller(
    transport: FakeTransport, browser: FakeTransport | None = None
) -> tuple[LightPoller, list[Observation], list[str]]:
    seen: list[Observation] = []
    renders: list[str] = []

    async def observe(observation: Observation) -> None:
        seen.append(observation)

    validator = FakeValidator()
    validator.confirm(Address(Chain.ETHEREUM, BRETT), "Base")
    light = LightPoller(
        URL,
        http=transport,
        browser=browser,
        extractor=AddressExtractor(),
        validator=validator,  # type: ignore[arg-type]
        normalizer=HtmlNormalizer(),
        tuning=FakeTuning(),
        observe=observe,
        request_render=renders.append,
        is_blocked=lambda: False,
    )
    return light, seen, renders


def rendered(*api_calls: str) -> PageCheckOutcome:
    signature = HtmlNormalizer().signature(html())
    return PageCheckOutcome(URL, PageStatus.BASELINE, signature=signature, api_calls=api_calls)


async def test_a_new_address_in_the_html_is_announced_at_once_and_a_304_is_a_read() -> None:
    with_ca = html(f"<p>CA: {BRETT}</p>")
    transport = FakeTransport(
        {URL: [answer(URL, html(), etag='"a"'), FetchResult(URL, 304), answer(URL, with_ca)]}
    )
    light, seen, _ = poller(transport)
    light.learn(rendered())
    await light.poll_once()  # nothing new; the HTML carries the render: the page is covered
    assert light.covered(URL) and seen == []
    await light.poll_once()  # asked with its ETag: unchanged, only a read of the page
    assert transport.calls[1] == (URL, '"a"') and [o.sightings for o in seen] == [()]
    await light.poll_once()
    [sighting] = seen[-1].sightings
    assert (sighting.address.value, sighting.source, sighting.hinted) == (
        BRETT,
        SightingSource.TEXT,
        True,
    )
    assert sighting.context.endswith("CA:")  # as a render would quote it
    assert seen[-1].results[0].contract is not None and seen[-1].read


async def test_new_scripts_in_the_html_ask_for_a_render() -> None:
    transport = FakeTransport({URL: [answer(URL, html()), answer(URL, html(script="/app-2.js"))]})
    light, _, renders = poller(transport)
    light.learn(rendered())
    await light.poll_once()
    await light.poll_once()  # a new deploy of the app
    assert renders == [URL]


async def test_the_site_api_is_polled_from_a_baseline_and_other_sites_apis_never() -> None:
    api = "https://api.x.io/launch?chain=base"
    foreign = "https://api.dexscreener.com/latest/dex/pairs/base/0x1"
    before, after = {"ca": None, "partner": PARTNER}, {"ca": BRETT, "partner": PARTNER}
    transport = FakeTransport(
        {
            URL: [answer(URL, html())],
            api: [answer(api, json.dumps(before)), answer(api, json.dumps(after))],
        }
    )
    light, seen, _ = poller(transport)
    light.learn(rendered(api, foreign))
    await light.poll_once()  # the API is tried: it holds an address, so it is polled
    await light.poll_once()  # now it names the CA; the partner was there from the start
    assert foreign not in [url for url, _ in transport.calls]
    [found] = [observation for observation in seen if observation.sightings]
    [sighting] = found.sightings
    assert (sighting.address.value, sighting.source, sighting.hinted) == (
        BRETT,
        SightingSource.API,
        True,
    )
    assert not found.read  # the API is not the page: no ⏱ window closes


async def test_a_site_that_refuses_plain_requests_is_asked_through_the_browser() -> None:
    refused = FetchResult(
        URL, 403, "<title>Just a moment...</title>", headers={"cf-mitigated": "challenge"}
    )
    plain = FakeTransport({URL: [refused]})
    browser = FakeTransport({URL: [answer(URL, html(f"<p>CA: {BRETT}</p>"))]})
    light, seen, _ = poller(plain, browser)
    light.learn(rendered())
    await light.poll_once()  # refused: from now on the browser asks
    await light.poll_once()
    assert light.via_browser and len(plain.calls) == 1
    assert [s.address.value for o in seen for s in o.sightings] == [BRETT]
