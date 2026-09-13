"""One writer per database: the guard that stops a second poller starting.

Two pollers on the same file would each notice the same change, each fetch the
same slice and each log its history, and every request to the portal would be
doubled. SQLite cannot prevent that, because every one of those writes is valid
on its own. A bulk ingestion running under a live poller is worse: it replaces
the corpus the poller is in the middle of updating.

The guard is an operating-system lock on a file beside the database, not a PID
file. The OS releases it the moment the holder ends, however it ends — Ctrl+C,
a closed window, a crash, Task Manager — so there is never a stale lock to
delete by hand. The file also carries a short note naming the holder, which is
what the refusal message and the website's "poller running" indicator read.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import DB_PATH

#: Windows locks a byte range, and nobody else can read a locked range. Locking
#: one byte far past the holder's note keeps the note readable. Locking beyond
#: the end of a file is allowed and does not grow it.
_LOCK_OFFSET = 1 << 20

#: Exit code a refused poller returns, so launch scripts can tell "another
#: poller already has this" apart from a real failure.
ALREADY_RUNNING = 3


def lock_path(db_path: Path | None = None) -> Path:
    """The lock sits beside the database it guards: one lock per file."""
    db_path = Path(db_path or DB_PATH)
    return db_path.with_name(db_path.name + ".poller.lock")


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class WriterLock:
    """Exclusive, non-blocking, released by the OS when the process ends."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else lock_path()
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, role: str = "poller", wait: float = 1.0) -> bool:
        """Take the lock, or return False if another holder has it.

        Retries for a moment first: `is_held()` takes the lock for an instant
        to test it, and a poller starting in that instant must not mistake the
        probe for a rival.
        """
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + wait
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.05)
        note = json.dumps({"pid": os.getpid(), "role": role,
                           "since": datetime.now(timezone.utc).isoformat()})
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, note.encode("utf-8"))
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        _unlock(self._fd)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> "WriterLock":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def holder(path: Path | None = None) -> dict | None:
    """The note left by whoever holds, or last held, the lock."""
    path = Path(path) if path else lock_path()
    try:
        return json.loads(path.read_text(encoding="utf-8") or "null")
    except (OSError, ValueError):
        return None


def is_held(path: Path | None = None) -> bool:
    """Is a writer running against this database right now?

    Answered by trying the lock, not by trusting the note: a note survives a
    crash, a lock does not.
    """
    path = Path(path) if path else lock_path()
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)
