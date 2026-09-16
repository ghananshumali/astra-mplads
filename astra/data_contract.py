"""What the analysis must know about how its data was ingested.

The analysis reads the corpus the eSAKSHI poller builds (`astra/ingestion`).
Several facts about that corpus cannot be seen from the column names, and each
has produced a wrong finding at some point. They are kept here as code, in one
place: the rules import them rather than re-deriving them, and `check()`
re-measures the ones that could change, on every run, so a change on the portal
shows up in the run record instead of silently skewing findings.

Facts about the works (measured on the live corpus, 13 Sep 2026)
----------------------------------------------------------------
Completion
    A work is complete when the portal's Works Completed report lists it
    (`in_completed > 0`); `completion_date` is that report's actual end date.
    The stage text is NOT the test: of 45,284 completed works, 40,472 read
    "Physical Inspection" and 4,816 "Work Completed".
Sanction
    A work is sanctioned when `sanction_date` is set. A work awaiting sanction
    (stage "Pending for Sanction", id `ES-<house>-<id>`) still carries the
    MP's recommended amount in `sanctioned_amount`.
Amounts
    `estimated_cost` (the recommended amount) and `sanctioned_amount` were
    identical on every one of the 132,990 works carrying both, so comparing
    them tells nothing, and a revision can only be seen as an observed edit.
District authority and implementing agency
    `ia_name` is the Implementing District Authority, e.g.
    "JAUNPUR(DISTRICT MAGISTRATE JAUNPUR_IDA)" — 777 of them, each covering one
    district — and `district` is derived from it. The agency the authority
    selected to execute the work (para 3.2.10) is `implementing_agency`, e.g.
    "BDPO Shri Anandpur Sahib". Only payment records carry it, so it is empty
    until a first payment; on 16 Sep 2026 each of 72,911 paid works had
    exactly one, under 7,327 distinct names. The names are as typed on the
    portal: one body can appear under several spellings, and suffixes such as
    "_5" may mark separate offices, so names are grouped only exactly.
Vendor
    `vendor_name` is the vendor paid the most on the work, present only once a
    payment has been made (54% of works). `vendor_id` is that vendor's portal
    id. A name is NOT a vendor: on 16 Sep 2026, 1,424 names belonged to more
    than one vendor id ("gurpreet singh" to 17; "ajay kumar" to 13, 12 of them
    paid in one state only). Vendors are grouped by id; no id carried two names.
Portal category
    `work_category` is the portal's own class of each work: "Normal/Others",
    "Repair and Renovation" (1,875 works), "Trust and Society" (1,117), "Bar and
    Associations" (19) or "N/A". `category` is something else: the activity type
    and the anomaly module's peer-group key. The category and the description
    can disagree: 855 works described as a repair are "Normal/Others", and 947
    "Repair and Renovation" works do not say repair.
Recommendation letter
    `letter_no` is the MP's recommendation letter. One letter can carry many
    works (13 letters carried more than 100 on 16 Sep 2026).
Member's term
    `term_start` / `term_end` are the recommending member's term. The portal
    reports ASTRA reads are the current terms: every term ends after
    16 Sep 2026, so no stored work belongs to a former member.
Payments
    `payments` (a table of its own) holds every payment record the portal's
    expenditure report lists: date, amount, vendor name and id, implementing
    agency, and status ("Payment Success" or "Payment In-Progress"). They add
    up to each work's `total_paid` and `payment_count`. The portal gives a
    payment no id: on 16 Sep 2026, 975 sets of records on 576 works were
    identical in every field but the row number (the same amount to the same
    vendor on the same day), and the portal's own expenditure figure counts
    each, so each is kept. On that day no payment was dated before its work's
    sanction or after the day it was read, and no work had been paid more than
    its sanctioned amount.
Place
    `constituency` is empty for Rajya Sabha works (the portal's field holds a
    membership type). `lat` and `lon` are never populated.
Financial year
    `fy` is the year in the work code, else of the recommendation date. The
    code is issued at sanction, so for a sanctioned work `fy` is the year of
    SANCTION: it matched the sanction date on all 100,705 coded works on
    16 Sep 2026, and 29,493 of them were recommended in an earlier year. It is
    never the year a work was paid. Limits on what an MP may recommend in a
    year (paras 5.1.9 and 6.2.6.2) count by `recommendation_fy()` instead. The
    current financial year is still in progress, and 2023-24 was eSAKSHI's
    first year.
Two portal records for one work
    After sanction the portal keeps the original recommendation (stage "NA")
    and lists the sanctioned record under a new id only from the sanctioned
    report onward (`in_recommended == 0`). Totals over works must not add
    both. A work the portal lists twice is kept twice (`#2` ids).
Observed edits
    `work_versions.observed_at` is when the poller saw a change, up to about a
    day after the edit on the portal. The portal publishes no history, so
    nothing before polling began (13 Sep 2026) is visible.

Provisions the data cannot check
--------------------------------
`UNCHECKABLE` lists guideline provisions with no usable field, with the
reason, so the run record says so instead of implying the corpus is clean.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

LIVE_SOURCE = "esakshi_api"

#: stage of a pending recommendation whose sanctioned record has its own id
RECORD_PAIR_STAGE = "NA"
PENDING_STAGE = "Pending for Sanction"

#: the portal's `work_category` values the rules read
REPAIR_CATEGORY = "Repair and Renovation"
TRUST_CATEGORY = "Trust and Society"

#: fields that change as a work simply moves forward; never a revision
PROGRESS_FIELDS = frozenset({
    "status", "sanction_date", "completion_date", "total_paid", "payment_count",
    "last_payment_date", "payment_status", "expenditure",
})
#: fields whose change after sanction alters what or where the work is
PLACE_FIELDS = ("description", "category", "work_type", "district", "state",
                "constituency")
AMOUNT_FIELDS = ("estimated_cost", "sanctioned_amount")

#: listing columns the analysis merges onto works
LISTING_COLUMNS = ("in_recommended", "in_sanctioned", "in_completed", "payments")

UNCHECKABLE = {
    "3.1.2.1": "Works outside the MP's usual region: the data does not map a constituency to its districts.",
    "3.2.5": "Sanctions within the entitlement up to the year: interest, redistribution and part-year allocations (para 10.4.3) are not in the data.",
    "3.2.6": "Model code of conduct periods: no calendar of them is held, so time limits are counted without excluding them.",
    "5.2.4": "Naming an asset after a person: not recorded in work descriptions.",
    "5.4.1": "SC/ST area shares: the data does not say which works are in SC/ST-inhabited areas.",
    "6.2.6.2": "Rs 1 crore a term for any one society or trust: works do not name the society or trust they are for. The Rs 50 lakh a year limit for all of them together is checked.",
    "8.12.1": "Calamity works: calamity consents are totals, not linked to works.",
    "10.6.1": "Former MPs' works: the portal reports read cover members' current terms, and none of those terms has ended.",
    "11.4.1": "Utilisation certificates: not published through the dashboard interface.",
}


# ------------------------------------------------------------- years
def financial_year_of(day: date | datetime | pd.Timestamp) -> str:
    """`2026-09-13` -> "2026-27" (April to March)."""
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def recommendation_fy(works: pd.DataFrame) -> pd.Series:
    """The financial year each work was recommended in, falling back to `fy`
    where the recommendation date is missing (see "Financial year" above)."""
    fallback = (works["fy"] if "fy" in works.columns
                else pd.Series(None, index=works.index, dtype=object))
    if "recommended_date" not in works.columns:
        return fallback
    dates = pd.to_datetime(works["recommended_date"], errors="coerce")
    years = dates.map(lambda d: None if pd.isna(d) else financial_year_of(d))
    return years.where(years.notna(), fallback)


def fy_start_year(fy) -> int | None:
    text = str(fy or "").strip()
    head = text.split("-")[0]
    return int(head) if head.isdigit() and len(head) == 4 else None


def fy_has_ended(fy, today: date | None = None) -> bool:
    """True once 31 March of the financial year has passed.

    A multi-year term aggregate ("2019-24") counts as ended when its last
    year has.
    """
    today = today or date.today()
    text = str(fy or "").strip()
    start = fy_start_year(text)
    if start is None:
        return False
    tail = text.split("-")[1] if "-" in text else ""
    end = int(tail) if len(tail) == 4 else (int(str(start)[:2] + tail) if tail.isdigit() else start + 1)
    if end <= start:
        end = start + 1
    return today > date(end, 3, 31)


# ------------------------------------------------------------- works
def enrich(works: pd.DataFrame, listing: pd.DataFrame | None = None) -> pd.DataFrame:
    """Add what the rules need to know about each work's place in the portal.

    Merges the listing counts and the slice id, and derives `is_sanctioned`
    and `is_completed` by the definitions above. A corpus without listings
    (the CSV path) falls back to the dates alone.
    """
    out = works.copy()
    if listing is not None and not listing.empty and not out.empty:
        cols = ["work_id"] + [c for c in (*LISTING_COLUMNS, "shard_id")
                              if c in listing.columns and c not in out.columns]
        out = out.merge(listing[cols], on="work_id", how="left")
    if out.empty:
        for col in ("is_sanctioned", "is_completed"):
            out[col] = pd.Series(dtype=bool)
        return out
    sanction = out["sanction_date"] if "sanction_date" in out else pd.Series(None, index=out.index)
    completion = out["completion_date"] if "completion_date" in out else pd.Series(None, index=out.index)
    out["is_sanctioned"] = sanction.notna()
    completed = completion.notna()
    if "in_completed" in out:
        completed = completed | (pd.to_numeric(out["in_completed"], errors="coerce").fillna(0) > 0)
    out["is_completed"] = completed
    return out


def counts_toward_totals(works: pd.DataFrame) -> pd.Series:
    """Works to include when adding amounts per MP: not the pending "NA"
    recommendation the portal keeps beside its sanctioned record."""
    status = works["status"] if "status" in works else pd.Series(None, index=works.index)
    return status.fillna("") != RECORD_PAIR_STAGE


def mp_key(row) -> str:
    """The entity key the constituency-level rules use: constituency, else the
    MP's name (Rajya Sabha has no constituency), then state."""
    def text(value):
        return value if isinstance(value, str) and value.strip() else None
    return f"{text(row.get('constituency')) or text(row.get('mp_name')) or 'NA'}|{text(row.get('state')) or 'NA'}"


