import logging
import re

import httpx
import pytest

from scraper_bot.telegram.client import SentMessage, TelegramClient


def make_client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramClient("123:secret", http), http


def form(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))


async def test_send_message_splits_long_text_at_newlines() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bot123:secret/sendMessage"
        seen.append(form(request))
        return httpx.Response(200, json={"ok": True, "result": {}})

    client, http = make_client(handler)
    text = "line\n" * 2000
    async with http:
        assert await client.send_message(7, text)
    assert len(seen) == 3
    assert all(len(chunk["text"]) <= TelegramClient.MAX_MESSAGE_LENGTH for chunk in seen)
    assert all(chunk["chat_id"] == "7" for chunk in seen)
    assert "\n".join(chunk["text"] for chunk in seen) == text.strip()


async def test_rate_limit_is_retried_once() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    client, http = make_client(handler)
    async with http:
        assert await client.send_message(1, "hi") is True
    assert calls == 2


async def test_a_long_flood_wait_is_not_waited_out() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 600}})

    client, http = make_client(handler)
    async with http:
        assert await client.send_message(1, "hi") is False  # at once, not after 10 minutes
    assert calls == 1


async def test_api_and_transport_errors_return_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if b"boom" in request.content:
            raise httpx.ConnectError("no network", request=request)
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})

    client, http = make_client(handler)
    async with http:
        assert await client.send_message(1, "hello") is False
        assert await client.send_message(1, "boom") is False


async def test_send_document_uses_multipart_upload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendDocument")
        assert request.headers["content-type"].startswith("multipart/form-data")
        assert b'filename="c.txt"' in request.content
        assert b"0xabc" in request.content
        return httpx.Response(200, json={"ok": True, "result": {}})

    client, http = make_client(handler)
    async with http:
        assert await client.send_document(1, "c.txt", b"0xabc\n", "caption")


