"""SQLite persistence layer (stdlib sqlite3 + pandas; zero extra deps).

Tables:
  works      — canonical work records
  fundflows  — constituency x FY release/expenditure rows
  flags      — orchestrator output (findings + narrative + tier views as JSON)
  feedback   — human review actions on flags (the feedback loop)
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

from .config import DB_PATH
from .schemas import Flag

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
    reviewer_note TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flag_id TEXT, action TEXT,
    authority_tier TEXT, note TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, mode TEXT, table_name TEXT,
    rows INTEGER, status TEXT, detail TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_flags_state ON flags(state);
CREATE INDEX IF NOT EXISTS ix_flags_score ON flags(risk_score);
CREATE INDEX IF NOT EXISTS ix_works_state ON works(state);
"""


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


def init_db() -> None:
    """Create tables, and rebuild any table whose columns drifted from SCHEMA.

    Data tables are fully re-ingested on every run, so dropping a stale table is
    safe and keeps schema evolution frictionless during the build. `feedback` is
    preserved because it holds human review decisions.
    """
    with connect() as con:
        con.executescript(SCHEMA)
        for table in ("works", "fundflows", "flags", "provenance"):
            have = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
            want = _expected_columns(table)
            if have and set(have) != set(want):
                con.execute(f"DROP TABLE {table}")
                con.executescript(SCHEMA)


def replace_df(table: str, df: pd.DataFrame) -> int:
    init_db()
    with connect() as con:
        con.execute(f"DELETE FROM {table}")
        df.to_sql(table, con, if_exists="append", index=False)
    return len(df)


def read_df(table: str, where: str = "", params: tuple = ()) -> pd.DataFrame:
    init_db()
    with connect() as con:
        q = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
        return pd.read_sql_query(q, con, params=params)


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
        )
        for f in flags
    ]
    with connect() as con:
        con.execute("DELETE FROM flags")
        con.executemany(
            "INSERT OR REPLACE INTO flags VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
    return len(rows)


def load_flags(where: str = "", params: tuple = ()) -> list[dict]:
    df = read_df("flags", where, params)
    out = []
    for _, r in df.iterrows():
        d = r.to_dict()
        d["findings"] = json.loads(d.pop("findings_json") or "[]")
        d["tier_views"] = json.loads(d.pop("tier_views_json") or "{}")
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
