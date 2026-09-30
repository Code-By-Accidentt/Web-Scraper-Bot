"""Verdicts on the addresses a page shows, handed over as soon as one of them is a token.

A token's alert must not wait for the slowest verdict of the addresses found with it: a
wallet next to the CA ("Deployer: 0x…") is asked on every network before it's rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

from scraper_bot.chains.validator import ContractValidator
from scraper_bot.models.addresses import Address, ValidationResult
from scraper_bot.models.sightings import Observation, Sighting

# How long a confirmed token waits for the verdicts still coming (s), so that the tokens a
# page shows together still go out together.
TOKEN_GRACE = 0.5


@dataclass(frozen=True, slots=True)
class VerdictBatch:
    """Some of the verdicts :func:`verdicts` hands over; ``final`` marks the last batch."""

    results: tuple[ValidationResult, ...]
    final: bool


async def verdicts(
    validator: ContractValidator,
    addresses: Iterable[Address],
    *,
    negative_cache: bool = True,
) -> AsyncIterator[VerdictBatch]:
    """Check the addresses and yield their verdicts in batches, a token's without delay.

    Each address is checked on its own. Once one is confirmed as a token, the verdicts that
    come in within ``TOKEN_GRACE`` seconds join its batch and the rest follow later; with no
    token there's one batch at the end. As in :meth:`ContractValidator.validate_many`, a
    batch lists the addresses' verdicts in the order given, then the tokens behind LP pairs
    (``via``). A token the addresses name themselves, or one listed already, isn't listed
    again.
    """
    asked = list(dict.fromkeys(addresses))
    listed = set(asked)
    loop = asyncio.get_running_loop()
    running: dict[asyncio.Task[list[ValidationResult]], int] = {}  # check -> address's place
    for index, address in enumerate(asked):
        check = validator.validate_many([address], negative_cache=negative_cache)
        running[asyncio.create_task(check)] = index
    try:
        while running:
            ready: dict[int, list[ValidationResult]] = {}
            deadline: float | None = None
            while running:
                wait = None if deadline is None else max(deadline - loop.time(), 0.0)
                done, _ = await asyncio.wait(
                    running, timeout=wait, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    break  # the grace is over
                for task in done:
                    results = task.result()
                    ready[running.pop(task)] = results
                    if deadline is None and any(r.contract is not None for r in results):
                        deadline = loop.time() + TOKEN_GRACE
            yield VerdictBatch(_batch(ready, listed), final=not running)
    finally:
        for task in running:
            task.cancel()


async def observations(
    validator: ContractValidator,
    page_url: str,
    sightings: tuple[Sighting, ...],
    *,
    seen_at: float,
    started: float | None,
    read: bool,
) -> AsyncIterator[Observation]:
    """Yield what one read of ``page_url`` showed, with verdicts, a token's without delay.

    Each observation carries the sightings its verdicts are about. Only the last one counts
    as the read of the page (``read``), so the tokens of every batch are dated from the read
    before this one. With no sightings it yields one observation that has none.
    """
    if not sightings:
        yield Observation(page_url, seen_at, (), (), started, read)
        return
    batches = verdicts(validator, (sighting.address for sighting in sightings))
    async with contextlib.aclosing(batches):
        async for batch in batches:
            spots = {result.via or result.address for result in batch.results}
            shown = tuple(sighting for sighting in sightings if sighting.address in spots)
            yield Observation(
                page_url, seen_at, shown, batch.results, started, read and batch.final
            )


def _batch(
    ready: dict[int, list[ValidationResult]], listed: set[Address]
) -> tuple[ValidationResult, ...]:
    """Return the verdicts that came in: the addresses' in the order given, then new tokens.

    ``ready`` maps each address's place to its verdicts. ``listed`` holds the addresses
    listed so far and takes in the tokens behind LP pairs listed now.
    """
    ordered = [result for _, results in sorted(ready.items()) for result in results]
    behind = []
    for result in ordered:
        if result.via is not None and result.address not in listed:
            listed.add(result.address)
            behind.append(result)
    return (*(result for result in ordered if result.via is None), *behind)
