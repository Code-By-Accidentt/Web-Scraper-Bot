"""A small asynchronous Telegram Bot API client with only the calls the bot needs."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from scraper_bot.telegram.markup import strip_markup

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"
_NO_PREVIEW = '{"is_disabled": true}'
_SEND_ATTEMPTS = 3
_MAX_RETRY_AFTER = 10.0  # a command reply facing a flood wait over this many seconds is dropped
_UNSENT_RETRY_PAUSE = 2.0  # pause before we resend a request that didn't get through (s)
_PATIENT_MAX_UNSENT_PAUSE = 30.0  # a patient send doubles that pause up to this
_GIVE_UP_AFTER = 600.0  # after this many seconds of waiting it's a long outage, so we give up
_CHAT_INTERVAL = 1.0  # gap between requests to one chat once its burst is used up (s)
_CHAT_BURST = 5  # Telegram allows short bursts, so a quiet chat may get this many at once
_GLOBAL_LIMIT = 30  # requests per second over all chats
# These errors mean the request never left, so sending it again can't make a duplicate.
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


@dataclass(frozen=True, slots=True)
class _Policy:
    """Where a request waits in its chat's queue and how it copes with failures."""

    lane: int  # served in order: 0 command replies, 1 token alerts and documents, 2 the rest
    attempts: int  # tries if it doesn't get through, 0 means keep going until _GIVE_UP_AFTER
    patient: bool  # a 5xx answer is retried too, and the pause doubles up to 30 s
    quick: bool = False  # a flood wait costs a try too, and one over 10 s drops the request


_REPLY = _Policy(lane=0, attempts=_SEND_ATTEMPTS, patient=False, quick=True)
_ALERT = _Policy(lane=1, attempts=0, patient=True)
_DETAILS = _Policy(lane=1, attempts=_SEND_ATTEMPTS, patient=False)
_NOTICE = _Policy(lane=2, attempts=_SEND_ATTEMPTS, patient=False)
_PATIENT_NOTICE = _Policy(lane=2, attempts=0, patient=True)


@dataclass(frozen=True, slots=True)
class _Answer:
    """How a request ended: Telegram's result, or the error that ended it."""

    result: Any = None
    error: str | None = None
    plain: bool = False  # it went out as plain text after Telegram rejected the HTML

    @property
    def ok(self) -> bool:
        """True when Telegram accepted the request."""
        return self.error is None


@dataclass(eq=False, slots=True)
class _Request:
    """One Bot API call waiting in a chat's queue, with what its failures cost so far."""

    method: str
    data: dict[str, Any]
    files: dict[str, Any] | None
    policy: _Policy
    future: asyncio.Future[_Answer]
    plain: dict[str, Any] | None = None  # sent instead once Telegram rejects the HTML
    as_plain: bool = False
    failures: int = 0
    waited: float = 0.0  # seconds of flood waits and retry pauses so far
    pause: float = _UNSENT_RETRY_PAUSE  # the pause before the next resend


class _ChatQueue:
    """The requests waiting for one chat, lane by lane, and when the chat may be written to."""

    def __init__(self) -> None:
        """Start with no requests and nothing to wait for."""
        self.lanes: tuple[deque[_Request], ...] = (deque(), deque(), deque())
        self.budget = float(_CHAT_BURST)  # requests the chat may still get at once
        self.counted_at = 0.0  # clock time the budget was last brought up to date
        self.hold = 0.0  # a flood wait or retry pause still to sit out before the next request
        self.held_until = 0.0  # clock time when Telegram's latest flood wait ends
        self.worker: asyncio.Task[None] | None = None

    def wait(self, now: float) -> float:
        """Return how long the chat must wait before its next request; 0 when it may go now.

        A chat that was quiet takes ``_CHAT_BURST`` requests at once, then one every
        ``_CHAT_INTERVAL`` seconds, and none before Telegram's latest flood wait ends.
        """
        self._refill(now)
        slot = 0.0 if self.budget >= 1 else (1 - self.budget) * _CHAT_INTERVAL
        return max(slot, self.held_until - now)

    def spend(self, now: float) -> None:
        """Count a request that just reached Telegram."""
        self._refill(now)
        self.budget = max(self.budget - 1, 0.0)

    def flooded(self, until: float) -> None:
        """Hold the chat until ``until``; after that it takes one request a second, no burst."""
        self.held_until = until
        self.budget, self.counted_at = 1.0, until

    def _refill(self, now: float) -> None:
        """Give back one request for every ``_CHAT_INTERVAL`` seconds since the last count."""
        elapsed = max(now - self.counted_at, 0.0)
        self.budget = min(self.budget + elapsed / _CHAT_INTERVAL, float(_CHAT_BURST))
        self.counted_at = max(now, self.counted_at)

    def head(self) -> _Request | None:
        """Return the request that goes next, dropping those whose callers stopped waiting."""
        for lane in self.lanes:
            while lane and lane[0].future.done():
                lane.popleft()
            if lane:
                return lane[0]
        return None

    def finish(self, request: _Request, answer: _Answer) -> None:
        """Take a request out of its lane and hand its answer to the caller."""
        lane = self.lanes[request.policy.lane]
        if request in lane:
            lane.remove(request)
        if not request.future.done():
            request.future.set_result(answer)


