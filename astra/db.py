"""SQLite persistence layer (stdlib sqlite3 + pandas; zero extra deps).

Tables:
  works            — canonical work records
  fundflows        — constituency x FY release/expenditure rows
  flags            — orchestrator output (findings + narrative + tier views as JSON)
  feedback         — human review actions on flags (the feedback loop)
  provenance       — one row per ingestion attempt
  shards           — the pollable slices of the eSAKSHI portal (live path)
  shard_watermarks — what the portal last told us about each slice (live path)
  work_versions    — append-only log of every observed change to a work
  poller_state     — the poller's schedule: when each periodic job last
                     succeeded, so a restart or a sleep catches up

Two distinct write paths
------------------------
`replace_df()` is the CSV/batch path: DELETE then append, whole table at a
time. Correct for a full rebuild and **fatal to incremental ingestion** — it
must never be used by the live poller.

`upsert_works()` is the live path: INSERT ... ON CONFLICT DO UPDATE, one shard
per transaction, safe to re-run. Together with `record_versions()` it keeps
both the current row and its history.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

from .config import DB_PATH
from .schemas import Flag, Work

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY, source TEXT, era TEXT, state TEXT, district TEXT,
    constituency TEXT, mp_name TEXT, house TEXT, category TEXT, description TEXT,
    recommended_date TEXT, sanction_date TEXT, completion_date TEXT, status TEXT,
    estimated_cost REAL, sanctioned_amount REAL, expenditure REAL,
    ia_name TEXT, vendor_name TEXT, work_type TEXT, total_paid REAL,
    payment_count INTEGER, last_payment_date TEXT, payment_status TEXT,
    is_sc_constituency INTEGER, is_st_constituency INTEGER,
    lat REAL, lon REAL, fy TEXT
);
CREATE TABLE IF NOT EXISTS fundflows (
    row_id TEXT PRIMARY KEY, source TEXT, era TEXT, state TEXT, constituency TEXT,
    mp_name TEXT, house TEXT, fy TEXT, entitlement REAL, released REAL,
    expenditure REAL, utilization_pct REAL, sc_expenditure REAL, st_expenditure REAL,
    recommended REAL, sanctioned REAL, works_count INTEGER
);
CREATE TABLE IF NOT EXISTS flags (
    flag_id TEXT PRIMARY KEY, entity_type TEXT, entity_id TEXT, entity_label TEXT,
    state TEXT, district TEXT, constituency TEXT, era TEXT,
    risk_score REAL, alert INTEGER, findings_json TEXT, narrative TEXT,
    tier_views_json TEXT, review_status TEXT DEFAULT 'pending',
    reviewer_note TEXT, created_at TEXT,
    display_title TEXT, primary_signal TEXT, tier_briefs_json TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flag_id TEXT, action TEXT,
    authority_tier TEXT, note TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, mode TEXT, table_name TEXT,
    rows INTEGER, status TEXT, detail TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS shards (
    shard_id TEXT PRIMARY KEY, house INTEGER, state_id INTEGER,
    constituency_id INTEGER, state_name TEXT, constituency_name TEXT,
    enumerated_at TEXT
);
CREATE TABLE IF NOT EXISTS shard_watermarks (
    shard_id TEXT PRIMARY KEY, signature_json TEXT, n_records INTEGER,
    total_amount REAL, payload_sha256 TEXT, lifecycle TEXT,
    n_stored INTEGER, count_matched INTEGER,
    checked_at TEXT, fetched_at TEXT, last_ok TEXT,
    consecutive_failures INTEGER DEFAULT 0, stale_since TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS work_versions (
    work_id TEXT, observed_at TEXT, field TEXT, old_value TEXT, new_value TEXT,
    shard_id TEXT, PRIMARY KEY (work_id, observed_at, field)
);
CREATE TABLE IF NOT EXISTS poller_state (
    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_flags_state ON flags(state);
CREATE INDEX IF NOT EXISTS ix_flags_status ON flags(review_status);
CREATE INDEX IF NOT EXISTS ix_flags_district ON flags(district);
CREATE INDEX IF NOT EXISTS ix_flags_constituency ON flags(constituency);
CREATE INDEX IF NOT EXISTS ix_flags_score ON flags(risk_score);
CREATE INDEX IF NOT EXISTS ix_works_state ON works(state);
CREATE INDEX IF NOT EXISTS ix_works_constituency ON works(constituency);
CREATE INDEX IF NOT EXISTS ix_wm_lifecycle ON shard_watermarks(lifecycle);
CREATE INDEX IF NOT EXISTS ix_work_versions_work ON work_versions(work_id);
"""

