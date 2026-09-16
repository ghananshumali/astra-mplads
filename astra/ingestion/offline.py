"""Offline Ingestion Agent — official MPLADS eSAKSHI CSV exports (`datasets/`).

MODE 2 of the dual-mode ingestion strategy. These are authentic MoSPI/eSAKSHI
exports, used whenever live ingestion is unavailable, slow, or incomplete. The
output is the SAME canonical `works` / `fundflows` schema the live path emits,
so everything downstream (agents, orchestrator, API, dashboard) is unchanged.

Source files and what each contributes to the canonical model:

  Works Recommended .......... base record: every work an MP recommended
                               (incl. 28k not-yet-sanctioned "NA-" rows)
  Works Sanctioned ........... sanction date, sanctioned amount, workflow status
  Works Completed ............ completion date, amount disbursed
  Expenditure on Completed
    and On-going Works ....... payment tranches WITH VENDOR NAME -> contractor
                               network analysis + payment timeline
  Allocated Limit for MPs .... per-MP entitlement -> utilization denominators
  Amount consented for
    Calamity ................. calamity consents (excluded from the ordinary
                               cost baseline: calamity spend is not comparable)

Join key: works carry an eSAKSHI work code embedded in the "Work" string as
`WS/MP<mp>/<FY>/<serial>-<standardised work type>`. Whitespace inside the code
varies between files, so it is normalised before joining (join coverage
measured at 99.5-100%). Rows whose code is literally `NA` are recommendations
that have not reached sanction; they keep a synthetic id and are retained
because prohibited-category and duplicate checks still apply to them.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import PROJECT_ROOT, load_rules

DATASETS_DIR = PROJECT_ROOT / "datasets"

FILES = {
    "recommended": "Works Recommended.csv",
    "sanctioned": "Works Sanctioned.csv",
    "completed": "Works Completed.csv",
    "expenditure": "Expenditure on Completed and On-going Works as on Date.csv",
    "allocated": "Allocated Limit for Honble MPs (1).csv",
    "calamity": "Amount consented for Calamity.csv",
}

_WORK_CODE = re.compile(r"^(WS/MP\d+/\d{4}-\d{4}/\d+)")
_TYPE_AFTER_CODE = re.compile(r"^(?:WS/MP\d+/\d{4}-\d{4}/\d+|NA)-(.+)$")
_NBSP = "\xa0"


def datasets_available() -> bool:
    return DATASETS_DIR.exists() and any(
        (DATASETS_DIR / f).exists() for f in FILES.values()
    )


# ---------------------------------------------------------------- helpers

def _squash(s) -> str | None:
    """Collapse all whitespace (incl. tabs/NBSP that appear mid-code)."""
    if not isinstance(s, str):
        return None
    t = re.sub(r"\s+", "", s.replace(_NBSP, " "))
    return t or None


def _clean_text(s) -> str | None:
    if not isinstance(s, str):
        return None
    t = re.sub(r"\s+", " ", s.replace(_NBSP, " ")).strip()
    return t or None


def _work_code(work_str) -> str | None:
    t = _squash(work_str)
    if not t:
        return None
    m = _WORK_CODE.match(t)
    return m.group(1) if m else None


def _work_type(work_str) -> str | None:
    t = _clean_text(work_str)
    if not t:
        return None
    t = re.sub(r"WS/\s*MP", "WS/MP", t)
    m = _TYPE_AFTER_CODE.match(re.sub(r"\s+", " ", t))
    return _clean_text(m.group(1)) if m else None


def _district_from_ida(ida) -> str | None:
    """IDA looks like 'GHAZIABAD(DISTRICT MAGISTRATE GHAZIABAD_IDA)'."""
    t = _clean_text(ida)
    if not t:
        return None
    return _clean_text(t.split("(")[0]) or None


def _to_num(s):
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return np.nan
    t = re.sub(r"[^\d.\-]", "", str(s).replace(_NBSP, ""))
    if t in ("", "-", "."):
        return np.nan
    try:
        return float(t)
    except ValueError:
        return np.nan


def _to_date(series: pd.Series) -> pd.Series:
    """eSAKSHI exports use dd-Mon-yyyy."""
    d = pd.to_datetime(series, format="%d-%b-%Y", errors="coerce")
    missing = d.isna() & series.notna()
    if missing.any():
        d.loc[missing] = pd.to_datetime(series[missing], errors="coerce", dayfirst=True)
    return d


def _read(name: str) -> pd.DataFrame:
    path = DATASETS_DIR / FILES[name]
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    df.columns = [_clean_text(c) or c for c in df.columns]
    # eSAKSHI exports carry a trailing grand-total row with NBSP placeholders
    if "State" in df.columns:
        df = df[df["State"].notna() & (df["State"].str.strip() != "")]
        df = df[df["State"].str.replace(_NBSP, "", regex=False).str.strip() != ""]
    return df.reset_index(drop=True)


def _col(df: pd.DataFrame, *candidates: str) -> str | None:
    """Column names differ in case between the eSAKSHI exports."""
    lowered = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def _reservation(constituency) -> tuple[int, int]:
    """Constituency labels carry the reservation, e.g. 'ALMORA(SC)', 'DAHOD(ST)'."""
    t = (constituency or "").upper()
    return (1 if re.search(r"\(\s*SC\s*\)", t) else 0,
            1 if re.search(r"\(\s*ST\s*\)", t) else 0)


def _fy_from(code: str | None, date: pd.Timestamp | None) -> str | None:
    """Financial year: prefer the FY embedded in the work code, else the date."""
    if code:
        m = re.search(r"/(\d{4})-(\d{4})/", code)
        if m:
            return f"{m.group(1)}-{m.group(2)[2:]}"
    if date is not None and not pd.isna(date):
        y = date.year if date.month >= 4 else date.year - 1
        return f"{y}-{str(y + 1)[2:]}"
    return None


# ---------------------------------------------------------------- works

def build_works() -> pd.DataFrame:
    """Recommended (base) <- Sanctioned <- Completed <- aggregated Expenditure."""
    rec = _read("recommended")
    if rec.empty:
        return pd.DataFrame()

    c_work = _col(rec, "WORK", "Work")
    c_cat = _col(rec, "Work category", "Work Category")
    c_desc = _col(rec, "Work description", "Work Description")
    c_mp = _col(rec, "Hon'ble Members of Parliament", "Hon'ble Members of Parliaments")
    c_amt = _col(rec, "RECOMMENDED AMOUNT   ( ₹ )", "RECOMMENDED AMOUNT ( ₹ )") or \
        next((c for c in rec.columns if "RECOMMENDED AMOUNT" in c.upper()), None)

    w = pd.DataFrame(index=rec.index)
    w["work_code"] = rec[c_work].map(_work_code)
    w["work_type"] = rec[c_work].map(_work_type)
    w["category"] = rec[c_work].map(_work_type)      # peer-group key (112 real types)
    w["category_group"] = rec[c_cat].map(_clean_text) if c_cat else None
    w["state"] = rec["State"].map(_clean_text).str.upper()
    w["ia_name"] = rec["IDA"].map(_clean_text)
    w["district"] = rec["IDA"].map(_district_from_ida)
    w["mp_name"] = rec[c_mp].map(_clean_text) if c_mp else None
    w["constituency"] = rec["Constituency"].map(_clean_text)
    w["description"] = rec[c_desc].map(_clean_text) if c_desc else None
    w["recommended_date"] = _to_date(rec[_col(rec, "Recommended date")])
    w["estimated_cost"] = rec[c_amt].map(_to_num) if c_amt else np.nan

    # Stable id: the real eSAKSHI code where present. Recommendations that have
    # not reached sanction carry no code, so they get a row-unique synthetic id
    # -- deliberately NOT content-hashed, because collapsing identical rows would
    # hide genuine double-entry, which is exactly what the entity-resolution
    # agent exists to surface.
    srno = rec[_col(rec, "Sr. No.", "Sr No", "SrNo")] if _col(rec, "Sr. No.", "Sr No", "SrNo") else rec.index.astype(str)
    w["work_id"] = [
        code if code else f"REC-NA-{str(sn).strip()}"
        for code, sn in zip(w["work_code"], srno)
    ]
    w = w.drop_duplicates(subset=["work_id"], keep="first")

    # ---- sanctioned
    san = _read("sanctioned")
    if not san.empty:
        s_work = _col(san, "Work", "WORK")
        s = pd.DataFrame({
            "work_id": san[s_work].map(_work_code),
            "sanction_date": _to_date(san[_col(san, "Sanction Date")]),
            "sanctioned_amount": san[
                _col(san, "Sanction Amount ( ₹ )") or
                next(c for c in san.columns if "SANCTION AMOUNT" in c.upper())
            ].map(_to_num),
            "status": san[_col(san, "Work Status")].map(_clean_text),
        }).dropna(subset=["work_id"]).drop_duplicates("work_id")
        w = w.merge(s, on="work_id", how="left")

    # ---- completed
    com = _read("completed")
    if not com.empty:
        m_work = _col(com, "Work", "WORK")
        c = pd.DataFrame({
            "work_id": com[m_work].map(_work_code),
            "completion_date": _to_date(com[_col(com, "Completion Date")]),
            "amount_disbursed": com[
                _col(com, "Amount Disbursed ( ₹ )") or
                next(c2 for c2 in com.columns if "AMOUNT DISBURSED" in c2.upper())
            ].map(_to_num),
        }).dropna(subset=["work_id"]).drop_duplicates("work_id")
        w = w.merge(c, on="work_id", how="left")

    # ---- expenditure tranches (vendor + payment timeline)
    exp = _read("expenditure")
    if not exp.empty:
        e_amt = _col(exp, "Fund Disbursed Amount ( ₹ )") or \
            next(c3 for c3 in exp.columns if "FUND DISBURSED" in c3.upper())
        e = pd.DataFrame({
            "work_id": exp["Work ID"].map(_squash),
            "vendor_name": exp[_col(exp, "Vendor Name")].map(_clean_text),
            "paid": exp[e_amt].map(_to_num),
            "pay_date": _to_date(exp[_col(exp, "Expenditure Date")]),
            "pay_status": exp[_col(exp, "Payment Status")].map(_clean_text),
        }).dropna(subset=["work_id"])

        # primary vendor = the one paid the most on that work
        vend = (e.dropna(subset=["vendor_name"])
                 .groupby(["work_id", "vendor_name"], as_index=False)["paid"].sum()
                 .sort_values("paid", ascending=False)
                 .drop_duplicates("work_id")[["work_id", "vendor_name"]])
        agg = e.groupby("work_id").agg(
            total_paid=("paid", "sum"),
            payment_count=("paid", "size"),
            last_payment_date=("pay_date", "max"),
            payment_status=("pay_status", "last"),
        ).reset_index().merge(vend, on="work_id", how="left")
        w = w.merge(agg, on="work_id", how="left")

    # ---- derived fields
    if "amount_disbursed" in w.columns and "total_paid" in w.columns:
        w["expenditure"] = w["total_paid"].fillna(w["amount_disbursed"])
    elif "total_paid" in w.columns:
        w["expenditure"] = w["total_paid"]
    else:
        w["expenditure"] = np.nan

    res = w["constituency"].map(_reservation)
    w["is_sc_constituency"] = [r[0] for r in res]
    w["is_st_constituency"] = [r[1] for r in res]

    ref_date = w.get("sanction_date")
    if ref_date is None:
        ref_date = w["recommended_date"]
    else:
        ref_date = ref_date.fillna(w["recommended_date"])
    w["fy"] = [_fy_from(code, d) for code, d in zip(w["work_code"], ref_date)]

    cutoff = load_rules()["era"]["cutoff_date"]
    w["era"] = ["unknown" if pd.isna(d) else
                ("post2023" if d.strftime("%Y-%m-%d") >= cutoff else "pre2023")
                for d in ref_date]

    # status for works that never reached sanction
    if "status" in w.columns:
        w["status"] = w["status"].fillna(
            pd.Series(np.where(w["work_code"].isna(), "Recommended (not sanctioned)", None),
                      index=w.index))
    w["house"] = None
    w["lat"] = np.nan
    w["lon"] = np.nan
    w["source"] = "esakshi_csv"

    for c in ("recommended_date", "sanction_date", "completion_date", "last_payment_date"):
        if c in w.columns:
            w[c] = pd.to_datetime(w[c], errors="coerce").dt.strftime("%Y-%m-%d")

    return w


# ---------------------------------------------------------------- fundflows

def build_fundflows(works: pd.DataFrame) -> pd.DataFrame:
    """Constituency x FY fund positions.

    Entitlement comes from the official 'Allocated Limit for Hon'ble MPs' file
    (a term-level figure). It is apportioned evenly across the financial years
    actually present for that MP so that per-FY utilisation is comparable; the
    term-level total is preserved on every row for the pile-up rule.
    """
    if works.empty:
        return pd.DataFrame()

    alloc = _read("allocated")
    alloc_map: dict[tuple[str, str], float] = {}
    if not alloc.empty:
        a_mp = _col(alloc, "Hon'ble Members of Parliaments",
                    "Hon'ble Members of Parliament")
        a_amt = _col(alloc, "Allocated AMOUNT ( ₹ )") or \
            next(c for c in alloc.columns if "ALLOCATED" in c.upper())
        for _, r in alloc.iterrows():
            key = ((_clean_text(r["Constituency"]) or "").upper(),
                   (_clean_text(r[a_mp]) or "").upper())
            alloc_map[key] = _to_num(r[a_amt])

    grp = works.dropna(subset=["fy"]).groupby(
        ["state", "constituency", "mp_name", "fy"], dropna=False)
    ff = grp.agg(
        recommended=("estimated_cost", "sum"),
        sanctioned=("sanctioned_amount", "sum"),
        expenditure=("expenditure", "sum"),
        works_count=("work_id", "size"),
        sc_works=("is_sc_constituency", "max"),
        st_works=("is_st_constituency", "max"),
    ).reset_index()

    years_per_mp = ff.groupby(["constituency", "mp_name"],
                              dropna=False)["fy"].transform("nunique")

    def _text(value) -> str:
        # `value or ""` is not enough: a missing value arrives as a NaN float,
        # which is truthy. Rajya Sabha works from the live path have no
        # constituency, so this is reached on every RS member.
        return value.upper() if isinstance(value, str) else ""

    def _entitlement(row, n_years):
        key = (_text(row["constituency"]), _text(row["mp_name"]))
        total = alloc_map.get(key)
        if total is None or np.isnan(total):
            return np.nan
        return total / max(int(n_years), 1)

    ff["entitlement"] = [
        _entitlement(r, n) for (_, r), n in zip(ff.iterrows(), years_per_mp)
    ]
    # MPLADS releases follow entitlement; where no separate release figure is
    # published, entitlement is the honest denominator for utilisation.
    ff["released"] = ff["entitlement"]
    ff["utilization_pct"] = np.where(
        ff["released"] > 0, 100 * ff["expenditure"] / ff["released"], np.nan
    ).round(2)

    # SC/ST AREA spend is deliberately left unpopulated.
    #
    # The scheme's 15% / 7.5% obligation is about SC/ST-inhabited AREAS within a
    # constituency. These published extracts only reveal whether a SEAT is
    # reserved, which is a different thing entirely. Deriving "SC spend" from
    # seat reservation would mark every general constituency as 0% and fire the
    # rule against the whole country - a false positive on a politically
    # sensitive metric, which is exactly what this system must not do.
    #
    # So R-SCST-01 correctly stands down on this data (the orchestrator reports
    # it as skipped-for-missing-inputs), and the honest signal available here is
    # the state-level reserved-seat screening proxy, R-SCST-02.
    ff["sc_expenditure"] = np.nan
    ff["st_expenditure"] = np.nan
    ff = ff.drop(columns=["sc_works", "st_works"])

    cutoff = load_rules()["era"]["cutoff_date"]
    ff["era"] = ff["fy"].map(
        lambda f: "post2023" if f and int(str(f)[:4]) >= int(cutoff[:4]) else "pre2023"
    )
    ff["house"] = None
    ff["source"] = "esakshi_csv"
    ff["row_id"] = [
        "FF-" + hashlib.sha1(f"{r.state}|{r.constituency}|{r.mp_name}|{r.fy}"
                             .encode("utf-8")).hexdigest()[:12]
        for r in ff.itertuples()
    ]
    return ff


def ingest_offline(verbose: bool = True) -> dict:
    """Load the official CSV datasets into the canonical schema."""
    started = datetime.now(timezone.utc).isoformat()
    works = build_works()
    flows = build_fundflows(works)
    if verbose:
        print(f"[offline] works={len(works):,} fundflows={len(flows):,}")
    return {
        "works": works,
        "fundflows": flows,
        "provenance": [
            {"source": "MPLADS eSAKSHI official CSV exports (datasets/)",
             "mode": "offline", "table": "works", "rows": len(works),
             "status": "ok", "detail": ", ".join(FILES.values()), "fetched_at": started},
            {"source": "MPLADS eSAKSHI official CSV exports (datasets/)",
             "mode": "offline", "table": "fundflows", "rows": len(flows),
             "status": "ok", "detail": "constituency x FY aggregation + Allocated Limit",
             "fetched_at": started},
        ],
    }
