"""Safe file writes."""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so that readers never see a half-written file.

    The data reaches the disk before the rename, so a power cut leaves either the old file
    or the new one, never an empty one. Each write goes through a temporary file of its
    own, so two writes of one file at once can't mix, and a failed one leaves none behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    handle = tmp.open("x", encoding="utf-8")
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
