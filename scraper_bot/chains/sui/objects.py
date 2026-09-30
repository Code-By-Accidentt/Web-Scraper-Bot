"""Sui JSON-RPC helpers shared by the validator and the token details.

A Sui token is a *coin type* ``0x<package>::<module>::<NAME>``. The chain keeps metadata
(name, symbol, decimals) for real coins only, which makes "is this a token?" a single call.
A bare 32-byte id is a token candidate when it is a package that defines such a coin.
"""

from __future__ import annotations

import asyncio
from typing import Any

from scraper_bot.chains.rpc.client import JsonRpcClient, RpcError

COIN_CHECK_BATCH = 8  # metadata lookups in flight per package
MAX_COIN_CANDIDATES = 128  # structs per package tried as coin types


async def object_kind(client: JsonRpcClient, address: str) -> str:
    """Return "package", "object" or "missing" for a Sui id (wallets and unknown ids: missing)."""
    # With showType a package reports type "package", and a Move object its struct type (mainnet).
    result = await client.call("sui_getObject", [address, {"showType": True}])
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict):
        return "package" if data.get("type") == "package" else "object"
    return "missing"


async def coin_metadata(client: JsonRpcClient, coin_type: str) -> dict[str, Any] | None:
    """Return the coin's metadata, or None when the type is not a coin."""
    result = await client.call("suix_getCoinMetadata", [coin_type])
    return result if isinstance(result, dict) else None


async def total_supply(client: JsonRpcClient, coin_type: str) -> int | None:
    """Return the coin's total supply in raw units, or None when the answer holds no number."""
    result = await client.call("suix_getTotalSupply", [coin_type])
    value = result.get("value") if isinstance(result, dict) else None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def coin_type_candidates(client: JsonRpcClient, package: str) -> list[str]:
    """Return the struct types of ``package`` that could be coins, likeliest first.

    A struct with type parameters is left out: a coin's type never has any. At most
    ``MAX_COIN_CANDIDATES`` are returned.
    """
    modules = await client.call("sui_getNormalizedMoveModulesByPackage", [package])
    if not isinstance(modules, dict):
        return []
    pairs: list[tuple[str, str]] = []
    for module_name, module in modules.items():
        structs = module.get("structs") if isinstance(module, dict) else None
        if isinstance(structs, dict):
            pairs.extend(
                (str(module_name), str(name))
                for name, struct in structs.items()
                if not (isinstance(struct, dict) and struct.get("typeParameters"))
            )
    # By convention a coin's witness struct is its module name in upper case, so those go first.
    pairs.sort(key=lambda pair: pair[1].upper() != pair[0].upper())
    return [f"{package}::{module}::{struct}" for module, struct in pairs[:MAX_COIN_CANDIDATES]]


async def find_coin(
    client: JsonRpcClient,
    package: str,
    *,
    no_coin: set[str] | None = None,
    budget: float | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Return the first coin type defined by ``package`` with its metadata, or None.

    Every candidate is tried (likeliest first, a batch at a time), so a coin behind many
    helper structs is still found. When no coin is found and a lookup failed, that error is
    raised: without it there is no verdict. Candidates without metadata are added to
    ``no_coin`` and those already in it are skipped, so a retry asks only the rest.
    ``budget`` caps the whole search in seconds; past it, RpcError is raised.
    """
    skip = no_coin if no_coin is not None else set()
    try:
        async with asyncio.timeout(budget):
            return await _search(client, package, skip)
    except TimeoutError as exc:
        raise RpcError(f"searching {package} for a coin took over {budget:.0f}s") from exc


async def _search(
    client: JsonRpcClient, package: str, no_coin: set[str]
) -> tuple[str, dict[str, Any]] | None:
    """Do the search of :func:`find_coin`, noting the candidates without metadata."""
    candidates = [c for c in await coin_type_candidates(client, package) if c not in no_coin]
    failure: BaseException | None = None
    for start in range(0, len(candidates), COIN_CHECK_BATCH):
        batch = candidates[start : start + COIN_CHECK_BATCH]
        answers = await asyncio.gather(
            *(coin_metadata(client, candidate) for candidate in batch), return_exceptions=True
        )
        for candidate, metadata in zip(batch, answers, strict=True):
            if isinstance(metadata, dict):
                return candidate, metadata
            if metadata is None:
                no_coin.add(candidate)
        failure = failure or next((a for a in answers if isinstance(a, BaseException)), None)
    if failure is not None:
        raise failure
    return None
