"""Map eSAKSHI REST records onto ASTRA's canonical `Work` schema.

Pure transformation: no network, no database, no files. Everything here runs
against saved fixtures, which is what makes it cheap to test.

Field parity with the CSV path
-----------------------------
`astra/ingestion/offline.py` is the reference implementation, and this module
deliberately reproduces its conventions so the two paths yield the same corpus:

  * `category` and `work_type` are both the activity-type string, i.e. the part
    of the work label after the code (`offline.py:_work_type`). `category` is
    the anomaly module's peer-group key, so it must not drift.
  * `fy` prefers the financial year embedded in the work code, else derives it
    from the recommendation date on an April-March year (`_fy_from`).
  * `is_sc_constituency` / `is_st_constituency` come from the `(SC)` / `(ST)`
    marker in the constituency label (`_reservation`).
  * `vendor_name` is the vendor paid the **most** on that work, not the most
    recent one.
  * `expenditure` is the sum of payment tranches, falling back to the amount
    disbursed on completion.
  * `lat` / `lon` stay empty — the CSV path leaves them empty too.

Two deliberate improvements
---------------------------
1. `work_id` keeps the `WS/MP.../...` code wherever the portal has assigned
   one, so existing `flag_id` values (a hash of the entity id) and the human
   decisions recorded against them in `feedback` survive. Only pre-sanction
   works, which have no code, are keyed `ES-<WORK_RECOMMENDATION_DTL_ID>`.
   That is also a bug fix: the CSV path keys those rows `REC-NA-<Sr. No.>`,
   a row number that shifts every time the export is regenerated.
2. The four tiles are joined on `WORK_RECOMMENDATION_DTL_ID`, an integer the
   portal assigns, instead of a work code parsed out of a string. Measured on
   Goa: zero orphans across recommended / sanctioned / completed / expenditure.

Known vocabulary difference
---------------------------
`status` carries the portal's own `WORK_STAGE`, so an unsanctioned work reads
"Pending for Sanction" where the CSV path synthesises "Recommended (not
sanctioned)". Immaterial to the rules: `status` is consulted in exactly one
place (`compliance.py` R-TIME-01) and only to check it is not the literal
string "completed", which neither vocabulary ever produces.
"""
from __future__ import annotations

import re
from datetime import datetime

from ..config import era_of
from ..schemas import Work
from .esakshi_api import HOUSE_LS

#: `WS/MP487/2024-2025/167969-Construction of roads...`
_WORK_CODE = re.compile(r"^(WS/MP\d+/\d{4}-\d{4}/\d+)")
#: everything after the code (or after a bare `NA`) is the activity type
_TYPE_AFTER_CODE = re.compile(r"^(?:WS/MP\d+/\d{4}-\d{4}/\d+|NA)-(.+)$")
_FY_IN_CODE = re.compile(r"/(\d{4})-(\d{4})/")
_SC = re.compile(r"\(\s*SC\s*\)")
_ST = re.compile(r"\(\s*ST\s*\)")
_NBSP = "\xa0"

#: the portal's date formats: record dates, and tenure timestamps
_DATE_FORMATS = ("%d-%b-%Y", "%b %d, %Y %I:%M:%S %p", "%Y-%m-%d")
_NULLISH = {"", "NA", "N/A", "-", "NULL", "NONE", "NAN"}


# ------------------------------------------------------------------ scalars
def clean_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value.replace(_NBSP, " ")).strip()
    return text or None


def squash(value) -> str | None:
    """Collapse every space, including the NBSP the portal puts mid-code."""
    if not isinstance(value, str):
        return None
    return re.sub(r"\s+", "", value.replace(_NBSP, " ")) or None


