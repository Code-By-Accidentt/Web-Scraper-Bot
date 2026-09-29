import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scraper_bot.chains.networks import EVM_CHAINS
from scraper_bot.chains.rpc.client import NO_RETRY
from scraper_bot.chains.solana.accounts import TOKEN_PROGRAM
from scraper_bot.models.addresses import (
    Address,
    AddressKind,
    Chain,
    ConfirmedContract,
    ValidationResult,
    ValidationStatus,
)
from scraper_bot.models.sightings import Sighting, SightingSource, Timeline
from scraper_bot.monitoring.alerts import AlertRegistry
from scraper_bot.monitoring.pace import TURBO, SitePace
from scraper_bot.monitoring.pending import watch as pending_module
from scraper_bot.monitoring.pending.entry import PendingEntry, check_interval
from scraper_bot.monitoring.pending.old_news import QUIET
from scraper_bot.monitoring.pending.watch import PendingWatch
from scraper_bot.monitoring.tokens import SiteTokens
from tests.conftest import FakeNotifier, FakeValidator, make_settings

SITE = "https://launch.io"
PAGE = "https://launch.io/app"
EVM = Address(Chain.ETHEREUM, "0x" + "4d" * 20)
OTHER = Address(Chain.ETHEREUM, "0x" + "5e" * 20)
MINT = Address(Chain.SOLANA, "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump")
ENV = {
    **{chain.env_var: "" for chain in EVM_CHAINS},  # every network off but the two below
    "ETH_RPC_URL": "https://eth.rpc",
    "BASE_RPC_URL": "https://base.rpc",
    "SOLANA_RPC_URL": "https://sol.rpc",
}


class Chains:
    """Two EVM networks and Solana behind one transport; everything starts empty."""

    def __init__(self) -> None:
        self.code: dict[str, str] = {}  # lower-case EVM address -> bytecode (both networks)
        self.host_code: dict[tuple[str, str], str] = {}  # (host, address): one network only
        self.methods: list[tuple[str, str]] = []  # (host, method) of every call
        self.nonce: dict[tuple[str, str], int] = {}  # (host, address) -> transactions sent
        self.accounts: dict[str, dict[str, Any] | None] = {}
        self.down: set[str] = set()
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.requests.append(host)
        if host in self.down:
            return httpx.Response(503)
        body = json.loads(request.content)
        items = body if isinstance(body, list) else [body]
        answers = [
            {"jsonrpc": "2.0", "id": item["id"], "result": self.answer(host, item)}
            for item in items
        ]
        return httpx.Response(200, json=answers if isinstance(body, list) else answers[0])

    def answer(self, host: str, item: dict[str, Any]) -> Any:
        method, params = item["method"], item["params"]
        self.methods.append((host, method))
        if method == "eth_getCode":
            found = self.host_code.get((host, params[0].lower()))
            return found or self.code.get(params[0].lower(), "0x")
        if method == "eth_getTransactionCount":
            return hex(self.nonce.get((host, params[0].lower()), 0))
        if method == "getMultipleAccounts":
            return {"context": {"slot": 1}, "value": [self.accounts.get(a) for a in params[0]]}
        raise AssertionError(f"unexpected {method}")


class Harness:
    def __init__(self, tmp_path: Path, *, paused: bool = False) -> None:
        self.chains = Chains()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.chains.handler))
        self.validator = FakeValidator()
        self.notifier = FakeNotifier()
        self.registry = AlertRegistry()
        self.now = 1_790_000_000.0
        self.turbo_interval = 1.0
        self.told: list[Address] = []
        self.paused_sites: set[str] = set()
        self.pace = SitePace()
        self.tokens = SiteTokens()
        self.path = tmp_path / "pending.json"
        self.watch = self.build(paused=paused)

    def build(self, *, paused: bool = False, **env: str) -> PendingWatch:
        return PendingWatch(
            self.http,
            make_settings(self.path.parent, **{**ENV, **env}),
            validator=self.validator,  # type: ignore[arg-type]
            registry=self.registry,
            notifier=self.notifier,  # type: ignore[arg-type]
            path=self.path,
            turbo_interval=lambda: self.turbo_interval,
            is_paused=lambda site: paused or site in self.paused_sites,
            clock=lambda: self.now,
            retry=NO_RETRY,
            pace=self.pace,
            tokens=self.tokens,
        )

    def start_turbo(self, site: str) -> None:
        """Speed ``site`` up as a turbo session does, and note the tokens it hears of."""
        self.pace.boost(site, TURBO)
        self.tokens.subscribe(site, self.heard)

    def stop_turbo(self, site: str) -> None:
        self.pace.release(site, TURBO)
        self.tokens.unsubscribe(site, self.heard)

    def heard(self, contract: ConfirmedContract) -> None:
        self.told.append(contract.address)

    async def tick(self, seconds: float = 1.0) -> None:
        self.now += seconds
        await self.watch.check_due()