#: Tables `init_db()` may rebuild when their columns drift from SCHEMA. The
#: live-ingestion tables are deliberately absent: `work_versions` holds history
#: that cannot be re-fetched from anywhere, and dropping `shard_watermarks`
#: would blind the change detector until the next full sweep. Migrate those by
#: hand if their shape ever has to change.
_REBUILDABLE = ("works", "fundflows", "flags", "provenance")


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _expected_columns(table: str) -> list[str]:
    """Column names declared for `table` in SCHEMA."""
    body = SCHEMA.split(f"CREATE TABLE IF NOT EXISTS {table} (")[1].split(");")[0]
    cols = []
    for part in body.split(","):
        tok = part.strip().split()
        if tok and tok[0].upper() not in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK"):
            cols.append(tok[0])
    return cols


_SCHEMA_READY = False


def init_db(force: bool = False) -> None:
    """Create tables, and rebuild any table whose columns drifted from SCHEMA.

    Memoised per process: this used to run a PRAGMA sweep on every query, which
    dominated the cost of the dashboard's small, frequent reads.

    Data tables are fully re-ingested on every run, so dropping a stale table is
    safe and keeps schema evolution frictionless during the build. `feedback` is
    preserved because it holds human review decisions.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with connect() as con:
        con.executescript(SCHEMA)
        for table in _REBUILDABLE:
            have = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
            want = _expected_columns(table)
            if have and set(have) != set(want):
                con.execute(f"DROP TABLE {table}")
                con.executescript(SCHEMA)
        # Write-ahead logging lets the API keep reading while the poller writes.
        # It is a persistent property of the file, so setting it once is enough.
        #
        # WAL keeps a `-wal` sidecar next to the database. A cloud-sync client
        # (OneDrive, Dropbox, Google Drive) can lock or half-upload that file, so
        # keep the database outside any synced folder, or set ASTRA_SQLITE_WAL=0
        # to stay on the rollback journal. A failure here is not fatal: the
        # previous journal mode still works.
        if os.environ.get("ASTRA_SQLITE_WAL", "1") != "0":
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
    _SCHEMA_READY = True


def replace_df(table: str, df: pd.DataFrame) -> int:
    init_db(force=True)
    with connect() as con:
        con.execute(f"DELETE FROM {table}")
        df.to_sql(table, con, if_exists="append", index=False)
    return len(df)


def read_df(table: str, where: str = "", params: tuple = ()) -> pd.DataFrame:
    init_db()
    with connect() as con:
        q = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
        return pd.read_sql_query(q, con, params=params)


# ===================================================================== live path
#: Canonical `works` columns, in the order SCHEMA declares them.
WORK_COLUMNS = tuple(Work.model_fields)

#: Fields whose change is worth remembering. Excludes the identity column and
#: the two that are always empty, so the log stays about substance: money
#: moving, dates shifting, vendors and agencies changing, status regressing.
VERSIONED_FIELDS = tuple(
    c for c in WORK_COLUMNS if c not in ("work_id", "lat", "lon", "source")
)


def _as_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def upsert_works(works: list[Work], *, shard_id: str | None = None,
                 record_history: bool = True) -> dict:
    """Insert or update `works` by `work_id`, in ONE transaction.

    Idempotent: re-running with identical input writes no new rows and logs no
    versions. Returns counts of what happened, which is what the caller records
    on the shard watermark.

    This is the live path. It never deletes, so a short or truncated response
    cannot remove records — only `validate.guard_zero` plus two agreeing full
    sweeps may conclude a work is really gone.
    """
    if not works:
        return {"written": 0, "inserted": 0, "changed": 0, "versions": 0}
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    cols = ", ".join(WORK_COLUMNS)
    placeholders = ", ".join("?" * len(WORK_COLUMNS))
    assignments = ", ".join(f"{c}=excluded.{c}" for c in WORK_COLUMNS
                            if c != "work_id")
    sql = (f"INSERT INTO works ({cols}) VALUES ({placeholders}) "
           f"ON CONFLICT(work_id) DO UPDATE SET {assignments}")

    ids = [w.work_id for w in works]
    with connect() as con:
        existing: dict[str, sqlite3.Row] = {}
        for chunk_start in range(0, len(ids), 500):          # SQLite param limit
            chunk = ids[chunk_start:chunk_start + 500]
            marks = ", ".join("?" * len(chunk))
            for row in con.execute(
                    f"SELECT {cols} FROM works WHERE work_id IN ({marks})", chunk):
                existing[row["work_id"]] = row

        versions: list[tuple] = []
        changed = 0
        for work in works:
            before = existing.get(work.work_id)
            if before is None:
                continue
            diffs = []
            for field in VERSIONED_FIELDS:
                old, new = _as_text(before[field]), _as_text(getattr(work, field))
                if old != new:
                    diffs.append((work.work_id, now, field, old, new, shard_id))
            if diffs:
                changed += 1
                if record_history:
                    versions.extend(diffs)

        con.executemany(sql, [tuple(getattr(w, c) for c in WORK_COLUMNS)
                              for w in works])
        if versions:
            con.executemany(
                "INSERT OR IGNORE INTO work_versions "
                "(work_id, observed_at, field, old_value, new_value, shard_id) "
                "VALUES (?, ?, ?, ?, ?, ?)", versions)

    return {"written": len(works), "inserted": len(works) - len(existing),
            "changed": changed, "versions": len(versions)}


def work_versions(work_id: str) -> list[dict]:
    """The observed history of one work, oldest first."""
    init_db()
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT observed_at, field, old_value, new_value, shard_id "
            "FROM work_versions WHERE work_id = ? ORDER BY observed_at", (work_id,))]


# ------------------------------------------------------------------- shards
def save_shards(shards: list[dict]) -> int:
    """Replace the shard registry. Re-enumerated from the portal, never guessed."""
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with connect() as con:
        con.executemany(
            "INSERT INTO shards (shard_id, house, state_id, constituency_id, "
            "state_name, constituency_name, enumerated_at) "
            "VALUES (:shard_id, :house, :state_id, :constituency_id, "
            ":state_name, :constituency_name, :enumerated_at) "
            "ON CONFLICT(shard_id) DO UPDATE SET "
            "state_name=excluded.state_name, "
            "constituency_name=excluded.constituency_name, "
            "enumerated_at=excluded.enumerated_at",
            [{**s, "enumerated_at": now} for s in shards])
    return len(shards)


def load_shards(house: int | None = None) -> list[dict]:
    init_db()
    where, params = ("WHERE house = ?", (house,)) if house is not None else ("", ())
    with connect() as con:
        return [dict(r) for r in con.execute(
            f"SELECT * FROM shards {where} ORDER BY state_id, constituency_id",
            params)]


def works_in_shard(shard_id: str) -> set[str]:
    """Work ids currently stored for a shard, resolved through the registry.

    `works` carries names rather than portal ids, so the join goes through
    `shards`. Lok Sabha shards are one constituency; Rajya Sabha shards are a
    whole state, since RS members are not tied to a constituency.
    """
    init_db()
    with connect() as con:
        shard = con.execute("SELECT * FROM shards WHERE shard_id = ?",
                            (shard_id,)).fetchone()
        if shard is None:
            return set()
        if shard["house"] == 1:              # Rajya Sabha: state-wide
            rows = con.execute(
                "SELECT work_id FROM works WHERE upper(state) = upper(?) "
                "AND house = 'RS'", (shard["state_name"],))
        else:
            rows = con.execute(
                "SELECT work_id FROM works WHERE upper(state) = upper(?) "
                "AND upper(constituency) = upper(?)",
                (shard["state_name"], shard["constituency_name"]))
        return {r["work_id"] for r in rows}


# -------------------------------------------------------------- watermarks
def get_watermark(shard_id: str) -> dict | None:
    init_db()
    with connect() as con:
        row = con.execute("SELECT * FROM shard_watermarks WHERE shard_id = ?",
                          (shard_id,)).fetchone()
        return dict(row) if row else None


def save_watermark(shard_id: str, *, signature: object = None,
                   n_records: int | None = None, total_amount: float | None = None,
                   payload_sha256: str | None = None, lifecycle: str = "IDLE",
                   n_stored: int | None = None, count_matched: bool | None = None,
                   fetched: bool = False) -> None:
    """Record what the portal told us about a shard, and how it went.

    A successful call clears `consecutive_failures` and `stale_since`; that is
    the only thing that does. `checked_at` moves on every watermark read,
    `fetched_at` only when records were actually pulled.
    """
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "shard_id": shard_id,
        "signature_json": json.dumps(signature, default=str) if signature is not None
        else None,
        "n_records": n_records,
        "total_amount": total_amount,
        "payload_sha256": payload_sha256,
        "lifecycle": lifecycle,
        "n_stored": n_stored,
        "count_matched": None if count_matched is None else int(count_matched),
        "checked_at": now,
        "fetched_at": now if fetched else None,
        "last_ok": now,
    }
    with connect() as con:
        con.execute(
            "INSERT INTO shard_watermarks (shard_id, signature_json, n_records, "
            "total_amount, payload_sha256, lifecycle, n_stored, count_matched, "
            "checked_at, fetched_at, last_ok, consecutive_failures, stale_since, "
            "last_error) VALUES (:shard_id, :signature_json, :n_records, "
            ":total_amount, :payload_sha256, :lifecycle, :n_stored, :count_matched, "
            ":checked_at, :fetched_at, :last_ok, 0, NULL, NULL) "
            "ON CONFLICT(shard_id) DO UPDATE SET "
            "signature_json=excluded.signature_json, n_records=excluded.n_records, "
            "total_amount=excluded.total_amount, "
            "payload_sha256=excluded.payload_sha256, "
            "lifecycle=excluded.lifecycle, "
            "n_stored=COALESCE(excluded.n_stored, shard_watermarks.n_stored), "
            "count_matched=COALESCE(excluded.count_matched, "
            "                       shard_watermarks.count_matched), "
            "checked_at=excluded.checked_at, "
            "fetched_at=COALESCE(excluded.fetched_at, shard_watermarks.fetched_at), "
            "last_ok=excluded.last_ok, consecutive_failures=0, "
            "stale_since=NULL, last_error=NULL", payload)


def mark_shard_failure(shard_id: str, error: str, *,
                       lifecycle: str = "QUARANTINED") -> int:
    """Increment the failure counter and start the staleness clock.

    Returns the new consecutive failure count so the caller can escalate. The
    previously stored records are left exactly as they were — a shard we cannot
    read is stale, not empty.
    """
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with connect() as con:
        con.execute(
            "INSERT INTO shard_watermarks (shard_id, lifecycle, checked_at, "
            "consecutive_failures, stale_since, last_error) "
            "VALUES (?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(shard_id) DO UPDATE SET "
            "lifecycle=excluded.lifecycle, checked_at=excluded.checked_at, "
            "consecutive_failures=shard_watermarks.consecutive_failures + 1, "
            "stale_since=COALESCE(shard_watermarks.stale_since, excluded.stale_since), "
            "last_error=excluded.last_error",
            (shard_id, lifecycle, now, now, error[:300]))
        row = con.execute("SELECT consecutive_failures FROM shard_watermarks "
                          "WHERE shard_id = ?", (shard_id,)).fetchone()
    return int(row["consecutive_failures"]) if row else 1


def get_state(key: str) -> str | None:
    """A value from the poller's persistent schedule, or None if never set."""
    init_db()
    with connect() as con:
        row = con.execute("SELECT value FROM poller_state WHERE key = ?",
                          (key,)).fetchone()
    return row["value"] if row else None


