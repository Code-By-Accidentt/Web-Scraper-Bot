"""On-chain validation of candidate addresses (EVM chains, Solana and Sui)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Sequence

import httpx

from scraper_bot.chains.address_extractor import is_sui_coin_type, sui_package
from scraper_bot.chains.evm import ERC20_PROBE
from scraper_bot.chains.rpc import JsonRpcClient, RpcCallError, RpcError
from scraper_bot.chains.solana import account_data, is_mint
from scraper_bot.chains.sui import coin_metadata, find_coin, object_kind
from scraper_bot.config import EvmNetwork
from scraper_bot.models import (
    Address,
    Chain,
    ConfirmedContract,
    ValidationResult,
    ValidationStatus,
)
from scraper_bot.preferences import Tuning
from scraper_bot.storage import AddressCache
from scraper_bot.telegram.notifier import Notifier

log = logging.getLogger(__name__)

_EMPTY_CODE = frozenset({"", "0x", "0x0"})
# JSON-RPC errors that mean the EVM ran the call and it failed: a real "not a token".
_REVERT_MARKERS = ("revert", "invalid opcode", "out of gas", "vm execution")


class RpcUnavailable(Exception):
    """No verdict is possible: at least one endpoint failed."""


class EvmContractValidator:
    """Looks for bytecode on every configured network at once.

    With ``require_erc20`` a contract only counts when it also answers an ERC-20 call.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        networks: Sequence[EvmNetwork],
        *,
        timeout: float,
        tuning: Tuning,
    ) -> None:
        self._clients = [(net.name, JsonRpcClient(http, net.rpc_url, timeout)) for net in networks]
        self._tuning = tuning
        self._failing: set[str] = set()

    async def find_network(self, address: str) -> str | None:
        """Name of the first network holding a contract at ``address``.

        Returns ``None`` only when every network answered and none had code;
        raises :class:`RpcUnavailable` when at least one endpoint failed, because
        a rejection based on partial information would be cached for hours.
        """
        results = await asyncio.gather(
            *(self._check(client, address) for _, client in self._clients),
            return_exceptions=True,
        )
        failed: list[str] = []
        for (name, _), result in zip(self._clients, results, strict=True):
            if isinstance(result, RpcError):
                failed.append(name)
                self._note_failure(name, result)
            elif isinstance(result, BaseException):
                raise result
            else:
                self._note_recovery(name)
                if result:
                    return name
        if failed:
            raise RpcUnavailable(f"no answer from {', '.join(failed)}")
        return None

    async def _check(self, client: JsonRpcClient, address: str) -> bool:
        code = await client.call("eth_getCode", [address, "latest"])
        if not isinstance(code, str) or code.lower() in _EMPTY_CODE:
            return False
        if not self._tuning.require_erc20:
            return True
        return await self._answers_erc20(client, address)

    @staticmethod
    async def _answers_erc20(client: JsonRpcClient, address: str) -> bool:
        """True when the contract returns data for at least one standard ERC-20 call.

        Empty results and EVM reverts are answers ("not a token"). A rate limit or
        timeout is not: it is re-raised so the network counts as failed, instead of
        a real token being rejected and cached for hours.
        """
        calls = (
            client.call("eth_call", [{"to": address, "data": selector}, "latest"])
            for selector in ERC20_PROBE
        )
        answers = await asyncio.gather(*calls, return_exceptions=True)
        if any(isinstance(a, str) and a.lower() not in _EMPTY_CODE for a in answers):
            return True
        for answer in answers:
            if isinstance(answer, BaseException) and not _is_revert(answer):
                raise answer
        return False

    def _note_failure(self, name: str, error: RpcError) -> None:
        if name not in self._failing:
            self._failing.add(name)
            log.warning("RPC endpoint %s is failing: %s", name, error)

    def _note_recovery(self, name: str) -> None:
        if name in self._failing:
            self._failing.discard(name)
            log.info("RPC endpoint %s recovered", name)


def _is_revert(error: BaseException) -> bool:
    if not isinstance(error, RpcCallError):
        return False
    lowered = error.message.lower()
    return error.code == 3 or any(marker in lowered for marker in _REVERT_MARKERS)


class SolanaContractValidator:
    """Accepts executable programs and SPL / Token-2022 mint accounts."""

    def __init__(self, http: httpx.AsyncClient, rpc_url: str, *, timeout: float) -> None:
        self._client = JsonRpcClient(http, rpc_url, timeout)

    async def is_program_or_mint(self, address: str) -> bool:
        try:
            result = await self._client.call("getAccountInfo", [address, {"encoding": "base64"}])
        except RpcError as exc:
            raise RpcUnavailable(str(exc)) from exc
        account = result.get("value") if isinstance(result, dict) else None
        if not isinstance(account, dict):
            return False
        if account.get("executable"):
            return True
        return is_mint(account.get("owner"), account_data(result))