def rejected(address: Address, kind: AddressKind) -> ValidationResult:
    return ValidationResult(address, ValidationStatus.REJECTED, kind=kind)


def text(address: Address, context: str = "CA:", hinted: bool = True) -> Sighting:
    return Sighting(address, SightingSource.TEXT, context, hinted)


@pytest.fixture
async def h(tmp_path: Path):
    harness = Harness(tmp_path)
    yield harness
    await harness.http.aclose()


async def test_an_address_published_before_deployment_is_announced_once_deployed(h) -> None:
    first_seen = h.now
    assert h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)  # classified: no code, no transaction anywhere
    await h.tick()
    assert h.notifier.contracts == [] and h.watch.count(SITE) == 1
    last_miss = h.now
    h.chains.code[EVM.value.lower()] = "0x6080"
    h.validator.confirm(EVM, "Base")
    await h.tick()
    assert h.notifier.contracts == [(ConfirmedContract(EVM, "Base"), PAGE)]
    [(sighting, timeline)] = h.notifier.contract_details
    assert sighting == text(EVM)
    assert timeline == Timeline(
        h.now, missing_at=last_miss, on_page_since=first_seen, deployed=True
    )
    assert h.watch.count() == 0
    assert h.validator.negative_cache_calls == [False]


async def test_wallets_are_dropped_at_once_and_not_taken_back_for_a_week(h) -> None:
    h.chains.nonce[("base.rpc", EVM.value.lower())] = 3  # it sent transactions on Base
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    assert h.watch.count() == 0
    assert not h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    h.now += 7 * 86_400 + 1
    assert h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))


async def test_an_eip7702_wallet_is_dropped(h) -> None:
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    h.chains.code[EVM.value.lower()] = "0xef0100" + "5a" * 20
    await h.tick(0)
    assert h.watch.count() == 0 and h.validator.calls == []


@pytest.mark.parametrize(
    ("result", "taken"),
    [
        (rejected(EVM, AddressKind.NO_CODE), True),
        (ValidationResult(EVM, ValidationStatus.UNAVAILABLE), True),
        (ValidationResult(EVM, ValidationStatus.ALREADY_REJECTED), True),  # the check will tell
        (rejected(EVM, AddressKind.NOT_TOKEN), False),
        (rejected(EVM, AddressKind.WALLET), False),
        (rejected(EVM, AddressKind.PAIR), False),
        (ValidationResult(EVM, ValidationStatus.CONFIRMED, "Base"), False),
        (ValidationResult(EVM, ValidationStatus.ALREADY_KNOWN), False),
        (rejected(Address(Chain.SUI, "0x" + "ab" * 32), AddressKind.NOT_TOKEN), False),
    ],
)
async def test_only_addresses_that_may_still_become_a_token_are_taken(
    h, result: ValidationResult, taken: bool
) -> None:
    assert h.watch.offer(SITE, PAGE, None, result) is taken


async def test_a_solana_mint_is_announced_when_its_account_appears(h) -> None:
    h.watch.offer(SITE, PAGE, text(MINT), rejected(MINT, AddressKind.MISSING))
    await h.tick(0)
    assert h.watch.count() == 1 and h.notifier.contracts == []
    h.chains.accounts[MINT.value] = {"owner": TOKEN_PROGRAM, "lamports": 1}
    h.validator.confirm(MINT, "Solana")
    await h.tick()
    assert h.notifier.contracts == [(ConfirmedContract(MINT, "Solana"), PAGE)]


async def test_a_solana_account_that_is_not_a_mint_is_dropped(h) -> None:
    h.watch.offer(SITE, PAGE, text(MINT), rejected(MINT, AddressKind.MISSING))
    h.chains.accounts[MINT.value] = {"owner": "11111111111111111111111111111111", "lamports": 5}
    await h.tick(0)
    assert h.watch.count() == 0


async def test_a_mint_without_supply_keeps_being_watched(h) -> None:
    h.watch.offer(SITE, PAGE, text(MINT), rejected(MINT, AddressKind.NOT_YET_MINTED))
    h.chains.accounts[MINT.value] = {"owner": TOKEN_PROGRAM, "lamports": 1}
    h.validator.verdicts[MINT] = [rejected(MINT, AddressKind.NOT_YET_MINTED)]
    await h.tick(0)
    assert h.watch.count() == 1
    h.validator.confirm(MINT, "Solana")
    await h.tick(1)
    assert h.notifier.contracts == [(ConfirmedContract(MINT, "Solana"), PAGE)]


