"""On-chain validation of candidate addresses (EVM chains and Solana)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import httpx

from scraper_bot.config import EvmNetwork
from scraper_bot.models import (
    Address,
    Chain,
    ConfirmedContract,
    ValidationResult,
    ValidationStatus,
)
from scraper_bot.rpc import JsonRpcClient, RpcError
from scraper_bot.storage import AddressCache

log = logging.getLogger(__name__)

# totalSupply(), symbol(), decimals()
ERC20_SELECTORS: tuple[str, ...] = ("0x18160ddd", "0x95d89b41", "0x313ce567")
SOLANA_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOLANA_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
_MINT_SIZE = 82
_TOKEN_ACCOUNT_SIZE = 165
_EMPTY_CODE = frozenset({"", "0x", "0x0"})


class RpcUnavailable(Exception):
    """The endpoints that failed make it impossible to reach a verdict."""


class EvmContractValidator:
    """Looks for bytecode (optionally ERC-20 bytecode) on every configured network at once."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        networks: Sequence[EvmNetwork],
        *,
        timeout: float,
        require_erc20: bool,
    ) -> None:
        self._clients = [(net.name, JsonRpcClient(http, net.rpc_url, timeout)) for net in networks]
        self._require_erc20 = require_erc20
        self._failing: set[str] = set()

    @property
    def network_names(self) -> list[str]:
        return [name for name, _ in self._clients]

    async def find_network(self, address: str) -> str | None:
        """Name of the first network holding a contract at ``address``.

        Returns ``None`` only when every network answered and none had code;
        raises :class:`RpcUnavailable` when at least one endpoint failed, because
        a rejection based on partial information would be cached for hours.
        """
        results = await asyncio.gather(
            *(self._check(name, client, address) for name, client in self._clients),
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

    async def _check(self, name: str, client: JsonRpcClient, address: str) -> bool:
        code = await client.call("eth_getCode", [address, "latest"])
        if not isinstance(code, str) or code.lower() in _EMPTY_CODE:
            return False
        if not self._require_erc20:
            return True
        return await self._answers_erc20(client, address)

    @staticmethod
    async def _answers_erc20(client: JsonRpcClient, address: str) -> bool:
        """True when the contract returns data for at least one standard ERC-20 call."""
        calls = (
            client.call("eth_call", [{"to": address, "data": selector}, "latest"])
            for selector in ERC20_SELECTORS
        )
        answers = await asyncio.gather(*calls, return_exceptions=True)
        return any(isinstance(a, str) and a.lower() not in _EMPTY_CODE for a in answers)

    def _note_failure(self, name: str, error: RpcError) -> None:
        if name not in self._failing:
            self._failing.add(name)
            log.warning("RPC endpoint %s is failing: %s", name, error)

    def _note_recovery(self, name: str) -> None:
        if name in self._failing:
            self._failing.discard(name)
            log.info("RPC endpoint %s recovered", name)


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
        return self._is_mint(account.get("owner"), self._decode_data(account.get("data")))

    @staticmethod
    def _decode_data(data: Any) -> bytes:
        if isinstance(data, list) and data and isinstance(data[0], str):
            try:
                return base64.b64decode(data[0])
            except (ValueError, binascii.Error):
                return b""
        return b""

    @staticmethod
    def _is_mint(owner: Any, data: bytes) -> bool:
        if owner == SOLANA_TOKEN_PROGRAM:
            return len(data) == _MINT_SIZE
        if owner == SOLANA_TOKEN_2022_PROGRAM:
            # A Token-2022 mint is either the bare 82 bytes or 82 bytes + padding + an
            # account-type byte (1 = Mint) at offset 165, followed by extensions.
            return len(data) == _MINT_SIZE or (
                len(data) > _TOKEN_ACCOUNT_SIZE and data[_TOKEN_ACCOUNT_SIZE] == 1
            )
        return False


class ContractValidator:
    """Validates addresses once, remembers verdicts and merges concurrent checks."""

    def __init__(
        self,
        evm: EvmContractValidator,
        solana: SolanaContractValidator,
        cache: AddressCache,
        *,
        max_concurrent: int,
        retry_delay: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._evm = evm
        self._solana = solana
        self._cache = cache
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._retry_delay = retry_delay
        self._clock = clock
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

        With ``negative_cache=False`` (turbo mode) earlier rejections and RPC
        failures are ignored and a new rejection is not remembered, so an address
        published before its contract is deployed can be re-checked freely.
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
        return None
