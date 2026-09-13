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
   works, which have no code, are keyed `ES-<house>-<WORK_RECOMMENDATION_DTL_ID>`
   (`ES-LS-1740`, `ES-RS-1740`). That is also a bug fix: the CSV path keys
   those rows `REC-NA-<Sr. No.>`, a row number that shifts every time the
   export is regenerated.
2. The four tiles are joined on the pair (`WORK_RECOMMENDATION_DTL_ID`, work
   code), not on the id alone.

The portal id is NOT unique
---------------------------
`WORK_RECOMMENDATION_DTL_ID` looks like a primary key and is not one. Measured
across all 579 slices on 13 Sep 2026, 183 ids are carried by two different
works — almost always one sanctioned work and one still awaiting sanction,
which appear to be numbered from separate sequences. Keyed on the id alone,
two of those pairs silently lost a work:

  * Rajya Sabha, Uttar Pradesh: id 1911 is both a sanctioned street-lights work
    (`WS/MP190/2023-2024/1911`) and an unrelated school work awaiting sanction
    in the same report. One overwrote the other, and the survivor inherited the
    other's stage from the sanctioned tile.
  * id 1740 is an unsanctioned work in Rajampet (Lok Sabha) and another under a
    Madhya Pradesh Rajya Sabha member. Both became `ES-1740`, so one replaced
    the other in the database.

Hence the house in the pre-sanction key, and the join on (id, code). The code
comes from `ACTIVITY_NAME` on three tiles and from `WORK_ID` on the expenditure
tile, which carries the bare activity type in `ACTIVITY_NAME`. Measured over
every cached slice, all 254,690 sanctioned, completed and expenditure rows that
have a recommended row match it exactly on that pair.

Mirroring the portal, not tidying it
------------------------------------
The target is that ASTRA's figures equal the portal's own tiles, slice by slice,
in counts and in rupees. So nothing the portal lists is dropped or merged away:
works listed only in a later report are built from that report, a work listed
twice is counted twice, ₹1 placeholder recommendations are kept, and
`portal_figures()` reproduces the four tile figures from what was stored.

Known vocabulary difference
---------------------------
`status` carries the portal's own `WORK_STAGE`, so an unsanctioned work reads
"Pending for Sanction" where the CSV path synthesises "Recommended (not
sanctioned)". Immaterial to the rules: `status` is consulted in exactly one
place (`compliance.py` R-TIME-01) and only to check it is not the literal
string "completed", which neither vocabulary ever produces.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
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


def row_code(row: dict) -> str | None:
    """The work code a record carries, wherever this tile puts it."""
    return work_code(row.get("ACTIVITY_NAME")) or work_code(row.get("WORK_ID"))


def _key(row: dict) -> tuple[str, str | None] | None:
    dtl = _dtl(row)
    return None if dtl is None else (dtl, row_code(row))


def work_id_for(dtl: str, code: str | None, house: str | None) -> str:
    """`WS/MP...` where sanctioned, else `ES-<house>-<portal id>`."""
    return code or f"ES-{house}-{dtl}"


def _house(row: dict, default: str | None = None) -> str | None:
    value = row.get("HOUSE_OF_PARLIAMENT")
    if value is None:
        return default
    return "LS" if value == HOUSE_LS else "RS"


#: Fields that describe a listing rather than the work: the row number, and
#: the attachment columns a work with several uploaded files repeats on.
PRESENTATION_FIELDS = frozenset({"Sno", "ATTACH_ID", "FILE_STATUS", "FLAG"})


def _details(row: dict) -> str:
    return json.dumps({k: v for k, v in row.items() if k not in PRESENTATION_FIELDS},
                      sort_keys=True, default=str)


def _first_date(row: dict) -> str | None:
    for field in ("RECOMMENDATION_DATE", "SANCTION_DATE", "ACTUAL_END_DATE",
                  "EXPENDITURE_DATE"):
        value = iso_date(row.get(field))
        if value:
            return value
    return None


@dataclass
class ShardMapping:
    """Everything one slice's four reports say, in ASTRA's terms.

    `works`       one `Work` per work the portal lists anywhere in the slice.
    `listing`     work_id -> how many rows of each report list it. This is
                  what reproduces the portal's own tile counts exactly, and
                  what tells a later fetch which works have gone.
    `duplicates`  works the portal lists more than once in one report.
    """

    works: list[Work]
    listing: dict[str, dict[str, int]]
    duplicates: list[dict] = field(default_factory=list)


def _empty_listing() -> dict[str, int]:
    return {"in_recommended": 0, "in_sanctioned": 0, "in_completed": 0, "payments": 0}


