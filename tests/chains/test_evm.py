from scraper_bot.chains.evm import (
    decode_abi_string,
    decode_abi_uint,
    has_code,
    is_delegated_wallet,
)


# ── encoders for fixtures ───────────────────────────────────────────────────
def abi_uint(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


def abi_string(value: str) -> str:
    raw = value.encode()
    padded = raw + b"\x00" * (-len(raw) % 32)
    head = (32).to_bytes(32, "big") + len(raw).to_bytes(32, "big")
    return "0x" + (head + padded).hex()


def abi_bytes32(value: str) -> str:
    return "0x" + value.encode().ljust(32, b"\x00").hex()


def test_abi_decoders() -> None:
    assert decode_abi_string(abi_string("USD Coin")) == "USD Coin"
    assert decode_abi_string(abi_bytes32("MKR")) == "MKR"
    assert decode_abi_string(abi_string("  padded\x00 name  ")) == "padded name"
    assert decode_abi_string("0x") is None
    assert decode_abi_string("0x" + "ff" * 64) is None  # offset beyond the payload
    assert decode_abi_string(RuntimeError("reverted")) is None
    assert decode_abi_string("0xzz") is None
    assert decode_abi_uint(abi_uint(6)) == 6
    assert decode_abi_uint(abi_uint(300), limit=36) is None
    assert decode_abi_uint("0x") is None and decode_abi_uint("nope") is None


def test_abi_decoder_edge_cases() -> None:
    head = (32).to_bytes(32, "big") + (100).to_bytes(32, "big")  # offset 32, length 100
    assert decode_abi_string("0x" + "00" * 40) is None  # neither bytes32 nor a full head
    assert decode_abi_string("0x" + (head + b"short").hex()) is None  # chunk shorter than said
    assert decode_abi_string("0x" + "00" * 64) is None  # a well-formed empty string
    assert decode_abi_string("0x" + "00" * 32) is None  # a blank bytes32
    assert decode_abi_string(abi_string("x" * 200)) == "x" * 64  # long names are cut
    assert decode_abi_uint("0xzz") is None
    assert decode_abi_uint(None) is None and decode_abi_uint(b"0x1") is None
    assert decode_abi_uint("0x0") == 0
    assert decode_abi_uint(abi_uint(36), limit=36) == 36
    assert decode_abi_uint(abi_uint(37), limit=36) is None


def test_every_spelling_of_no_bytecode() -> None:
    for empty in ("", "0x", "0X", "0x0", None, 0, RuntimeError("down")):
        assert not has_code(empty), empty
    delegation = "0xef0100" + "5a" * 20
    assert has_code("0x6080604052") and has_code(delegation)
    assert is_delegated_wallet(delegation) and not is_delegated_wallet("0x6080604052")
