import asyncio
import json
from pathlib import Path

import httpx
import pytest

from scraper_bot.chains.networks import EVM_CHAINS
from scraper_bot.chains.rpc.warmup import ConnectionWarmer
from tests.conftest import make_settings

ENV = {
    **{chain.env_var: "" for chain in EVM_CHAINS},  # every network off but the three below
    "ETH_RPC_URL": "https://eth.rpc",
    "BASE_RPC_URL": "https://base.rpc",
    "ARB_RPC_URL": "https://arb.rpc",
    "SOLANA_RPC_URL": "https://sol.rpc",
    "SUI_RPC_URL": "https://sui.rpc",
}


def node(chain_ids: dict[str, str | None], seen: list[tuple[str, str]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.host, body["method"]))
        answer = chain_ids.get(request.url.host, "0x1")
        if answer is None:
            return httpx.Response(503)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": answer})

    return httpx.MockTransport(handler)


async def test_endpoints_on_the_wrong_chain_are_named(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []
    # Ethereum is right (1), Base answers 1 instead of 8453, Arbitrum does not answer.
    transport = node({"eth.rpc": "0x1", "base.rpc": "0x1", "arb.rpc": None}, seen)
    async with httpx.AsyncClient(transport=transport) as http:
        warmer = ConnectionWarmer(http, make_settings(tmp_path, **ENV))
        assert await warmer.wrong_chains() == ["Base: chain 1, expected 8453"]


async def test_every_endpoint_is_pinged_each_interval(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) == 2:
            raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=node({"sol.rpc": None}, seen)) as http:
        warmer = ConnectionWarmer(http, make_settings(tmp_path, **ENV), interval=30, sleep=sleep)
        with pytest.raises(asyncio.CancelledError):
            await warmer.run()  # a failing endpoint does not stop the warm-up
    assert pauses == [30, 30]
    assert sorted(set(seen)) == [
        ("arb.rpc", "eth_chainId"),
        ("base.rpc", "eth_chainId"),
        ("eth.rpc", "eth_chainId"),
        ("sol.rpc", "getSlot"),
        ("sui.rpc", "sui_getChainIdentifier"),
    ]
    assert len(seen) == 10  # five endpoints, two rounds, no retries


async def test_answers_and_failures_feed_the_endpoint_health(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []
    recorded: list[tuple[str, bool]] = []

    async def stop(_seconds: float) -> None:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=node({"sol.rpc": None}, seen)) as http:
        warmer = ConnectionWarmer(
            http,
            make_settings(tmp_path, **ENV),
            sleep=stop,
            record=lambda name, error: recorded.append((name, error is None)),
        )
        with pytest.raises(asyncio.CancelledError):
            await warmer.run()
    # An answer ends a glitch, so two glitches far apart never read as one long outage.
    assert sorted(recorded) == [
        ("Arbitrum", True),
        ("Base", True),
        ("Ethereum", True),
        ("Solana", False),
        ("Sui", True),
    ]
