import asyncio
import logging
import re
from itertools import pairwise

import httpx
import pytest

from scraper_bot.telegram.client import SentMessage, TelegramClient


async def no_wait(_seconds: float) -> None:
    return None


def make_client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramClient("123:secret", http, sleep=no_wait), http


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


async def test_a_group_that_became_a_supergroup_is_followed_to_its_new_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(form(request)["chat_id"])
        if seen[-1] == "-1":  # what the Bot API answers once a group was upgraded
            description = "Bad Request: group chat was upgraded to a supergroup chat"
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": description,
                    "parameters": {"migrate_to_chat_id": -100123},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    client, http = make_client(handler)
    with caplog.at_level(logging.ERROR, logger="scraper_bot.telegram.client"):
        async with http:
            sent = await client.send_tracked(-1, "alert")
            assert sent is not None and sent.chat_id == -100123  # the details edit goes there
            assert await client.send_message(-1, "reply")
    assert seen == ["-1", "-100123", "-100123"]  # resent, then sent there straight away
    assert client.former_id(-100123) == -1  # so its commands count like the group's
    assert "Put -100123 in place of -1 in TELEGRAM_CHAT_IDS" in caplog.text


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
        assert client.username is None
        assert await client.get_me() == {"id": 5, "username": "my_bot"}
        assert client.username == "my_bot"  # commands for other bots are told apart by it
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
    assert len(messages) == 6  # the unsent message is tried three times
    assert all("<token>" in message for message in messages)
    assert all("secret" not in message for message in messages)
    assert messages[0].startswith("sendMessage did not reach Telegram (ConnectError: cannot")
    assert messages[2].startswith("sendMessage failed: ConnectError: cannot reach")
    assert messages[4].startswith("getUpdates HTTP 500: gateway error for")
    assert messages[5].startswith("getMe failed: cannot reach")


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
            assert request.headers["content-type"].startswith("multipart/form-data")
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


async def test_an_unchanged_edit_is_fine_and_a_rate_limited_one_waits_its_turn() -> None:
    calls: list[str] = []
    answers = iter(
        [
            httpx.Response(400, json={"ok": False, "description": "message is not modified"}),
            httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}}),
            httpx.Response(200, json={"ok": True, "result": {"message_id": 9}}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return next(answers)

    client, http = make_client(handler)
    async with http:
        message = SentMessage(1, 9)
        assert await client.edit_message_text(message, "same") is True
        assert await client.edit_message_text(message, "new") is True
    assert len(calls) == 3  # the details of an alert are not lost to a flood wait


async def test_a_quiet_chat_takes_a_short_burst_at_once_then_one_a_second() -> None:
    now = 0.0
    seen: list[tuple[float, str]] = []

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds
        await asyncio.sleep(0)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((now, request.url.path.rsplit("/", 1)[-1]))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramClient("123:secret", http, sleep=sleep, clock=lambda: now)
        message = await client.send_tracked(1, "alert 0")
        assert message is not None
        assert await client.edit_message_text(message, "alert 0 + details")
        more = [client.send_tracked(1, f"alert {i}") for i in range(1, 6)]
        assert all(await asyncio.gather(*more))
    # An alert, its details and three more alerts go at once; then one a second.
    assert seen == [
        (0.0, "sendMessage"),
        (0.0, "editMessageText"),
        (0.0, "sendMessage"),
        (0.0, "sendMessage"),
        (0.0, "sendMessage"),
        (1.0, "sendMessage"),
        (2.0, "sendMessage"),
    ]


async def test_a_message_too_long_to_edit_is_sent_but_not_tracked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client, http = make_client(handler)
    async with http:
        assert await client.send_tracked(1, "line\n" * 2000) is None


async def test_a_patient_send_waits_out_a_long_flood_limit() -> None:
    answers = [
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 35}}),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 7}}),
    ]
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        return answers.pop(0) if len(answers) > 1 else answers[0]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramClient("123:secret", http, sleep=record)
        sent = await client.send_tracked(1, "alert")
    assert sent is not None and sent.message_id == 7 and waits == [35.0]

    answers[:] = [httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 35}})]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramClient("123:secret", http, sleep=record)
        assert await client.send_message(1, "reply") is False  # a reply does not wait 35 s


async def test_a_burst_waits_its_turn_and_a_flood_wait_pauses_the_whole_chat() -> None:
    now = 0.0
    seen: list[tuple[float, str]] = []

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds
        await asyncio.sleep(0)

    def handler(request: httpx.Request) -> httpx.Response:
        if len(seen) == 2 and now < 30:  # Telegram asks the chat to wait 30 s, once
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 30}})
        seen.append((now, form(request)["text"]))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(seen)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramClient("123:secret", http, sleep=sleep, clock=lambda: now)
        alerts = [asyncio.create_task(client.send_tracked(1, f"alert {i}")) for i in range(10)]
        while len(seen) < 4:
            await asyncio.sleep(0)
        assert await client.send_message(1, "reply")  # a command reply skips the backlog
        assert all(await asyncio.gather(*alerts))  # none is dropped for waiting so long
    assert [text for _, text in seen] == [
        *(f"alert {i}" for i in range(4)),
        "reply",
        *(f"alert {i}" for i in range(4, 10)),
    ]
    times = [at for at, _ in seen]
    assert times[:2] == [0.0, 0.0]  # a quiet chat takes a short burst at once
    # After the flood wait no burst: one a second.
    assert times[2] >= 30 and all(later - at >= 1 for at, later in pairwise(times[2:]))


