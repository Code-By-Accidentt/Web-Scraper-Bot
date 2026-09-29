"""Token alerts and documents on their way to Telegram, kept in ``outbox.json`` until sent."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scraper_bot.storage.files import atomic_write_text

log = logging.getLogger(__name__)


OUTBOX_VERSION = 1


@dataclass(slots=True)
class OutboxEntry:
    """One alert or document, with the chats that have not had it yet."""

    id: str
    kind: str  # "alert" or "document"
    chats: list[int]
    payload: dict[str, Any] = field(default_factory=dict)  # what it takes to send it again


class Outbox:
    """Keeps what is on its way to Telegram in one JSON file, so a restart can send it again.

    The file is read on first use and rewritten in one atomic step at every change.
    """

    def __init__(self, path: Path) -> None:
        """Use the outbox file at ``path``; nothing is read until the first use."""
        self._path = path
        self._entries: dict[str, OutboxEntry] = {}
        self._left_over: list[OutboxEntry] | None = None  # None until the file was read
        self._frozen = False  # a damaged file couldn't be moved aside, so never overwrite it
        # Set by the first read of a damaged file: (what was wrong, where it was moved or None).
        self.broken: tuple[str, Path | None] | None = None

    def left_over(self) -> list[OutboxEntry]:
        """Return what an earlier run left unsent; every later call returns an empty list."""
        self._load()
        left, self._left_over = self._left_over or [], []
        return [entry for entry in left if entry.id in self._entries]

    def add(self, entry: OutboxEntry) -> None:
        """Keep an entry until every chat got it.

        It is written to disk before this returns; OSError means it is kept in memory only.
        """
        self._load()
        self._entries[entry.id] = entry
        self._save()

    def delivered(self, entry: OutboxEntry, chat_id: int) -> None:
        """Strike a chat off an entry; the entry leaves once no chat is left.

        Raise OSError when the change cannot be written.
        """
        if chat_id in entry.chats:
            entry.chats.remove(chat_id)
        if entry.id not in self._entries:
            return
        if not entry.chats:
            del self._entries[entry.id]
        self._save()

    def _load(self) -> None:
        """Read the file once; a damaged one is moved aside and ``broken`` says why."""
        if self._left_over is not None:
            return
        self._left_over = []
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8-sig"))
            raw = data.get("entries") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                raise ValueError("'entries' is not a list")
        except (OSError, ValueError, RecursionError) as exc:
            self._set_aside(f"unreadable: {exc}")
            return
        bad = 0
        for item in raw:
            entry = _entry_from_dict(item)
            if entry is None:
                bad += 1
                continue
            self._entries[entry.id] = entry
            self._left_over.append(entry)
        if bad:
            self._set_aside(f"{bad} damaged entr{'y' if bad == 1 else 'ies'} left out")
            try:
                self._save()  # save the readable ones, so a crash now doesn't lose them
            except OSError as exc:
                log.error("outbox %s: could not save the readable entries: %s", self._path, exc)

    def _set_aside(self, problem: str) -> None:
        """Move the damaged file aside for a person to look at; the next save starts a new one.

        If it can be neither moved nor copied, it is never overwritten.
        """
        target: Path | None = self._path.with_name(
            f"{self._path.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        try:
            os.replace(self._path, target)  # type: ignore[arg-type]
        except OSError:
            try:
                shutil.copyfile(self._path, target)  # type: ignore[arg-type]
            except OSError:
                target = None
                self._frozen = True
        self.broken = (problem, target)
        log.error("outbox %s: %s; the damaged file was moved to %s", self._path, problem, target)

    def _save(self) -> None:
        """Write every entry to the file, replacing it in one atomic step."""
        if self._frozen:
            raise OSError(f"{self._path.name} is damaged and could not be moved aside")
        entries = [
            {"id": entry.id, "kind": entry.kind, "chats": entry.chats, "payload": entry.payload}
            for entry in self._entries.values()
        ]
        data = {"version": OUTBOX_VERSION, "entries": entries}
        atomic_write_text(self._path, json.dumps(data, ensure_ascii=False))


def _entry_from_dict(item: Any) -> OutboxEntry | None:
    """Return the entry a saved record describes, or None when the record is damaged."""
    if not isinstance(item, dict):
        return None
    entry_id, kind, chats, payload = (item.get(k) for k in ("id", "kind", "chats", "payload"))
    if not (isinstance(entry_id, str) and entry_id and isinstance(kind, str)):
        return None
    if not isinstance(chats, list) or not isinstance(payload, dict):
        return None
    if not all(isinstance(chat, int) and not isinstance(chat, bool) for chat in chats):
        return None
    return OutboxEntry(entry_id, kind, list(chats), payload)
