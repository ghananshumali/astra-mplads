"""Gzipped on-disk cache of raw shard responses.

Every response the poller accepts is kept verbatim, so any row in the database
can be traced back to the exact bytes it came from, and so the demo runs with
the network unplugged — from a cache of live pulls rather than a folder of
hand-downloaded spreadsheets.

Only the latest payload per shard is retained. A full national sweep is about
276 MB raw, and keeping every sweep would add gigabytes a week; gzip takes a
real tile response down 15.5x, so the retained set is roughly 18 MB. The
change history lives in `work_versions`, not in a pile of old JSON.

Layout::

    data/raw/shards/<house>/<state_id>/<constituency_id>/<tile>.json.gz
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from ..config import SHARD_CACHE_DIR
from .validate import gzip_json, ungzip_json


def shard_dir(shard_id: str, root: Path | None = None) -> Path:
    """`'2:33:418'` -> `<root>/2/33/418`."""
    base = root or SHARD_CACHE_DIR
    parts = [p for p in str(shard_id).split(":") if p != ""]
    return base.joinpath(*parts)


def write(shard_id: str, tile: str, payload: object,
          root: Path | None = None) -> Path:
    """Store one tile response. Written to a temp file then moved into place,
    so a crash mid-write cannot leave a truncated cache entry behind."""
    directory = shard_dir(shard_id, root)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{tile}.json.gz"
    tmp = directory / f".{tile}.json.gz.tmp"
    tmp.write_bytes(gzip_json(payload))
    os.replace(tmp, target)
    return target


def read(shard_id: str, tile: str, root: Path | None = None) -> object | None:
    """The cached tile response, or None if it was never stored."""
    path = shard_dir(shard_id, root) / f"{tile}.json.gz"
    if not path.exists():
        return None
    return ungzip_json(path.read_bytes())


def read_all(shard_id: str, tiles: tuple[str, ...],
             root: Path | None = None) -> dict[str, list[dict]] | None:
    """Every requested tile for a shard, or None if any of them is missing."""
    out: dict[str, list[dict]] = {}
    for tile in tiles:
        rows = read(shard_id, tile, root)
        if rows is None:
            return None
        out[tile] = rows if isinstance(rows, list) else []
    return out


def cached_at(shard_id: str, tile: str, root: Path | None = None) -> str | None:
    """When the cache entry was written, ISO-8601 UTC."""
    path = shard_dir(shard_id, root) / f"{tile}.json.gz"
    if not path.exists():
        return None
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return stamp.isoformat()


def usage(root: Path | None = None) -> dict:
    """Entry count and bytes on disk — for the freshness endpoint and sizing."""
    base = root or SHARD_CACHE_DIR
    if not base.exists():
        return {"entries": 0, "bytes": 0, "path": str(base)}
    entries = list(base.rglob("*.json.gz"))
    return {"entries": len(entries),
            "bytes": sum(p.stat().st_size for p in entries),
            "path": str(base)}