def set_state(key: str, value: str) -> None:
    """Record a schedule value. Kept in the database, not in process memory,
    so a restart, a crash or a sleeping laptop does not forget it."""
    init_db()
    with connect() as con:
        con.execute(
            "INSERT INTO poller_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, datetime.now(timezone.utc).isoformat()))


def stale_shards(min_failures: int = 3) -> list[dict]:
    """Shards that have failed repeatedly — the escalation list.

    A slice missing for days must never be quietly excluded from the freshness
    figure; it has to degrade it. This is what `/meta/freshness` reports and
    what the dashboard banner names.
    """
    init_db()
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT w.shard_id, w.consecutive_failures, w.stale_since, "
            "       w.last_error, w.lifecycle, w.fetched_at, "
            "       s.state_name, s.constituency_name, s.house "
            "FROM shard_watermarks w LEFT JOIN shards s USING (shard_id) "
            "WHERE w.consecutive_failures >= ? "
            "ORDER BY w.consecutive_failures DESC, w.stale_since", (min_failures,))]


def watermark_summary() -> dict:
    """Aggregate freshness. Staleness lowers the numbers, never hides in them."""
    init_db()
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS shards, "
            "  SUM(CASE WHEN lifecycle = 'QUARANTINED' THEN 1 ELSE 0 END) AS quarantined, "
            "  SUM(CASE WHEN count_matched = 0 THEN 1 ELSE 0 END) AS mismatched, "
            "  SUM(CASE WHEN count_matched = 1 THEN 1 ELSE 0 END) AS reconciled, "
            "  MIN(fetched_at) AS oldest_fetch, MAX(fetched_at) AS newest_fetch "
            "FROM shard_watermarks").fetchone()
        registry = con.execute("SELECT COUNT(*) AS n FROM shards").fetchone()["n"]
    out = dict(row) if row else {}
    out["registered_shards"] = registry
    out["never_fetched"] = max(0, registry - (out.get("shards") or 0))
    return out


