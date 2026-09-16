"""Daily copies of the database, taken while everything keeps running.

    python -m astra.ops.backup --now              # take one now
    python -m astra.ops.backup --list             # what there is
    python -m astra.ops.backup --restore FILE     # put one back (everything stopped)

Why a copy matters
------------------
Most of `data/astra.db` can be read again from the portal, at the cost of a
full sweep. Some of it cannot: the edit history in `work_versions` exists only
because the poller was watching when the edit happened, and the review
decisions in `feedback` exist only here. A damaged file would lose both.

How
---
SQLite's online backup API copies a consistent snapshot while the poller and
the API keep working (913 MB in about 4 s on 16 Sep 2026). The copy is written
under a temporary name, checked with `PRAGMA quick_check`, and only then given
its final name, so a file named `astra-YYYYMMDD-HHMMSS.db` is always a complete,
readable database. The newest `KEEP` are kept; files not named that way, such
as the hand-made `astra.db.bak-*` copies, are never touched.

It runs once a day at `BACKUP_AT` (local time) from the supervisor, and catches
up on the next minute the machine is awake if it slept through the slot.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import DATA_DIR, DB_PATH
from . import state

BACKUP_DIR = Path(os.environ.get("ASTRA_BACKUP_DIR") or (DATA_DIR / "backups"))
#: Local time of the daily copy, HH:MM, or "off".
BACKUP_AT = os.environ.get("ASTRA_BACKUP_AT", "02:30")
#: Daily copies kept.
KEEP = max(1, int(os.environ.get("ASTRA_BACKUP_KEEP", "7")))
#: A failed copy is retried after this long, not every minute.
RETRY_GAP = timedelta(minutes=int(os.environ.get("ASTRA_BACKUP_RETRY_MIN", "30")))
#: Free space the copy must leave behind, as a multiple of the database size.
SPACE_FACTOR = 2.0

_NAME = re.compile(r"^astra-(\d{8})-(\d{6})\.db$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def backups(directory: Path | None = None) -> list[dict]:
    """The backups ASTRA made, newest first."""
    directory = Path(directory or BACKUP_DIR)
    if not directory.exists():
        return []
    out = []
    for path in directory.iterdir():
        match = _NAME.match(path.name)
        if match and path.is_file():
            taken = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
            out.append({"path": str(path), "name": path.name, "bytes": path.stat().st_size,
                        "taken_at": taken.replace(tzinfo=timezone.utc).isoformat()})
    return sorted(out, key=lambda b: b["name"], reverse=True)


def due(now: datetime | None = None, *, at: str | None = None,
        state_path: Path | None = None) -> bool:
    """Is today's copy owed? The same slot rule as the nightly sweep."""
    from ..ingestion.poller import most_recent_slot

    now = now or datetime.now().astimezone()
    slot = most_recent_slot(now, BACKUP_AT if at is None else at)
    if slot is None:
        return False
    saved = state.load(state_path).get("backup") or {}
    last_ok = _stamp(saved.get("last_ok_at"))
    if last_ok is not None and last_ok >= slot:
        return False
    last_try = _stamp(saved.get("last_attempt_at"))
    return not (last_try is not None and now - last_try < RETRY_GAP)