class TelegramClient:
    """Talks to the Bot API and never raises on delivery problems.

    Every request to a chat waits its turn in that chat's queue: command replies first,
    then token alerts and documents, then everything else. A chat that was quiet takes a
    short burst at once, then about one request a second; a flood limit pauses the whole
    chat, not one message. Messages go out as HTML; long ones are
    split without breaking tags, and if Telegram rejects the markup, the text goes out
    plain, so a formatting slip never loses an alert.
    """

    MAX_MESSAGE_LENGTH = 4096

    def __init__(
        self,
        token: str,
        http: httpx.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Call the API with ``token`` over ``http``; ``sleep`` and ``clock`` are test seams."""
        self._token = token
        self._base = f"{_API_BASE}/bot{token}"
        self._http = http
        self._sleep = sleep
        self._clock = clock
        self._queues: dict[int, _ChatQueue] = {}
        self._recent: deque[float] = deque(maxlen=_GLOBAL_LIMIT)  # when the latest requests left
        self.username: str | None = None  # the bot's own @name, None until get_me() answers

    async def send_message(self, chat_id: int, text: str, *, patient: bool = False) -> bool:
        """Send ``text``, split into chunks if needed; return True when every chunk went out.

        It goes ahead of the alerts waiting for the chat, as a command reply should: three
        tries, and a flood wait over 10 s drops it. A ``patient`` send queues like a token
        alert instead and rides out flood waits and outages.
        """
        answers = await self._send_text(chat_id, text, _ALERT if patient else _REPLY)
        return all(answer.ok for answer in answers)

    async def send_tracked(
        self, chat_id: int, text: str, *, patient: bool = False
    ) -> SentMessage | None:
        """Send ``text`` and return the message, so it can be edited later.

        A ``patient`` send is a token alert: only command replies go ahead of it, and it rides
        out flood waits and outages. Return None when sending failed, or when the text needed
        several chunks: it is still sent then, but cannot be edited.
        """
        answers = await self._send_text(chat_id, text, _ALERT if patient else _NOTICE)
        if len(answers) != 1 or not answers[0].ok:
            return None
        return SentMessage(chat_id, _message_id(answers[0].result), plain=answers[0].plain)

    async def edit_message_text(self, message: SentMessage, text: str) -> bool:
        """Replace the text of a sent message, in its markup; return True when done.

        This is how a token alert gets its details, so only the token alerts queued before it
        go first, and it waits out flood limits. If Telegram rejects the HTML, a plain edit
        follows; "message is not modified" counts as done.
        """
        data: dict[str, Any] = {
            "chat_id": message.chat_id,
            "message_id": message.message_id,
            "text": strip_markup(text) if message.plain else text,
            "link_preview_options": _NO_PREVIEW,
        }
        plain = None
        if not message.plain:
            plain = {**data, "text": strip_markup(text)}
            data["parse_mode"] = "HTML"
        answer = await self._submit(message.chat_id, "editMessageText", data, _DETAILS, plain=plain)
        return answer.ok or "not modified" in (answer.error or "").lower()

    async def broadcast(
        self,
        chat_ids: Iterable[int],
        text: str,
        *,
        patient: bool = False,
        wait: float | None = None,
    ) -> bool:
        """Send ``text`` to every chat; return True when at least one of them received it.

        It queues behind the token alerts and waits out flood limits; a ``patient`` one
        rides out outages too. With ``wait`` the caller waits that many seconds at most: the
        text stays queued, and only the chats it reached by then count.
        """
        policy = _PATIENT_NOTICE if patient else _NOTICE
        queued = [self._queue_text(chat_id, text, policy) for chat_id in chat_ids]
        futures = [future for chunks in queued for future in chunks]
        if futures:
            await asyncio.wait(futures, timeout=wait)
        return any(all(_accepted(future) for future in chunks) for chunks in queued)

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

        A ``patient`` one queues like a token alert. The caption is cut to Telegram's limit
        of 1024 characters.
        """
        answer = await self._queue_document(chat_id, filename, content, caption, patient)
        return answer.ok

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
                self._queue_document(chat_id, filename, content, caption, patient)
                for chat_id in chat_ids
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

    async def _send_text(self, chat_id: int, text: str, policy: _Policy) -> list[_Answer]:
        """Queue ``text`` for a chat, chunk by chunk, and return the answer of each chunk."""
        return list(await asyncio.gather(*self._queue_text(chat_id, text, policy)))

    def _queue_text(
        self, chat_id: int, text: str, policy: _Policy
    ) -> list[asyncio.Future[_Answer]]:
        """Queue the chunks of ``text`` for a chat, in order; return the futures of their answers.

        A chunk whose HTML Telegram rejects is sent again as plain text.
        """
        futures = []
        for chunk in self._chunks(text):
            data = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "link_preview_options": _NO_PREVIEW,
            }
            plain = {
                "chat_id": chat_id,
                "text": strip_markup(chunk),
                "link_preview_options": _NO_PREVIEW,
            }
            futures.append(self._submit(chat_id, "sendMessage", data, policy, plain=plain))
        return futures

    def _queue_document(
        self, chat_id: int, filename: str, content: bytes, caption: str, patient: bool
    ) -> asyncio.Future[_Answer]:
        """Queue a text file for a chat and return the future of its answer."""
        return self._submit(
            chat_id,
            "sendDocument",
            {"chat_id": chat_id, "caption": caption[:1024]},
            _ALERT if patient else _NOTICE,
            files={"document": (filename, content, "text/plain")},
        )

    def _submit(
        self,
        chat_id: int,
        method: str,
        data: dict[str, Any],
        policy: _Policy,
        *,
        files: dict[str, Any] | None = None,
        plain: dict[str, Any] | None = None,
    ) -> asyncio.Future[_Answer]:
        """Queue one request for a chat and return the future of its answer.

        A command reply is dropped at once while the chat sits out a long flood wait.
        """
        future: asyncio.Future[_Answer] = asyncio.get_running_loop().create_future()
        queue = self._queues.setdefault(chat_id, _ChatQueue())
        held = queue.held_until - self._clock()
        if policy.quick and held > _MAX_RETRY_AFTER:
            log.error("%s rate limited for %.0fs; giving up on this message", method, held)
            future.set_result(_Answer(error=f"rate limited for {held:.0f}s"))
            return future
        queue.lanes[policy.lane].append(_Request(method, data, files, policy, future, plain))
        if queue.worker is None:
            queue.worker = asyncio.create_task(
                self._serve(chat_id, queue), name=f"telegram-chat-{chat_id}"
            )
        return future

    async def _serve(self, chat_id: int, queue: _ChatQueue) -> None:
        """Send the requests queued for one chat, one at a time, until none is left.

        A request to be tried again stays at the head of its lane while the chat sits out
        its pause; a command reply that comes in meanwhile goes first. Once the HTTP client
        is closed (the bot stops), what is left is dropped.
        """
        try:
            while queue.head() is not None and not self._http.is_closed:
                await self._turn(queue)
                request = queue.head()
                if request is None or self._http.is_closed:
                    break
                try:
                    answer = await self._attempt(chat_id, queue, request)
                except Exception as exc:
                    error = self._redact(f"{type(exc).__name__}: {exc}")
                    log.error("%s to chat %s failed: %s", request.method, chat_id, error)
                    answer = _Answer(error=error)
                if answer is not None:
                    queue.finish(request, answer)
        finally:
            queue.worker = None
            queue.hold = 0.0
            for lane in queue.lanes:  # leftovers mean we were cancelled or the HTTP client closed
                while lane:
                    lane.popleft().future.cancel()

    async def _turn(self, queue: _ChatQueue) -> None:
        """Wait until the chat may be written to again, within the limit over all chats."""
        if queue.hold:
            hold, queue.hold = queue.hold, 0.0
            await self._sleep(hold)
        else:
            wait = queue.wait(self._clock())
            if wait > 0:
                await self._sleep(wait)
        if len(self._recent) == _GLOBAL_LIMIT:
            wait = self._recent[0] + 1.0 - self._clock()
            if wait > 0:
                await self._sleep(wait)
        self._recent.append(self._clock())

    async def _attempt(self, chat_id: int, queue: _ChatQueue, request: _Request) -> _Answer | None:
        """Send a request once; return how it ended, or None when it waits for another try.

        A request that never reached Telegram is resent after a pause, and so is a patient
        one that got a 5xx answer. One that may have arrived (its answer timed out) is never
        resent, so a message cannot come twice.
        """
        method, policy = request.method, request.policy
        try:
            response = await self._http.post(
                f"{self._base}/{method}", data=request.data, files=request.files, timeout=30
            )
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {self._redact(exc)}"
            if isinstance(exc, _NOT_SENT) and self._backoff(queue, request):
                log.warning(
                    "%s did not reach Telegram (%s); retrying in %.0fs", method, error, queue.hold
                )
                return None
            log.error("%s failed: %s", method, error)
            return _Answer(error=error)
        queue.spend(self._clock())
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        status = response.status_code
        if status == 200 and payload.get("ok"):
            return _Answer(result=payload.get("result"), plain=request.as_plain)
        error = str(payload.get("description") or "") or f"HTTP {status}"
        if status == 429:
            return self._flood(queue, request, _retry_after(payload), error)
        if status >= 500 and policy.patient and self._backoff(queue, request):
            log.warning(
                "%s to chat %s failed: HTTP %s %s; retrying in %.0fs",
                method,
                chat_id,
                status,
                error,
                queue.hold,
            )
            return None
        if request.plain is not None and "parse" in error.lower():
            log.warning("message for chat %s had invalid HTML; resending as plain text", chat_id)
            request.data, request.plain, request.as_plain = request.plain, None, True
            return None
        log.error("%s to chat %s failed: HTTP %s %s", method, chat_id, status, error)
        return _Answer(error=error)

    def _flood(
        self, queue: _ChatQueue, request: _Request, wait: float, error: str
    ) -> _Answer | None:
        """Pause the whole chat for a flood wait; return None when the request waits it out.

        A command reply counts each flood wait as an attempt and gives up on one over 10 s;
        everything else waits as long as Telegram asks, within the outage limit.
        """
        queue.hold = wait
        queue.flooded(self._clock() + wait)
        policy = request.policy
        if policy.quick:
            request.failures += 1
            gives_up = wait > _MAX_RETRY_AFTER or request.failures >= policy.attempts
        else:
            gives_up = request.waited + wait > _GIVE_UP_AFTER
        if gives_up:
            log.error("%s rate limited for %.0fs; giving up on this message", request.method, wait)
            return _Answer(error=error)
        request.waited += max(wait, _CHAT_INTERVAL)
        log.warning("%s rate limited, retrying in %.0fs", request.method, wait)
        return None

    def _backoff(self, queue: _ChatQueue, request: _Request) -> bool:
        """Pause the chat before a resend and return True, or return False to give up.

        The pause doubles for a patient request, up to 30 s. A request gives up after its
        attempts, or after about ten minutes of pauses (a long outage).
        """
        policy = request.policy
        request.failures += 1
        if policy.attempts and request.failures >= policy.attempts:
            return False
        pause = request.pause
        if request.waited + pause > _GIVE_UP_AFTER:
            return False
        request.waited += pause
        if policy.patient:
            request.pause = min(pause * 2, _PATIENT_MAX_UNSENT_PAUSE)
        queue.hold = pause
        return True

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
                # Cut earlier until it fits, but never below a quarter, so the loop always ends.
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


def _accepted(future: asyncio.Future[_Answer]) -> bool:
    """Return True when a queued request has ended and Telegram accepted it."""
    return future.done() and not future.cancelled() and future.result().ok


def _retry_after(payload: Any) -> float:
    """Return the flood wait Telegram asked for, in seconds; 3 when the answer lacks it."""
    parameters = payload.get("parameters") if isinstance(payload, dict) else None
    value = parameters.get("retry_after", 3) if isinstance(parameters, dict) else 3
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 3.0
    return max(seconds, 0.0) if math.isfinite(seconds) else 3.0


def _message_id(result: Any) -> int:
    """Return the message id from a sendMessage result, or 0 when it is missing."""
    value = result.get("message_id") if isinstance(result, dict) else None
    return value if isinstance(value, int) else 0