async def test_get_updates_returns_list_or_none() -> None:
    responses = [
        httpx.Response(200, json={"ok": True, "result": [{"update_id": 5}]}),
        httpx.Response(409, json={"ok": False}),
        httpx.Response(200, json={"ok": False, "description": "bad"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["timeout"] == "30"
        if not responses:
            raise httpx.ReadTimeout("slow", request=request)
        if request.url.params.get("offset") == "10":
            assert request.url.params["offset"] == "10"
        return responses.pop(0)

    client, http = make_client(handler)
    async with http:
        assert await client.get_updates(10, 30) == [{"update_id": 5}]
        assert await client.get_updates(None, 30) is None
        assert await client.get_updates(None, 30) is None
        assert await client.get_updates(None, 30) is None


async def test_get_me_reports_profile_or_rejects_token() -> None:
    from scraper_bot.telegram.client import TelegramAuthError

    responses = [
        httpx.Response(200, json={"ok": True, "result": {"id": 5, "username": "my_bot"}}),
        httpx.Response(401, json={"ok": False, "error_code": 401}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getMe")
        if not responses:
            raise httpx.ConnectError("offline", request=request)
        return responses.pop(0)

    client, http = make_client(handler)
    async with http:
        assert await client.get_me() == {"id": 5, "username": "my_bot"}
        try:
            await client.get_me()
        except TelegramAuthError:
            pass
        else:
            raise AssertionError("401 must raise TelegramAuthError")
        assert await client.get_me() is None


async def test_messages_use_html_and_fall_back_to_plain_text() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = form(request)
        seen.append(payload)
        if payload.get("parse_mode") == "HTML":
            return httpx.Response(
                400, json={"ok": False, "description": "Bad Request: can't parse entities"}
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    client, http = make_client(handler)
    async with http:
        assert await client.send_message(5, "<code>/add &lt;url&gt;</code> broken <b")
    assert [p.get("parse_mode") for p in seen] == ["HTML", None]
    assert seen[0]["link_preview_options"] == '{"is_disabled": true}'
    assert seen[1]["text"] == "/add <url> broken <b"


async def test_other_api_errors_do_not_trigger_the_fallback() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked"})

    client, http = make_client(handler)
    async with http:
        assert await client.send_message(5, "<code>x</code>") is False
    assert calls == 1


async def test_token_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("offset") == "1":
            return httpx.Response(500, text=f"gateway error for {request.url}")
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    client, http = make_client(handler)
    with caplog.at_level(logging.WARNING, logger="scraper_bot.telegram.client"):
        async with http:
            assert await client.send_message(1, "hi") is False
            assert await client.get_updates(None, 30) is None
            assert await client.get_updates(1, 30) is None
            assert await client.get_me() is None
    messages = [r.getMessage() for r in caplog.records if r.name == "scraper_bot.telegram.client"]
    assert len(messages) == 4
    assert all("<token>" in message for message in messages)
    assert all("secret" not in message for message in messages)
    assert messages[0].startswith("sendMessage failed: ConnectError: cannot reach")
    assert messages[2].startswith("getUpdates HTTP 500: gateway error for")
    assert messages[3].startswith("getMe failed: cannot reach")


async def test_long_text_without_newlines_is_hard_cut_at_4096() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(form(request)["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    client, http = make_client(handler)
    limit = TelegramClient.MAX_MESSAGE_LENGTH
    async with http:
        assert await client.send_message(1, "a" * (limit + 100))
        assert seen == ["a" * limit, "a" * 100]
        seen.clear()
        # A newline in the first half is too early to be a useful cut point.
        text = "head\n" + "b" * (limit + 5)
        assert await client.send_message(1, text)
    assert [len(chunk) for chunk in seen] == [limit, 10]
    assert "".join(seen) == text


async def test_get_updates_handles_http_errors_and_invalid_json() -> None:
    responses = [
        httpx.Response(502, text="<html>Bad Gateway</html>"),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"ok": True}),  # no result field
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client, http = make_client(handler)
    async with http:
        assert await client.get_updates(None, 30) is None
        assert await client.get_updates(None, 30) is None
        assert await client.get_updates(None, 30) == []


async def test_get_me_with_invalid_json_returns_none() -> None:
    responses = [
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json={"ok": True, "result": "not a dict"}),
        httpx.Response(200, json={"ok": False, "description": "nope"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client, http = make_client(handler)
    async with http:
        assert await client.get_me() is None
        assert await client.get_me() is None
        assert await client.get_me() is None


async def test_broadcast_and_broadcast_document_reach_every_chat() -> None:
    messages: list[str] = []
    documents: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            messages.append(form(request)["chat_id"])
        else:
            assert request.url.path.endswith("/sendDocument")
            documents.append(request.content)
        return httpx.Response(200, json={"ok": True, "result": {}})

    client, http = make_client(handler)
    async with http:
        await client.broadcast([1, 2, 3], "hello")
        await client.broadcast_document([4, 5], "c.txt", b"0xabc\n", "caption")
    assert sorted(messages) == ["1", "2", "3"]
    chat_ids = {re.search(rb'name="chat_id"\r\n\r\n(\d+)\r\n', body)[1] for body in documents}
    assert chat_ids == {b"4", b"5"}
    assert all(b'filename="c.txt"' in body and b"0xabc" in body for body in documents)


# ── tracked messages and edits ──────────────────────────────────────────────
async def test_rate_limited_sends_get_three_attempts() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})

    client, http = make_client(handler)
    async with http:
        assert await client.send_message(1, "hi") is False
    assert calls == 3


async def test_a_tracked_message_can_be_edited_in_its_own_markup() -> None:
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        seen.append((method, form(request)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    client, http = make_client(handler)
    async with http:
        sent = await client.send_tracked(7, "<b>hi</b>")
        assert sent is not None and (sent.chat_id, sent.message_id, sent.plain) == (7, 42, False)
        assert await client.edit_message_text(sent, "<b>hi</b> there")
    method, edit = seen[-1]
    assert method == "editMessageText"
    assert (edit["chat_id"], edit["message_id"], edit["parse_mode"]) == ("7", "42", "HTML")
    assert edit["text"] == "<b>hi</b> there"


async def test_a_message_sent_as_plain_text_is_edited_as_plain_text() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        data = form(request)
        seen.append(data)
        if request.url.path.endswith("/sendMessage") and "parse_mode" in data:
            return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    client, http = make_client(handler)
    async with http:
        sent = await client.send_tracked(1, "<code>broken")
        assert sent is not None and sent.plain
        assert await client.edit_message_text(sent, "<code>0xabc</code> + details")
    assert "parse_mode" not in seen[-1] and seen[-1]["text"] == "0xabc + details"


async def test_edits_are_never_retried_and_an_unchanged_text_is_fine() -> None:
    calls: list[str] = []
    answers = iter(
        [
            httpx.Response(400, json={"ok": False, "description": "message is not modified"}),
            httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return next(answers)

    client, http = make_client(handler)
    async with http:
        message = SentMessage(1, 9)
        assert await client.edit_message_text(message, "same") is True
        assert await client.edit_message_text(message, "new") is False
    assert len(calls) == 2  # the rate-limited edit was not repeated


async def test_a_message_too_long_to_edit_is_sent_but_not_tracked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client, http = make_client(handler)
    async with http:
        assert await client.send_tracked(1, "line\n" * 2000) is None