async def test_a_patient_send_rides_out_a_telegram_5xx() -> None:
    answers = [
        httpx.Response(502, text="<html>Bad Gateway</html>"),
        httpx.Response(500, json={"ok": False, "description": "Internal Server Error"}),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}),
    ]
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        return answers.pop(0) if len(answers) > 1 else answers[0]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TelegramClient("123:secret", http, sleep=sleep)
        assert await client.send_tracked(1, "alert") is not None
        assert pauses == [2, 4]
        answers[:] = [httpx.Response(502, text="Bad Gateway"), answers[0]]
        assert not await client.send_message(1, "reply")  # a reply is not resent after a 5xx


async def test_only_requests_that_never_left_are_sent_again() -> None:
    calls: list[str] = []

    def refused(request: httpx.Request) -> httpx.Response:
        calls.append("refused")
        if len(calls) < 3:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client, http = make_client(refused)
    async with http:
        assert await client.send_message(1, "hi") is True
    assert calls == ["refused"] * 3

    reads: list[str] = []

    def lost_answer(request: httpx.Request) -> httpx.Response:
        reads.append("sent")
        raise httpx.ReadTimeout("no answer", request=request)  # it may have been delivered

    client, http = make_client(lost_answer)
    async with http:
        assert await client.send_tracked(1, "hi") is None
    assert reads == ["sent"]  # never resent: that could duplicate the alert


async def test_a_cut_inside_an_element_closes_and_reopens_it() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(form(request)["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    client, http = make_client(handler)
    lines = "\n".join(f"+ <b>item {i}</b> &amp; more text to fill the line" for i in range(150))
    text = f"‼️ Changes\n\nAdded:\n<pre>{lines}</pre>\n\n➡️ <a href='https://x.io'>x.io</a>"
    async with http:
        assert await client.send_message(1, text)
    assert len(seen) == 2
    assert all(len(chunk) <= TelegramClient.MAX_MESSAGE_LENGTH for chunk in seen)
    assert seen[0].endswith("</pre>") and seen[1].startswith("<pre>")
    for chunk in seen:  # every chunk is balanced markup
        assert chunk.count("<pre>") == chunk.count("</pre>") == 1
        assert chunk.count("<b>") == chunk.count("</b>")
    assert seen[0][: -len("</pre>")] + "\n" + seen[1][len("<pre>") :] == text


def test_a_hard_cut_splits_neither_a_tag_nor_an_entity() -> None:
    from scraper_bot.telegram.client import _safe_cut

    text = "x" * 20 + "&amp;" + "y" * 20 + "<b>bold</b>"
    assert _safe_cut(text, 22) == 20  # inside &amp;
    assert _safe_cut(text, 47) == 45  # inside <b>
    assert _safe_cut(text, 30) == 30


def test_a_cut_moves_up_when_the_closing_tags_would_not_fit() -> None:
    client = TelegramClient("123:secret", httpx.AsyncClient())
    limit = TelegramClient.MAX_MESSAGE_LENGTH
    text = "<pre>" + "x" * 1000 + "\n" + "y" * 3086 + "\n" + "z" * 3000 + "</pre>"
    # The line break at 4092 leaves no room for </pre>: the cut moves up until it fits.
    first, second = client._chunks(text)
    assert first == text[: limit - len("</pre>")] + "</pre>"
    assert second == "<pre>" + text[limit - len("</pre>") :]


def test_a_tag_longer_than_a_message_is_cut_through_instead_of_looping() -> None:
    client = TelegramClient("123:secret", httpx.AsyncClient())
    text = "<a href='https://x.io/" + "p" * 9000 + "'>link</a>"
    chunks = client._chunks(text)  # plain text then, but it ends
    assert "".join(chunks) == text and all(len(c) <= 4096 for c in chunks)


def test_emoji_count_twice_towards_the_limit() -> None:
    client = TelegramClient("123:secret", httpx.AsyncClient())
    text = "<pre>" + "\n".join("🚀 launch " + str(i) for i in range(600)) + "</pre>"
    chunks = client._chunks(text)
    assert all(len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks)
    assert all(c.startswith("<pre>") and c.endswith("</pre>") for c in chunks)


async def test_a_patient_send_waits_longer_and_longer_while_offline() -> None:
    pauses: list[float] = []
    calls = 0

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 8:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TelegramClient("123:secret", http, sleep=sleep)
    async with http:
        assert await client.send_tracked(1, "🔔 token") is not None
    assert pauses == [2, 4, 8, 16, 30, 30, 30]  # about two minutes offline
    pauses.clear()
    calls = 3  # three more failures: all a reply gets
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http2:
        quick = TelegramClient("123:secret", http2, sleep=sleep)
        assert not await quick.send_message(1, "reply")  # a reply: three tries, 2 s apart
    assert pauses == [2, 2]


def test_a_wall_of_emoji_without_line_breaks_is_split_and_ends() -> None:
    client = TelegramClient("123:secret", httpx.AsyncClient())
    chunks = client._chunks("🚀" * 5000)
    assert all(len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks)
    assert "".join(chunks) == "🚀" * 5000
