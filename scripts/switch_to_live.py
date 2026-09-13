"""Switch data/astra.db from the six CSV exports to the live eSAKSHI corpus.

    python scripts/switch_to_live.py              # build, validate, swap in
    python scripts/switch_to_live.py --dry-run    # build and validate, do not swap
    python scripts/switch_to_live.py --swap-only  # swap a staging build already made

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
6. Validates the result against the portal's own counts. If anything is off,
   it stops and leaves the staging build for inspection; nothing is swapped.
7. Moves the CSV database to data/astra.db.bak-prelive-<timestamp> and puts
   the live one in its place.

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

#: Live corpus may trail the portal by works entered during the ~20 min sweep,
#: plus the one id-less summary row per report. Anything beyond this means
#: shards are missing, not that time passed.
COUNT_TOLERANCE = 0.005
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

    log("validating against the portal's own counts")
    for house, label in ((2, "LS"), (1, "RS")):
        portal = client.watermark(f"0,0,0,{house}").counts.get("Works Recommended") or 0
        stored = int((works["house"] == label).sum())
        gap = abs(portal - stored) / max(portal, 1)
        check(gap <= COUNT_TOLERANCE,
              f"{label}: stored {stored:,} vs portal {portal:,} "
              f"({gap:.2%} apart, tolerance {COUNT_TOLERANCE:.1%})")

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


# ------------------------------------------------------------------------ swap
def swap() -> Path | None:
    if not STAGING_DB.exists():
        raise SystemExit("no staging build to swap in; run without --swap-only")
    for sidecar in ("-wal", "-shm"):
        if Path(str(STAGING_DB) + sidecar).exists():
            raise SystemExit(f"staging database still has a {sidecar} file open; "
                             "close whatever is using it and retry --swap-only")
    if not file_is_free(REAL_DB):
        raise SystemExit("data/astra.db is open in another process (the API or "
                         "dev server?). Stop it, then run: "
                         "python scripts/switch_to_live.py --swap-only")

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    kept = None
    if REAL_DB.exists():
        kept = REAL_DB.with_name(f"astra.db.bak-prelive-{stamp}")
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
        shutil.copy2(report, REAL_PROCESSED / report.name)
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
        log(f"swapped in. CSV database kept as {kept.name if kept else 'n/a'}")
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

    report_copy = json.dumps(report, indent=2, default=str)
    kept = swap()
    (REAL_PROCESSED / "switch_report.json").write_text(report_copy, encoding="utf-8")
    log(f"done in {report['seconds']:.0f}s. data/astra.db is now the live corpus; "
        f"the CSV database is kept as data/{kept.name if kept else 'n/a'}")
    log("keep it current with:  python -m astra.ingestion.poller")
    return 0


if __name__ == "__main__":
    sys.exit(main())