def is_district_authority(name) -> bool:
    return isinstance(name, str) and "_IDA" in name.upper()


# ------------------------------------------------------------- checks
def check(works: pd.DataFrame, payments: pd.DataFrame | None = None) -> dict:
    """Re-measure the facts above that the portal could change.

    Returns {fact: {"holds": bool, "observed": ...}}. A fact that stops
    holding does not stop the analysis; it is recorded with the run so the
    rules that depend on it can be revisited.
    """
    facts: dict[str, dict] = {}
    if works.empty:
        return facts
    live = works[works["source"] == LIVE_SOURCE] if "source" in works else works
    if live.empty:
        return {"live_corpus": {"holds": False, "observed": "no works from the eSAKSHI interface"}}

    def fact(name, holds, observed):
        facts[name] = {"holds": bool(holds), "observed": observed}

    both = live[(pd.to_numeric(live.get("estimated_cost"), errors="coerce") > 0)
                & (pd.to_numeric(live.get("sanctioned_amount"), errors="coerce") > 0)]
    differ = int((both["estimated_cost"] != both["sanctioned_amount"]).sum()) if not both.empty else 0
    fact("recommended_equals_sanctioned_amount", differ == 0,
         f"{differ} of {len(both)} works differ")
    coords = int(pd.to_numeric(live.get("lat"), errors="coerce").notna().sum()) if "lat" in live else 0
    fact("no_coordinates", coords == 0, f"{coords} works with coordinates")
    rs = live[live.get("house") == "RS"] if "house" in live else live.iloc[0:0]
    rs_const = int(rs["constituency"].notna().sum()) if not rs.empty else 0
    fact("rajya_sabha_without_constituency", rs_const == 0, f"{rs_const} RS works with a constituency")
    if "ia_name" in live and "district" in live:
        spread = int((live.groupby("ia_name")["district"].nunique() > 1).sum())
        fact("district_authority_is_one_district", spread == 0,
             f"{spread} district authorities spanning several districts")
    if "is_completed" in live and "status" in live:
        done = live[live["is_completed"]]
        stage_says = int((done["status"] == "Work Completed").sum())
        fact("completion_from_report_not_stage", True,
             f"{stage_says} of {len(done)} completed works read 'Work Completed'")
    pending = live[live.get("status") == PENDING_STAGE] if "status" in live else live.iloc[0:0]
    dated = int(pending["sanction_date"].notna().sum()) if not pending.empty else 0
    fact("pending_works_have_no_sanction_date", dated == 0,
         f"{dated} pending works with a sanction date")
    if {"vendor_id", "vendor_name"} <= set(live.columns):
        paid = live.dropna(subset=["vendor_id"])
        renamed = int((paid.groupby("vendor_id")["vendor_name"].nunique() > 1).sum())
        shared = int((paid.assign(_n=paid["vendor_name"].str.strip().str.upper())
                      .groupby("_n")["vendor_id"].nunique() > 1).sum())
        fact("vendor_id_has_one_name", renamed == 0,
             f"{renamed} vendor ids under several names; {shared} names used by several vendor ids")
    if payments is not None and not payments.empty and "total_paid" in live.columns:
        sums = payments.groupby("work_id").agg(n=("amount", "size"), paid=("amount", "sum"))
        paid = live.set_index("work_id")[["payment_count", "total_paid"]].dropna(subset=["total_paid"])
        both = paid.join(sums, how="inner")
        off = int(((both["n"] != both["payment_count"])
                   | ((both["paid"] - both["total_paid"]).abs() > 0.5)).sum())
        missing = int(len(paid.index.difference(sums.index)))
        fact("payments_add_up_to_works", off == 0 and missing == 0,
             f"{off} of {len(both)} paid works differ from their payment records; "
             f"{missing} paid works without payment records")
        dated = payments.merge(live[["work_id", "sanction_date"]], on="work_id", how="inner")
        early = int((pd.to_datetime(dated["paid_on"], errors="coerce")
                     < pd.to_datetime(dated["sanction_date"], errors="coerce")).sum())
        fact("payments_follow_sanction", early == 0,
             f"{early} of {len(dated)} payments dated before their work's sanction")
    if "term_end" in live.columns:
        ends = pd.to_datetime(live["term_end"], errors="coerce")
        ended = int((ends < pd.Timestamp(date.today())).sum())
        fact("members_in_office", ended == 0,
             f"{ended} works of members whose term has ended; "
             f"{int(ends.isna().sum())} without a term end date")
    return facts
