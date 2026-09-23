"""Sui JSON-RPC helpers shared by the validator and the token details.

A Sui token is a *coin type* ``0x<package>::<module>::<NAME>``. The chain keeps
metadata (name, symbol, decimals) for real coins only, which makes "is this a
token?" a single call. A bare 32-byte id is a token candidate when it is a package
that defines such a coin.
"""

from __future__ import annotations

import asyncio
from typing import Any

from scraper_bot.rpc import JsonRpcClient

MAX_COIN_CANDIDATES = 8  # structs of a package tried as coin types


async def object_kind(client: JsonRpcClient, address: str) -> str:
    """``package``, ``object`` or ``missing`` (wallets and unknown ids are missing)."""
    result = await client.call("sui_getObject", [address, {"showType": True}])
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, dict):
        return "package" if data.get("type") == "package" else "object"
    return "missing"


async def coin_metadata(client: JsonRpcClient, coin_type: str) -> dict[str, Any] | None:
    """The coin's metadata, or ``None`` when the type is not a coin."""
    result = await client.call("suix_getCoinMetadata", [coin_type])
    return result if isinstance(result, dict) else None


async def total_supply(client: JsonRpcClient, coin_type: str) -> int | None:
    result = await client.call("suix_getTotalSupply", [coin_type])
    value = result.get("value") if isinstance(result, dict) else None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def coin_type_candidates(
    client: JsonRpcClient, package: str, limit: int = MAX_COIN_CANDIDATES
) -> list[str]:
    """Struct types of ``package`` that could be coins, likeliest first."""
    modules = await client.call("sui_getNormalizedMoveModulesByPackage", [package])
    if not isinstance(modules, dict):
        return []
    pairs: list[tuple[str, str]] = []
    for module_name, module in modules.items():
        structs = module.get("structs") if isinstance(module, dict) else None
        if isinstance(structs, dict):
            pairs.extend((str(module_name), str(struct)) for struct in structs)
    # By convention a coin's witness struct is the module name in upper case.
    pairs.sort(key=lambda pair: pair[1].upper() != pair[0].upper())
    return [f"{package}::{module}::{struct}" for module, struct in pairs[:limit]]


async def find_coin(client: JsonRpcClient, package: str) -> tuple[str, dict[str, Any]] | None:
    """The first coin type defined by ``package`` together with its metadata."""
    candidates = await coin_type_candidates(client, package)
    if not candidates:
        return None
    answers = await asyncio.gather(
        *(coin_metadata(client, candidate) for candidate in candidates), return_exceptions=True
    )
    for candidate, metadata in zip(candidates, answers, strict=True):
        if isinstance(metadata, dict):
            return candidate, metadata
    for answer in answers:
        if isinstance(answer, BaseException):
            raise answer  # nothing confirmed and a lookup failed: no verdict possible
    return None