class SuiCoinValidator:
    """Accepts coin types that have metadata and packages that define such a coin."""

    def __init__(
        self, http: httpx.AsyncClient, rpc_url: str, *, timeout: float, tuning: Tuning
    ) -> None:
        self._client = JsonRpcClient(http, rpc_url, timeout)
        self._tuning = tuning

    async def is_coin_or_package(self, value: str) -> bool:
        try:
            if is_sui_coin_type(value):
                if await coin_metadata(self._client, value) is not None:
                    return True
                if self._tuning.require_erc20:
                    return False
                # Lenient mode counts any type of a real package, like any EVM contract.
                return await object_kind(self._client, sui_package(value)) == "package"
            if await object_kind(self._client, value) != "package":
                return False  # a wallet, an owned object or nothing at all
            if not self._tuning.require_erc20:
                return True
            return await find_coin(self._client, value) is not None
        except RpcError as exc:
            raise RpcUnavailable(str(exc)) from exc


class ContractValidator:
    """Validates addresses once, remembers verdicts and merges concurrent checks."""

    def __init__(
        self,
        evm: EvmContractValidator,
        solana: SolanaContractValidator,
        cache: AddressCache,
        *,
        sui: SuiCoinValidator,
        max_concurrent: int,
        retry_delay: float,
        clock: Callable[[], float] = time.monotonic,
        notifier: Notifier | None = None,
    ) -> None:
        self._evm = evm
        self._solana = solana
        self._sui = sui
        self._cache = cache
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._retry_delay = retry_delay
        self._clock = clock
        self._notifier = notifier
        self._retry_after: dict[Address, float] = {}
        self._inflight: dict[Address, asyncio.Task[ValidationResult]] = {}

    async def validate_many(
        self, addresses: Iterable[Address], *, negative_cache: bool = True
    ) -> list[ValidationResult]:
        return list(
            await asyncio.gather(
                *(self.validate(a, negative_cache=negative_cache) for a in addresses)
            )
        )

    async def validate(self, address: Address, *, negative_cache: bool = True) -> ValidationResult:
        """Verdict for ``address``.

        With ``negative_cache=False`` (turbo and /unverified mode) earlier rejections
        and RPC failures are ignored and a new rejection is not remembered, so an
        address published before its contract is deployed can be re-checked freely.
        """
        if self._cache.is_known(address):
            return ValidationResult(address, ValidationStatus.ALREADY_KNOWN)
        if negative_cache:
            if self._cache.is_rejected(address):
                return ValidationResult(address, ValidationStatus.ALREADY_REJECTED)
            not_before = self._retry_after.get(address)
            if not_before is not None:
                if self._clock() < not_before:
                    return ValidationResult(address, ValidationStatus.UNAVAILABLE)
                del self._retry_after[address]

        task = self._inflight.get(address)
        if task is None:
            task = asyncio.create_task(self._validate_uncached(address, negative_cache))
            self._inflight[address] = task
            task.add_done_callback(lambda _: self._inflight.pop(address, None))
        # shield: a cancelled caller must not cancel the check other callers wait for
        return await asyncio.shield(task)

    async def _validate_uncached(self, address: Address, negative_cache: bool) -> ValidationResult:
        async with self._semaphore:
            try:
                network = await self._lookup(address)
            except RpcUnavailable as exc:
                # Endpoint-level failures are logged once by the validators; per address
                # this is routine, so keep it at INFO.
                log.info(
                    "%s %s: %s; retry in %.0fs",
                    address.chain.value,
                    address,
                    exc,
                    self._retry_delay,
                )
                if negative_cache:
                    self._retry_after[address] = self._clock() + self._retry_delay
                if self._notifier is not None:
                    await self._notifier.problem(
                        "rpc",
                        "On-chain address checks are failing",
                        f"{address.chain.value} {address}: {exc}",
                        hint="Check the RPC endpoints in .env. Affected addresses are re-checked "
                        f"automatically after {self._retry_delay:.0f} s.",
                    )
                return ValidationResult(address, ValidationStatus.UNAVAILABLE)
        if network is None:
            if negative_cache:
                self._cache.add_rejected(address)
            log.info("%s %s is not a contract", address.chain.value, address)
            return ValidationResult(address, ValidationStatus.REJECTED)
        self._cache.add_known(ConfirmedContract(address, network))
        log.info("%s %s confirmed as a contract on %s", address.chain.value, address, network)
        return ValidationResult(address, ValidationStatus.CONFIRMED, network)

    async def _lookup(self, address: Address) -> str | None:
        if address.chain is Chain.ETHEREUM:
            return await self._evm.find_network(address.value)
        if address.chain is Chain.SOLANA:
            return "Solana" if await self._solana.is_program_or_mint(address.value) else None
        return "Sui" if await self._sui.is_coin_or_package(address.value) else None