def test_the_schedule_slows_down_with_age() -> None:
    fresh = PendingEntry(EVM, SITE, PAGE, first_seen=0.0, fast=True)
    assert check_interval(fresh, 60) == 1.0
    assert check_interval(fresh, 20 * 60) == 5.0
    assert check_interval(fresh, 2 * 3600) == 30.0
    slow = PendingEntry(EVM, SITE, PAGE, first_seen=0.0, fast=False)
    assert check_interval(slow, 60) == 30.0
    assert check_interval(slow, 60, turbo=1.0) == 1.0  # a site in turbo: the turbo pace


async def test_unhinted_addresses_are_checked_slowly_hinted_ones_every_second(h) -> None:
    h.watch.offer(SITE, PAGE, text(EVM, "Treasury:", False), rejected(EVM, AddressKind.NO_CODE))
    h.watch.offer(SITE, PAGE, text(OTHER), rejected(OTHER, AddressKind.NO_CODE))
    await h.tick(0)
    h.chains.requests.clear()
    h.chains.code[EVM.value.lower()] = h.chains.code[OTHER.value.lower()] = "0x6080"
    await h.tick(1)  # only the hinted one is due
    assert len(h.validator.calls) == 1 and h.validator.calls[0] == [OTHER]
    await h.tick(29)
    assert h.validator.calls[-1] == [EVM]


async def test_a_turbo_site_is_checked_at_the_turbo_pace_and_told_about_its_tokens(h) -> None:
    h.start_turbo(SITE)
    h.watch.offer(SITE, PAGE, text(EVM, "x", False), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    h.chains.code[EVM.value.lower()] = "0x6080"
    h.validator.confirm(EVM, "Base")
    await h.tick(1)  # slow by its words, but the site is in turbo: checked after 1 s
    assert h.told == [EVM]
    h.stop_turbo(SITE)
    h.validator.known(OTHER)
    h.watch.offer(SITE, PAGE, text(OTHER), rejected(OTHER, AddressKind.NO_CODE))
    h.chains.code[OTHER.value.lower()] = "0x6080"
    await h.tick(0)
    assert h.told == [EVM]  # no listener any more; ALREADY_KNOWN just leaves the watch
    assert h.watch.count() == 0 and len(h.notifier.contracts) == 1


async def test_turbo_baseline_existing_is_old_news_absent_is_watched(h) -> None:
    h.chains.code[EVM.value.lower()] = "0x6080"  # already deployed when turbo started
    h.watch.offer_baseline(SITE, PAGE, text(EVM))
    h.watch.offer_baseline(SITE, PAGE, text(OTHER))  # not deployed yet
    await h.tick(0)
    assert h.watch.count() == 1 and h.validator.calls == []
    h.chains.code[OTHER.value.lower()] = "0x6080"
    h.validator.confirm(OTHER, "Base")
    await h.tick(1)
    assert h.notifier.contracts == [(ConfirmedContract(OTHER, "Base"), PAGE)]


async def test_limits_keep_fast_entries_and_never_churn(h, monkeypatch) -> None:
    monkeypatch.setattr(pending_module, "SITE_LIMIT", 2)
    slow = [Address(Chain.ETHEREUM, f"0x{i:040x}") for i in range(0x10001, 0x10004)]
    unhinted = [text(a, "", False) for a in slow]
    assert h.watch.offer(SITE, PAGE, unhinted[0], rejected(slow[0], AddressKind.NO_CODE))
    h.now += 1
    assert h.watch.offer(SITE, PAGE, unhinted[1], rejected(slow[1], AddressKind.NO_CODE))
    assert not h.watch.offer(SITE, PAGE, unhinted[2], rejected(slow[2], AddressKind.NO_CODE))
    assert h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))  # fast
    assert h.watch.count(SITE) == 2
    assert h.watch.offer(SITE, PAGE, text(OTHER), rejected(OTHER, AddressKind.NO_CODE))
    assert not h.watch.offer(SITE, PAGE, text(MINT), rejected(MINT, AddressKind.MISSING))
    assert h.watch.count() == 2  # two fast entries: nothing slow is left to push out


async def test_an_address_is_given_up_after_a_day(h) -> None:
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(24 * 3600 + 1)
    assert h.watch.count() == 0
    unhinted = text(EVM, "", False)
    assert not h.watch.offer(SITE, PAGE, unhinted, rejected(EVM, AddressKind.NO_CODE))
    # Given up on, not a wallet: named as the CA (or seen by turbo) it is watched again.
    assert h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    h.watch.forget_site(SITE)
    h.watch.offer_baseline(SITE, PAGE, text(EVM))
    assert h.watch.count() == 1


