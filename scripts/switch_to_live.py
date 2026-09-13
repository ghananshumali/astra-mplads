"""Build the live eSAKSHI corpus fresh and swap it in as data/astra.db.

    python scripts/switch_to_live.py              # build, validate, swap in
    python scripts/switch_to_live.py --dry-run    # build and validate, do not swap
    python scripts/switch_to_live.py --swap-only  # swap a staging build already made

First used on 13 Sep 2026 to move from the six CSV exports to the live corpus,
and again the same day to rebuild it after the portal's record id turned out
not to be unique (see `astra/ingestion/esakshi_map.py`). A rebuild is the only
safe way to change how works are keyed, for the same reason as below.

Why a new database rather than an in-place update
-------------------------------------------------
The CSV corpus keys works that have not reached sanction as `REC-NA-<Sr. No.>`;
the live path keys them `ES-<portal id>`. The live upsert never deletes, so
sweeping into the existing file would leave both behind: about 24,560 works
stored twice, which the duplicate-detection module would then report as
duplicate works. So the live corpus is built into `data/live-staging/`,
checked, and only then moved into place. The CSV database is kept beside it.

What it does
------------
1. Checks the portal is reachable and that nothing holds data/astra.db open.
2. Builds a fresh database: shard registry, then one full record-level sweep of
   both Houses (about 20 minutes).
3. Builds fund flows from the new works, plus the cached pre-2023 CKAN rows.
4. Carries the human review decisions in `feedback` across.
5. Runs the analysis pipeline, so flags come from the live corpus.
6. Validates the result against the portal's own counts, slice by slice and
   exactly. If anything is off, it stops and leaves the staging build for
   inspection; nothing is swapped.
7. At swap time, carries the observed change history (`work_versions`) and the
   poller's update log across from the old database, which the poller may have
   kept writing during the build. History cannot be re-fetched from anywhere.
8. Moves the old database to data/astra.db.bak-<timestamp> and puts the new
   one in its place. Refuses while a poller is running on it.

Afterwards start the poller to keep it current:

    python -m astra.ingestion.poller
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "data" / "astra.db"
REAL_PROCESSED = ROOT / "data" / "processed"
STAGING = ROOT / "data" / "live-staging"
STAGING_DB = STAGING / "astra.db"
STAGING_PROCESSED = STAGING / "processed"

# Must be set before anything from `astra` is imported: config resolves these
# paths at import time.
os.environ["ASTRA_DB_PATH"] = str(STAGING_DB)
os.environ["ASTRA_PROCESSED_DIR"] = str(STAGING_PROCESSED)
sys.path.insert(0, str(ROOT))

#: The national figure is read after a ~20 minute sweep, so it may lead the
#: stored corpus by works entered meanwhile. Each slice, by contrast, is
#: compared with the count read alongside its own records, and must be exact.
COUNT_TOLERANCE = 0.005
#: Fields that say where a work belongs. A logged "change" to one of these was
#: a different work overwriting the id, never an edit, and is not carried over.
IDENTITY_FIELDS = ("house", "state", "constituency")
#: Share of registered shards allowed to end quarantined.
MAX_QUARANTINED = 0.01


def log(message: str) -> None:
    print(f"[switch {datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def file_is_free(path: Path) -> bool:
    """On Windows a file another process has open cannot be renamed."""
    if not path.exists():
        return True
    probe = path.with_name(path.name + ".lockcheck")
    try:
        os.replace(path, probe)
        os.replace(probe, path)
        return True
    except OSError:
        if probe.exists() and not path.exists():
            os.replace(probe, path)
        return False


# ----------------------------------------------------------------------- build
def build() -> dict:
    from astra import db
    from astra.ingestion import live, offline
    from astra.ingestion.esakshi_api import EsakshiClient, SourceError
    from astra.ingestion.poller import Poller
    from astra.ingestion.router import _conform
    from astra.pipeline import run_pipeline
    from astra.schemas import FundFlow

    import pandas as pd

    report: dict = {"started_at": datetime.now(timezone.utc).isoformat()}

    # -------------------------------------------------------------- 1. preflight
    client = EsakshiClient()
    try:
        national = {h: client.watermark(f"0,0,0,{h}") for h in (2, 1)}
    except SourceError as exc:
        raise SystemExit(f"portal unreachable, nothing changed: {exc}")
    log("portal reachable: LS recommended "
        f"{national[2].counts.get('Works Recommended'):,}, RS "
        f"{national[1].counts.get('Works Recommended'):,}")

    # astra.config creates the staging folder on import, so test for a database
    # rather than the folder before claiming there was an earlier build.
    if STAGING_DB.exists():
        log("removing an earlier staging build — the corpus is always built fresh")
    shutil.rmtree(STAGING, ignore_errors=True)
    STAGING_PROCESSED.mkdir(parents=True, exist_ok=True)
    db.init_db(force=True)

    # --------------------------------------------------- 2. registry + sweep
    poller = Poller(client=client, verbose=True)
    report["registered_shards"] = poller.refresh_registry()
    started = time.monotonic()
    sweep = poller.reconcile_all()
    report["sweep"] = {k: v for k, v in sweep.items() if k != "missing_locally"}
    log(f"sweep finished in {time.monotonic() - started:.0f}s")

    # The fetch gate tolerates a one-record disagreement, because the count and
    # the records are separate reads. A rebuild should end exact, so re-read
    # any slice left one record out. What still disagrees after that fails
    # validation: that is how the non-unique ids were found.
    reread = []
    for _round in (1, 2):
        with db.connect() as con:
            inexact = {r[0] for r in con.execute(
                "SELECT shard_id FROM shard_parity WHERE exact = 0")}
        off = [sh for sh in poller.registry()
               if sh.shard_id in inexact
               or (db.get_watermark(sh.shard_id) or {}).get("count_matched") == 0]
        if not off:
            break
        log(f"re-reading {len(off)} slice(s) not yet in exact parity: "
            + ", ".join(sh.label for sh in off[:6]))
        for sh in off:
            poller.fetch_shard(sh, force=True)
            reread.append(sh.shard_id)
    report["reread_for_exact_count"] = reread

    works = db.read_df("works")
    log(f"works stored: {len(works):,} "
        f"({dict(works['house'].value_counts())})")

    # --------------------------------------------------------- 3. fund flows
    flows = offline.build_fundflows(works)
    if not flows.empty:
        houses = (works.assign(_c=works["constituency"].fillna(""))
                  .drop_duplicates(["state", "_c", "mp_name"])
                  .set_index(["state", "_c", "mp_name"])["house"])
        flows["house"] = [houses.get((s, c or "", m))
                          for s, c, m in zip(flows["state"],
                                             flows["constituency"].fillna(""),
                                             flows["mp_name"])]
        flows["source"] = "esakshi_api"
    history, _prov = live.fetch_ckan_fundflows()
    parts = [f for f in (_conform(flows, FundFlow), _conform(history, FundFlow))
             if not f.empty]
    all_flows = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    db.replace_df("fundflows", _conform(all_flows, FundFlow))
    report["fundflows"] = len(all_flows)
    report["fundflows_pre2023"] = len(history)
    log(f"fund flows: {len(flows):,} from live works + {len(history):,} pre-2023 "
        f"rows from the CKAN cache")

    # ------------------------------------------------------ 4. carry feedback
    carried = 0
    if REAL_DB.exists():
        old = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
        try:
            rows = old.execute("SELECT id, flag_id, action, authority_tier, note, "
                               "created_at FROM feedback ORDER BY id").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            old.close()
        if rows:
            with db.connect() as con:
                con.executemany("INSERT INTO feedback (id, flag_id, action, "
                                "authority_tier, note, created_at) "
                                "VALUES (?, ?, ?, ?, ?, ?)", rows)
            carried = len(rows)
    report["feedback_carried"] = carried
    log(f"review decisions carried across: {carried}")

    # ----------------------------------------------------------- 5. analysis
    log("running the analysis pipeline over the live corpus")
    pipe = run_pipeline(verbose=True)
    report["pipeline"] = {k: v for k, v in pipe.items() if k != "router_trace"}
    report["agents"] = {t["agent"]: (t.get("findings", 0) if t["dispatched"]
                                     else f"skipped: {t['reason']}")
                        for t in pipe["router_trace"]}

    # ----------------------------------------------------------- ingest meta
    meta = {
        "mode_requested": "api", "mode_resolved": "api",
        "works": len(works), "fundflows": len(all_flows),
        "eras": works["era"].value_counts().to_dict(),
        "houses": works["house"].value_counts().to_dict(),
        "shards": report["registered_shards"],
        "sweep": report["sweep"],
        "watermarks": db.watermark_summary(),
        "live_tiles": {str(h): {k: national[h].counts.get(k)
                                for k in national[h].counts} for h in national},
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "provenance": [],
    }
    (STAGING_PROCESSED / "ingest_meta.json").write_text(
        json.dumps(meta, indent=2, default=str), encoding="utf-8")

    report["checks"] = validate(db, client, works, all_flows, pipe, sweep)
    client.close()

    # A single self-contained file is the only safe thing to move.
    with db.connect() as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
    return report


# -------------------------------------------------------------------- validate
def validate(db, client, works, flows, pipe, sweep) -> list[tuple[bool, str]]:
    checks: list[tuple[bool, str]] = []

    def check(ok: bool, message: str) -> None:
        checks.append((bool(ok), message))
        log(("  PASS " if ok else "  FAIL ") + message)

    log("validating against the portal's own figures")
    parity = db.parity_summary()
    for house, label in ((2, "LS"), (1, "RS")):
        portal = client.watermark(f"0,0,0,{house}").counts.get("Works Recommended") or 0
        stored = (parity["national"].get(label, {}).get("recommended", {})
                  .get("stored", [0, 0])[0])
        gap = abs(portal - stored) / max(portal, 1)
        check(gap <= COUNT_TOLERANCE,
              f"{label}: {stored:,} works recommended vs portal now {portal:,} "
              f"({gap:.2%} apart, tolerance {COUNT_TOLERANCE:.1%} for works entered "
              f"during the build)")
    check(parity["exact_slices"] == parity["registered_slices"] > 0,
          f"every slice equals the portal on all four figures, counts and rupees "
          f"({parity['exact_slices']} of {parity['registered_slices']})"
          + (f" — off: {[(e['place'], e['differences']) for e in parity['exceptions'][:4]]}"
             if parity["exceptions"] else ""))
    for label, tiles in sorted(parity["national"].items()):
        for tile, fig in sorted(tiles.items()):
            check(fig["exact"],
                  f"{label} {tile}: stored {fig['stored'][0] if fig['stored'][0] is not None else '-'}"
                  f" / Rs {fig['stored'][1]:,.2f} = portal "
                  f"{fig['portal'][0] if fig['portal'][0] is not None else '-'}"
                  f" / Rs {fig['portal'][1]:,.2f}")

    with db.connect() as con:
        off = [dict(r) for r in con.execute(
            "SELECT s.shard_id, s.state_name, s.constituency_name, w.n_records, "
            "w.n_stored FROM shards s JOIN shard_watermarks w "
            "ON w.shard_id = s.shard_id WHERE COALESCE(w.count_matched, 0) = 0")]
        per_house = {r["house"]: (r["slices"], r["stored"]) for r in con.execute(
            "SELECT s.house, COUNT(*) AS slices, SUM(w.n_stored) AS stored "
            "FROM shards s JOIN shard_watermarks w ON w.shard_id = s.shard_id "
            "GROUP BY s.house")}
    check(not off, "every slice's recommended report equals the portal's count for it"
          + (f" — off: {[(o['state_name'], o['constituency_name'], o['n_records'], o['n_stored']) for o in off[:5]]}"
             if off else ""))
    for house, label in ((2, "LS"), (1, "RS")):
        stored = int((works["house"] == label).sum())
        slices_total = (per_house.get(house) or (0, 0))[1] or 0
        check(stored == slices_total,
              f"{label}: {stored:,} distinct works = {slices_total:,} records across "
              f"its slices (no two slices share a work id)")
    es_ids = works["work_id"].astype(str)
    unscoped = int((es_ids.str.startswith("ES-")
                    & ~es_ids.str.match(r"^ES-(LS|RS)-\d+$")).sum())
    check(unscoped == 0, f"every pre-sanction id carries its house ({unscoped} without)")

    quarantined = len(sweep.get("quarantined", []))
    check(quarantined <= MAX_QUARANTINED * max(sweep.get("shards", 1), 1),
          f"{quarantined} of {sweep.get('shards')} shards quarantined")
    check(sweep.get("recorded_as_complete") is True,
          "the sweep counts as a complete, healthy reconciliation")
    legacy = int(works["work_id"].astype(str).str.startswith("REC-NA-").sum())
    check(legacy == 0, f"no CSV-style REC-NA ids in the new corpus ({legacy})")
    check(int((works["house"] == "RS").sum()) > 0, "Rajya Sabha works are present")
    check(int(works.loc[works["house"] == "RS", "constituency"].notna().sum()) == 0,
          "no Rajya Sabha work carries a pseudo-constituency")
    check(len(flows) > 0, f"fund flows built ({len(flows):,})")
    check(pipe["flags"] > 0, f"flags generated ({pipe['flags']:,}, "
                             f"{pipe['alerts']:,} alerts)")
    dispatched = [t["agent"] for t in pipe["router_trace"] if t["dispatched"]]
    check(len(dispatched) == len(pipe["router_trace"]),
          f"every analysis module ran ({', '.join(dispatched)})")
    return checks


# ------------------------------------------------------------- carry history
def _scoped_id(work_id: str, shard_id: str | None, house_of: dict) -> str:
    """`ES-1740` from the old keying -> `ES-LS-1740` / `ES-RS-1740`."""
    if not (work_id.startswith("ES-") and work_id[3:].isdigit()):
        return work_id
    house = ("LS" if (shard_id or "").startswith("2:") else
             "RS" if (shard_id or "").startswith("1:") else house_of.get(work_id))
    return f"ES-{house}-{work_id[3:]}" if house else work_id


def carry_history(old: sqlite3.Connection, new: sqlite3.Connection) -> dict:
    """Bring observed changes and the poller's update log into the new build.

    Change history is the one thing a rebuild cannot re-fetch. Ids are moved to
    the house-scoped keying; entries where a work "changed" house, state or
    constituency are dropped, because those were one work overwriting another.
    """
    house_of = dict(old.execute(
        "SELECT work_id, house FROM works WHERE work_id LIKE 'ES-%'").fetchall())
    rows = old.execute("SELECT work_id, observed_at, field, old_value, new_value, "
                       "shard_id FROM work_versions").fetchall()
    flips = {(w, t) for w, t, f, *_ in rows if f in IDENTITY_FIELDS}
    existing = {r[0] for r in new.execute("SELECT work_id FROM works")}
    keep, orphaned = [], 0
    for work_id, observed_at, field, old_value, new_value, shard_id in rows:
        if (work_id, observed_at) in flips:
            continue
        scoped = _scoped_id(work_id, shard_id, house_of)
        if scoped not in existing:
            orphaned += 1
            continue
        keep.append((scoped, observed_at, field, old_value, new_value, shard_id))
    new.executemany("INSERT OR IGNORE INTO work_versions (work_id, observed_at, "
                    "field, old_value, new_value, shard_id) VALUES (?,?,?,?,?,?)", keep)

    # Removals already confirmed stay removed and keep their record.
    tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    retired = []
    if "retired_works" in tables:
        retired = [r for r in old.execute("SELECT work_id, shard_id, retired_at, "
                                          "first_missing_at, row_json FROM retired_works")
                   if r[0] not in existing]
        new.executemany("INSERT OR IGNORE INTO retired_works VALUES (?,?,?,?,?)", retired)

    # The build's own slice stores are the initial load, not portal updates.
    new.execute("DELETE FROM provenance WHERE mode = 'api'")
    log_rows = old.execute("SELECT source, mode, table_name, rows, status, detail, "
                           "fetched_at FROM provenance WHERE mode = 'api' "
                           "ORDER BY id").fetchall()
    new.executemany("INSERT INTO provenance (source, mode, table_name, rows, status, "
                    "detail, fetched_at) VALUES (?,?,?,?,?,?,?)", log_rows)
    new.commit()
    return {"versions_carried": len(keep),
            "version_groups_dropped_as_id_clashes": len(flips),
            "versions_without_a_work": orphaned,
            "update_log_rows_carried": len(log_rows),
            "retired_works_carried": len(retired)}


# ------------------------------------------------------------------------ swap
def swap() -> Path | None:
    if not STAGING_DB.exists():
        raise SystemExit("no staging build to swap in; run without --swap-only")
    for sidecar in ("-wal", "-shm"):
        if Path(str(STAGING_DB) + sidecar).exists():
            raise SystemExit(f"staging database still has a {sidecar} file open; "
                             "close whatever is using it and retry --swap-only")
    from astra.ingestion import instance_lock
    if instance_lock.is_held(instance_lock.lock_path(REAL_DB)):
        note = instance_lock.holder(instance_lock.lock_path(REAL_DB)) or {}
        raise SystemExit(f"a poller is running on data/astra.db (pid {note.get('pid', '?')}). "
                         "Stop it (Ctrl+C in its window), then run: "
                         "python scripts/switch_to_live.py --swap-only")
    if not file_is_free(REAL_DB):
        raise SystemExit("data/astra.db is open in another process (the API or "
                         "dev server?). Stop it, then run: "
                         "python scripts/switch_to_live.py --swap-only")

    carried = None
    if REAL_DB.exists():
        # Fold any WAL content into the file first, so the kept copy is whole
        # and the history read below includes the poller's last writes.
        old = sqlite3.connect(REAL_DB)
        try:
            old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            new = sqlite3.connect(STAGING_DB)
            try:
                carried = carry_history(old, new)
            finally:
                new.close()
        finally:
            old.close()
        log(f"carried across: {carried}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    kept = None
    if REAL_DB.exists():
        kept = REAL_DB.with_name(f"astra.db.bak-{stamp}")
        os.replace(REAL_DB, kept)
        for sidecar in ("-wal", "-shm"):
            side = Path(str(REAL_DB) + sidecar)
            if side.exists():
                os.replace(side, Path(str(kept) + sidecar))
    os.replace(STAGING_DB, REAL_DB)

    REAL_PROCESSED.mkdir(parents=True, exist_ok=True)
    for item in STAGING_PROCESSED.glob("*.json"):
        shutil.copy2(item, REAL_PROCESSED / item.name)
    # The report sits at the staging root, not in processed/, so a --swap-only
    # run would otherwise delete the only record of the build it swapped in.
    report = STAGING / "switch_report.json"
    if report.exists():
        data = json.loads(report.read_text(encoding="utf-8"))
        data["carried_at_swap"] = carried
        (REAL_PROCESSED / report.name).write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8")
    shutil.rmtree(STAGING, ignore_errors=True)
    return kept


# ------------------------------------------------------------------------ main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="build and validate only; leave data/astra.db alone")
    mode.add_argument("--swap-only", action="store_true",
                      help="swap in a staging build that already passed")
    args = parser.parse_args()

    if args.swap_only:
        kept = swap()
        log(f"swapped in. previous database kept as {kept.name if kept else 'n/a'}")
        return 0

    if not args.dry_run and not file_is_free(REAL_DB):
        raise SystemExit("data/astra.db is open in another process. Stop the API "
                         "or dev server before switching, so the 20-minute build "
                         "is not wasted.")

    clock = time.monotonic()
    report = build()
    failed = [message for ok, message in report["checks"] if not ok]
    report["seconds"] = round(time.monotonic() - clock, 1)
    (STAGING / "switch_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")

    if failed:
        log(f"{len(failed)} check(s) failed — nothing swapped. Staging build kept "
            f"at {STAGING} for inspection:")
        for message in failed:
            log(f"    {message}")
        return 1
    if args.dry_run:
        log(f"dry run passed in {report['seconds']:.0f}s — staging build ready at "
            f"{STAGING}. Swap it in with --swap-only.")
        return 0

    kept = swap()
    log(f"done in {report['seconds']:.0f}s. data/astra.db is now the live corpus; "
        f"the previous database is kept as data/{kept.name if kept else 'n/a'}")
    log("keep it current with:  python -m astra.ingestion.poller")
    return 0


if __name__ == "__main__":
    sys.exit(main())
