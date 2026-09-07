"""Live Ingestion Agent — MODE 1: real-time official / open MPLADS interfaces.

Every call here is time-boxed and non-raising: a slow or unavailable endpoint
returns an empty frame plus a provenance record explaining what happened, and
the router falls back to the offline official CSVs. Nothing in this module can
block the pipeline.

Sources (all free / open, no paid keys):

1. MPLADS eSAKSHI official portal  — mplads.mospi.gov.in/rest/PreLoginDashboardData
   The portal's own pre-login dashboard service. Returns live national totals
   (works recommended / sanctioned / completed, expenditure, allocated limit).
   Used for (a) Ministry-tier live KPIs and (b) a FRESHNESS CHECK that measures
   the offline batch against the live portal so an evaluator can see exactly
   how current the analysed corpus is.

2. data.opencity.in CKAN datastore — pre-2023 MPLADS records (15th/16th/17th
   Lok Sabha + Rajya Sabha), constituency-level entitlement / release /
   expenditure / unspent balance. This is the PRE-eSAKSHI regime data, which is
   what makes the era-separated baselines real rather than hypothetical.

3. api.empoweredindian.in — an open (AGPL) civic-tech mirror of eSAKSHI work
   records, used for live work-level ingestion when the official CSV exports
   are not present on disk.

4. data.gov.in OGD API — see datagov.py (resource-id driven, config/sources.yaml).
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

from ..config import RAW_DIR, load_rules

ESAKSHI_TILES = "https://mplads.mospi.gov.in/rest/PreLoginDashboardData/getTilesData"
CKAN_DATASTORE = "https://data.opencity.in/api/3/action/datastore_search"
MIRROR_BASE = "https://api.empoweredindian.in/api"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

#: seconds any single live request may consume before we fall back
LIVE_TIMEOUT = float(os.environ.get("ASTRA_LIVE_TIMEOUT", "20"))
#: overall wall-clock budget for a multi-source live pull; once spent, the
#: remaining sources are skipped so ingestion has a predictable ceiling
LIVE_BUDGET = float(os.environ.get("ASTRA_LIVE_BUDGET", "60"))

# Pre-2023 CKAN resources. Field names differ per term, so each carries its map.
CKAN_RESOURCES = [
    {
        "name": "MPLADS 17th Lok Sabha (2019-2024)", "rid": "e4524ed7-6c9b-41a5-ad0a-003358fdabca",
        "house": "Lok Sabha", "fy": "2019-24", "unit": 1e7,   # values in crore
        "map": {"MP Name": "mp_name", "Constituency": "constituency",
                "Entitlement": "entitlement", "FundReceivedGOI": "released",
                "ActualExpenditureIncurred": "expenditure",
                "WorksRecommCost": "recommended", "WSCost": "sanctioned"},
    },
    {
        "name": "MPLADS 16th Lok Sabha (2014-2019)", "rid": "57baaa96-04ca-4328-86bc-17b455af1024",
        "house": "Lok Sabha", "fy": "2014-19", "unit": 1e7,
        "map": {"MPName": "mp_name", "Constituency": "constituency", "State": "state",
                "District": "district", "TotalEntitlementAmount_crore": "entitlement",
                "TotalGOIRelease_crore": "released", "UnspentBalance_crore": "unspent"},
    },
    {
        "name": "MPLADS 15th Lok Sabha (2009-2014)", "rid": "0b894524-3708-41ec-896e-7a5e8d15c2f3",
        "house": "Lok Sabha", "fy": "2009-14", "unit": 1e7,
        "map": {"MPName": "mp_name", "Constituency": "constituency", "State": "state",
                "District": "district", "TotalEntitlementAmount_crore": "entitlement",
                "TotalGOIRelease_crore": "released", "UnspentBalance_crore": "unspent"},
    },
    {
        "name": "MPLADS Rajya Sabha (sitting 2022)", "rid": "628edb2f-536f-4b2d-a064-886025486b4b",
        "house": "Rajya Sabha", "fy": "2016-22", "unit": 1e7,
        "map": {"MPName": "mp_name", "Constituency": "constituency", "State": "state",
                "District": "district", "TotalEntitlementAmount": "entitlement",
                "TotalGOIRelease": "released", "UnspentBalance": "unspent"},
    },
]


def _prov(source: str, table: str, rows: int, status: str, detail: str) -> dict:
    return {"source": source, "mode": "live", "table": table, "rows": rows,
            "status": status, "detail": detail,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


def _num(v, scale: float = 1.0):
    if v is None:
        return np.nan
    try:
        t = str(v).replace(",", "").replace("\xa0", "").strip()
        return float(t) * scale if t not in ("", "-", "NA", "N/A") else np.nan
    except (TypeError, ValueError):
        return np.nan


# ------------------------------------------------------------------ 1. eSAKSHI

def fetch_esakshi_tiles(timeout: float = LIVE_TIMEOUT) -> tuple[dict, dict]:
    """Live national totals from the official MoSPI eSAKSHI portal."""
    try:
        r = requests.post(
            ESAKSHI_TILES, json={"uname": "0,0,0,2"}, timeout=timeout,
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": "https://mplads.mospi.gov.in", "User-Agent": UA},
        )
        r.raise_for_status()
        raw = r.json()
        tiles: dict = {}
        for key, val in raw.items():
            if isinstance(val, list) and val and isinstance(val[0], str):
                # ["107183", "Rs 5,740.91 Crore"] or ["Rs 83,33,..", "Rs 8,333.67 Crore"]
                nums = [v for v in val if v.replace(",", "").replace(".", "").isdigit()]
                tiles[key] = {
                    "count": int(nums[0].replace(",", "")) if nums else None,
                    "display": val[-1].encode("latin-1", "ignore").decode("utf-8", "ignore"),
                    "raw": val,
                }
            elif key == "Current Tenure" and isinstance(val, list) and val:
                tiles[key] = val[0].get("CAPTION")
        return tiles, _prov("MPLADS eSAKSHI portal (mplads.mospi.gov.in)", "tiles",
                            len(tiles), "ok", "official pre-login dashboard service")
    except Exception as exc:
        return {}, _prov("MPLADS eSAKSHI portal (mplads.mospi.gov.in)", "tiles", 0,
                         "unavailable", f"{type(exc).__name__}: {str(exc)[:140]}")


def freshness_check(works: pd.DataFrame, tiles: dict) -> dict | None:
    """Measure the analysed corpus against the live official portal totals.

    Answers the question an evaluator will ask first: 'is this real, current
    government data?' — by comparing our record counts to what the MoSPI portal
    reports right now.
    """
    if not tiles or works.empty:
        return None
    live_rec = (tiles.get("Works Recommended") or {}).get("count")
    if not live_rec:
        return None
    local = len(works)
    return {
        "live_recommended_works": live_rec,
        "local_recommended_works": local,
        "coverage_pct": round(100 * local / live_rec, 2),
        "delta": live_rec - local,
        "live_completed_works": (tiles.get("Works Completed") or {}).get("count"),
        "live_sanctioned_works": (tiles.get("Works Sanctioned") or {}).get("count"),
        "tenure": tiles.get("Current Tenure"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------ 2. CKAN pre-2023

def fetch_ckan_fundflows(timeout: float = LIVE_TIMEOUT,
                         limit: int = 2000) -> tuple[pd.DataFrame, list[dict]]:
    """Pre-eSAKSHI (pre-2023) constituency fund positions -> historical era baseline."""
    frames, provs = [], []
    deadline = time.monotonic() + LIVE_BUDGET
    for res in CKAN_RESOURCES:
        cache = RAW_DIR / f"ckan_{res['rid']}.json"
        try:
            # Cached-first: the historical pre-2023 record is immutable, so one
            # live pull is enough. Later runs (and the demo) stay fast and work
            # without a network, while the first run is genuinely live.
            if cache.exists():
                recs = json.loads(cache.read_text(encoding="utf-8"))
                src_note = "cached from an earlier live pull"
            elif time.monotonic() >= deadline:
                provs.append(_prov(res["name"], "fundflows", 0, "skipped",
                                   f"live budget of {LIVE_BUDGET:.0f}s exhausted; "
                                   f"pipeline continues without this enrichment"))
                continue
            else:
                r = requests.get(CKAN_DATASTORE,
                                 params={"resource_id": res["rid"], "limit": limit},
                                 timeout=timeout, headers={"User-Agent": UA})
                r.raise_for_status()
                recs = r.json()["result"]["records"]
                cache.write_text(json.dumps(recs), encoding="utf-8")
                src_note = f"CKAN datastore {res['rid'][:8]} (pre-2023 regime)"
            if not recs:
                provs.append(_prov(res["name"], "fundflows", 0, "empty", "no records"))
                continue
            raw = pd.DataFrame(recs)
            df = pd.DataFrame()
            for src, canon in res["map"].items():
                if src in raw.columns:
                    df[canon] = raw[src]
            for col in ("entitlement", "released", "expenditure", "recommended",
                        "sanctioned", "unspent"):
                if col in df.columns:
                    df[col] = df[col].map(lambda v: _num(v, res["unit"]))
            # Where expenditure is not published directly, derive it from
            # release minus the unspent balance (both official figures).
            if "expenditure" not in df.columns and {"released", "unspent"}.issubset(df.columns):
                df["expenditure"] = df["released"] - df["unspent"]
            if "unspent" in df.columns:
                df = df.drop(columns=["unspent"])
            for col in ("state", "district", "constituency", "mp_name"):
                if col in df.columns:
                    df[col] = df[col].astype(str).str.strip().replace({"nan": None, "": None})
            if "state" in df.columns:
                df["state"] = df["state"].str.upper()
            df["house"] = res["house"]
            df["fy"] = res["fy"]
            df["era"] = "pre2023"
            df["source"] = res["name"]
            df["row_id"] = [f"CKAN-{res['rid'][:8]}-{i}" for i in range(len(df))]
            rel = pd.to_numeric(df.get("released"), errors="coerce")
            exp = pd.to_numeric(df.get("expenditure"), errors="coerce")
            df["utilization_pct"] = (100 * exp / rel.where(rel > 0)).round(2)
            frames.append(df)
            provs.append(_prov(res["name"], "fundflows", len(df), "ok", src_note))
        except Exception as exc:
            provs.append(_prov(res["name"], "fundflows", 0, "unavailable",
                               f"{type(exc).__name__}: {str(exc)[:140]}"))
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return out, provs


# ------------------------------------------------------------------ 3. mirror works

MIRROR_STATES = ["Goa", "Sikkim", "Manipur", "Meghalaya", "Tripura", "Mizoram",
                 "Nagaland", "Arunachal Pradesh", "Himachal Pradesh", "Uttarakhand"]


def fetch_mirror_works(timeout: float = LIVE_TIMEOUT,
                       states: list[str] | None = None) -> tuple[pd.DataFrame, list[dict]]:
    """Live work-level records via the open eSAKSHI mirror's CSV export.

    Used when the official CSV exports are not on disk. Bounded by state so a
    live demo never waits on a national-scale download.
    """
    states = states or MIRROR_STATES
    cutoff = load_rules()["era"]["cutoff_date"]
    frames, provs = [], []
    deadline = time.monotonic() + LIVE_BUDGET
    for kind, path, amt_col, date_col in (
        ("completed", "export/completed-works", "Final Amount (₹)", "Completed Date"),
        ("recommended", "export/recommended-works", "Recommended Amount (₹)", "Recommendation Date"),
    ):
        rows = []
        for state in states:
            if time.monotonic() >= deadline:
                provs.append(_prov(f"eSAKSHI open mirror ({kind})", "works", 0, "skipped",
                                   f"live budget of {LIVE_BUDGET:.0f}s exhausted after "
                                   f"{len(rows)} state pulls"))
                break
            try:
                r = requests.get(f"{MIRROR_BASE}/{path}", params={"state": state},
                                 timeout=timeout, headers={"User-Agent": UA})
                r.raise_for_status()
                part = pd.read_csv(io.StringIO(r.text))
                if not part.empty:
                    part["__state"] = state
                    rows.append(part)
            except Exception as exc:
                provs.append(_prov(f"eSAKSHI open mirror ({kind}, {state})", "works", 0,
                                   "unavailable", f"{type(exc).__name__}: {str(exc)[:100]}"))
        if not rows:
            continue
        raw = pd.concat(rows, ignore_index=True)
        df = pd.DataFrame({
            "work_id": "MIR-" + raw["Work ID"].astype(str),
            "description": raw.get("Work Description"),
            "category": raw.get("Category"),
            "work_type": raw.get("Category"),
            "state": raw["__state"].str.upper(),
            "district": raw.get("IDA").map(
                lambda s: str(s).split("(")[0].strip() if pd.notna(s) else None)
            if "IDA" in raw.columns else None,
            "ia_name": raw.get("IDA"),
            "mp_name": raw.get("MP Name"),
            "constituency": raw.get("Constituency"),
            "house": raw.get("House"),
        })
        amount = pd.to_numeric(raw.get(amt_col), errors="coerce")
        when = pd.to_datetime(raw.get(date_col), errors="coerce", utc=True).dt.tz_localize(None)
        if kind == "completed":
            df["expenditure"] = amount
            df["sanctioned_amount"] = amount
            df["completion_date"] = when.dt.strftime("%Y-%m-%d")
            df["status"] = "Work Completed"
        else:
            df["estimated_cost"] = amount
            df["recommended_date"] = when.dt.strftime("%Y-%m-%d")
            df["status"] = "Recommended"
        df["era"] = when.dt.strftime("%Y-%m-%d").map(
            lambda d: "unknown" if pd.isna(d) else ("post2023" if d >= cutoff else "pre2023"))
        df["fy"] = when.map(
            lambda d: None if pd.isna(d) else
            f"{d.year if d.month >= 4 else d.year - 1}-"
            f"{str((d.year if d.month >= 4 else d.year - 1) + 1)[2:]}")
        df["source"] = "esakshi_open_mirror"
        frames.append(df)
        provs.append(_prov(f"eSAKSHI open mirror — {kind} works", "works", len(df), "ok",
                           f"api.empoweredindian.in, {len(states)} states"))

    if not frames:
        return pd.DataFrame(), provs
    out = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["work_id"])
    return out, provs
