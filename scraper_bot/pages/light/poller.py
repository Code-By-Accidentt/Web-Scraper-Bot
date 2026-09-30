"""A site's pages and APIs polled over HTTP between renders: the light mode.

A new address in a page's HTML, or in the answer of an API the page calls, goes through the
usual pipeline at once (checked on-chain, then announced). A page whose HTML carries what
its render shows, and that neither ticks nor polls on its own, is "covered" while the light
mode polls it: the monitor then renders it only every ``render_interval``. A new set of
scripts in a page's HTML (a new deploy of a single-page app) asks the monitor to render
that page right away.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import logging
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from typing import Any, TypeVar
from urllib.parse import urlsplit

from scraper_bot.chains.addresses.extractor import AddressExtractor
from scraper_bot.chains.validator import ContractValidator
from scraper_bot.models.pages import PageCheckOutcome, Signature
from scraper_bot.models.sightings import Observation, Sighting, SightingSource
from scraper_bot.monitoring.rounds import wait_or_stop
from scraper_bot.pages.changes.normalizer import HtmlNormalizer
from scraper_bot.pages.fetching.blocking import BlockDetector
from scraper_bot.pages.fetching.result import FetchResult
from scraper_bot.pages.light.transport import LightTransport
from scraper_bot.pages.sightings import find_sightings, locate_raw, raw_sightings
from scraper_bot.pages.verdicts import observations
from scraper_bot.settings.live import Tuning

log = logging.getLogger(__name__)

Observe = Callable[[Observation], Awaitable[None]]
_T = TypeVar("_T")

COVERAGE = 0.8  # share of a render's texts its HTML must carry for the page to be covered
APIS_PER_PAGE = 2
APIS_PER_SITE = 6
_MIN_TEXTS = 10  # a render with fewer texts is too small to judge
_API_TRIES = 30  # most API URLs we try on one site (later ones are left alone)
_FAILURES = 3  # failed polls in a row before a page or an API rests
_RENDER_EVERY = 10.0  # new scripts on a page may ask for a render at most this often (s)
_SCRIPT_FLIPS = 3  # more changes in a row than this mean scripts change per request, not a deploy
_REFUSED_REST = 60.0  # seconds the light mode rests after the browser was refused as well
_THREAD_FROM = 10_000  # a text longer than this (chars) is searched in a thread
_JUDGE_EVERY = 30.0  # a changed HTML alone may trigger a new judging at most this often (s)
_TAG_RE = re.compile(r"<(script|link)\b([^<>]{0,2000})>", re.IGNORECASE)
_ATTR_RE = re.compile(r"\b(src|href|rel)\s*=\s*[\"']?([^\"'\s>]{1,1000})", re.IGNORECASE)
_HINTED_KEY_RE = re.compile(
    r"[\"'](?:ca|contract|contract_?address|mint|token_?address)[\"']\s*:", re.IGNORECASE
)
_SECOND_LEVELS = frozenset({"co", "com", "net", "org", "gov", "edu", "ac"})
# Hosting services whose sub-domains belong to different people: coin.vercel.app and
# indexer.vercel.app are two sites.
_SHARED_HOSTS = frozenset(
    {
        "4everland.app",
        "amplifyapp.com",
        "appspot.com",
        "azurestaticapps.net",
        "azurewebsites.net",
        "bubbleapps.io",
        "cloudfront.net",
        "deno.dev",
        "firebaseapp.com",
        "fly.dev",
        "framer.app",
        "framer.website",
        "github.io",
        "gitbook.io",
        "gitlab.io",
        "glitch.me",
        "herokuapp.com",
        "ipfs.dweb.link",
        "netlify.app",
        "ngrok-free.app",
        "ngrok.app",
        "ngrok.io",
        "notion.site",
        "on.fleek.co",
        "onrender.com",
        "pages.dev",
        "railway.app",
        "repl.co",
        "replit.app",
        "surge.sh",
        "trycloudflare.com",
        "up.railway.app",
        "vercel.app",
        "web.app",
        "webflow.io",
        "workers.dev",
    }
)


@dataclass(slots=True)
class _Page:
    """What the light mode knows about one page of the site."""

    url: str
    origin: str
    etag: str | None = None
    modified: str | None = None
    digest: str | None = None  # of its last HTML
    html: str = ""
    candidates: frozenset[str] = frozenset()  # address values its last HTML carried
    served: frozenset[str] | None = None  # the texts of its last HTML, once judged
    scripts: frozenset[str] | None = None
    script_flips: int = 0
    asked_render_at: float = float("-inf")
    rendered: Signature | None = None  # its last render that read the page
    ticking: bool = False
    countdowns: dict[str, float] = field(default_factory=dict)  # launch countdown -> time left
    polls: bool = False  # it calls one of its APIs again and again
    covered: bool = False
    judge: bool = False  # its coverage needs judging again
    judged_at: float = float("-inf")
    verdict: str | None = None  # why it is covered or not, as last logged
    failures: int = 0
    rest_until: float = 0.0


@dataclass(slots=True)
class _Api:
    """One JSON API of the site that the light mode polls for a page."""

    url: str
    page: str
    origin: str
    etag: str | None = None
    modified: str | None = None
    digest: str | None = None
    seen: set[str] = field(default_factory=set)  # every address value it carried
    failures: int = 0
    rest_until: float = 0.0


class LightPoller:
    """Polls one site's pages and APIs every ``light_interval`` seconds between renders.

    ``observe`` gets what they show as observations, like the page checker's, and
    ``request_render`` asks the monitor to render a page soon. Nothing is asked while
    ``is_blocked`` says the site blocks the bot. A site that refuses the plain client but
    lets the browser in is asked through the browser from then on.
    """

    def __init__(
        self,
        site_url: str,
        *,
        http: LightTransport,
        browser: LightTransport | None,
        extractor: AddressExtractor,
        validator: ContractValidator,
        normalizer: HtmlNormalizer,
        tuning: Tuning,
        observe: Observe,
        request_render: Callable[[str], None],
        is_blocked: Callable[[], bool],
        turbo_active: Callable[[], bool] = lambda: False,
        block_detector: BlockDetector | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Set up the polls of ``site_url``; nothing is asked until :meth:`run`.

        ``sleep`` replaces the stop-aware pause between polls (tests pass an instant one).
        """
        self._site_url = site_url
        self._site = _site_of(urlsplit(site_url).hostname or "")
        self._http = http
        self._browser = browser
        self._extractor = extractor
        self._validator = validator
        self._normalizer = normalizer
        self._tuning = tuning
        self._observe = observe
        self._request_render = request_render
        self._is_blocked = is_blocked
        self._turbo_active = turbo_active
        self._blocks = block_detector or BlockDetector()
        self._clock = clock
        self._wall = wall_clock
        self._sleep = sleep
        self._pages: dict[str, _Page] = {}
        self._apis: dict[str, _Api] = {}  # by scheme, host and path
        self._tried: set[str] = set()  # API keys tried, adopted or not
        self._to_try: dict[str, tuple[str, str]] = {}  # API key -> (URL, page that called it)
        self._via_browser = False
        self._rest_until = 0.0

    @property
    def via_browser(self) -> bool:
        """True once the site refused the plain client and is asked through the browser."""
        return self._via_browser

    def covered(self, page_url: str) -> bool:
        """True when the page's HTML carries what its render shows and it does not tick.

        Only while the light mode really polls it: the mode is on, the site doesn't block
        the bot, neither the mode nor the page rests, and the page's last poll went well.
        """
        page = self._pages.get(page_url)
        if page is None or not page.covered or page.failures:
            return False
        now = self._clock()
        return (
            self._interval() > 0
            and not self._is_blocked()
            and now >= self._rest_until
            and now >= page.rest_until
        )

    def summary(self) -> str | None:
        """Return one line for /status: what is polled and how often, or None when off."""
        interval = self._interval()
        if interval <= 0 or not self._pages:
            return None
        polled = f"{len(self._pages)} page(s)"
        if self._apis:
            polled += f" and {len(self._apis)} API(s)"
        parts = [f"{polled} every {interval:g} s"]
        covered = sum(1 for url in self._pages if self.covered(url))
        if covered:
            parts.append(f"{covered} rendered every {self._tuning.render_interval:g} s")
        if self._via_browser:
            parts.append("through the browser")
        return ", ".join(parts)

    def learn(self, outcome: PageCheckOutcome) -> None:
        """Take in what a render showed: the page's nodes and the APIs it called.

        Only a render that read the page counts; the page is polled from then on. APIs of
        other sites (price feeds, DexScreener) are never polled.
        """
        if outcome.signature is None:
            return
        page = self._pages.get(outcome.url)
        if page is None:
            page = self._pages[outcome.url] = _Page(outcome.url, _origin_of(outcome.url))
        page.rendered = outcome.signature
        # A launch countdown ticks too. A new one counts as ticking until the next render
        # shows it standing still.
        countdowns = (
            {c.key: c.remaining for c in outcome.launch.countdowns} if outcome.launch else {}
        )
        moving = any(page.countdowns.get(key) != left for key, left in countdowns.items())
        page.countdowns = countdowns
        page.ticking = outcome.ticking or moving
        calls = Counter(_api_key(url) for url in outcome.api_calls)
        page.polls = any(count > 1 for count in calls.values())
        page.judge = True
        for url in outcome.api_calls:
            key = _api_key(url)
            if key in self._tried or key in self._to_try or len(self._tried) >= _API_TRIES:
                continue
            if _site_of(urlsplit(url).hostname or "") == self._site:
                self._to_try[key] = (url, outcome.url)

    def keep(self, pages: Collection[str]) -> None:
        """Forget the pages not in ``pages`` (gone from the site), with the APIs polled for them."""
        for url in [url for url in self._pages if url not in pages]:
            del self._pages[url]
        for key in [key for key, api in self._apis.items() if api.page not in pages]:
            del self._apis[key]

    async def run(self, stop: asyncio.Event) -> None:
        """Poll every page and API once per interval until ``stop`` is set.

        While turbo runs for the site the turbo interval applies. An interval of 0 turns the
        light mode off; nothing is asked while the site blocks the bot.
        """
        while not stop.is_set():
            interval = self._interval()
            started = self._clock()
            if interval > 0 and not self._is_blocked() and started >= self._rest_until:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("[%s] light poll failed", self._site_url)
            wait = interval - (self._clock() - started) if interval > 0 else 1.0
            await self._pause(max(wait, 0.0), stop)

    async def poll_once(self) -> None:
        """Ask every page and API once (what one tick of :meth:`run` does)."""
        await self._tick()

    def _interval(self) -> float:
        """Return the seconds between two polls: 0 when the light mode is off."""
        if self._tuning.light_interval <= 0:
            return 0.0
        if self._turbo_active():
            return self._tuning.light_turbo_interval
        return self._tuning.light_interval

    async def _tick(self) -> None:
        """Ask every page and API once, try one new API, and judge the pages' coverage."""
        transport = self._transport()
        now = self._clock()
        results = await asyncio.gather(
            *(
                self._poll_page(transport, page)
                for page in list(self._pages.values())
                if now >= page.rest_until
            ),
            *(
                self._poll_api(transport, api)
                for api in list(self._apis.values())
                if now >= api.rest_until
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                log.warning("[%s] a light poll failed: %r", self._site_url, result)
        if self._to_try:
            key, (url, page_url) = next(iter(self._to_try.items()))
            del self._to_try[key]
            self._tried.add(key)
            await self._try_api(transport, key, url, page_url)
        for page in list(self._pages.values()):
            if page.judge and page.html:
                page.judge = False
                page.judged_at = self._clock()
                page.covered, verdict = await asyncio.to_thread(self._judge, page)
                if verdict != page.verdict:
                    page.verdict = verdict
                    log.info("[%s] %s: %s", self._site_url, page.url, verdict)

    def _transport(self) -> LightTransport:
        """Return the browser once the site refused the plain client, else the plain client."""
        if self._via_browser and self._browser is not None:
            return self._browser
        return self._http

    async def _poll_page(self, transport: LightTransport, page: _Page) -> None:
        """Ask one page's HTML; report new addresses, and ask for a render after a new deploy."""
        started = self._wall()
        answer = await transport.get(
            page.url, origin=page.origin, etag=page.etag, modified=page.modified
        )
        if answer.status == 304:
            page.failures = 0
            await self._read(page, started)
            return
        if not self._usable(answer, page, transport):
            return
        page.etag = answer.headers.get("etag")
        page.modified = answer.headers.get("last-modified")
        html = answer.html
        digest = _digest(html)
        if digest == page.digest:
            await self._read(page, started)
            return
        page.digest, page.html, page.served = digest, html, None
        # Judge a new render right away, but a changed HTML at most every _JUDGE_EVERY seconds.
        if page.rendered is not None and self._clock() - page.judged_at >= _JUDGE_EVERY:
            page.judge = True
        self._check_scripts(page, await _off_loop(len(html), _script_list, html))
        found = await self._locate(html)
        new = found - page.candidates
        page.candidates = found
        if not new:
            await self._read(page, started)
            return
        sightings = await asyncio.to_thread(self._sightings, html, new)
        await self._report(page.url, sightings, started, read=page.covered)

    async def _poll_api(self, transport: LightTransport, api: _Api) -> None:
        """Ask one API; report the addresses it never carried before."""
        started = self._wall()
        answer = await transport.get(
            api.url, origin=api.origin, etag=api.etag, modified=api.modified
        )
        if answer.status == 304:
            api.failures = 0
            return
        if not self._usable(answer, api, transport):
            return
        api.etag = answer.headers.get("etag")
        api.modified = answer.headers.get("last-modified")
        digest = _digest(answer.html)
        if digest == api.digest:
            return
        api.digest = digest
        found = await self._locate(answer.html)
        new = found - api.seen
        api.seen |= found
        if new:
            text = answer.html
            sightings = await _off_loop(len(text), self._api_sightings, text, new)
            await self._report(api.page, sightings, started, read=False)

    async def _try_api(self, transport: LightTransport, key: str, url: str, page_url: str) -> None:
        """Ask a new API once and poll it from now on if it may carry token addresses.

        Only a JSON answer that holds addresses or names a CA qualifies, up to
        ``APIS_PER_PAGE`` per page and ``APIS_PER_SITE`` in all. What it carries now is its
        baseline, never news. One the site refused is tried again later.
        """
        page = self._pages.get(page_url)
        if page is None or len(self._apis) >= APIS_PER_SITE:
            return
        if sum(1 for api in self._apis.values() if api.page == page_url) >= APIS_PER_PAGE:
            return
        answer = await transport.get(url, origin=page.origin)
        if self._refused(answer, transport):
            self._to_try[key] = (url, page_url)
            return
        if answer.status != 200 or not answer.html:
            return
        json_like = answer.html.lstrip()[:1] in {"{", "["}
        if not json_like and "json" not in answer.headers.get("content-type", ""):
            return
        found = await self._locate(answer.html)
        if not found and not _HINTED_KEY_RE.search(answer.html):
            return
        self._apis[key] = _Api(
            url,
            page_url,
            page.origin,
            etag=answer.headers.get("etag"),
            modified=answer.headers.get("last-modified"),
            digest=_digest(answer.html),
            seen=set(found),
        )
        log.info(
            "[%s] light mode polls the API %s (for %s; %d address(es) in it now)",
            self._site_url,
            url,
            page_url,
            len(found),
        )

    def _usable(self, answer: FetchResult, polled: _Page | _Api, transport: LightTransport) -> bool:
        """True for an answer worth reading; a refusal or a failure is handled here.

        A page or API that keeps failing rests longer and longer (up to five minutes).
        """
        if self._refused(answer, transport):
            return False
        if not answer.ok or not answer.html:
            polled.failures += 1
            if polled.failures >= _FAILURES:
                polled.rest_until = self._clock() + 30.0 * min(polled.failures - _FAILURES + 1, 10)
            return False
        polled.failures = 0
        return True

    def _refused(self, answer: FetchResult, transport: LightTransport) -> bool:
        """True when the site refused what ``transport`` asked (a server that is down doesn't).

        A refusal of the plain client moves the site to the browser. The light mode rests
        ``_REFUSED_REST`` seconds only when the browser was refused, or the plain client
        with no browser to turn to.
        """
        refusal = self._blocks.inspect(answer)
        if refusal is None or refusal.outage:
            return False
        if transport is self._http and self._browser is not None and not self._is_blocked():
            if not self._via_browser:
                self._via_browser = True
                log.info(
                    "[%s] the site refuses plain requests (%s); the light mode asks through "
                    "the browser",
                    self._site_url,
                    refusal.describe(),
                )
            return True
        self._rest_until = self._clock() + _REFUSED_REST
        log.info(
            "[%s] light requests refused (%s); resting %.0f s",
            self._site_url,
            refusal.describe(),
            _REFUSED_REST,
        )
        return True

    def _check_scripts(self, page: _Page, scripts: frozenset[str]) -> None:
        """Ask for a render when the page loads other ``scripts`` than before (a new deploy).

        At most one render per ``_RENDER_EVERY`` seconds; a page whose script list changes on
        every request is not trusted.
        """
        if page.scripts is None or scripts == page.scripts:
            page.scripts = scripts
            page.script_flips = 0
            return
        page.scripts = scripts
        page.script_flips += 1
        now = self._clock()
        if page.script_flips > _SCRIPT_FLIPS or now - page.asked_render_at < _RENDER_EVERY:
            return
        page.asked_render_at = now
        log.info(
            "[%s] %s loads new scripts (a new deploy?); rendering it now", self._site_url, page.url
        )
        self._request_render(page.url)

    async def _locate(self, text: str) -> frozenset[str]:
        """Return the values of the addresses in ``text`` (in a thread when the text is big)."""
        found = await _off_loop(len(text), locate_raw, self._extractor, text)
        return frozenset(address.value for address, _ in found)

    def _sightings(self, html: str, values: frozenset[str]) -> tuple[Sighting, ...]:
        """Return the sightings of the addresses ``values`` in a page's HTML, as a render's."""
        signature = self._normalizer.signature(html)
        return tuple(
            sighting
            for sighting in find_sightings(self._extractor, signature, html)
            if sighting.address.value in values
        )

    def _api_sightings(self, text: str, values: frozenset[str]) -> tuple[Sighting, ...]:
        """Return the sightings of the addresses ``values`` in an API's answer."""
        return tuple(
            sighting
            for sighting in raw_sightings(self._extractor, text, SightingSource.API)
            if sighting.address.value in values
        )

    async def _report(
        self, page_url: str, sightings: tuple[Sighting, ...], started: float, *, read: bool
    ) -> None:
        """Check the addresses on-chain and hand them to the monitor, a token without delay."""
        if not sightings:
            if read:
                await self._observe(Observation(page_url, self._wall(), (), (), started, True))
            return
        found = observations(
            self._validator, page_url, sightings, seen_at=self._wall(), started=started, read=read
        )
        async with contextlib.aclosing(found):
            async for observation in found:
                await self._observe(observation)

    async def _read(self, page: _Page, started: float) -> None:
        """Tell the monitor a covered page was read as it was, for the ⏱ window."""
        if page.covered:
            await self._observe(Observation(page.url, self._wall(), (), (), started, True))

    def _judge(self, page: _Page) -> tuple[bool, str]:
        """Return whether the page's HTML carries at least ``COVERAGE`` of its render's texts,
        and why, in words for the log.

        A page that ticks or polls its API on its own changes without a new HTML, so it is
        never covered, and neither is a render with too little text to judge. The texts of
        the HTML are read once per HTML, however many renders it is judged against.
        """
        usual = "rendered every round"
        if page.rendered is None:
            return False, f"{usual}: not rendered yet"
        if page.ticking:
            return False, f"{usual}: some of its elements tick (a timer or a clock)"
        if page.polls:
            return False, f"{usual}: it asks its API again and again on its own"
        shown = [node.text for node in page.rendered if node.text]
        if len(shown) < _MIN_TEXTS:
            return False, f"{usual}: too little text to compare with its HTML"
        if page.served is None:
            signature = self._normalizer.signature(page.html)
            page.served = frozenset(node.text for node in signature if node.text)
        share = sum(1 for text in shown if text in page.served) / len(shown)
        if share < COVERAGE:
            return False, f"{usual}: its HTML carries {share:.0%} of what it shows"
        every = self._tuning.render_interval
        return True, f"its HTML carries {share:.0%} of what it shows: rendered every {every:g} s"

    async def _pause(self, seconds: float, stop: asyncio.Event) -> None:
        """Wait ``seconds``, or less when ``stop`` is set meanwhile."""
        if self._sleep is not None:
            await self._sleep(seconds)
            return
        await wait_or_stop(stop, seconds)


async def _off_loop(size: int, call: Callable[..., _T], *args: Any) -> _T:
    """Return ``call(*args)``, run in a thread when it reads more than ``_THREAD_FROM`` chars.

    Searching a long text on the event loop would hold up every other site meanwhile.
    """
    if size > _THREAD_FROM:
        return await asyncio.to_thread(call, *args)
    return call(*args)


def _script_list(html: str) -> frozenset[str]:
    """Return the scripts a page loads (script src, modulepreload links), without queries."""
    found: set[str] = set()
    for tag, attrs in _TAG_RE.findall(html):
        values = {name.lower(): value for name, value in _ATTR_RE.findall(attrs)}
        if tag.lower() == "script" and "src" in values:
            found.add(values["src"].split("?", 1)[0])
        elif "modulepreload" in values.get("rel", "").lower() and "href" in values:
            found.add(values["href"].split("?", 1)[0])
    return frozenset(found)


def _site_of(host: str) -> str:
    """Return the site a host belongs to: ``api.x.io`` and ``app.x.io`` are both ``x.io``.

    Country domains such as ``x.co.uk`` keep three labels, a project on a shared host keeps
    its own label (``coin.vercel.app``), and an IP address is its own site.
    """
    host = host.lower().rstrip(".")
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host)
        return host
    labels = host.split(".")
    for index in range(1, len(labels)):
        if ".".join(labels[index:]) in _SHARED_HOSTS:
            return ".".join(labels[index - 1 :])
    size = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVELS else 2
    return ".".join(labels[-size:])


def _origin_of(url: str) -> str:
    """Return the scheme and host (with port) of ``url``."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _api_key(url: str) -> str:
    """Return an API URL without its query: one API however the page varies its parameters."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _digest(text: str) -> str:
    """Return a short digest of ``text``, to tell an unchanged answer at once."""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
