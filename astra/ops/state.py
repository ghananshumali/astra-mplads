"""The operations state file: what the supervisor, backups and alerts remember.

Kept in `data/processed/ops_state.json`, not in the database: the database is
what the backups copy and what the alerts are about, so their own record must
survive the database being locked, damaged or restored. Written atomically, so
a crash mid-write leaves the previous version.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ..config import PROCESSED_DIR

STATE_PATH = PROCESSED_DIR / "ops_state.json"
_LOCK = threading.Lock()


def load(path: Path | None = None) -> dict:
    target = Path(path or STATE_PATH)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update(changes: dict, path: Path | None = None) -> dict:
    """Merge `changes` into the state (top-level keys) and write it back."""
    target = Path(path or STATE_PATH)
    with _LOCK:
        data = load(target)
        data.update(changes)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, target)
    return data