def take(*, source: Path | None = None, directory: Path | None = None,
         keep: int | None = None, state_path: Path | None = None,
         now: datetime | None = None) -> dict:
    """Copy the database, check the copy, keep the newest `keep`. Never raises:
    the outcome, success or failure, is returned and recorded."""
    source = Path(source or DB_PATH)
    directory = Path(directory or BACKUP_DIR)
    keep = KEEP if keep is None else keep
    started = now or _now()
    record = {"last_attempt_at": started.isoformat()}
    previous = state.load(state_path).get("backup") or {}
    outcome: dict = {"ok": False, "started_at": started.isoformat()}
    part = None
    try:
        if not source.exists():
            raise FileNotFoundError(f"no database at {source}")
        directory.mkdir(parents=True, exist_ok=True)
        size = source.stat().st_size
        free = shutil.disk_usage(directory).free
        if free < SPACE_FACTOR * size:
            raise OSError(f"only {free / 1e9:.1f} GB free for a {size / 1e9:.1f} GB copy")
        name = f"astra-{started.strftime('%Y%m%d-%H%M%S')}.db"
        final = directory / name
        part = directory / (name + ".part")
        t0 = time.monotonic()
        src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            dst = sqlite3.connect(part)
            try:
                src.backup(dst)
                check = dst.execute("PRAGMA quick_check").fetchone()[0]
                works = dst.execute("SELECT COUNT(*) FROM works").fetchone()[0] \
                    if dst.execute("SELECT 1 FROM sqlite_master WHERE name = 'works'").fetchone() else None
            finally:
                dst.close()
        finally:
            src.close()
        if check != "ok":
            raise sqlite3.DatabaseError(f"the copy failed its integrity check: {check}")
        os.replace(part, final)
        part = None
        removed = []
        for old in backups(directory)[keep:]:
            Path(old["path"]).unlink(missing_ok=True)
            removed.append(old["name"])
        outcome.update(ok=True, file=str(final), bytes=final.stat().st_size, works=works,
                       seconds=round(time.monotonic() - t0, 1), removed=removed)
        record.update(last_ok_at=started.isoformat(), last_file=str(final),
                      last_bytes=outcome["bytes"], last_seconds=outcome["seconds"],
                      last_error=None)
    except Exception as exc:                          # recorded, never raised
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        record.update(last_error=outcome["error"], last_error_at=started.isoformat())
        if part is not None:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
    state.update({"backup": {**previous, **record}}, state_path)
    return outcome


def restore(file: Path, *, target: Path | None = None) -> Path:
    """Put a backup in place of the database, keeping the current file aside.

    Refuses while a poller holds the writer lock. The API must be stopped too:
    Windows will not replace a file another process has open, and the error
    says so. Returns where the replaced database was moved.
    """
    from ..ingestion import instance_lock

    file, target = Path(file), Path(target or DB_PATH)
    if not _NAME.match(file.name) and not file.name.startswith("astra.db.bak"):
        raise ValueError(f"{file.name} is not an ASTRA backup")
    check = sqlite3.connect(f"file:{file.as_posix()}?mode=ro", uri=True)
    try:
        result = check.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        raise sqlite3.DatabaseError(f"{file.name} failed its integrity check: {result}")
    if instance_lock.is_held(instance_lock.lock_path(target)):
        raise RuntimeError("a poller is writing to the database; stop ASTRA first")
    aside = target.with_name(f"{target.name}.replaced-{_now().strftime('%Y%m%d-%H%M%S')}")
    try:
        if target.exists():
            os.replace(target, aside)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(target) + suffix)
            if sidecar.exists():
                os.replace(sidecar, Path(str(aside) + suffix))
    except PermissionError as exc:
        raise RuntimeError("the database is open in another process (the API or the "
                           "supervisor); stop ASTRA first") from exc
    shutil.copy2(file, target)
    return aside


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ASTRA database backups")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--now", action="store_true", help="take a backup now")
    group.add_argument("--list", action="store_true", help="list the backups")
    group.add_argument("--restore", metavar="FILE", help="put a backup back in place")
    args = parser.parse_args(argv)
    if args.list:
        for b in backups():
            print(f"{b['name']}  {b['bytes'] / 1e6:,.0f} MB  taken {b['taken_at']}")
        return 0
    if args.now:
        outcome = take()
        print(json.dumps(outcome, indent=2))
        return 0 if outcome["ok"] else 1
    try:
        aside = restore(Path(args.restore))
    except (RuntimeError, ValueError, sqlite3.DatabaseError, OSError) as exc:
        print(f"not restored: {exc}", file=sys.stderr)
        return 1
    print(f"restored {args.restore}; the previous database is at {aside}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
