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
CREATE INDEX IF NOT EXISTS ix_flags_state ON flags(state);
CREATE INDEX IF NOT EXISTS ix_flags_status ON flags(review_status);
CREATE INDEX IF NOT EXISTS ix_flags_district ON flags(district);
CREATE INDEX IF NOT EXISTS ix_flags_constituency ON flags(constituency);
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
        for table in ("works", "fundflows", "flags", "provenance"):
            have = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
            want = _expected_columns(table)
            if have and set(have) != set(want):
                con.execute(f"DROP TABLE {table}")
                con.executescript(SCHEMA)
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
