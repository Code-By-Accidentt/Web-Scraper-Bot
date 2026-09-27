"""A small asynchronous Telegram Bot API client with only the calls the bot needs."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from scraper_bot.telegram.markup import strip_markup

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"
_NO_PREVIEW = '{"is_disabled": true}'
_SEND_ATTEMPTS = 3
_MAX_RETRY_AFTER = 10.0  # seconds; a message facing a longer flood wait is dropped
# Patient sends (token alerts, documents) wait longer: a lost token alert is never resent.
_PATIENT_ATTEMPTS = 8
_PATIENT_MAX_RETRY_AFTER = 60.0
_UNSENT_RETRY_PAUSE = 2.0  # seconds before resending a request that never reached Telegram
# A patient send doubles that pause up to this, so it rides out about two minutes offline.
_PATIENT_MAX_UNSENT_PAUSE = 30.0
# Transport errors that prove the request never left: sending it again cannot duplicate it.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)\b[^<>]*>")


class TelegramAuthError(Exception):
    """The Bot API rejected the token."""


@dataclass(frozen=True, slots=True)
class SentMessage:
    """One delivered message, with what is needed to edit it later."""

    chat_id: int
    message_id: int
    plain: bool = False  # Telegram rejected the HTML, so it went out plain and is edited plain


class TelegramClient:
    """Talks to the Bot API and never raises on delivery problems.

    Messages go out as HTML; long ones are split without breaking tags, and flood limits
    are waited out. If Telegram rejects the markup, the text goes out plain, so a
    formatting slip never loses an alert.
    """

    MAX_MESSAGE_LENGTH = 4096

    def __init__(
        self,
        token: str,
        http: httpx.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Call the API with ``token`` over ``http``; ``sleep`` can be swapped out in tests."""
        self._token = token
        self._base = f"{_API_BASE}/bot{token}"
        self._http = http
        self._sleep = sleep
        self._last_error: str | None = None
        self.username: str | None = None  # the bot's own @name; None until get_me() answers

    async def send_message(self, chat_id: int, text: str, *, patient: bool = False) -> bool:
        """Send ``text``, split into chunks if needed; return True when every chunk went out.

        A ``patient`` send retries more and waits out longer flood limits (background alerts).
        """
        ok = True
        for chunk in self._chunks(text):
            ok = await self._send_chunk(chat_id, chunk, patient=patient) is not None and ok
        return ok

    async def send_tracked(
        self, chat_id: int, text: str, *, patient: bool = False
    ) -> SentMessage | None:
        """Send ``text`` and return the message, so it can be edited later.

        Return None when sending failed, or when the text needed several chunks: it is
        still sent then, but cannot be edited.
        """
        chunks = self._chunks(text)
        if len(chunks) > 1:
            await self.send_message(chat_id, text, patient=patient)
            return None
        return await self._send_chunk(chat_id, chunks[0], patient=patient)

    async def edit_message_text(self, message: SentMessage, text: str) -> bool:
        """Replace the text of a sent message, in its markup; return True when done.

        There is no retry on a rate limit. If Telegram rejects the HTML, one plain edit
        follows; "message is not modified" (nothing changed) counts as done.
        """
        data: dict[str, Any] = {
            "chat_id": message.chat_id,
            "message_id": message.message_id,
            "text": strip_markup(text) if message.plain else text,
            "link_preview_options": _NO_PREVIEW,
        }
        if not message.plain:
            data["parse_mode"] = "HTML"
        if await self._post("editMessageText", data=data, attempts=1) is not None:
            return True
        error = (self._last_error or "").lower()
        if "not modified" in error:
            return True
        if not message.plain and "parse" in error:
            data = {**data, "text": strip_markup(text)}
            del data["parse_mode"]
            return await self._post("editMessageText", data=data, attempts=1) is not None
        return False

    async def broadcast(self, chat_ids: Iterable[int], text: str, *, patient: bool = False) -> bool:
        """Send ``text`` to every chat; return True when at least one of them received it."""
        sent = await asyncio.gather(
            *(self.send_message(cid, text, patient=patient) for cid in chat_ids)
        )
        return any(sent)

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        caption: str = "",
        *,
        patient: bool = False,
    ) -> bool:
        """Send ``content`` as a text file; return True when it went out.

        The caption is cut to Telegram's limit of 1024 characters.
        """
        result = await self._post(
            "sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"document": (filename, content, "text/plain")},
            **_policy(patient),
        )
        return result is not None

    async def broadcast_document(
        self,
        chat_ids: Iterable[int],
        filename: str,
        content: bytes,
        caption: str = "",
        *,
        patient: bool = False,
    ) -> None:
        """Send the same text file to every chat at once."""
        await asyncio.gather(
            *(
                self.send_document(cid, filename, content, caption, patient=patient)
                for cid in chat_ids
            )
        )

    async def get_me(self) -> dict[str, Any] | None:
        """Return the bot's profile and remember its username.

        Return None on a network problem or an odd answer; raise TelegramAuthError when
        Telegram rejects the token.
        """
        try:
            response = await self._http.get(f"{self._base}/getMe", timeout=15)
        except httpx.HTTPError as exc:
            log.warning("getMe failed: %s", self._redact(exc))
            return None
        if response.status_code in {401, 404}:
            raise TelegramAuthError("Telegram rejected TELEGRAM_BOT_TOKEN (getMe returned 401)")
        try:
            payload = response.json()
        except ValueError:
            return None
        result = payload.get("result") if payload.get("ok") else None
        if not isinstance(result, dict):
            return None
        username = result.get("username")
        if isinstance(username, str) and username:
            self.username = username
        return result

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]] | None:
        """Long-poll for new updates and return them.

        Return None (not an empty list) on any failure, so the caller can back off.
        """
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        try:
            response = await self._http.get(
                f"{self._base}/getUpdates", params=params, timeout=timeout + 10
            )
        except httpx.HTTPError as exc:
            log.warning("getUpdates failed: %s", self._redact(exc))
            return None
        if response.status_code == 409:
            log.warning("getUpdates conflict: another bot instance is polling with this token")
            return None
        if response.status_code != 200:
            log.error("getUpdates HTTP %s: %s", response.status_code, self._redact(response.text))
            return None
        try:
            payload = response.json()
        except ValueError:
            log.error("getUpdates returned invalid JSON")
            return None
        if not payload.get("ok"):
            log.error("getUpdates error: %s", payload.get("description"))
            return None
        return list(payload.get("result", []))

    async def _send_chunk(
        self, chat_id: int, chunk: str, *, patient: bool = False
    ) -> SentMessage | None:
        """Send one chunk and return the sent message, or None on failure.

        If Telegram rejects the HTML, the chunk is sent again as plain text.
        """
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "link_preview_options": _NO_PREVIEW,
        }
        result = await self._post("sendMessage", data=data, **_policy(patient))
        if result is not None:
            return SentMessage(chat_id, _message_id(result))
        if self._last_error and "parse" in self._last_error.lower():
            log.warning("message for chat %s had invalid HTML; resending as plain text", chat_id)
            data = {
                "chat_id": chat_id,
                "text": strip_markup(chunk),
                "link_preview_options": _NO_PREVIEW,
            }
            result = await self._post("sendMessage", data=data, **_policy(patient))
            if result is not None:
                return SentMessage(chat_id, _message_id(result), plain=True)
        return None

    async def _post(
        self,
        method: str,
        *,
        data: dict[str, Any],
        files: dict[str, Any] | None = None,
        attempts: int = _SEND_ATTEMPTS,
        max_wait: float = _MAX_RETRY_AFTER,
        pause: float = _UNSENT_RETRY_PAUSE,
        max_pause: float = _UNSENT_RETRY_PAUSE,
    ) -> dict[str, Any] | None:
        """Call one API method and return its result, or None on failure.

        A request that never reached Telegram is resent after ``pause`` seconds, doubling up
        to ``max_pause``, and a flood wait up to ``max_wait`` seconds is waited out; each
        retry uses up one of ``attempts``. The error text is left in ``_last_error``.
        """
        self._last_error = None
        again = {"data": data, "files": files, "attempts": attempts - 1, "max_wait": max_wait}
        try:
            response = await self._http.post(
                f"{self._base}/{method}", data=data, files=files, timeout=30
            )
        except httpx.HTTPError as exc:
            self._last_error = f"{type(exc).__name__}: {self._redact(exc)}"
            if isinstance(exc, _NOT_SENT) and attempts > 1:
                log.warning(
                    "%s did not reach Telegram (%s); retrying in %.0fs",
                    method,
                    self._last_error,
                    pause,
                )
                await self._sleep(pause)
                return await self._post(
                    method, **again, pause=min(pause * 2, max_pause), max_pause=max_pause
                )
            log.error("%s failed: %s", method, self._last_error)
            return None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code == 429 and attempts > 1:
            wait = _retry_after(payload)
            if wait <= max_wait:
                log.warning("%s rate limited, retrying in %.0fs", method, wait)
                await self._sleep(wait)
                return await self._post(method, **again, pause=pause, max_pause=max_pause)
            # A long flood wait would stall command replies and page checks behind it.
            log.error("%s rate limited for %.0fs; giving up on this message", method, wait)
        if response.status_code != 200 or not payload.get("ok"):
            self._last_error = (
                str(payload.get("description") or "") or f"HTTP {response.status_code}"
            )
            log.error(
                "%s to chat %s failed: HTTP %s %s",
                method,
                data.get("chat_id"),
                response.status_code,
                self._last_error,
            )
            return None
        return payload.get("result")

    def _chunks(self, text: str) -> list[str]:
        """Split ``text`` into messages Telegram accepts, cut at line breaks where possible.

        A cut inside an element (a long ``<pre>`` of a change report) closes it at the end
        of one chunk and opens it again in the next. So each chunk stays valid HTML and is
        not rejected and resent as plain text.
        """
        text = text.strip() or "(empty message)"
        limit = self.MAX_MESSAGE_LENGTH  # Telegram counts UTF-16 units: an emoji takes two
        chunks: list[str] = []
        reopen = ""  # the tags still open at the last cut
        while _units(reopen + text) > limit:
            room = limit - len(reopen)
            while True:
                cut = text.rfind("\n", 0, room)
                if cut < room // 2:
                    cut = _safe_cut(text, room)
                piece = reopen + text[:cut]
                still_open = _open_tags(piece)
                closing = "".join(f"</{name}>" for name, _ in reversed(still_open))
                excess = _units(piece + closing) - limit
                if excess <= 0 or room <= limit // 4:
                    break
                # Cut earlier until it fits; never below a quarter, so that the loop ends.
                room = max(min(room, cut) - excess, limit // 4)
            chunks.append(piece + closing)
            reopen = "".join(tag for _, tag in still_open)
            text = text[cut:].lstrip("\n")
        chunks.append(reopen + text)
        return chunks

    def _redact(self, value: object) -> str:
        """Return ``value`` as text with the bot token hidden, so it never leaks into logs."""
        return str(value).replace(self._token, "<token>")


def _open_tags(html: str) -> list[tuple[str, str]]:
    """Return (name, opening tag) for each element still open at the end of ``html``."""
    stack: list[tuple[str, str]] = []
    for match in _TAG_RE.finditer(html):
        name = match.group(2).lower()
        if not match.group(1):
            stack.append((name, match.group(0)))
            continue
        for index in range(len(stack) - 1, -1, -1):
            if stack[index][0] == name:
                del stack[index:]
                break
    return stack


def _safe_cut(text: str, at: int) -> int:
    """Return ``at``, moved back so the cut splits neither a tag nor an entity like ``&amp;``.

    A tag longer than the whole room is cut through; that chunk then goes out as plain text.
    """
    cut = at
    tag = text.rfind("<", 0, cut)
    if tag > text.rfind(">", 0, cut):
        cut = tag
    entity = text.rfind("&", 0, cut)
    if entity != -1 and cut - entity < 10 and ";" not in text[entity:cut]:
        cut = entity
    return cut if cut > 0 else at


def _units(text: str) -> int:
    """Return the length of ``text`` as Telegram counts it, in UTF-16 code units."""
    return len(text) if text.isascii() else len(text.encode("utf-16-le")) // 2


def _policy(patient: bool) -> dict[str, Any]:
    """Return the ``_post`` retry settings for a normal or a patient send."""
    if patient:
        return {
            "attempts": _PATIENT_ATTEMPTS,
            "max_wait": _PATIENT_MAX_RETRY_AFTER,
            "max_pause": _PATIENT_MAX_UNSENT_PAUSE,
        }
    return {"attempts": _SEND_ATTEMPTS, "max_wait": _MAX_RETRY_AFTER}


def _retry_after(payload: Any) -> float:
    """Return the flood wait Telegram asked for, in seconds; 3 when the answer lacks it."""
    parameters = payload.get("parameters") if isinstance(payload, dict) else None
    value = parameters.get("retry_after", 3) if isinstance(parameters, dict) else 3
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 3.0


def _message_id(result: Any) -> int:
    """Return the message id from a sendMessage result, or 0 when it is missing."""
    value = result.get("message_id") if isinstance(result, dict) else None
    return value if isinstance(value, int) else 0
