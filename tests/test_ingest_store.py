"""Stage B gate: storage, the four gates, and the raw shard cache.

    python tests/test_ingest_store.py

Runs entirely offline against a throwaway database in a temp directory. Points
ASTRA_DB_PATH at it before importing anything from `astra`, so the real corpus
at data/astra.db is never opened — not even read-only.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# must be set before `astra.config` is imported
_TMP = Path(tempfile.mkdtemp(prefix="astra-stage-b-"))
os.environ["ASTRA_DB_PATH"] = str(_TMP / "scratch.db")
os.environ["ASTRA_PROCESSED_DIR"] = str(_TMP / "processed")
os.environ["ASTRA_SHARD_CACHE"] = str(_TMP / "shards")

from astra import db                                        # noqa: E402
from astra.config import DB_PATH                            # noqa: E402
from astra.ingestion import esakshi_map as emap             # noqa: E402
from astra.ingestion import shard_cache, validate           # noqa: E402
from astra.ingestion.esakshi_api import RECORD_TILES        # noqa: E402
from astra.schemas import Work                              # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "esakshi"
SHARD = "2:33:418"
REAL_DB = ROOT / "data" / "astra.db"


def _real_files() -> dict[str, tuple[int, int] | None]:
    """Size and mtime of the real corpus and its sidecars, or None if absent."""
    out = {}
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(REAL_DB) + suffix)
        out[path.name] = ((path.stat().st_size, path.stat().st_mtime_ns)
                          if path.exists() else None)
    return out


#: Taken before any test runs. The real corpus is live now, so its WAL sidecars
#: can legitimately exist (a running poller, or one stopped with Ctrl+C); what
#: this run must not do is touch them.
_REAL_BEFORE = _real_files()

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    tag = PASS if cond else FAIL
    results.append((tag, name, detail))
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def skip(name: str, why: str) -> None:
    results.append((SKIP, name, why))
    print(f"  [{SKIP}] {name} — {why}")


def load_tiles() -> dict[str, list[dict]] | None:
    tiles = {}
    for tile in RECORD_TILES:
        path = FIXTURES / f"aligarh_{tile}.json"
        if not path.exists():
            return None
        tiles[tile] = json.loads(path.read_text(encoding="utf-8"))
    return tiles


def seed_registry() -> None:
    db.save_shards([{"shard_id": SHARD, "house": 2, "state_id": 33,
                     "constituency_id": 418, "state_name": "UTTAR PRADESH",
                     "constituency_name": "ALIGARH"},
                    {"shard_id": "1:33:0", "house": 1, "state_id": 33,
                     "constituency_id": 0, "state_name": "UTTAR PRADESH",
                     "constituency_name": ""}])


# --------------------------------------------------------------------- schema
def test_schema() -> None:
    print("\n[1] schema — new tables exist and survive init_db()")
    db.init_db(force=True)
    with db.connect() as con:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("shards", "shard_watermarks", "work_versions"):
        check(f"{table} created", table in names)

    check("the new tables are excluded from the drift rebuild",
          not {"shards", "shard_watermarks", "work_versions"} & set(db._REBUILDABLE),
          f"rebuildable = {db._REBUILDABLE}")

    # the trap: init_db() drops any *rebuildable* table whose columns drift.
    # Put a row in each new table, force a drift on `works`, and check the new
    # tables are still there afterwards with their data.
    seed_registry()
    db.save_watermark(SHARD, n_records=103, lifecycle=validate.STORED)
    with db.connect() as con:
        con.execute("INSERT OR IGNORE INTO work_versions "
                    "(work_id, observed_at, field, old_value, new_value, shard_id) "
                    "VALUES ('W1','2026-09-12','status','a','b',?)", (SHARD,))
        con.execute("ALTER TABLE works ADD COLUMN drift_me TEXT")   # force drift
    db.init_db(force=True)
    with db.connect() as con:
        cols = [r[1] for r in con.execute("PRAGMA table_info(works)")]
        survived = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("shards", "shard_watermarks", "work_versions")}
    check("a drifted works table is rebuilt", "drift_me" not in cols)
    check("shards survived the rebuild", survived["shards"] == 2, str(survived))
    check("shard_watermarks survived the rebuild", survived["shard_watermarks"] == 1)
    check("work_versions survived the rebuild", survived["work_versions"] == 1)

    with db.connect() as con:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    check("journal mode is WAL", str(mode).lower() == "wal", f"mode={mode}")


# ---------------------------------------------------------------- upsert path
def test_upsert(tiles: dict[str, list[dict]]) -> list[Work]:
    print("\n[2] upsert_works — idempotent, and it logs real changes")
    works = emap.to_works(tiles)

    first = db.upsert_works(works, shard_id=SHARD)
    with db.connect() as con:
        n1 = con.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    check("first upsert inserts every record", n1 == len(works),
          f"{n1} rows for {len(works)} works")
    check("first upsert logs no history", first["versions"] == 0, str(first))

    second = db.upsert_works(works, shard_id=SHARD)
    with db.connect() as con:
        n2 = con.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    check("re-running changes nothing (idempotent)", n2 == n1,
          f"{n1} -> {n2}, changed={second['changed']}")
    check("an unchanged re-run logs no versions", second["versions"] == 0,
          str(second))

    # now move one field the way the portal would: an amount revised upward
    target = next(w for w in works if w.sanctioned_amount)
    before = target.sanctioned_amount
    bumped = target.model_copy(update={"sanctioned_amount": before + 50_000})
    third = db.upsert_works([bumped], shard_id=SHARD)
    check("a changed field is detected", third["changed"] == 1, str(third))
    check("exactly one version row is written", third["versions"] == 1)

    history = db.work_versions(target.work_id)
    check("history records the old and new value",
          len(history) == 1
          and history[0]["field"] == "sanctioned_amount"
          and float(history[0]["new_value"]) == before + 50_000,
          str(history[:1]))
    with db.connect() as con:
        stored = con.execute("SELECT sanctioned_amount FROM works WHERE work_id=?",
                             (target.work_id,)).fetchone()[0]
    check("the current row carries the new value", stored == before + 50_000)

    # put it back so later assertions see the fixture's own numbers
    db.upsert_works([target], shard_id=SHARD)
    return works


def test_collision_guard(works: list[Work]) -> None:
    print("\n[2b] a different work can never silently replace a stored one")
    target = next(w for w in works if w.work_id.startswith("ES-"))
    # id 1740: a Rajampet work and a Madhya Pradesh Rajya Sabha work
    impostor = target.model_copy(update={"house": "RS", "state": "MADHYA PRADESH",
                                         "constituency": None,
                                         "description": "an unrelated work"})
    try:
        db.upsert_works([impostor], shard_id="1:20:0")
        raised = None
    except db.WorkIdCollision as exc:
        raised = exc
    check("a work from another house or state is refused", raised is not None,
          str(raised)[:120])
    with db.connect() as con:
        row = con.execute("SELECT house, state, description FROM works WHERE work_id=?",
                          (target.work_id,)).fetchone()
        logged = con.execute("SELECT COUNT(*) FROM work_versions WHERE work_id=?",
                             (target.work_id,)).fetchone()[0]
    check("the stored work is untouched",
          row["state"] == target.state and row["description"] == target.description)
    check("and no false history is logged", logged == 0, f"{logged} version rows")

    try:
        db.upsert_works([target, impostor], shard_id=SHARD)
        twice = False
    except db.WorkIdCollision:
        twice = True
    check("a batch carrying one id twice is refused", twice)

    edited = target.model_copy(update={"estimated_cost": (target.estimated_cost or 0) + 1})
    result = db.upsert_works([edited], shard_id=SHARD)
    check("an ordinary edit in the same place still goes through",
          result["changed"] == 1, str(result))
    db.upsert_works([target], shard_id=SHARD)
    with db.connect() as con:
        con.execute("DELETE FROM work_versions WHERE work_id=?", (target.work_id,))


def test_shard_queries(works: list[Work]) -> None:
    print("\n[3] shard registry and reverse lookup")
    shards = db.load_shards(house=2)
    check("registry stores the LS shard", len(shards) == 1
          and shards[0]["shard_id"] == SHARD, str(len(shards)))

    stored_ids = db.works_in_shard(SHARD)
    check("works_in_shard finds the shard's records",
          stored_ids == {w.work_id for w in works},
          f"{len(stored_ids)} ids")
    check("an unknown shard yields nothing", db.works_in_shard("9:9:9") == set())


# ----------------------------------------------------------------- the gates
def test_figure_differences() -> None:
    print("\n[3b] parity comparison — every portal figure, count and rupees")
    portal = {"recommended": [10, 1000.0], "sanctioned": [7, 700.0],
              "completed": [3, 300.0], "expenditure": [None, 250.0]}
    same = {"recommended": (10, 1000.4), "sanctioned": (7, 700.0),
            "completed": (3, 300.0), "expenditure": (41, 250.0)}
    check("identical figures, and paise of float rounding, are exact",
          validate.figure_differences(portal, same) == [])
    off = {**same, "sanctioned": (6, 700.0), "expenditure": (41, 262.0)}
    found = {(d["tile"], d["measure"]) for d in validate.figure_differences(portal, off)}
    check("a count or a rupee total that differs is named by tile",
          found == {("sanctioned", "count"), ("expenditure", "rupees")}, str(found))
    check("counts_only ignores rupee differences",
          {(d["tile"], d["measure"]) for d in
           validate.figure_differences(portal, off, counts_only=True)} == {("sanctioned", "count")})
    check("a figure the portal does not report is never compared",
          validate.figure_differences({"completed": [None, None]}, {"completed": (9, 9.0)}) == [])


def test_gates(tiles: dict[str, list[dict]]) -> None:
    print("\n[4] gate 1 — assert_contract")
    for tile in RECORD_TILES:
        check(f"{tile} fixture satisfies its contract",
              bool(validate.assert_contract(tile, tiles[tile])))

    stripped = [{k: v for k, v in r.items() if k != "RECOMMENDED_AMOUNT"}
                for r in tiles["recommended"]]
    result = validate.assert_contract("recommended", stripped)
    check("a missing required field quarantines the shard",
          not result and result.lifecycle == validate.QUARANTINED, result.reason)

    padded = [{**r, "SOME_NEW_COLUMN": 1} for r in tiles["recommended"]]
    check("an unexpected extra field is allowed",
          bool(validate.assert_contract("recommended", padded)))

    print("\n[5] gate 2 — guard_zero")
    zero = validate.guard_zero([], suspicious_zero=True)
    check("an all-zero watermark is refused",
          not zero and zero.lifecycle == validate.QUARANTINED, zero.reason)
    check("no rows where records were stored is refused",
          not validate.guard_zero([], previous_stored=103))
    check("no rows where nothing was stored is accepted (genuinely empty)",
          bool(validate.guard_zero([], previous_stored=0)))
    check("a collapse from 103 to 2 is refused",
          not validate.guard_zero([{}, {}], previous_stored=103))
    check("a normal response passes",
          bool(validate.guard_zero(tiles["recommended"], previous_stored=103)))

    print("\n[6] gate 3 — reconcile_count")
    check("an exact match passes and is marked matched",
          validate.reconcile_count(103, 103).detail["matched"] is True)
    check("a one-record skew is tolerated but not marked matched",
          bool(validate.reconcile_count(103, 104))
          and validate.reconcile_count(103, 104).detail["matched"] is False)
    truncated = validate.reconcile_count(103, 40)
    check("a truncated response is refused",
          not truncated and truncated.lifecycle == validate.QUARANTINED,
          truncated.reason)
    check("no portal count means no verdict",
          validate.reconcile_count(None, 103).detail["matched"] is None)

    print("\n[7] validate_shard — the gates in order")
    mapped = len(emap.to_works(tiles))
    ok = validate.validate_shard(tiles, portal_count=103, mapped=mapped,
                                 previous_stored=103)
    check("the real fixture shard passes every gate", bool(ok), ok.reason)
    bad = validate.validate_shard({"recommended": stripped}, portal_count=103,
                                  mapped=mapped)
    check("a contract failure short-circuits the rest", not bad, bad.reason)


# ----------------------------------------------------- escalation / freshness
def test_escalation() -> None:
    print("\n[8] quarantine escalation and freshness")
    bad_shard = "2:33:419"
    db.save_shards([{"shard_id": bad_shard, "house": 2, "state_id": 33,
                     "constituency_id": 419, "state_name": "UTTAR PRADESH",
                     "constituency_name": "BARABANKI(SC)"}])
    counts = [db.mark_shard_failure(bad_shard, "HTTP 500") for _ in range(3)]
    check("consecutive failures accumulate", counts == [1, 2, 3], str(counts))

    escalated = db.stale_shards(min_failures=3)
    check("a repeatedly failing shard reaches the escalation list",
          any(s["shard_id"] == bad_shard for s in escalated), str(len(escalated)))
    named = next(s for s in escalated if s["shard_id"] == bad_shard)
    check("the escalation names the place, not just an id",
          named["constituency_name"] == "BARABANKI(SC)"
          and bool(named["stale_since"]),
          f"{named['constituency_name']} stale since {named['stale_since']}")
    check("shards below the threshold are not escalated",
          not any(s["shard_id"] == SHARD for s in db.stale_shards(min_failures=3)))

    summary = db.watermark_summary()
    check("the summary counts quarantined shards rather than hiding them",
          summary.get("quarantined") == 1, json.dumps(summary, default=str))

    # a success is the only thing that clears the counter
    db.save_watermark(bad_shard, n_records=7, lifecycle=validate.STORED,
                      n_stored=7, count_matched=True, fetched=True)
    row = db.get_watermark(bad_shard)
    check("a successful read clears the failure counter and staleness",
          row["consecutive_failures"] == 0 and row["stale_since"] is None
          and row["count_matched"] == 1)


# --------------------------------------------------------------- fingerprints
def test_fingerprints(tiles: dict[str, list[dict]]) -> None:
    print("\n[9] payload hashing and the raw cache")
    h1 = validate.payload_hash(tiles)
    h2 = validate.payload_hash(tiles)
    check("the hash is stable across calls", h1 == h2, h1[:16])

    reordered = {k: list(reversed(v)) for k, v in tiles.items()}
    check("row order does not change the hash",
          validate.payload_hash(reordered) == h1)

    renumbered = {k: [{**r, "Sno": (r.get("Sno") or 0) + 1000} for r in v]
                  for k, v in tiles.items()}
    check("the presentation row number Sno is excluded",
          validate.payload_hash(renumbered) == h1)

    touched = {**tiles, "recommended": [
        {**tiles["recommended"][0], "RECOMMENDED_AMOUNT": 1},
        *tiles["recommended"][1:]]}
    check("a real content change changes the hash",
          validate.payload_hash(touched) != h1)

    for tile in RECORD_TILES:
        shard_cache.write(SHARD, tile, tiles[tile])
    back = shard_cache.read_all(SHARD, RECORD_TILES)
    check("the cache round-trips every tile",
          back is not None and all(len(back[t]) == len(tiles[t])
                                   for t in RECORD_TILES))
    check("a cached shard rebuilds identical works",
          [w.model_dump() for w in emap.to_works(back)]
          == [w.model_dump() for w in emap.to_works(tiles)])
    use = shard_cache.usage()
    raw = sum(len(json.dumps(v, default=str)) for v in tiles.values())
    check("the cache is stored compressed", use["bytes"] < raw / 5,
          f"{use['entries']} entries, {use['bytes'] / 1024:.0f} KB "
          f"vs {raw / 1024:.0f} KB raw "
          f"({raw / max(1, use['bytes']):.1f}x)")
    check("no temp files are left behind",
          not list(Path(use["path"]).rglob(".*tmp")))
    check("the cache honoured ASTRA_SHARD_CACHE",
          str(_TMP) in use["path"], use["path"])


# ------------------------------------------------------------------ isolation
def test_isolation() -> None:
    print("\n[10] isolation — the real corpus was never opened")
    check("DB_PATH points at the scratch database", str(_TMP) in str(DB_PATH),
          str(DB_PATH))
    from astra.ingestion import instance_lock
    if not REAL_DB.exists():
        skip("data/astra.db untouched", "no real corpus on this machine")
    elif instance_lock.is_held(instance_lock.lock_path(REAL_DB)):
        skip("data/astra.db untouched", "a poller is writing to the real corpus")
    else:
        after = _real_files()
        check("data/astra.db and its sidecars were not touched by this run",
              after == _REAL_BEFORE,
              "" if after == _REAL_BEFORE else f"{_REAL_BEFORE} -> {after}")


# ----------------------------------------------------------------------- main
def main() -> int:
    print("=" * 74)
    print("ASTRA Stage B — storage, gates, raw cache")
    print(f"scratch database: {DB_PATH}")
    print("=" * 74)
    tiles = load_tiles()
    if tiles is None:
        print("\nFixtures missing — run: python tests/test_esakshi.py --refresh")
        return 1
    try:
        test_schema()
        works = test_upsert(tiles)
        test_collision_guard(works)
        test_shard_queries(works)
        test_figure_differences()
        test_gates(tiles)
        test_escalation()
        test_fingerprints(tiles)
        test_isolation()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print("\n" + "=" * 74)
    failed = [r for r in results if r[0] == FAIL]
    passed = sum(1 for r in results if r[0] == PASS)
    skipped = sum(1 for r in results if r[0] == SKIP)
    print(f"  {passed} passed, {len(failed)} failed, {skipped} skipped")
    for _, name, detail in failed:
        print(f"    FAILED: {name} — {detail}")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