def save_flags(flags: list[Flag]) -> int:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            f.flag_id, f.entity_type, f.entity_id, f.entity_label, f.state,
            f.district, f.constituency, f.era, f.risk_score, int(f.alert),
            json.dumps([fd.model_dump() for fd in f.findings]),
            f.narrative, json.dumps(f.tier_views), f.review_status,
            f.reviewer_note, f.created_at or now,
            f.display_title, f.primary_signal, json.dumps(f.tier_briefs),
        )
        for f in flags
    ]
    with connect() as con:
        con.execute("DELETE FROM flags")
        con.executemany(
            "INSERT OR REPLACE INTO flags VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
    return len(rows)


def load_flags(where: str = "", params: tuple = ()) -> list[dict]:
    df = read_df("flags", where, params)
    out = []
    for _, r in df.iterrows():
        d = r.to_dict()
        d["findings"] = json.loads(d.pop("findings_json") or "[]")
        d["tier_views"] = json.loads(d.pop("tier_views_json") or "{}")
        d["tier_briefs"] = json.loads(d.pop("tier_briefs_json") or "{}")
        d["alert"] = bool(d["alert"])
        out.append(d)
    out.sort(key=lambda x: -x["risk_score"])
    return out


def record_feedback(flag_id: str, action: str, tier: str, note: str = "") -> None:
    """Human-in-the-loop: store review outcome and update the flag status.

    false_positive feedback is the retraining signal for threshold tuning
    (stage-2 roadmap: per-rule threshold recalibration from FP rates).
    """
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with connect() as con:
        con.execute(
            "INSERT INTO feedback (flag_id, action, authority_tier, note, created_at) VALUES (?,?,?,?,?)",
            (flag_id, action, tier, note, now),
        )
        con.execute(
            "UPDATE flags SET review_status=?, reviewer_note=? WHERE flag_id=?",
            (action, note, flag_id),
        )


def feedback_stats() -> pd.DataFrame:
    init_db()
    with connect() as con:
        return pd.read_sql_query(
            "SELECT action, authority_tier, COUNT(*) n FROM feedback GROUP BY action, authority_tier",
            con,
        )


def record_provenance(entries: list[dict]) -> None:
    """Record which source/mode produced the current data batch.

    Surfaced in the API and dashboard so an evaluator can always see whether a
    given batch came from live official ingestion or the offline official CSVs.
    """
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with connect() as con:
        con.execute("DELETE FROM provenance")
        con.executemany(
            "INSERT INTO provenance (source, mode, table_name, rows, status, detail, fetched_at)"
            " VALUES (?,?,?,?,?,?,?)",
            [(e.get("source"), e.get("mode"), e.get("table"), int(e.get("rows") or 0),
              e.get("status"), e.get("detail"), e.get("fetched_at") or now) for e in entries],
        )


def load_provenance() -> pd.DataFrame:
    init_db()
    with connect() as con:
        return pd.read_sql_query("SELECT * FROM provenance ORDER BY id", con)


# --------------------------------------------------------------- UI queries
# The dashboard must stay responsive over ~36k flags. These helpers filter and
# paginate in SQL rather than materialising every flag in Python on each click.

def flag_facets() -> dict:
    """Distinct filter values + counts, for populating UI controls cheaply."""
    init_db()
    with connect() as con:
        def col(name: str) -> list[str]:
            rows = con.execute(
                f"SELECT DISTINCT {name} FROM flags WHERE {name} IS NOT NULL "
                f"AND {name} != '' ORDER BY {name}").fetchall()
            return [r[0] for r in rows]
        counts = dict(con.execute(
            "SELECT review_status, COUNT(*) FROM flags GROUP BY review_status").fetchall())
        total = con.execute("SELECT COUNT(*) FROM flags").fetchone()[0]
        alerts = con.execute("SELECT COUNT(*) FROM flags WHERE alert=1").fetchone()[0]
        high = con.execute("SELECT COUNT(*) FROM flags WHERE risk_score>=70").fetchone()[0]
        return {"states": col("state"), "districts": col("district"),
                "constituencies": col("constituency"),
                "status_counts": counts, "total": total,
                "alerts": alerts, "high": high}


def _flag_filters(states=None, districts=None, constituencies=None,
                  entity_types=None, statuses=None, rule_ids=None,
                  min_score: float = 0, search: str = "") -> tuple[str, list]:
    """Shared WHERE clause for the flag queries, so list and count always agree."""
    where, params = ["risk_score >= ?"], [float(min_score)]

    def inset(col: str, vals):
        if vals:
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            params.extend(list(vals))

    inset("state", states)
    inset("district", districts)
    inset("constituency", constituencies)
    inset("entity_type", entity_types)
    inset("review_status", statuses)
    if search:
        where.append("(display_title LIKE ? OR entity_id LIKE ? OR entity_label LIKE ?"
                     " OR primary_signal LIKE ?)")
        params.extend([f"%{search}%"] * 4)
    if rule_ids:
        where.append("(" + " OR ".join(["findings_json LIKE ?"] * len(rule_ids)) + ")")
        params.extend([f'%"{r}"%' for r in rule_ids])
    return " AND ".join(where), params


def query_flags(order: str = "risk", limit: int = 200, offset: int = 0,
                **filters) -> list[dict]:
    """Filtered, ordered page of flags. Same dict shape as load_flags()."""
    init_db()
    clause, params = _flag_filters(**filters)
    order_sql = {"risk": "risk_score DESC",
                 "risk_asc": "risk_score ASC",
                 "recent": "created_at DESC",
                 "state": "state ASC, risk_score DESC"}.get(order, "risk_score DESC")
    sql = f"SELECT * FROM flags WHERE {clause} ORDER BY {order_sql} LIMIT ? OFFSET ?"
    with connect() as con:
        rows = con.execute(sql, params + [int(limit), int(offset)]).fetchall()
    return [_hydrate(dict(r)) for r in rows]


def count_flags(**filters) -> int:
    """Row count for the same filters as query_flags, counted in SQL."""
    init_db()
    for k in ("limit", "offset", "order"):
        filters.pop(k, None)
    clause, params = _flag_filters(**filters)
    with connect() as con:
        return con.execute(
            f"SELECT COUNT(*) FROM flags WHERE {clause}", params).fetchone()[0]


def get_flag(flag_id: str) -> dict | None:
    init_db()
    with connect() as con:
        row = con.execute("SELECT * FROM flags WHERE flag_id=?", (flag_id,)).fetchone()
    return _hydrate(dict(row)) if row else None


def _hydrate(d: dict) -> dict:
    d["findings"] = json.loads(d.pop("findings_json", None) or "[]")
    d["tier_views"] = json.loads(d.pop("tier_views_json", None) or "{}")
    d["tier_briefs"] = json.loads(d.pop("tier_briefs_json", None) or "{}")
    d["alert"] = bool(d.get("alert"))
    return d


def rule_histogram(states=None) -> dict[str, int]:
    """How often each rule appears across flags (optionally scoped to states)."""
    init_db()
    sql = "SELECT findings_json FROM flags"
    params: list = []
    if states:
        sql += f" WHERE state IN ({','.join('?' * len(states))})"
        params = list(states)
    counts: dict[str, int] = {}
    with connect() as con:
        for (fj,) in con.execute(sql, params):
            for f in json.loads(fj or "[]"):
                counts[f["rule_id"]] = counts.get(f["rule_id"], 0) + 1
    return counts


def state_risk_summary() -> pd.DataFrame:
    """Per-state flag counts and mean risk - drives the overview visualisations."""
    init_db()
    with connect() as con:
        return pd.read_sql_query(
            "SELECT state, COUNT(*) AS flags, "
            "SUM(CASE WHEN risk_score>=70 THEN 1 ELSE 0 END) AS high_risk, "
            "SUM(alert) AS alerts, ROUND(AVG(risk_score),1) AS avg_risk "
            "FROM flags WHERE state IS NOT NULL GROUP BY state ORDER BY high_risk DESC",
            con)


def district_risk_summary(state: str | None = None, limit: int = 25) -> pd.DataFrame:
    init_db()
    sql = ("SELECT state, district, COUNT(*) AS flags, "
           "SUM(CASE WHEN risk_score>=70 THEN 1 ELSE 0 END) AS high_risk, "
           "ROUND(AVG(risk_score),1) AS avg_risk FROM flags "
           "WHERE district IS NOT NULL")
    params: list = []
    if state:
        sql += " AND state = ?"
        params.append(state)
    sql += " GROUP BY state, district ORDER BY high_risk DESC, flags DESC LIMIT ?"
    params.append(int(limit))
    with connect() as con:
        return pd.read_sql_query(sql, con, params=params)


def flag_stats(**filters) -> dict:
    """Headline counts + risk-band distribution, aggregated in SQL.

    The overview must not materialise tens of thousands of flags just to show
    five numbers, so every figure here is a COUNT over an indexed table.
    """
    init_db()
    for k in ("limit", "offset", "order"):
        filters.pop(k, None)
    clause, params = _flag_filters(**filters)
    with connect() as con:
        row = con.execute(
            f"SELECT COUNT(*), "
            f"SUM(CASE WHEN risk_score>=70 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN risk_score>=40 AND risk_score<70 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN risk_score<40 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN review_status='under_review' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN review_status IN ('confirmed','false_positive') "
            f"THEN 1 ELSE 0 END) "
            f"FROM flags WHERE {clause}", params).fetchone()
    total, high, med, low, reviewing, closed = [int(x or 0) for x in row]
    return {"total": total, "high": high, "medium": med, "low": low,
            "under_review": reviewing, "closed": closed,
            "bands": {"High risk": high, "Medium risk": med, "Low risk": low}}


def rule_histogram_scoped(**filters) -> dict[str, int]:
    """Rule frequency across the flags matching the current filters."""
    init_db()
    for k in ("limit", "offset", "order"):
        filters.pop(k, None)
    clause, params = _flag_filters(**filters)
    counts: dict[str, int] = {}
    with connect() as con:
        for (fj,) in con.execute(
                f"SELECT findings_json FROM flags WHERE {clause}", params):
            for f in json.loads(fj or "[]"):
                counts[f["rule_id"]] = counts.get(f["rule_id"], 0) + 1
    return counts