def iso_date(value) -> str | None:
    """Portal dates -> `YYYY-MM-DD`. 'NA' and blanks become None."""
    if value is None:
        return None
    text = clean_text(str(value))
    if text is None or text.upper() in _NULLISH:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def number(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = clean_text(str(value))
    if text is None or text.upper() in _NULLISH:
        return None
    stripped = re.sub(r"[^\d.\-]", "", text)
    if stripped in ("", ".", "-"):
        return None
    try:
        return float(stripped)
    except ValueError:
        return None


def work_code(activity_name) -> str | None:
    """The `WS/MP.../...` code, or None for a work not yet sanctioned."""
    text = squash(activity_name)
    if not text:
        return None
    match = _WORK_CODE.match(text)
    return match.group(1) if match else None


def activity_type(activity_name) -> str | None:
    """The standardised work-type string after the code — the peer-group key."""
    text = clean_text(activity_name)
    if not text:
        return None
    text = re.sub(r"WS/\s*MP", "WS/MP", text)
    match = _TYPE_AFTER_CODE.match(text)
    return clean_text(match.group(1)) if match else text


def district_from_ida(ida) -> str | None:
    """`GHAZIABAD(DISTRICT MAGISTRATE GHAZIABAD_IDA)` -> `GHAZIABAD`."""
    text = clean_text(ida)
    if not text:
        return None
    return clean_text(text.split("(")[0])


def reservation(constituency) -> tuple[int, int]:
    text = (clean_text(constituency) or "").upper()
    return (1 if _SC.search(text) else 0, 1 if _ST.search(text) else 0)


def financial_year(code: str | None, date_iso: str | None) -> str | None:
    """FY embedded in the work code if present, else from the date (Apr-Mar)."""
    if code:
        match = _FY_IN_CODE.search(code)
        if match:
            return f"{match.group(1)}-{match.group(2)[2:]}"
    if date_iso:
        try:
            year, month = int(date_iso[:4]), int(date_iso[5:7])
        except ValueError:
            return None
        start = year if month >= 4 else year - 1
        return f"{start}-{str(start + 1)[2:]}"
    return None


def _dtl(row: dict) -> str | None:
    value = row.get("WORK_RECOMMENDATION_DTL_ID")
    return str(value) if value is not None else None


# -------------------------------------------------------------------- works
def to_works(tiles: dict[str, list[dict]]) -> list[Work]:
    """Build canonical `Work` records from the four record tiles of one shard.

    `tiles` is keyed by the short tile names in `esakshi_api.RECORD_TILES`;
    missing keys are simply treated as empty. The recommended tile is the base
    — every work exists there — and the other three enrich it.

    Rows without a `WORK_RECOMMENDATION_DTL_ID` are skipped: the portal emits
    one id-less summary row per report (Aligarh returns 104 rows for a count of
    103, Goa 249 for 248), and it carries no work of its own.
    """
    recommended = tiles.get("recommended") or []
    works: dict[str, Work] = {}

    for row in recommended:
        dtl = _dtl(row)
        if dtl is None:
            continue
        code = work_code(row.get("ACTIVITY_NAME"))
        rec_date = iso_date(row.get("RECOMMENDATION_DATE"))
        constituency = clean_text(row.get("CONSTITUENCY"))
        is_sc, is_st = reservation(constituency)
        state = clean_text(row.get("STATE_NAME"))
        work_kind = activity_type(row.get("ACTIVITY_NAME"))
        works[dtl] = Work(
            work_id=code or f"ES-{dtl}",
            source="esakshi_api",
            era=era_of(rec_date),
            state=state.upper() if state else None,
            district=district_from_ida(row.get("IDA_NAME")),
            constituency=constituency,
            mp_name=clean_text(row.get("MP_NAME")),
            house="LS" if row.get("HOUSE_OF_PARLIAMENT") == HOUSE_LS else "RS",
            category=work_kind,
            work_type=work_kind,
            description=clean_text(row.get("WORK_DESCRIPTION")),
            recommended_date=rec_date,
            sanction_date=iso_date(row.get("SANCTION_DATE")),
            status=clean_text(row.get("WORK_STAGE")),
            estimated_cost=number(row.get("RECOMMENDED_AMOUNT")),
            sanctioned_amount=number(row.get("SANCTION_AMOUNT")),
            ia_name=clean_text(row.get("IDA_NAME")),
            is_sc_constituency=is_sc,
            is_st_constituency=is_st,
            fy=financial_year(code, rec_date),
        )

    # ---- sanctioned: authoritative sanction date, amount and workflow stage
    for row in tiles.get("sanctioned") or []:
        work = works.get(_dtl(row) or "")
        if work is None:
            continue
        work.sanction_date = iso_date(row.get("SANCTION_DATE")) or work.sanction_date
        amount = number(row.get("SANCTION_AMOUNT"))
        if amount is not None:
            work.sanctioned_amount = amount
        work.status = clean_text(row.get("WORK_STAGE")) or work.status

    # ---- completed: completion date and the amount disbursed on completion
    disbursed: dict[str, float] = {}
    for row in tiles.get("completed") or []:
        dtl = _dtl(row)
        work = works.get(dtl or "")
        if work is None:
            continue
        work.completion_date = iso_date(row.get("ACTUAL_END_DATE"))
        amount = number(row.get("ACTUAL_AMOUNT"))
        if amount is not None:
            disbursed[dtl] = amount

    # ---- expenditure: payment tranches, vendor, timeline
    tranches: dict[str, list[dict]] = {}
    for row in tiles.get("expenditure") or []:
        dtl = _dtl(row)
        if dtl is not None and dtl in works:
            tranches.setdefault(dtl, []).append(row)

    for dtl, rows in tranches.items():
        work = works[dtl]
        amounts = [number(r.get("FUND_DISBURSED_AMT")) or 0.0 for r in rows]
        dates = sorted(d for d in (iso_date(r.get("EXPENDITURE_DATE")) for r in rows)
                       if d)
        work.total_paid = sum(amounts)
        work.payment_count = len(rows)
        work.last_payment_date = dates[-1] if dates else None
        work.payment_status = clean_text(rows[-1].get("WORK_STATUS"))
        # primary vendor = the one paid the most on this work (offline.py parity)
        paid_by_vendor: dict[str, float] = {}
        for row, amount in zip(rows, amounts):
            name = clean_text(row.get("VENDOR_NAME"))
            if name:
                paid_by_vendor[name] = paid_by_vendor.get(name, 0.0) + amount
        if paid_by_vendor:
            work.vendor_name = max(paid_by_vendor, key=paid_by_vendor.get)

    # ---- derived: expenditure prefers tranches, falls back to completion
    for dtl, work in works.items():
        work.expenditure = work.total_paid if work.total_paid else disbursed.get(dtl)

    return list(works.values())


def join_report(tiles: dict[str, list[dict]]) -> dict[str, set[str]]:
    """Per-tile id sets — used by the tests to assert zero orphans."""
    out: dict[str, set[str]] = {}
    for name, rows in tiles.items():
        out[name] = {d for d in (_dtl(r) for r in rows) if d is not None}
    return out
