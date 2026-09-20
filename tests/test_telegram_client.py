import httpx

from scraper_bot.telegram_client import TelegramClient


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
    from scraper_bot.telegram_client import TelegramAuthError

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
