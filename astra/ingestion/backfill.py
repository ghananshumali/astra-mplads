"""Fill what the store newly keeps from the raw shard cache, without asking the portal.

When the mapping starts keeping something the portal was already sending, the
database lacks it until each slice is read again. Waiting for those re-reads
has two costs: a slice whose records do not change is never rewritten (its
payload hash matches), and a re-read would log every filled-in field as an edit
in `work_versions`, which is history that cannot be told apart from a real
change afterwards.

The cache holds the payload each slice last stored, written right after the
works it produced (`poller._attempt_fetch`), so the database can be completed
from exactly the records already in it: no request, no history entry. Two jobs,
each run once, in one pass over the cache:

  fields    work columns added on 16 Sep 2026 (`FIELDS`). A field is only
            filled where it is still empty, so a value a newer read stored is
            never replaced.
  payments  the payment records behind each work's `total_paid` (`db.payments`).
            A work's payments are stored only if they add up to the work as
            stored, in count and in rupees; any other work waits for its slice's
            next read.

Works whose slice has no cache entry wait for their slice's next read.
Runs under the writer lock (`poller.main`).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

from .. import db
from . import shard_cache
from .esakshi_api import RECORD_TILES
from .esakshi_map import map_shard

#: Work fields added on 16 Sep 2026 that the cached payloads already carry.
FIELDS = ("work_category", "letter_no", "term_start", "term_end",
          "implementing_agency", "vendor_id")
STATE_KEY = "backfill_portal_fields"
PAYMENTS_KEY = "backfill_payments"


def done() -> bool:
    return bool(db.get_state(STATE_KEY))


def payments_done() -> bool:
    return bool(db.get_state(PAYMENTS_KEY))


def pending() -> bool:
    return not done() or not payments_done()


def run_pending(*, log: Callable[[str], None] = print, root=None) -> dict:
    """Run whichever jobs have not run yet, in one pass over the cache."""
    fields, payments = not done(), not payments_done()
    if not (fields or payments):
        return {"skipped": True}
    return _run(fields=fields, payments=payments, log=log, root=root)


def fill_from_cache(*, log: Callable[[str], None] = print, root=None,
                    force: bool = False) -> dict:
    """Fill `FIELDS` on stored works from each slice's cached payload."""
    if done() and not force:
        return {"skipped": True}
    return _run(fields=True, payments=False, log=log, root=root)["fields"]


def fill_payments_from_cache(*, log: Callable[[str], None] = print, root=None,
                             force: bool = False) -> dict:
    """Store each work's payment records from its slice's cached payload."""
    if payments_done() and not force:
        return {"skipped": True}
    return _run(fields=False, payments=True, log=log, root=root)["payments"]


def _run(*, fields: bool, payments: bool, log: Callable[[str], None], root) -> dict:
    started = datetime.now(timezone.utc)
    assignments = ", ".join(f"{f} = COALESCE({f}, ?)" for f in FIELDS)
    columns = ", ".join(("work_id", "payment_count", "total_paid") + FIELDS)
    slices = {"slices": 0, "slices_without_cache": 0, "works_seen": 0}
    filled = {"works_filled": 0}
    paid = {"works_with_payments": 0, "payments_stored": 0, "works_not_matching": 0}
    shards = db.load_shards()
    jobs = [name for name, due in (("fields", fields), ("payments", payments)) if due]
    log(f"completing stored works from the cache of {len(shards)} slices "
        f"({', '.join(jobs)}; no portal requests)")
    for shard in shards:
        shard_id = shard["shard_id"]
        tiles = shard_cache.read_all(shard_id, RECORD_TILES, root)
        if tiles is None:
            slices["slices_without_cache"] += 1
            continue
        slices["slices"] += 1
        mapping = map_shard(tiles)
        with db.connect() as con:
            stored = {r["work_id"]: r for r in con.execute(
                f"SELECT {columns} FROM works WHERE work_id IN "
                f"(SELECT work_id FROM work_listing WHERE shard_id = ?)", (shard_id,))}
            slices["works_seen"] += len(stored)
            if fields:
                # only where a stored field is empty and the cache has a value
                rows = [tuple(getattr(w, f) for f in FIELDS) + (w.work_id,)
                        for w in mapping.works
                        if w.work_id in stored
                        and any(stored[w.work_id][f] is None and getattr(w, f) is not None
                                for f in FIELDS)]
                if rows:
                    con.executemany(f"UPDATE works SET {assignments} WHERE work_id = ?", rows)
                filled["works_filled"] += len(rows)
        if payments:
            by_work: dict[str, list[dict]] = {}
            for row in mapping.payments:
                by_work.setdefault(row["work_id"], []).append(row)
            keep, rows = [], []
            for work_id, records in by_work.items():
                work = stored.get(work_id)
                if work is None:
                    continue
                total = sum(r["amount"] or 0.0 for r in records)
                if (work["payment_count"] != len(records)
                        or abs((work["total_paid"] or 0.0) - total) > 0.5):
                    paid["works_not_matching"] += 1
                    continue
                keep.append(work_id)
                rows += records
            if keep:
                db.replace_payments(shard_id, keep, rows)
            paid["works_with_payments"] += len(keep)
            paid["payments_stored"] += len(rows)
    seconds = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    out = {}
    if fields:
        out["fields"] = {**slices, **filled, **_still_empty(), "seconds": seconds}
        db.set_state(STATE_KEY, json.dumps({"at": started.isoformat(), **out["fields"]}))
        log(f"filled {filled['works_filled']:,} works; works still without a portal "
            f"category: {out['fields']['works_without_category']:,}")
    if payments:
        out["payments"] = {**slices, **paid, "seconds": seconds}
        db.set_state(PAYMENTS_KEY, json.dumps({"at": started.isoformat(), **out["payments"]}))
        log(f"stored {paid['payments_stored']:,} payments on {paid['works_with_payments']:,} "
            f"works; {paid['works_not_matching']} works left for their next read")
    if filled["works_filled"] or paid["payments_stored"]:
        # What the analysis reads has changed, so the flags are now behind.
        db.set_state("data_changed_at", datetime.now(timezone.utc).isoformat())
    log(f"cache pass: {slices['slices']} slices in {seconds}s, "
        f"{slices['slices_without_cache']} without a cache entry")
    return out


def _still_empty() -> dict:
    with db.connect() as con:
        row = con.execute(
            "SELECT SUM(work_category IS NULL), SUM(letter_no IS NULL), "
            "SUM(payment_count > 0 AND implementing_agency IS NULL), "
            "SUM(payment_count > 0 AND vendor_id IS NULL) "
            "FROM works WHERE source = 'esakshi_api'").fetchone()
    return {"works_without_category": int(row[0] or 0),
            "works_without_letter": int(row[1] or 0),
            "paid_works_without_agency": int(row[2] or 0),
            "paid_works_without_vendor_id": int(row[3] or 0)}