async def test_a_paused_site_waits(tmp_path: Path) -> None:
    h = Harness(tmp_path, paused=True)
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(5)
    assert h.chains.requests == [] and h.watch.count() == 1
    await h.http.aclose()


async def test_a_failing_network_neither_classifies_nor_drops(h) -> None:
    h.chains.down.add("base.rpc")
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    entry = h.watch._entries[EVM]
    assert h.watch.count() == 1 and not entry.classified and entry.next_check > h.now
    h.chains.down.clear()
    await h.tick(1)
    assert entry.classified


async def test_a_failing_optional_network_still_classifies(h) -> None:
    h.watch = h.build(ARC_RPC_URL="https://arc.rpc")  # Arc: an optional network
    h.chains.down.add("arc.rpc")
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    entry = h.watch._entries[EVM]
    assert entry.classified and set(entry.absent_on) == {"Ethereum", "Base"}
    await h.tick(1)
    nonces = [host for host, method in h.chains.methods if method == "eth_getTransactionCount"]
    assert sorted(nonces) == ["base.rpc", "eth.rpc"]  # asked once, not on every tick


async def test_a_token_on_an_optional_network_is_announced_once_it_answers(h) -> None:
    h.watch = h.build(ARC_RPC_URL="https://arc.rpc")
    h.chains.down.add("arc.rpc")
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    h.chains.down.clear()
    h.chains.host_code[("arc.rpc", EVM.value.lower())] = "0x6080"  # it was there all along
    h.validator.confirm(EVM, "Arc")
    await h.tick(1)
    assert h.notifier.contracts == [(ConfirmedContract(EVM, "Arc"), PAGE)]
    assert h.validator.options == [(("Arc",), True)]
    [(_, timeline)] = h.notifier.contract_details
    # Nobody saw Arc empty, so nothing is said about when it was deployed.
    assert timeline is not None and not timeline.deployed and timeline.missing_at is None


async def test_late_tokens_of_a_first_check_pause_go_out_as_one_document(h) -> None:
    h.watch = h.build(ARC_RPC_URL="https://arc.rpc")  # Arc: an optional network
    unanswered = Address(Chain.ETHEREUM, "0x" + "6f" * 20)
    for address in (EVM, OTHER):
        h.watch.offer(SITE, PAGE, text(address), rejected(address, AddressKind.NO_CODE))
    verdicts = [rejected(EVM, AddressKind.NO_CODE), rejected(OTHER, AddressKind.NO_CODE)]
    verdicts.append(ValidationResult(unanswered, ValidationStatus.UNAVAILABLE))
    h.watch.first_check_paused(SITE, verdicts)
    h.chains.host_code[("arc.rpc", EVM.value.lower())] = "0x6080"  # there all along: late
    h.chains.host_code[("base.rpc", OTHER.value.lower())] = "0x6080"  # a launch meanwhile
    h.validator.confirm(EVM, "Arc")
    h.validator.confirm(OTHER, "Base")
    await h.tick(0)
    assert h.notifier.contracts == [(ConfirmedContract(OTHER, "Base"), PAGE)]  # news
    assert h.watch.take_old_news(SITE, ConfirmedContract(unanswered, "Base"))  # the monitor's
    await h.tick(1)
    assert h.notifier.late_batches == []  # the burst is still coming in
    await h.tick(QUIET)
    late = (ConfirmedContract(EVM, "Arc"), ConfirmedContract(unanswered, "Base"))
    assert h.notifier.late_batches == [(SITE, late)]
    assert len(h.notifier.contracts) == 1 and not h.registry.claim(late[0])


async def test_entries_survive_a_restart_and_a_broken_file_is_ignored(h) -> None:
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    h.watch.offer(SITE, PAGE, text(MINT, "mint:"), rejected(MINT, AddressKind.MISSING))
    h.chains.nonce[("eth.rpc", OTHER.value.lower())] = 1
    h.watch.offer(SITE, PAGE, text(OTHER), rejected(OTHER, AddressKind.NO_CODE))
    await h.tick(0)  # OTHER is a wallet: dropped and remembered
    h.watch.save()
    again = h.build()
    again.load()
    assert again.count(SITE) == 2
    assert again._entries[MINT].sighting == text(MINT, "mint:")
    assert not again.offer(SITE, PAGE, text(OTHER), rejected(OTHER, AddressKind.NO_CODE))
    again.forget_site(SITE)
    assert again.count() == 0

    h.path.write_text("{not json", encoding="utf-8")
    broken = h.build()
    broken.load()
    assert broken.count() == 0


