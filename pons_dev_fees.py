#!/usr/bin/env python3
"""Creator ("dev") fee forensics for Pons launches on Robinhood Chain.

Answers two questions about a token launched through pons.family
(https://ponsfamily.com), the dominant launchpad on Robinhood Chain
(EVM L2, chain id 4663):

  1. How much has the token's creator actually earned in fees?
  2. Where is that money now — still pending on the curve/hook, sitting
     unclaimed in the fee escrow, or claimed and moved on?

Everything is read straight from chain state and logs; the Blockscout API is
only used for wallet history (and can be turned off with --no-explorer).

Fee model implemented here mirrors the verified sources at
https://github.com/ponsdotdev/ponsfamily:

  V2 (PonsV2LaunchFactory 0x7eD598...EC7e)
    - Pre-graduation: every trade on PonsV2BondingCurve charges a quote-leg
      fee plus an optional creator tax. `_sweepFees` splits the fee into
      protocol / buyback / creator and credits the creator leg (tax included
      in full) to PonsV2FeeEscrow -> event FeesSwept(protocol, buyback, creator).
    - Post-graduation: PonsV2MemeHook takes an afterSwap cut on the graduated
      Uniswap V4 pool and distributes it the same way
      -> event PoolFeesSwept(poolId, protocol, buyback, creator, tokensLocked).
    - Both credit PonsV2FeeEscrow, from which the creator calls claim().

  V1 (PonsLaunchFactory 0xA5aAb3...1feB)
    - Creator revenue is the Uniswap V3 trading fee on the locked position
      NFT, routed by the locker to the launch's fee wallet. Realised amounts
      are read from NonfungiblePositionManager `Collect` events for the
      launch's positionId; still-uncollected fees are simulated with an
      eth_call to `collect`.

Usage:
    python pons_dev_fees.py 0x8d1612b4B78eBf08CFBf01A04Fa270CCbB0509A2
    python pons_dev_fees.py <token> --rpc https://rpc.mainnet.chain.robinhood.com
    python pons_dev_fees.py <token> --json report.json --no-explorer

Requires network access to the RPC endpoint (and to the explorer unless
--no-explorer is passed).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from typing import Any, Iterable, Sequence

import requests
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

# --------------------------------------------------------------------------
# Robinhood Chain / Pons constants
# --------------------------------------------------------------------------

DEFAULT_RPC = "https://rpc.mainnet.chain.robinhood.com"
DEFAULT_EXPLORER = "https://robinhoodchain.blockscout.com"
EXPECTED_CHAIN_ID = 4663

PONS_V1_FACTORY = "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB"
PONS_V2_FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"

ZERO = "0x0000000000000000000000000000000000000000"
BPS = 10_000

GRADUATION_PHASES = ("NotGraduated", "Swept", "PoolCreated", "Rescued")


def topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


# Bonding curve (one instance per V2 launch)
T_FEES_SWEPT = topic("FeesSwept(uint256,uint256,uint256)")
T_FEES_RESCUED = topic("FeesRescued(address,address,uint256,uint256)")
T_CURVE_BUY = topic("CurveBuy(address,address,uint256,uint256,uint256,uint256)")
T_CURVE_SELL = topic("CurveSell(address,address,uint256,uint256,uint256,uint256)")

# PonsV2MemeHook (singleton, all graduated pools)
T_POOL_FEES_SWEPT = topic("PoolFeesSwept(bytes32,uint256,uint256,uint256,uint256)")
T_POOL_FEES_RESCUED = topic("PoolFeesRescued(bytes32,address,uint256,uint256)")
T_HOOK_FEE_COLLECTED = topic("HookFeeCollected(bytes32,address,uint256,uint256)")

# PonsV2LaunchFactory
T_V2_TOKEN_LAUNCHED = topic("TokenLaunched(address,address,address,address,uint256,uint256)")
T_V2_CREATOR_RECIPIENT = topic("CreatorFeeRecipientUpdated(address,address,address)")
T_V2_POOL_GRADUATED = topic("PoolGraduated(address,uint256,uint256,uint256)")

# Uniswap V3 NonfungiblePositionManager (V1 launches)
T_V3_COLLECT = topic("Collect(uint256,address,uint256,uint256)")

V2_LAUNCH_RECORD_TYPES = [
    "address",  # token
    "address",  # curve
    "address",  # deployer
    "address",  # creatorFeeRecipient
    "address",  # pairToken
    "uint256",  # graduationThreshold
    "uint24",   # poolFee
    "int24",    # tickSpacing
    "uint16",   # creatorTaxBps
    "bool",     # buybackEnabled
    "uint8",    # phase
    "uint256",  # sweptQuote
    "uint256",  # sweptTokens
    "uint256",  # sweptAt
    "bool",     # exists
]
V2_LAUNCH_RECORD_FIELDS = [
    "token", "curve", "deployer", "creator_fee_recipient", "pair_token",
    "graduation_threshold", "pool_fee", "tick_spacing", "creator_tax_bps",
    "buyback_enabled", "phase", "swept_quote", "swept_tokens", "swept_at", "exists",
]

V1_LAUNCH_RECORD_TYPES = [
    "address",  # token
    "address",  # deployer
    "address",  # pairedToken
    "address",  # positionManager
    "uint256",  # positionId
    "uint256",  # dexId
    "uint256",  # launchConfigId
    "uint256",  # restrictionsEndBlock
    "uint256",  # supply
    "bool",     # isToken0
    "uint24",   # poolFee
    "bool",     # exists
    "uint256",  # initialBuyAmount
]
V1_LAUNCH_RECORD_FIELDS = [
    "token", "deployer", "paired_token", "position_manager", "position_id",
    "dex_id", "launch_config_id", "restrictions_end_block", "supply",
    "is_token0", "pool_fee", "exists", "initial_buy_amount",
]

# PonsV2MemeHook.launches(PoolId) -> LaunchInfo (flattened public getter)
HOOK_LAUNCH_TYPES = [
    "bool", "bool", "address", "address", "address", "address", "address",
    "uint16", "uint16", "uint16", "uint16", "uint16", "bool",
]
HOOK_LAUNCH_FIELDS = [
    "registered", "memecoin_is_currency0", "memecoin", "quote_token", "creator",
    "buyback_creator_recipient", "protocol_fee_recipient", "creator_tax_bps",
    "protocol_fee_share_bps", "buyback_burn_bps", "hook_fee_bps",
    "max_internal_price_impact_bps", "buyback_enabled",
]


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------


class RpcError(RuntimeError):
    pass


class Rpc:
    """Minimal JSON-RPC client with retries and adaptive log-range splitting."""

    def __init__(self, url: str, timeout: int = 30, retries: int = 4, verbose: bool = False):
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose
        self._id = 0
        self._session = requests.Session()
        self._block_ts: dict[int, int] = {}

    def call(self, method: str, params: Sequence[Any]) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": list(params)}
        delay = 2.0
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self._session.post(self.url, json=payload, timeout=self.timeout)
                resp.raise_for_status()
                body = resp.json()
                if "error" in body:
                    raise RpcError(f"{method}: {body['error']}")
                return body["result"]
            except RpcError:
                raise
            except Exception as exc:  # network / decode problems are retryable
                last = exc
                if attempt == self.retries - 1:
                    break
                if self.verbose:
                    print(f"  ! {method} failed ({exc}); retrying in {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                delay *= 2
        raise RpcError(f"{method} failed after {self.retries} attempts: {last}")

    # -- primitives --------------------------------------------------------

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def get_code(self, address: str, block: int | str = "latest") -> str:
        tag = block if isinstance(block, str) else hex(block)
        return self.call("eth_getCode", [address, tag])

    def get_balance(self, address: str, block: int | str = "latest") -> int:
        tag = block if isinstance(block, str) else hex(block)
        return int(self.call("eth_getBalance", [address, tag]), 16)

    def eth_call(self, to: str, data: str, block: int | str = "latest", frm: str | None = None) -> str:
        tx: dict[str, str] = {"to": to, "data": data}
        if frm:
            tx["from"] = frm
        tag = block if isinstance(block, str) else hex(block)
        return self.call("eth_call", [tx, tag])

    def block_timestamp(self, number: int) -> int | None:
        if number in self._block_ts:
            return self._block_ts[number]
        block = self.call("eth_getBlockByNumber", [hex(number), False])
        if not block:
            return None
        ts = int(block["timestamp"], 16)
        self._block_ts[number] = ts
        return ts

    # -- logs --------------------------------------------------------------

    def get_logs(
        self,
        address: str | list[str] | None,
        topics: list[Any] | None,
        from_block: int,
        to_block: int,
        chunk: int = 50_000,
    ) -> list[dict]:
        """eth_getLogs over a range, halving the window when a node rejects it."""
        out: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(start + chunk - 1, to_block)
            flt: dict[str, Any] = {"fromBlock": hex(start), "toBlock": hex(end)}
            if address:
                flt["address"] = address
            if topics:
                flt["topics"] = topics
            try:
                logs = self.call("eth_getLogs", [flt])
            except RpcError as exc:
                if chunk > 1_000:
                    chunk = max(1_000, chunk // 4)
                    if self.verbose:
                        print(f"  . narrowing log window to {chunk} blocks ({exc})", file=sys.stderr)
                    continue
                raise
            out.extend(logs)
            if self.verbose:
                print(f"  . scanned {start}-{end} ({len(out)} logs so far)", file=sys.stderr)
            start = end + 1
        return out


# --------------------------------------------------------------------------
# ABI helpers
# --------------------------------------------------------------------------


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def encode_call(signature: str, arg_types: Sequence[str] = (), args: Sequence[Any] = ()) -> str:
    data = selector(signature)
    if arg_types:
        data += abi_encode(list(arg_types), list(args))
    return "0x" + data.hex()


def try_call(
    rpc: Rpc,
    to: str,
    signature: str,
    out_types: Sequence[str],
    arg_types: Sequence[str] = (),
    args: Sequence[Any] = (),
    frm: str | None = None,
) -> tuple | None:
    """eth_call that returns None instead of raising when the call reverts."""
    try:
        raw = rpc.eth_call(to, encode_call(signature, arg_types, args), frm=frm)
    except RpcError:
        return None
    if not raw or raw in ("0x", "0x0"):
        return None
    try:
        return abi_decode(list(out_types), bytes.fromhex(raw[2:]))
    except Exception:
        return None


def named(values: Sequence[Any] | None, fields: Sequence[str]) -> dict | None:
    if values is None:
        return None
    record = dict(zip(fields, values))
    for key, value in record.items():
        if isinstance(value, str) and value.startswith("0x") and len(value) == 42:
            record[key] = to_checksum_address(value)
    return record


def log_topic_address(value: str) -> str:
    return to_checksum_address("0x" + value[-40:])


def decode_log_data(types: Sequence[str], data: str) -> tuple:
    return abi_decode(list(types), bytes.fromhex(data[2:]))


def erc20_meta(rpc: Rpc, address: str) -> dict:
    """Name/symbol/decimals for an ERC-20, or the native-currency stand-in."""
    if address == ZERO:
        return {"address": ZERO, "symbol": "ETH", "name": "Robinhood Chain ETH (native)", "decimals": 18}
    name = try_call(rpc, address, "name()", ["string"])
    symbol = try_call(rpc, address, "symbol()", ["string"])
    decimals = try_call(rpc, address, "decimals()", ["uint8"])
    supply = try_call(rpc, address, "totalSupply()", ["uint256"])
    return {
        "address": to_checksum_address(address),
        "name": name[0] if name else None,
        "symbol": symbol[0] if symbol else None,
        "decimals": int(decimals[0]) if decimals else 18,
        "total_supply": int(supply[0]) if supply else None,
    }


def fmt_amount(raw: int | None, decimals: int = 18, symbol: str = "ETH", places: int = 6) -> str:
    if raw is None:
        return "n/a"
    value = Decimal(raw) / (Decimal(10) ** decimals)
    quantum = Decimal(1).scaleb(-places)
    return f"{value.quantize(quantum):,} {symbol}".replace(",", " ")


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def fmt_time(ts: int | None) -> str:
    if not ts:
        return "unknown time"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


# --------------------------------------------------------------------------
# Explorer (Blockscout) — optional, used for wallet history only
# --------------------------------------------------------------------------


class Explorer:
    def __init__(self, base: str, timeout: int = 30, enabled: bool = True, verbose: bool = False):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.enabled = enabled
        self.verbose = verbose
        self._session = requests.Session()

    def get(self, path: str, params: dict | None = None) -> Any:
        if not self.enabled:
            return None
        url = f"{self.base}{path}"
        delay = 2.0
        for attempt in range(3):
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                if attempt == 2:
                    if self.verbose:
                        print(f"  ! explorer {path} failed: {exc}", file=sys.stderr)
                    return None
                time.sleep(delay)
                delay *= 2
        return None

    def paged(self, path: str, params: dict | None = None, max_pages: int = 10) -> list[dict]:
        items: list[dict] = []
        query = dict(params or {})
        for _ in range(max_pages):
            body = self.get(path, query)
            if not body or "items" not in body:
                break
            items.extend(body["items"])
            next_params = body.get("next_page_params")
            if not next_params:
                break
            query = {**(params or {}), **next_params}
        return items

    def creation_block(self, address: str) -> int | None:
        info = self.get(f"/api/v2/addresses/{address}")
        if not info:
            return None
        tx_hash = info.get("creation_transaction_hash") or info.get("creation_tx_hash")
        if not tx_hash:
            return None
        tx = self.get(f"/api/v2/transactions/{tx_hash}")
        if tx and tx.get("block_number") is not None:
            return int(tx["block_number"])
        return None


def find_creation_block(rpc: Rpc, address: str, latest: int, verbose: bool = False) -> int:
    """Binary search for the first block where the address has code.

    Needs an archive node; falls back to block 0 when historical state is
    unavailable, which only makes the log scan slower, never wrong.
    """
    try:
        if rpc.get_code(address, 0) not in ("0x", "0x0", ""):
            return 0
    except RpcError:
        return 0
    lo, hi = 0, latest
    while lo < hi:
        mid = (lo + hi) // 2
        try:
            code = rpc.get_code(address, mid)
        except RpcError:
            return 0
        if code in ("0x", "0x0", ""):
            lo = mid + 1
        else:
            hi = mid
    if verbose:
        print(f"  . {address} deployed at block {lo}", file=sys.stderr)
    return lo


# --------------------------------------------------------------------------
# V2 analysis (bonding curve + graduated Uniswap V4 pool)
# --------------------------------------------------------------------------


def pool_id_for(token: str, pair_token: str, pool_fee: int, tick_spacing: int, hook: str) -> str:
    """Uniswap V4 PoolId = keccak256(abi.encode(PoolKey)), currencies sorted."""
    a = int(token, 16)
    b = int(pair_token, 16)
    currency0, currency1 = (token, pair_token) if a < b else (pair_token, token)
    encoded = abi_encode(
        ["address", "address", "uint24", "int24", "address"],
        [currency0, currency1, pool_fee, tick_spacing, hook],
    )
    return "0x" + keccak(encoded).hex()


def creator_share(
    pending_fee: int, pending_tax: int, pending_buyback: int, protocol_share_bps: int
) -> tuple[int, int]:
    """Creator's slice of an unswept fee bucket, as (minimum, maximum).

    Mirrors PonsV2BondingCurve._sweepFees / PonsV2MemeHook._distribute: the
    protocol takes its share off the top, the buyback earmark is either spent
    (minimum case) or folded back into the creator payout when the venue is
    too thin (maximum case), and the creator tax is always paid in full.
    """
    protocol = (pending_fee * protocol_share_bps) // BPS
    bucket = pending_fee - protocol
    buyback = min(pending_buyback, bucket)
    return bucket - buyback + pending_tax, bucket + pending_tax


def analyze_v2(rpc: Rpc, explorer: Explorer, token: str, record: dict, args) -> dict:
    latest = rpc.block_number()
    curve = record["curve"]
    factory = PONS_V2_FACTORY

    fee_escrow = try_call(rpc, factory, "feeEscrow()", ["address"])
    meme_hook = try_call(rpc, factory, "memeHook()", ["address"])
    fee_escrow = to_checksum_address(fee_escrow[0]) if fee_escrow else None
    meme_hook = to_checksum_address(meme_hook[0]) if meme_hook else None

    quote = erc20_meta(rpc, record["pair_token"])
    qdec, qsym = quote["decimals"], quote["symbol"] or "?"

    result: dict[str, Any] = {
        "generation": "V2",
        "factory": factory,
        "token": erc20_meta(rpc, token),
        "launch": record,
        "quote_asset": quote,
        "fee_escrow": fee_escrow,
        "meme_hook": meme_hook,
        "phase": GRADUATION_PHASES[record["phase"]] if record["phase"] < len(GRADUATION_PHASES) else str(record["phase"]),
    }

    from_block = args.from_block
    if from_block is None:
        from_block = explorer.creation_block(curve)
    if from_block is None:
        from_block = find_creation_block(rpc, curve, latest, args.verbose)
    result["scan_from_block"] = from_block
    result["scan_to_block"] = latest

    # ---- realised creator fees on the bonding curve ----------------------
    curve_state = {
        "graduated": (try_call(rpc, curve, "graduated()", ["bool"]) or (None,))[0],
        "fee_bps": (try_call(rpc, curve, "feeBps()", ["uint256"]) or (None,))[0],
        "creator_tax_bps": (try_call(rpc, curve, "creatorTaxBps()", ["uint256"]) or (None,))[0],
        "protocol_fee_share_bps": (try_call(rpc, curve, "protocolFeeShareBps()", ["uint16"]) or (None,))[0],
        "quote_fee_balance": (try_call(rpc, curve, "quoteFeeBalance()", ["uint256"]) or (None,))[0],
        "creator_tax_balance": (try_call(rpc, curve, "creatorTaxBalance()", ["uint256"]) or (None,))[0],
        "buyback_quote_balance": (try_call(rpc, curve, "buybackQuoteBalance()", ["uint256"]) or (None,))[0],
        "real_quote_reserve": (try_call(rpc, curve, "realQuoteReserve()", ["uint256"]) or (None,))[0],
        "curve_creator_recipient": (try_call(rpc, curve, "deployer()", ["address"]) or (None,))[0],
    }
    if curve_state["curve_creator_recipient"]:
        curve_state["curve_creator_recipient"] = to_checksum_address(curve_state["curve_creator_recipient"])
    result["curve_state"] = curve_state

    swept_logs = rpc.get_logs(curve, [[T_FEES_SWEPT, T_FEES_RESCUED]], from_block, latest, args.chunk)
    curve_events: list[dict] = []
    curve_total = 0
    for log in swept_logs:
        block = int(log["blockNumber"], 16)
        if log["topics"][0] == T_FEES_SWEPT:
            protocol, buyback, creator = decode_log_data(["uint256", "uint256", "uint256"], log["data"])
            kind = "FeesSwept"
        else:
            protocol, creator = decode_log_data(["uint256", "uint256"], log["data"])
            buyback = 0
            kind = "FeesRescued"
        curve_total += creator
        curve_events.append({
            "kind": kind,
            "block": block,
            "timestamp": rpc.block_timestamp(block),
            "tx": log["transactionHash"],
            "protocol_amount": protocol,
            "buyback_amount": buyback,
            "creator_amount": creator,
        })
    result["curve_fee_events"] = curve_events
    result["curve_creator_total"] = curve_total

    # ---- realised creator fees on the graduated V4 pool -------------------
    pool_total = 0
    pool_events: list[dict] = []
    hook_state: dict[str, Any] = {}
    pool_id = None
    if meme_hook and record["phase"] >= 2:
        pool_id = pool_id_for(token, record["pair_token"], record["pool_fee"], record["tick_spacing"], meme_hook)
        result["pool_id"] = pool_id
        info = named(
            try_call(rpc, meme_hook, "launches(bytes32)", HOOK_LAUNCH_TYPES, ["bytes32"], [bytes.fromhex(pool_id[2:])]),
            HOOK_LAUNCH_FIELDS,
        )
        hook_state["launch_info"] = info
        if info and not info.get("registered"):
            result["warnings"] = result.get("warnings", []) + [
                "Reconstructed poolId is not registered on the meme hook; "
                "post-graduation numbers may be incomplete."
            ]
        hook_from = args.from_block
        if hook_from is None:
            hook_from = explorer.creation_block(meme_hook) or find_creation_block(rpc, meme_hook, latest, args.verbose)
        pool_logs = rpc.get_logs(
            meme_hook, [[T_POOL_FEES_SWEPT, T_POOL_FEES_RESCUED], pool_id], hook_from, latest, args.chunk
        )
        for log in pool_logs:
            block = int(log["blockNumber"], 16)
            if log["topics"][0] == T_POOL_FEES_SWEPT:
                protocol, buyback, creator, locked = decode_log_data(
                    ["uint256", "uint256", "uint256", "uint256"], log["data"]
                )
                kind = "PoolFeesSwept"
            else:
                _quote_token, protocol, creator = decode_log_data(["address", "uint256", "uint256"], log["data"])
                buyback, locked = 0, 0
                kind = "PoolFeesRescued"
            pool_total += creator
            pool_events.append({
                "kind": kind,
                "block": block,
                "timestamp": rpc.block_timestamp(block),
                "tx": log["transactionHash"],
                "protocol_amount": protocol,
                "buyback_amount": buyback,
                "creator_amount": creator,
                "tokens_locked": locked,
            })
        # unswept, still sitting on the hook
        for label, sig in (("pending_fees", "pendingFees(bytes32,address)"),
                           ("pending_creator_tax", "pendingCreatorTax(bytes32,address)"),
                           ("pending_buyback", "pendingBuyback(bytes32,address)")):
            for currency_label, currency in (("quote", record["pair_token"]), ("memecoin", token)):
                value = try_call(
                    rpc, meme_hook, sig, ["uint256"], ["bytes32", "address"],
                    [bytes.fromhex(pool_id[2:]), currency],
                )
                hook_state[f"{label}_{currency_label}"] = int(value[0]) if value else None
    result["pool_fee_events"] = pool_events
    result["pool_creator_total"] = pool_total
    result["hook_state"] = hook_state

    # ---- pending (accrued but not yet swept) -----------------------------
    pending: dict[str, Any] = {}
    if curve_state["quote_fee_balance"] is not None and not curve_state.get("graduated"):
        lo, hi = creator_share(
            int(curve_state["quote_fee_balance"]),
            int(curve_state["creator_tax_balance"] or 0),
            int(curve_state["buyback_quote_balance"] or 0),
            int(curve_state["protocol_fee_share_bps"] or 0),
        )
        pending["curve_creator_min"] = lo
        pending["curve_creator_max"] = hi
    info = hook_state.get("launch_info")
    if info and hook_state.get("pending_fees_quote") is not None:
        lo, hi = creator_share(
            int(hook_state.get("pending_fees_quote") or 0),
            int(hook_state.get("pending_creator_tax_quote") or 0),
            int(hook_state.get("pending_buyback_quote") or 0),
            int(info.get("protocol_fee_share_bps") or 0),
        )
        pending["pool_creator_min"] = lo
        pending["pool_creator_max"] = hi
        pending["pool_pending_in_memecoin"] = (
            int(hook_state.get("pending_fees_memecoin") or 0)
            + int(hook_state.get("pending_creator_tax_memecoin") or 0)
        )
    result["pending"] = pending

    # ---- where the money is now ------------------------------------------
    recipients: list[str] = []
    for candidate in (record["creator_fee_recipient"], record["deployer"], curve_state.get("curve_creator_recipient")):
        if candidate and candidate != ZERO and candidate not in recipients:
            recipients.append(candidate)
    if info and info.get("creator") and info["creator"] not in recipients:
        recipients.append(info["creator"])
    result["creator_addresses"] = recipients

    escrow: dict[str, Any] = {}
    if fee_escrow:
        for who in recipients:
            native = try_call(rpc, fee_escrow, "balanceOf(address)", ["uint256"], ["address"], [who])
            entry = {"unclaimed_native": int(native[0]) if native else None}
            if record["pair_token"] != ZERO:
                tok = try_call(
                    rpc, fee_escrow, "balanceOfToken(address,address)", ["uint256"],
                    ["address", "address"], [who, record["pair_token"]],
                )
                entry["unclaimed_quote_token"] = int(tok[0]) if tok else None
            escrow[who] = entry
    result["escrow_balances"] = escrow

    result["claims"] = trace_escrow_claims(rpc, explorer, fee_escrow, recipients, from_block, latest, args)
    result["recipient_wallets"] = {who: describe_wallet(rpc, explorer, who) for who in recipients}

    # ---- history of creator-fee-recipient changes -------------------------
    factory_from = args.from_block
    if factory_from is None:
        factory_from = from_block
    changes = []
    token_topic = "0x" + "0" * 24 + token[2:].lower()
    for log in rpc.get_logs(factory, [T_V2_CREATOR_RECIPIENT, token_topic], factory_from, latest, args.chunk):
        block = int(log["blockNumber"], 16)
        changes.append({
            "block": block,
            "timestamp": rpc.block_timestamp(block),
            "tx": log["transactionHash"],
            "previous": log_topic_address(log["topics"][2]),
            "new": log_topic_address(log["topics"][3]),
        })
    result["creator_recipient_changes"] = changes

    result["totals"] = {
        "realised_creator_fees": curve_total + pool_total,
        "pending_creator_fees_min": pending.get("curve_creator_min", 0) + pending.get("pool_creator_min", 0),
        "pending_creator_fees_max": pending.get("curve_creator_max", 0) + pending.get("pool_creator_max", 0),
        "unclaimed_in_escrow": sum(
            (entry.get("unclaimed_native") or 0) + (entry.get("unclaimed_quote_token") or 0)
            for entry in escrow.values()
        ),
        "claimed_out": sum(claim["amount"] for claim in result["claims"].get("events", [])),
        "quote_decimals": qdec,
        "quote_symbol": qsym,
    }
    return result


# --------------------------------------------------------------------------
# V1 analysis (locked Uniswap V3 position)
# --------------------------------------------------------------------------


def analyze_v1(rpc: Rpc, explorer: Explorer, token: str, record: dict, args) -> dict:
    latest = rpc.block_number()
    position_manager = record["position_manager"]
    position_id = int(record["position_id"])
    locker = try_call(rpc, PONS_V1_FACTORY, "locker()", ["address"])
    locker = to_checksum_address(locker[0]) if locker else None

    quote = erc20_meta(rpc, record["paired_token"])
    result: dict[str, Any] = {
        "generation": "V1",
        "factory": PONS_V1_FACTORY,
        "token": erc20_meta(rpc, token),
        "launch": record,
        "quote_asset": quote,
        "locker": locker,
        "position_id": position_id,
        "position_manager": position_manager,
    }

    from_block = args.from_block
    if from_block is None:
        from_block = explorer.creation_block(token)
    if from_block is None:
        from_block = find_creation_block(rpc, token, latest, args.verbose)
    result["scan_from_block"] = from_block
    result["scan_to_block"] = latest

    # Realised LP fees: Collect events for this position id.
    id_topic = "0x" + f"{position_id:064x}"
    collects = []
    total0 = total1 = 0
    for log in rpc.get_logs(position_manager, [T_V3_COLLECT, id_topic], from_block, latest, args.chunk):
        block = int(log["blockNumber"], 16)
        recipient, amount0, amount1 = decode_log_data(["address", "uint256", "uint256"], log["data"])
        total0 += amount0
        total1 += amount1
        collects.append({
            "block": block,
            "timestamp": rpc.block_timestamp(block),
            "tx": log["transactionHash"],
            "recipient": to_checksum_address(recipient),
            "amount0": amount0,
            "amount1": amount1,
        })
    result["collect_events"] = collects
    result["collected_totals"] = {"amount0": total0, "amount1": total1}

    # Uncollected fees: simulate collect() as the position owner.
    owner = try_call(rpc, position_manager, "ownerOf(uint256)", ["address"], ["uint256"], [position_id])
    owner = to_checksum_address(owner[0]) if owner else locker
    result["position_owner"] = owner
    max_u128 = (1 << 128) - 1
    simulated = None
    if owner:
        simulated = try_call(
            rpc, position_manager,
            "collect((uint256,address,uint128,uint128))", ["uint256", "uint256"],
            ["(uint256,address,uint128,uint128)"], [(position_id, owner, max_u128, max_u128)],
            frm=owner,
        )
    result["uncollected_fees"] = (
        {"amount0": int(simulated[0]), "amount1": int(simulated[1])} if simulated else None
    )

    recipients = [record["deployer"]]
    for collect in collects:
        if collect["recipient"] not in recipients:
            recipients.append(collect["recipient"])
    result["creator_addresses"] = recipients
    result["recipient_wallets"] = {who: describe_wallet(rpc, explorer, who) for who in recipients}
    result["totals"] = {
        "quote_decimals": quote["decimals"],
        "quote_symbol": quote["symbol"] or "?",
        "is_token0": record["is_token0"],
    }
    return result


# --------------------------------------------------------------------------
# Money-flow tracing
# --------------------------------------------------------------------------


def trace_escrow_claims(
    rpc: Rpc, explorer: Explorer, escrow: str | None, recipients: Iterable[str],
    from_block: int, latest: int, args,
) -> dict:
    """Native payouts made by the fee escrow to the creator.

    Preferred source is the explorer's internal-transaction index (a claim
    pays out with a plain value transfer, which leaves no log). Falls back to
    scanning escrow logs that carry the recipient in a topic.
    """
    out: dict[str, Any] = {"source": None, "events": []}
    if not escrow:
        return out
    recipients = list(recipients)

    if explorer.enabled:
        events = []
        for who in recipients:
            items = explorer.paged(f"/api/v2/addresses/{who}/internal-transactions", {"filter": "to"})
            for item in items:
                sender = (item.get("from") or {}).get("hash", "")
                value = int(item.get("value") or 0)
                if sender and sender.lower() == escrow.lower() and value:
                    events.append({
                        "recipient": who,
                        "amount": value,
                        "block": item.get("block_number"),
                        "timestamp": item.get("timestamp"),
                        "tx": item.get("transaction_hash") or item.get("tx_hash"),
                    })
        if events:
            out["source"] = "explorer:internal-transactions"
            out["events"] = sorted(events, key=lambda e: e.get("block") or 0)
            return out

    # Fallback: any escrow log mentioning the recipient in a topic, with the
    # first data word read as the amount. Heuristic - the escrow's ABI is not
    # published in the launchpad repository.
    events = []
    for who in recipients:
        who_topic = "0x" + "0" * 24 + who[2:].lower()
        for position in (1, 2):
            topics: list[Any] = [None] * (position + 1)
            topics[position] = who_topic
            try:
                logs = rpc.get_logs(escrow, topics, from_block, latest, args.chunk)
            except RpcError:
                continue
            for log in logs:
                data = log.get("data") or "0x"
                amount = int(data[2:66], 16) if len(data) >= 66 else 0
                block = int(log["blockNumber"], 16)
                events.append({
                    "recipient": who,
                    "amount": amount,
                    "block": block,
                    "timestamp": rpc.block_timestamp(block),
                    "tx": log["transactionHash"],
                    "topic0": log["topics"][0],
                })
    if events:
        out["source"] = "rpc:escrow-logs (heuristic amounts)"
        out["events"] = sorted(events, key=lambda e: e["block"])
    return out


def describe_wallet(rpc: Rpc, explorer: Explorer, address: str) -> dict:
    """Current balance plus a summary of where value left this wallet."""
    info: dict[str, Any] = {
        "address": address,
        "native_balance": rpc.get_balance(address),
        "is_contract": rpc.get_code(address) not in ("0x", "0x0", ""),
    }
    if not explorer.enabled:
        return info

    outflows: dict[str, int] = {}
    txs = explorer.paged(f"/api/v2/addresses/{address}/transactions", {"filter": "from"}, max_pages=5)
    for tx in txs:
        value = int(tx.get("value") or 0)
        to_hash = (tx.get("to") or {}).get("hash")
        if value and to_hash:
            outflows[to_checksum_address(to_hash)] = outflows.get(to_checksum_address(to_hash), 0) + value
    info["native_outflows"] = sorted(
        ({"to": k, "amount": v} for k, v in outflows.items()), key=lambda e: -e["amount"]
    )[:15]
    info["native_out_total"] = sum(outflows.values())
    info["tx_count_out"] = len(txs)

    transfers = explorer.paged(f"/api/v2/addresses/{address}/token-transfers", {"filter": "from"}, max_pages=3)
    token_out: dict[str, dict] = {}
    for tr in transfers:
        tok = tr.get("token") or {}
        key = tok.get("address") or "?"
        entry = token_out.setdefault(key, {"symbol": tok.get("symbol"), "decimals": int(tok.get("decimals") or 18), "amount": 0})
        entry["amount"] += int((tr.get("total") or {}).get("value") or 0)
    info["token_outflows"] = [{"token": k, **v} for k, v in token_out.items()][:15]

    balances = explorer.get(f"/api/v2/addresses/{address}/token-balances")
    if isinstance(balances, list):
        info["token_balances"] = [
            {
                "token": (b.get("token") or {}).get("address"),
                "symbol": (b.get("token") or {}).get("symbol"),
                "decimals": int((b.get("token") or {}).get("decimals") or 18),
                "value": int(b.get("value") or 0),
            }
            for b in balances[:25]
        ]
    return info


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def print_v2_report(data: dict) -> None:
    q = data["quote_asset"]
    dec, sym = q["decimals"], q["symbol"] or "?"
    tok = data["token"]
    launch = data["launch"]
    money = lambda raw: fmt_amount(raw, dec, sym)  # noqa: E731

    print("=" * 78)
    print(f"Token      : {tok['name']} ({tok['symbol']})  {tok['address']}")
    print(f"Launchpad  : Pons V2  (factory {data['factory']})")
    print(f"Curve      : {launch['curve']}")
    print(f"Phase      : {data['phase']}")
    print(f"Quote asset: {sym} ({q['address']})")
    print(f"Deployer   : {launch['deployer']}")
    print(f"Fee wallet : {launch['creator_fee_recipient']}")
    print(f"Creator tax: {launch['creator_tax_bps'] / 100:.2f}%   "
          f"curve fee: {(data['curve_state'].get('fee_bps') or 0) / 100:.2f}%   "
          f"protocol share of fee: {(data['curve_state'].get('protocol_fee_share_bps') or 0) / 100:.2f}%")
    print(f"Scanned    : blocks {data['scan_from_block']}-{data['scan_to_block']}")
    for warning in data.get("warnings", []):
        print(f"WARNING    : {warning}")

    print("-" * 78)
    print("EARNED (credited to the creator, per FeesSwept / PoolFeesSwept)")
    print(f"  bonding curve phase : {money(data['curve_creator_total'])}"
          f"   ({plural(len(data['curve_fee_events']), 'sweep')})")
    print(f"  graduated pool      : {money(data['pool_creator_total'])}"
          f"   ({plural(len(data['pool_fee_events']), 'sweep')})")
    print(f"  TOTAL REALISED      : {money(data['totals']['realised_creator_fees'])}")

    events = data["curve_fee_events"] + data["pool_fee_events"]
    events.sort(key=lambda e: e["block"])
    if events:
        print(f"  first sweep         : block {events[0]['block']} ({fmt_time(events[0].get('timestamp'))})")
        print(f"  last sweep          : block {events[-1]['block']} ({fmt_time(events[-1].get('timestamp'))})")

    pending = data.get("pending") or {}
    if pending:
        print("-" * 78)
        print("PENDING (accrued on the curve/hook, not yet swept)")
        if "curve_creator_min" in pending:
            print(f"  on the curve        : {money(pending['curve_creator_min'])}"
                  f" .. {money(pending['curve_creator_max'])}")
        if "pool_creator_min" in pending:
            print(f"  on the hook         : {money(pending['pool_creator_min'])}"
                  f" .. {money(pending['pool_creator_max'])}")
        if pending.get("pool_pending_in_memecoin"):
            print(f"  unconverted memecoin: {fmt_amount(pending['pool_pending_in_memecoin'], tok['decimals'], tok['symbol'] or '?')}")

    print("-" * 78)
    print(f"WHERE IT IS NOW   (fee escrow {data['fee_escrow']})")
    for who, entry in (data.get("escrow_balances") or {}).items():
        print(f"  {who}")
        print(f"    unclaimed in escrow: {money(entry.get('unclaimed_native'))}")
        if entry.get("unclaimed_quote_token") is not None:
            print(f"    unclaimed (ERC-20) : {money(entry.get('unclaimed_quote_token'))}")

    claims = data.get("claims") or {}
    if claims.get("events"):
        total_claimed = sum(c["amount"] for c in claims["events"])
        print(f"  claimed out        : {money(total_claimed)} in {plural(len(claims['events']), 'claim')}"
              f"   [source: {claims['source']}]")
        for claim in claims["events"][-10:]:
            print(f"    {fmt_time(_ts(claim.get('timestamp')))}  {money(claim['amount'])}"
                  f"  -> {claim['recipient']}  tx {claim.get('tx')}")
    else:
        print("  claimed out        : no claim payouts found")

    for who, wallet in (data.get("recipient_wallets") or {}).items():
        print(f"  wallet {who} ({'contract' if wallet['is_contract'] else 'EOA'})")
        print(f"    native balance now : {fmt_amount(wallet['native_balance'], 18, 'ETH')}")
        if wallet.get("native_out_total"):
            print(f"    native sent out    : {fmt_amount(wallet['native_out_total'], 18, 'ETH')}"
                  f" across {wallet.get('tx_count_out', 0)} txs")
        for flow in (wallet.get("native_outflows") or [])[:5]:
            print(f"      -> {flow['to']}  {fmt_amount(flow['amount'], 18, 'ETH')}")
        for bal in (wallet.get("token_balances") or [])[:5]:
            print(f"    holds {fmt_amount(bal['value'], bal['decimals'], bal['symbol'] or '?')}")

    changes = data.get("creator_recipient_changes") or []
    if changes:
        print("-" * 78)
        print("FEE WALLET CHANGES")
        for change in changes:
            print(f"  {fmt_time(change.get('timestamp'))}  {change['previous']} -> {change['new']}"
                  f"  tx {change['tx']}")
    print("=" * 78)


def print_v1_report(data: dict) -> None:
    tok = data["token"]
    q = data["quote_asset"]
    launch = data["launch"]
    is_token0 = launch["is_token0"]
    quote_dec, quote_sym = q["decimals"], q["symbol"] or "?"

    print("=" * 78)
    print(f"Token      : {tok['name']} ({tok['symbol']})  {tok['address']}")
    print(f"Launchpad  : Pons V1  (factory {data['factory']})")
    print(f"Deployer   : {launch['deployer']}")
    print(f"Position   : #{data['position_id']} on {data['position_manager']} (locker {data['locker']})")
    print(f"Paired     : {quote_sym} ({q['address']})   token is currency{'0' if is_token0 else '1'}")
    print(f"Scanned    : blocks {data['scan_from_block']}-{data['scan_to_block']}")
    print("-" * 78)
    print("EARNED (Uniswap V3 fees collected from the locked position)")
    totals = data["collected_totals"]
    quote_collected = totals["amount1"] if is_token0 else totals["amount0"]
    token_collected = totals["amount0"] if is_token0 else totals["amount1"]
    print(f"  collected in {quote_sym:<6}: {fmt_amount(quote_collected, quote_dec, quote_sym)}")
    print(f"  collected in {(tok['symbol'] or '?'):<6}: {fmt_amount(token_collected, tok['decimals'], tok['symbol'] or '?')}")
    print(f"  collect events     : {len(data['collect_events'])}")
    for collect in data["collect_events"][-10:]:
        print(f"    {fmt_time(collect.get('timestamp'))} -> {collect['recipient']}  tx {collect['tx']}")
    uncollected = data.get("uncollected_fees")
    if uncollected:
        unc_quote = uncollected["amount1"] if is_token0 else uncollected["amount0"]
        unc_token = uncollected["amount0"] if is_token0 else uncollected["amount1"]
        print("-" * 78)
        print("PENDING (still uncollected on the position)")
        print(f"  {fmt_amount(unc_quote, quote_dec, quote_sym)} + "
              f"{fmt_amount(unc_token, tok['decimals'], tok['symbol'] or '?')}")
    print("-" * 78)
    print("WHERE IT IS NOW")
    for who, wallet in (data.get("recipient_wallets") or {}).items():
        print(f"  wallet {who} ({'contract' if wallet['is_contract'] else 'EOA'})")
        print(f"    native balance now : {fmt_amount(wallet['native_balance'], 18, 'ETH')}")
        if wallet.get("native_out_total"):
            print(f"    native sent out    : {fmt_amount(wallet['native_out_total'], 18, 'ETH')}")
        for flow in (wallet.get("native_outflows") or [])[:5]:
            print(f"      -> {flow['to']}  {fmt_amount(flow['amount'], 18, 'ETH')}")
    print("=" * 78)


def _ts(value: Any) -> int | None:
    """Normalise explorer ISO timestamps and RPC integers to epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(time.mktime(time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pons creator-fee forensics on Robinhood Chain")
    parser.add_argument("token", help="Token contract address")
    parser.add_argument("--rpc", default=DEFAULT_RPC, help=f"JSON-RPC endpoint (default: {DEFAULT_RPC})")
    parser.add_argument("--explorer", default=DEFAULT_EXPLORER, help="Blockscout base URL")
    parser.add_argument("--no-explorer", action="store_true", help="RPC only, skip wallet history")
    parser.add_argument("--from-block", type=int, default=None, help="Force the first block to scan")
    parser.add_argument("--chunk", type=int, default=50_000, help="eth_getLogs window size")
    parser.add_argument("--json", dest="json_out", help="Write the full result as JSON to this file")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    token = to_checksum_address(args.token)
    rpc = Rpc(args.rpc, timeout=args.timeout, verbose=args.verbose)
    explorer = Explorer(args.explorer, timeout=args.timeout, enabled=not args.no_explorer, verbose=args.verbose)

    chain_id = rpc.chain_id()
    if chain_id != EXPECTED_CHAIN_ID:
        print(f"WARNING: connected to chain id {chain_id}, expected {EXPECTED_CHAIN_ID} (Robinhood Chain)",
              file=sys.stderr)

    if rpc.get_code(token) in ("0x", "0x0", ""):
        print(f"{token} has no contract code on chain {chain_id}", file=sys.stderr)
        return 2

    v2 = named(
        try_call(rpc, PONS_V2_FACTORY, "getLaunchedToken(address)", V2_LAUNCH_RECORD_TYPES, ["address"], [token]),
        V2_LAUNCH_RECORD_FIELDS,
    )
    if v2 and v2.get("exists"):
        data = analyze_v2(rpc, explorer, token, v2, args)
        print_v2_report(data)
    else:
        v1 = named(
            try_call(rpc, PONS_V1_FACTORY, "getLaunchedToken(address)", V1_LAUNCH_RECORD_TYPES, ["address"], [token]),
            V1_LAUNCH_RECORD_FIELDS,
        )
        if not (v1 and v1.get("exists")):
            print(f"{token} was not launched by either Pons factory.", file=sys.stderr)
            print("Nothing to report: this tool only understands Pons V1/V2 creator fees.", file=sys.stderr)
            return 3
        data = analyze_v1(rpc, explorer, token, v1, args)
        print_v1_report(data)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, default=str)
        print(f"\nFull result written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