def map_shard(tiles: dict[str, list[dict]]) -> ShardMapping:
    """Build canonical works from the four record tiles of one slice.

    `tiles` is keyed by the short tile names in `esakshi_api.RECORD_TILES`;
    missing keys are treated as empty. Rows without a
    `WORK_RECOMMENDATION_DTL_ID` are skipped: the portal emits one id-less
    summary row per report (Aligarh returns 104 rows for a count of 103), and
    it carries no work of its own.

    Three portal behaviours shape it, each measured over all 579 slices:

    * The recommended report is the base, but not every work is in it. On
      13 Sep 2026, 608 sanctioned works were in no recommended report anywhere,
      yet the portal counts them in Works Sanctioned, Works Completed and
      expenditure. They are built from the first report that lists them.
    * The same work may be listed more than once. Rows that differ only in the
      row number or attachment columns are one work listed twice; rows with
      the same key but different details are kept as separate works
      (`<id>#2`), because the portal counts and totals both.
    * Rows are matched across reports on (portal id, work code) and nothing
      looser (see `match`).
    """
    houses = [row.get("HOUSE_OF_PARLIAMENT") for rows in tiles.values()
              for row in rows or [] if row.get("HOUSE_OF_PARLIAMENT") is not None]
    slice_house = (("LS" if max(set(houses), key=houses.count) == HOUSE_LS else "RS")
                   if houses else None)

    works: dict[str, Work] = {}                     # work_id -> Work
    primary: dict[tuple[str, str | None], str] = {}  # key -> first work_id
    listing: dict[str, dict[str, int]] = {}
    duplicates: list[dict] = []

    def build(row: dict, key: tuple[str, str | None], work_id: str) -> Work:
        dtl, code = key
        house = _house(row, slice_house)
        # For Rajya Sabha the portal's CONSTITUENCY field holds a membership
        # type — "Sitting Rajya Sabha" or "Nominated Rajya Sabha" — not a seat.
        # Stored as a constituency it would merge every RS member in a state
        # into one pseudo-constituency: the constituency-level rules key
        # entities as `constituency|state`, so R-SPIKE-01 and R-PILE-01 would
        # run on several MPs' combined fund series. Left empty, those rules
        # fall back to the MP's name and each member is their own entity. The
        # membership type is still in the raw shard cache.
        constituency = None if house != "LS" else clean_text(row.get("CONSTITUENCY"))
        is_sc, is_st = reservation(constituency)
        state = clean_text(row.get("STATE_NAME"))
        work_kind = activity_type(row.get("ACTIVITY_NAME"))
        rec_date = iso_date(row.get("RECOMMENDATION_DATE"))
        return Work(
            work_id=work_id,
            source="esakshi_api",
            era=era_of(rec_date or _first_date(row)),
            state=state.upper() if state else None,
            district=district_from_ida(row.get("IDA_NAME")),
            constituency=constituency,
            mp_name=clean_text(row.get("MP_NAME")),
            house=house,
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

    def register(key, work_id: str, work: Work) -> None:
        works[work_id] = work
        listing[work_id] = _empty_listing()
        primary.setdefault(key, work_id)

    # ---- recommended: the base report
    groups: dict[tuple[str, str | None], list[dict]] = {}
    for row in tiles.get("recommended") or []:
        key = _key(row)
        if key is not None:
            groups.setdefault(key, []).append(row)
    for key, rows in groups.items():
        variants: dict[str, list[dict]] = {}
        for row in rows:
            variants.setdefault(_details(row), []).append(row)
        # A stable order, so `#2` names the same record on every fetch.
        ordered = sorted(variants.values(), key=lambda same: _details(same[0]))
        base = work_id_for(key[0], key[1], _house(rows[0], slice_house))
        for index, same in enumerate(ordered):
            work_id = base if index == 0 else f"{base}#{index + 1}"
            register(key, work_id, build(same[0], key, work_id))
            listing[work_id]["in_recommended"] = len(same)
        if len(rows) > 1:
            duplicates.append({"work_id": base, "report": "recommended",
                               "rows": len(rows), "distinct_records": len(ordered)})

    def match(row: dict) -> str | None:
        """The work this row belongs to: exact on (id, code), nothing looser.

        There is deliberately no fallback to the id alone. The id is shared by
        unrelated works, so any looser rule can attach one work's sanction or
        payment to another. A work sanctioned between two report reads shows up
        briefly as two works; the next consistent read lists only one, and the
        other is retired (see `db.apply_listing`).
        """
        key = _key(row)
        return None if key is None else primary.get(key)

    def attach(row: dict) -> str | None:
        """Match, or create the work from this row if no report listed it yet."""
        found = match(row)
        if found is not None:
            return found
        key = _key(row)
        if key is None:
            return None
        work_id = work_id_for(key[0], key[1], _house(row, slice_house))
        if work_id in works:          # same pending id, a different record
            taken = sum(1 for w in works if w == work_id or w.startswith(work_id + "#"))
            work_id = f"{work_id}#{taken + 1}"
        register(key, work_id, build(row, key, work_id))
        return work_id

    # ---- sanctioned: authoritative sanction date, amount and workflow stage
    seen_sanctioned: dict[str, int] = {}
    for row in tiles.get("sanctioned") or []:
        work_id = attach(row)
        if work_id is None:
            continue
        work = works[work_id]
        listing[work_id]["in_sanctioned"] += 1
        seen_sanctioned[work_id] = seen_sanctioned.get(work_id, 0) + 1
        work.sanction_date = iso_date(row.get("SANCTION_DATE")) or work.sanction_date
        amount = number(row.get("SANCTION_AMOUNT"))
        if amount is not None:
            work.sanctioned_amount = amount
        work.status = clean_text(row.get("WORK_STAGE")) or work.status
    duplicates += [{"work_id": w, "report": "sanctioned", "rows": n}
                   for w, n in seen_sanctioned.items() if n > 1]

    # ---- completed: completion date and the amount disbursed on completion
    disbursed: dict[str, float] = {}
    for row in tiles.get("completed") or []:
        work_id = attach(row)
        if work_id is None:
            continue
        work = works[work_id]
        listing[work_id]["in_completed"] += 1
        work.completion_date = iso_date(row.get("ACTUAL_END_DATE"))
        amount = number(row.get("ACTUAL_AMOUNT"))
        if amount is not None:
            disbursed[work_id] = amount

    # ---- expenditure: payment tranches, vendor, timeline
    tranches: dict[str, list[dict]] = {}
    for row in tiles.get("expenditure") or []:
        work_id = attach(row)
        if work_id is not None:
            tranches.setdefault(work_id, []).append(row)
            listing[work_id]["payments"] += 1

    for work_id, rows in tranches.items():
        work = works[work_id]
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
    for work_id, work in works.items():
        work.expenditure = work.total_paid if work.total_paid else disbursed.get(work_id)

    return ShardMapping(list(works.values()), listing, duplicates)


def to_works(tiles: dict[str, list[dict]]) -> list[Work]:
    """The works of one slice. See `map_shard` for how they are built."""
    return map_shard(tiles).works


def portal_figures(listing: dict[str, dict[str, int]],
                   works: dict[str, dict]) -> dict[str, tuple[int, float]]:
    """ASTRA's version of the portal's four tile figures for one slice.

    Definitions measured against every slice's own tiles (all 579 exact):
      Works Recommended   rows listed, and the sum of recommended amounts
      Works Sanctioned    rows listed, and the sum of sanctioned amounts
      Works Completed     rows listed, and the SANCTIONED amount of those works
      Expenditure         the sum of every payment tranche
    `works` maps work_id to a record with estimated_cost, sanctioned_amount
    and total_paid (a `Work` dumped to a dict, or a database row).
    """
    out = {"recommended": [0, 0.0], "sanctioned": [0, 0.0],
           "completed": [0, 0.0], "expenditure": [0, 0.0]}
    for work_id, seen in listing.items():
        work = works.get(work_id) or {}
        sanctioned = work.get("sanctioned_amount") or 0.0
        out["recommended"][0] += seen["in_recommended"]
        out["recommended"][1] += seen["in_recommended"] * (work.get("estimated_cost") or 0.0)
        out["sanctioned"][0] += seen["in_sanctioned"]
        out["sanctioned"][1] += seen["in_sanctioned"] * sanctioned
        out["completed"][0] += seen["in_completed"]
        out["completed"][1] += seen["in_completed"] * sanctioned
        out["expenditure"][0] += seen["payments"]
        out["expenditure"][1] += work.get("total_paid") or 0.0
    return {k: (n, round(total, 2)) for k, (n, total) in out.items()}


def join_report(tiles: dict[str, list[dict]]) -> dict[str, set[str]]:
    """Per-tile id sets — used by the tests to assert zero orphans."""
    out: dict[str, set[str]] = {}
    for name, rows in tiles.items():
        out[name] = {d for d in (_dtl(r) for r in rows) if d is not None}
    return out