async def test_the_loop_is_silent_without_addresses(h) -> None:
    ticks = 0

    async def sleep(_seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks > 3:
            raise asyncio.CancelledError

    h.watch._sleep = sleep
    with pytest.raises(asyncio.CancelledError):
        await h.watch.run()
    assert h.chains.requests == []


async def test_a_flaky_network_does_not_turn_a_baseline_launch_into_old_news(h) -> None:
    h.chains.down.add("eth.rpc")  # one of the networks keeps failing
    h.watch.offer_baseline(SITE, PAGE, text(EVM))  # the CA was up when turbo started
    await h.tick(0)  # Base answers: nothing there yet
    h.chains.code[EVM.value.lower()] = "0x6080"
    h.validator.confirm(EVM, "Base")
    await h.tick(1)
    assert h.notifier.contracts == [(ConfirmedContract(EVM, "Base"), PAGE)]
    assert h.validator.options == [(("Base",), True)]  # only where it appeared, no queue


async def test_old_news_at_turbo_start_is_not_remembered_as_dropped(h) -> None:
    h.chains.code[EVM.value.lower()] = "0x6080"
    h.watch.offer_baseline(SITE, PAGE, text(EVM))
    await h.tick(0)
    assert h.watch.count() == 0
    h.watch.offer_baseline(SITE, PAGE, text(EVM))  # a later session may ask again
    assert h.watch.count() == 1


async def test_a_cached_lp_pair_is_never_watched(h) -> None:
    pair = ValidationResult(EVM, ValidationStatus.ALREADY_REJECTED, kind=AddressKind.PAIR)
    assert not h.watch.offer(SITE, PAGE, text(EVM), pair)


async def test_an_old_token_behind_a_new_pair_is_not_news_for_turbo(h) -> None:
    h.start_turbo(SITE)
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    h.chains.code[EVM.value.lower()] = "0x6080"
    h.validator.verdicts[EVM] = [
        ValidationResult(EVM, ValidationStatus.REJECTED, "Base", kind=AddressKind.PAIR)
    ]
    await h.tick(1)
    assert h.told == [] and h.watch.count() == 0  # a pair: dropped, nobody told


async def test_an_address_shown_by_two_sites_is_checked_while_either_is_active(h) -> None:
    other_site = "https://other.io"
    h.paused_sites.add(SITE)
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    assert h.chains.requests == []  # its only site is paused
    assert not h.watch.offer(other_site, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    assert h.watch.count(other_site) == 1
    h.start_turbo(other_site)
    h.chains.code[EVM.value.lower()] = "0x6080"
    h.validator.confirm(EVM, "Base")
    await h.tick(1)
    assert h.told == [EVM] and len(h.notifier.contracts) == 1
    h.watch.offer(SITE, PAGE, text(OTHER), rejected(OTHER, AddressKind.NO_CODE))
    h.watch.forget_site(SITE)
    assert h.watch.count() == 0


async def test_turbo_on_a_paused_site_still_checks_its_addresses(h) -> None:
    h.paused_sites.add(SITE)
    h.start_turbo(SITE)
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    assert h.chains.requests


async def test_a_token_the_monitor_announced_leaves_the_watch(h) -> None:
    other_site = "https://other.io"
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    h.watch.offer(other_site, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    assert h.watch.announced(SITE, EVM) == {other_site}  # its turbo hears of it too
    assert h.watch.count() == 0


async def test_a_restart_keeps_what_was_seen_absent(h) -> None:
    h.watch.offer_baseline(SITE, PAGE, text(EVM))
    await h.tick(0)  # absent everywhere: its appearance would be news
    h.watch.save()
    again = h.build()
    again.load()
    h.watch = again
    h.chains.code[EVM.value.lower()] = "0x6080"  # deployed while the bot was down
    h.validator.confirm(EVM, "Base")
    await h.tick(60)
    assert len(h.notifier.contracts) == 1


async def test_no_deployed_window_when_absence_was_never_seen(h) -> None:
    h.chains.code[EVM.value.lower()] = "0x6080"  # an RPC hiccup sent an old token here
    h.watch.offer(SITE, PAGE, text(EVM), ValidationResult(EVM, ValidationStatus.UNAVAILABLE))
    h.validator.confirm(EVM, "Base")
    await h.tick(0)
    [(_, timeline)] = h.notifier.contract_details
    assert timeline is not None and not timeline.deployed and timeline.missing_at is None


async def test_a_slow_entry_speeds_up_when_the_page_names_it(h) -> None:
    h.watch.offer(SITE, PAGE, text(EVM, "", False), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    assert not h.watch._entries[EVM].fast
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    entry = h.watch._entries[EVM]
    assert entry.fast and entry.next_check <= h.now


async def test_saving_forgets_expired_drops(h) -> None:
    h.chains.nonce[("base.rpc", EVM.value.lower())] = 1
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    assert EVM in h.watch._dropped
    h.now += 7 * 86_400 + 1
    h.watch.save()
    assert EVM not in h.watch._dropped


async def test_a_clock_stepping_back_does_not_stall_the_checks(h) -> None:
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    h.now -= 3600  # NTP stepped the clock back an hour
    h.chains.requests.clear()
    await h.tick(0)
    await h.tick(1)
    assert h.chains.requests


class EvictingValidator(FakeValidator):
    """Confirms the address while its entry is pushed out of the watch mid-check."""

    def __init__(self, harness: Harness) -> None:
        super().__init__()
        self.harness = harness

    async def validate_many(self, addresses, **options):  # type: ignore[override]
        self.harness.watch.forget_site(SITE)  # /remove (or an eviction) during the check
        return await super().validate_many(addresses, **options)


async def test_a_token_confirmed_while_its_entry_left_the_watch_is_still_announced(h) -> None:
    h.validator = EvictingValidator(h)
    h.watch = h.build()
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    h.chains.code[EVM.value.lower()] = "0x6080"
    h.validator.confirm(EVM, "Base")
    await h.tick(1)
    assert h.notifier.contracts == [(ConfirmedContract(EVM, "Base"), PAGE)]


class PairValidator(FakeValidator):
    """The watched address turned out to be an LP pair holding ``token``."""

    def __init__(self, token: Address, status: ValidationStatus) -> None:
        super().__init__()
        self.token, self.status = token, status

    async def validate_many(self, addresses, **options):  # type: ignore[override]
        [pair] = list(addresses)
        network = "Base" if self.status is ValidationStatus.CONFIRMED else None
        return [
            ValidationResult(pair, ValidationStatus.REJECTED, "Base", kind=AddressKind.PAIR),
            ValidationResult(self.token, self.status, network, via=pair),
        ]


async def test_an_old_token_behind_a_deployed_pair_is_nobodys_news(h) -> None:
    h.validator = PairValidator(OTHER, ValidationStatus.ALREADY_KNOWN)
    h.watch = h.build()
    h.start_turbo(SITE)
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    h.chains.code[EVM.value.lower()] = "0x6080"
    await h.tick(1)
    assert h.told == [] and h.notifier.contracts == [] and h.watch.count() == 0


async def test_a_new_token_behind_a_pair_gets_no_deployment_window(h) -> None:
    h.validator = PairValidator(OTHER, ValidationStatus.CONFIRMED)
    h.watch = h.build()
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    h.chains.code[EVM.value.lower()] = "0x6080"
    await h.tick(1)
    [(_, timeline)] = h.notifier.contract_details
    assert timeline is not None and not timeline.deployed  # the pair's age, not the token's


async def test_starting_turbo_checks_the_sites_addresses_at_once(h) -> None:
    h.watch.offer(SITE, PAGE, text(EVM, "", False), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    assert h.watch._entries[EVM].next_check == h.now + 30  # slow: every 30 s
    h.start_turbo(SITE)
    assert h.watch._entries[EVM].next_check <= h.now


async def test_removing_one_site_keeps_the_page_of_another(h) -> None:
    other_site, other_page = "https://other.io", "https://other.io/mint"
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    h.watch.offer(other_site, other_page, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    h.watch.forget_site(SITE)
    entry = h.watch._entries[EVM]
    assert (entry.site_url, entry.page_url) == (other_site, other_page)


async def test_a_full_site_pushes_out_only_its_own_claim_on_a_shared_address(
    h, monkeypatch
) -> None:
    monkeypatch.setattr(pending_module, "SITE_LIMIT", 1)
    other_site = "https://other.io"
    h.watch.offer(SITE, PAGE, text(EVM, "", False), rejected(EVM, AddressKind.NO_CODE))
    h.watch.offer(other_site, PAGE, text(EVM, "", False), rejected(EVM, AddressKind.NO_CODE))
    assert h.watch.offer(SITE, PAGE, text(OTHER), rejected(OTHER, AddressKind.NO_CODE))
    assert h.watch.count(other_site) == 1 and h.watch.count(SITE) == 1  # EVM stays for other


async def test_files_of_earlier_builds_load(h) -> None:
    raw = {
        "address": f"Ethereum:{EVM.value}",
        "site": SITE,
        "page": PAGE,
        "first_seen": h.now,
        "fast": True,
        "classified": True,
        "baseline": True,
        "sites": [SITE],  # a list, as an earlier build wrote it
    }
    h.path.write_text(json.dumps({"version": 1, "entries": [raw]}), encoding="utf-8")
    h.watch.load()
    entry = h.watch._entries[EVM]
    assert entry.sites == {SITE: PAGE}
    assert not entry.baseline  # no per-network absence recorded: not taken for old news


async def test_a_hanging_optional_network_costs_a_tick_only_the_grace(
    h, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pending_module, "OPTIONAL_GRACE", 0.05)
    hang = asyncio.Event()
    original = h.chains.handler
    arc_requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "arc.rpc":
            arc_requests.append("batch")
            await hang.wait()
        return original(request)

    h.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    h.watch = h.build(ARC_RPC_URL="https://arc.rpc")
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    h.watch.offer(SITE, PAGE, text(MINT), rejected(MINT, AddressKind.MISSING))
    h.chains.accounts[MINT.value] = {"owner": TOKEN_PROGRAM, "data": ["", "base64"]}
    h.validator.confirm(MINT, "Solana")
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(h.tick(0), timeout=2)
    assert asyncio.get_running_loop().time() - started < 1  # not the 5 s RPC timeout
    assert h.notifier.contracts == [(ConfirmedContract(MINT, "Solana"), PAGE)]
    entry = h.watch._entries[EVM]
    assert entry.classified and "Arc" not in entry.absent_on  # Arc said nothing yet
    await asyncio.wait_for(h.tick(1), timeout=2)  # Arc still hangs: it is not asked twice
    assert arc_requests == ["batch"]
    hang.set()
    await asyncio.sleep(0.05)
    await h.http.aclose()


async def test_existence_checks_feed_the_endpoint_health(h) -> None:
    recorded: list[tuple[str, bool]] = []
    h.watch._health = lambda name, error: recorded.append((name, error is None))
    h.chains.down.add("base.rpc")
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    h.watch.offer(SITE, PAGE, text(MINT), rejected(MINT, AddressKind.MISSING))
    await h.tick(0)
    await asyncio.sleep(0)
    assert sorted(recorded) == [("Base", False), ("Ethereum", True), ("Solana", True)]


async def test_an_error_in_a_tick_is_reported_and_the_watch_goes_on(h) -> None:
    ticks = 0

    async def sleep(_seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks > 3:
            raise asyncio.CancelledError

    async def broken(_chain: object = None) -> None:
        raise KeyError("odd entry")

    h.watch._sleep = sleep
    h.watch.check_due = broken  # type: ignore[method-assign]
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    with pytest.raises(asyncio.CancelledError):
        await h.watch.run()
    # Every tick of each loop tried again (the real notifier rate-limits the reports).
    assert len(h.notifier.problems) >= 3
    assert h.notifier.problems[0][0] == "pending" and "KeyError" in h.notifier.problems[0][2]


async def test_the_memory_of_dropped_addresses_is_bounded(
    h, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scraper_bot.monitoring.pending.watch as pending_module

    monkeypatch.setattr(pending_module, "DROPPED_LIMIT", 2)
    wallets = [Address(Chain.ETHEREUM, f"0x{i:040x}") for i in range(1, 4)]
    for wallet in wallets:
        assert h.watch.offer(SITE, PAGE, text(wallet), rejected(wallet, AddressKind.NO_CODE))
        h.watch._drop(h.watch._entries[wallet], h.now)
    assert list(h.watch._dropped) == wallets[1:]  # the first one dropped went first
    newest = wallets[2]
    assert not h.watch.offer(SITE, PAGE, text(newest), rejected(newest, AddressKind.NO_CODE))
    oldest = wallets[0]
    assert h.watch.offer(SITE, PAGE, text(oldest), rejected(oldest, AddressKind.NO_CODE))


async def test_code_an_optional_network_reports_after_its_tick_is_confirmed_next_tick(
    h, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pending_module, "OPTIONAL_GRACE", 0.02)
    original = h.chains.handler
    slow = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "arc.rpc":
            await slow.wait()  # Arc answers, but only after the tick has moved on
        return original(request)

    h.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    h.watch = h.build(ARC_RPC_URL="https://arc.rpc")
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    h.chains.host_code[("arc.rpc", EVM.value.lower())] = "0x6080"
    slow.set()
    await asyncio.sleep(0.05)  # the late batch comes back with code on Arc
    assert h.watch._entries[EVM].late == {"Arc"}
    h.validator.confirm(EVM, "Arc")
    await h.tick(1)
    assert h.notifier.contracts == [(ConfirmedContract(EVM, "Arc"), PAGE)]
    assert h.validator.options == [(("Arc",), True)]
    await h.http.aclose()


async def test_a_token_the_validators_found_late_is_confirmed_at_the_next_tick(h) -> None:
    h.watch.offer(SITE, PAGE, text(EVM, hinted=False), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)  # a slow entry: its next look is 30 s away
    h.validator.confirm(EVM, "Arc")
    h.watch.hurry(EVM, "Arc")
    await h.tick(1)
    assert h.notifier.contracts == [(ConfirmedContract(EVM, "Arc"), PAGE)]
    h.watch.hurry(OTHER, "Arc")  # not watched: nothing happens


async def test_a_network_switched_back_on_starts_without_old_absences(h) -> None:
    h.watch = h.build(ARC_RPC_URL="https://arc.rpc")
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    assert "Arc" in h.watch._entries[EVM].absent_on
    h.watch.save()
    again = h.build(ARC_RPC_URL="https://arc.rpc")
    again._new_networks = frozenset({"Arc"})  # Arc was switched off and on again
    again._stale_absences = frozenset({"Arc"})
    again.load()
    entry = again._entries[EVM]
    assert entry.baseline and "Arc" not in entry.absent_on and "Base" in entry.absent_on


async def test_the_first_start_after_the_update_keeps_the_absences_2_1_saw(h) -> None:
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    await h.tick(0)
    h.watch.save()
    again = h.build()
    again._new_networks = frozenset({"Ethereum", "Base"})  # no networks file: all "new"
    again.load()
    entry = again._entries[EVM]
    assert entry.baseline and set(entry.absent_on) == {"Ethereum", "Base"}


async def test_a_late_empty_answer_counts_as_seen_absent(
    h, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pending_module, "OPTIONAL_GRACE", 0.02)
    original = h.chains.handler
    slow = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "arc.rpc":
            await slow.wait()  # Arc always answers after the tick moved on
        return original(request)

    h.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    h.watch = h.build(ARC_RPC_URL="https://arc.rpc")
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    sent_at = h.now
    await h.tick(0)
    slow.set()
    await asyncio.sleep(0.05)
    entry = h.watch._entries[EVM]
    assert entry.absent_on["Arc"] == sent_at  # the late "nothing here" counts
    await h.http.aclose()


async def test_a_hurry_during_the_entry_s_own_check_is_not_undone(h) -> None:
    h.watch.offer(SITE, PAGE, text(EVM, hinted=False), rejected(EVM, AddressKind.NO_CODE))
    entry = h.watch._entries[EVM]
    entry.late.add("Arc")  # code reported while its look was running
    h.watch._reschedule(entry, h.now)
    assert entry.next_check == h.now  # confirmed at the next tick, not in 30 s


async def test_hanging_evm_confirmations_do_not_hold_back_a_solana_mint(h) -> None:
    release = asyncio.Event()
    validate_many = h.validator.validate_many

    async def slow_evm(addresses, **options):
        addresses = list(addresses)
        if addresses and addresses[0].chain is Chain.ETHEREUM:
            await release.wait()
        return await validate_many(addresses, **options)

    h.validator.validate_many = slow_evm  # type: ignore[method-assign]
    wallets = [Address(Chain.ETHEREUM, f"0x{i:040x}") for i in range(1, 6)]
    for wallet in wallets:
        h.watch.offer(SITE, PAGE, text(wallet), rejected(wallet, AddressKind.NO_CODE))
        h.chains.code[wallet.value.lower()] = "0x6080"  # all "deployed": confirmations hang
    h.watch.offer(SITE, PAGE, text(MINT), rejected(MINT, AddressKind.MISSING))
    h.chains.accounts[MINT.value] = {"owner": TOKEN_PROGRAM, "data": ["", "base64"]}
    h.validator.confirm(MINT, "Solana")
    evm = asyncio.create_task(h.watch.check_due(Chain.ETHEREUM))
    await asyncio.sleep(0.02)
    await asyncio.wait_for(h.watch.check_due(Chain.SOLANA), timeout=1)
    assert h.notifier.contracts == [(ConfirmedContract(MINT, "Solana"), PAGE)]
    release.set()
    await evm


async def test_the_watch_runs_one_loop_per_chain_family(h) -> None:
    ticks: list[object] = []

    async def check_due(chain: object = None) -> None:
        ticks.append(chain)

    async def sleep(_seconds: float) -> None:
        if len(ticks) >= 4:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    h.watch._sleep = sleep
    h.watch.check_due = check_due  # type: ignore[method-assign]
    h.watch.offer(SITE, PAGE, text(EVM), rejected(EVM, AddressKind.NO_CODE))
    with pytest.raises(asyncio.CancelledError):
        await h.watch.run()
    assert set(ticks) == {Chain.ETHEREUM, Chain.SOLANA}
