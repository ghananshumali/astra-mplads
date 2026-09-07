"""Normalize heterogeneous MPLADS sources into the canonical schema.

Each source is a declarative spec (config/sources.yaml): where it comes
from, whether it yields work-level or fundflow rows, and a field map from
source columns -> canonical columns. Adding a source is config, not code.

Era tagging happens here: every row gets pre2023/post2023 from its best
available date (or the FY when only that exists) so downstream baselines
never straddle the eSAKSHI cutover.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pandas as pd
import yaml

from ..config import PROJECT_ROOT, load_rules
from . import datagov

SOURCES_PATH = PROJECT_ROOT / "config" / "sources.yaml"

_num_re = re.compile(r"[^\d.\-]")


def _to_num(s):
    if pd.isna(s):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = _num_re.sub("", str(s))
    try:
        return float(t) if t not in ("", "-", ".") else None
    except ValueError:
        return None


def _era_from_fy(fy: str | None, cutoff: str) -> str:
    """FY strings like '2018-19', '2018-2019', '2018'. Compare FY start year."""
    if not fy:
        return "unknown"
    m = re.search(r"(19|20)\d{2}", str(fy))
    if not m:
        return "unknown"
    start_year = int(m.group(0))
    cutoff_year = int(cutoff[:4])
    return "post2023" if start_year >= cutoff_year else "pre2023"


def load_sources() -> list[dict]:
    if not SOURCES_PATH.exists():
        return []
    with open(SOURCES_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return [s for s in cfg.get("sources", []) if s.get("enabled", True)]


def _fetch(spec: dict) -> pd.DataFrame:
    kind = spec["fetch"]
    if kind == "datagov":
        return datagov.fetch_resource(spec["resource_id"], spec.get("max_records", 50000))
    if kind == "csv_url":
        import requests, io
        from ..config import RAW_DIR
        cache = RAW_DIR / f"csv_{hashlib.sha1(spec['url'].encode()).hexdigest()[:10]}.csv"
        if not cache.exists():
            r = requests.get(spec["url"], timeout=120)
            r.raise_for_status()
            cache.write_bytes(r.content)
        return pd.read_csv(cache)
    if kind == "local_csv":
        return pd.read_csv(PROJECT_ROOT / spec["path"])
    raise ValueError(f"unknown fetch kind {kind}")


def normalize_source(spec: dict) -> tuple[str, pd.DataFrame]:
    """Returns (table_name, canonical dataframe)."""
    cutoff = load_rules()["era"]["cutoff_date"]
    raw = _fetch(spec)
    raw.columns = [c.strip().lower() for c in raw.columns]
    fmap = {k.lower(): v for k, v in spec["field_map"].items()}
    df = pd.DataFrame({canon: raw[src] for src, canon in fmap.items() if src in raw.columns})

    for col in ("estimated_cost", "sanctioned_amount", "expenditure", "entitlement",
                "released", "utilization_pct", "sc_expenditure", "st_expenditure",
                "lat", "lon"):
        if col in df.columns:
            df[col] = df[col].map(_to_num)
    # unit scaling (many MoSPI tables are in lakh / crore)
    scale = float(spec.get("amount_scale", 1))
    if scale != 1:
        for col in ("estimated_cost", "sanctioned_amount", "expenditure",
                    "entitlement", "released", "sc_expenditure", "st_expenditure"):
            if col in df.columns:
                df[col] = df[col] * scale

    for col in ("state", "district", "constituency", "mp_name", "category",
                "description", "ia_name", "house", "status", "fy"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().replace({"nan": None, "": None})
    if "state" in df.columns:
        df["state"] = df["state"].str.upper()

    df["source"] = spec["name"]

    # era tag: prefer a real date, fall back to FY
    date_col = next((c for c in ("sanction_date", "recommended_date", "completion_date")
                     if c in df.columns), None)
    if date_col:
        parsed = pd.to_datetime(df[date_col], errors="coerce", dayfirst=True)
        df[date_col] = parsed.dt.strftime("%Y-%m-%d")
        df["era"] = parsed.dt.strftime("%Y-%m-%d").map(
            lambda d: "unknown" if pd.isna(d) else ("post2023" if d >= cutoff else "pre2023"))
    elif "fy" in df.columns:
        df["era"] = df["fy"].map(lambda v: _era_from_fy(v, cutoff))
    else:
        df["era"] = spec.get("era_default", "unknown")

    table = "works" if spec["kind"] == "works" else "fundflows"
    idcol = "work_id" if table == "works" else "row_id"
    if idcol not in df.columns:
        basis = df.astype(str).agg("|".join, axis=1)
        df[idcol] = [f"{spec['name']}-{hashlib.sha1(b.encode()).hexdigest()[:10]}"
                     for b in basis]
    else:
        df[idcol] = spec["name"] + "-" + df[idcol].astype(str)
    df = df.drop_duplicates(subset=[idcol])

    # utilization derivation for fundflows
    if table == "fundflows" and "utilization_pct" not in df.columns and \
            {"released", "expenditure"}.issubset(df.columns):
        rel = pd.to_numeric(df["released"], errors="coerce")
        df["utilization_pct"] = (100 * pd.to_numeric(df["expenditure"], errors="coerce")
                                 / rel.where(rel > 0)).round(1)

    return table, df


def run_ingestion(verbose: bool = True) -> dict:
    from .. import db
    from ..schemas import FundFlow, Work

    frames: dict[str, list[pd.DataFrame]] = {"works": [], "fundflows": []}
    report = {}
    for spec in load_sources():
        try:
            table, df = normalize_source(spec)
            frames[table].append(df)
            report[spec["name"]] = {"table": table, "rows": len(df)}
            if verbose:
                print(f"[ingest] {spec['name']}: {len(df)} rows -> {table}")
        except Exception as exc:  # keep going: one broken source must not kill the demo
            report[spec["name"]] = {"error": str(exc)[:200]}
            if verbose:
                print(f"[ingest] {spec['name']} FAILED: {exc}")

    canon_cols = {"works": list(Work.model_fields), "fundflows": list(FundFlow.model_fields)}
    for table, dfs in frames.items():
        if not dfs:
            continue
        merged = pd.concat(dfs, ignore_index=True)
        for col in canon_cols[table]:
            if col not in merged.columns:
                merged[col] = None
        merged = merged[canon_cols[table]]
        n = db.replace_df(table, merged)
        report[f"_{table}_total"] = n
        if verbose:
            print(f"[ingest] {table}: {n} rows persisted")
    return report
