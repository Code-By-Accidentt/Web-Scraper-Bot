"""Minimal asynchronous Telegram Bot API client (only the calls the bot needs)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

import httpx

from scraper_bot.markup import strip_markup

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"
_NO_PREVIEW = '{"is_disabled": true}'


class TelegramAuthError(Exception):
    """The Bot API rejected the token."""


class TelegramClient:
    """Thin wrapper over the Bot API that never raises on delivery problems.

    Messages are sent with HTML formatting (see :mod:`scraper_bot.markup`). If
    Telegram rejects the markup of a message, the same text is re-sent as plain
    text, so a formatting slip never swallows an alert.
    """

    MAX_MESSAGE_LENGTH = 4096

    def __init__(self, token: str, http: httpx.AsyncClient) -> None:
        self._token = token
        self._base = f"{_API_BASE}/bot{token}"
        self._http = http
        self._last_error: str | None = None

    # ── outgoing ────────────────────────────────────────────────────────────
    async def send_message(self, chat_id: int, text: str) -> bool:
        """Send ``text`` (split into API-sized chunks); True when every chunk went out."""
        ok = True
        for chunk in self._chunks(text):
            ok = await self._send_chunk(chat_id, chunk) and ok
        return ok

    async def broadcast(self, chat_ids: Iterable[int], text: str) -> None:
        await asyncio.gather(*(self.send_message(cid, text) for cid in chat_ids))

    async def send_document(
        self, chat_id: int, filename: str, content: bytes, caption: str = ""
    ) -> bool:
        result = await self._post(
            "sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"document": (filename, content, "text/plain")},
        )
        return result is not None

    async def broadcast_document(
        self, chat_ids: Iterable[int], filename: str, content: bytes, caption: str = ""
    ) -> None:
        await asyncio.gather(
            *(self.send_document(cid, filename, content, caption) for cid in chat_ids)
        )

    # ── incoming ────────────────────────────────────────────────────────────
    async def get_me(self) -> dict[str, Any] | None:
        """Bot profile, ``None`` on a network problem; raises on a rejected token."""
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
        return result if isinstance(result, dict) else None

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]] | None:
        """Long-poll for updates.

        Returns the (possibly empty) list of updates, or ``None`` when the request
        failed so that the caller can back off instead of polling in a tight loop.
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

    # ── internals ───────────────────────────────────────────────────────────
    async def _send_chunk(self, chat_id: int, chunk: str) -> bool:
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "link_preview_options": _NO_PREVIEW,
        }
        if await self._post("sendMessage", data=data) is not None:
            return True
        if self._last_error and "parse" in self._last_error.lower():
            log.warning("message for chat %s had invalid HTML; resending as plain text", chat_id)
            data = {
                "chat_id": chat_id,
                "text": strip_markup(chunk),
                "link_preview_options": _NO_PREVIEW,
            }
            return await self._post("sendMessage", data=data) is not None
        return False

    async def _post(
        self,
        method: str,
        *,
        data: dict[str, Any],
        files: dict[str, Any] | None = None,
        retry: bool = True,
    ) -> dict[str, Any] | None:
        self._last_error = None
        try:
            response = await self._http.post(
                f"{self._base}/{method}", data=data, files=files, timeout=30
            )
        except httpx.HTTPError as exc:
            self._last_error = f"{type(exc).__name__}: {self._redact(exc)}"
            log.error("%s failed: %s", method, self._last_error)
            return None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code == 429 and retry:
            wait = float(payload.get("parameters", {}).get("retry_after", 3))
            log.warning("%s rate limited, retrying in %.0fs", method, wait)
            await asyncio.sleep(wait)
            return await self._post(method, data=data, files=files, retry=False)
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
        text = text.strip() or "(empty message)"
        limit = self.MAX_MESSAGE_LENGTH
        chunks: list[str] = []
        while len(text) > limit:
            cut = text.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = limit
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        chunks.append(text)
        return chunks

    def _redact(self, value: object) -> str:
        """Make sure the bot token never leaks into logs through error messages."""
        return str(value).replace(self._token, "<token>")
