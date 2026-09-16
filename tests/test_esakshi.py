"""Stage A gate: the eSAKSHI client and the field mapping.

    python tests/test_esakshi.py

Two groups. The OFFLINE group runs against fixtures under
`tests/fixtures/esakshi/` and needs no network — that includes the check that
matters most, which is that the API corpus agrees with the CSV corpus already
in `data/astra.db`. The LIVE group probes the portal and is skipped when it is
unreachable.

Never writes to the database; `data/astra.db` is opened read-only.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astra.ingestion import esakshi_map as emap             # noqa: E402
from astra.ingestion.esakshi_api import (                   # noqa: E402
    HOUSE_LS, RECORD_TILES, CircuitBreaker, CircuitOpen, EsakshiClient, SourceError,
    parse_tile, shard_combo,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "esakshi"
#: the fixture shard: Uttar Pradesh / Aligarh, Lok Sabha
FIX_STATE, FIX_CONST, FIX_NAME = 33, 418, "ALIGARH"
DB = ROOT / "data" / "astra.db"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    tag = PASS if cond else FAIL
    results.append((tag, name, detail))
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def skip(name: str, why: str) -> None:
    results.append((SKIP, name, why))
    print(f"  [{SKIP}] {name} — {why}")


# --------------------------------------------------------------- fixtures
def load_fixtures() -> dict[str, list[dict]] | None:
    if not FIXTURES.is_dir():
        return None
    tiles: dict[str, list[dict]] = {}
    for tile in RECORD_TILES:
        path = FIXTURES / f"{FIX_NAME.lower()}_{tile}.json"
        if not path.exists():
            return None
        tiles[tile] = json.loads(path.read_text(encoding="utf-8"))
    return tiles


def write_fixtures(client: EsakshiClient) -> dict[str, list[dict]]:
    """Refresh the fixture shard from the portal. Also used by --refresh."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    combo = shard_combo(FIX_STATE, FIX_CONST, house=HOUSE_LS)
    tiles = {}
    for tile in RECORD_TILES:
        rows = client.tile_report(combo, tile)
        tiles[tile] = rows
        (FIXTURES / f"{FIX_NAME.lower()}_{tile}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return tiles


# ------------------------------------------------------------------ offline
def test_parse_tile() -> None:
    print("\n[1] parse_tile — the portal's two response quirks")

    # the tile value is itself a JSON string, so it needs a second decode
    nested = {"Total Works Recommended": json.dumps(
        [{"WORK_RECOMMENDATION_DTL_ID": 1}, {"WORK_RECOMMENDATION_DTL_ID": 2}])}
    check("double-encoded JSON is unwrapped", len(parse_tile(nested)) == 2)

    # "no records" arrives as a single {"Total_Amt": 0.0} row, not an empty list
    degenerate = {"Total Works Recommended": json.dumps([{"Total_Amt": 0.0}])}
    check("degenerate Total_Amt row is dropped", parse_tile(degenerate) == [])

    # an already-decoded list should also work
    plain = {"Total Sanction Work": [{"WORK_RECOMMENDATION_DTL_ID": 9}]}
    check("plain list payload is accepted", len(parse_tile(plain)) == 1)

    try:
        parse_tile({"Total Works Recommended": "{not json"})
        check("unparseable tile value raises SourceError", False)
    except SourceError:
        check("unparseable tile value raises SourceError", True)


def test_scalars() -> None:
    print("\n[2] field helpers — parity with offline.py")
    check("dd-Mon-yyyy parses", emap.iso_date("22-Jan-2025") == "2025-01-22")
    check("tenure timestamp parses",
          emap.iso_date("Jun 4, 2024 12:00:00 AM") == "2024-06-04")
    check("'NA' becomes None", emap.iso_date("NA") is None)
    check("blank becomes None", emap.iso_date("   ") is None)

    code = emap.work_code("WS/MP487/2024-2025/167969-Construction of roads")
    check("work code extracted", code == "WS/MP487/2024-2025/167969", str(code))
    check("pre-sanction row has no code",
          emap.work_code("NA-Construction of community centers") is None)
    check("activity type after the code",
          emap.activity_type("WS/MP487/2024-2025/167969-Construction of roads")
          == "Construction of roads")
    check("activity type after NA-",
          emap.activity_type("NA-Street lights") == "Street lights")

    check("FY from the work code",
          emap.financial_year("WS/MP487/2024-2025/167969", None) == "2024-25")
    check("FY from an April date", emap.financial_year(None, "2025-04-02") == "2025-26")
    check("FY from a March date", emap.financial_year(None, "2025-03-31") == "2024-25")

    check("district out of the IDA label",
          emap.district_from_ida("GHAZIABAD(DISTRICT MAGISTRATE GHAZIABAD_IDA)")
          == "GHAZIABAD")
    check("SC reservation read", emap.reservation("BARABANKI(SC)") == (1, 0))
    check("ST reservation read", emap.reservation("DAHOD(ST)") == (0, 1))
    check("unreserved constituency", emap.reservation("ALIGARH") == (0, 0))
    check("amount parses", emap.number("1,100,000.00") == 1100000.0)
    check("non-numeric amount is None", emap.number("NA") is None)


def test_mapping(tiles: dict[str, list[dict]]) -> list:
    print("\n[3] to_works — builds validated Work records")
    t0 = time.monotonic()
    works = emap.to_works(tiles)
    dt = time.monotonic() - t0

    ids = emap.join_report(tiles)
    n_rec = len(ids["recommended"])
    check("one Work per recommended id", len(works) == n_rec,
          f"{len(works)} works / {n_rec} ids ({dt * 1000:.0f} ms)")

    orphans = {t: len(ids[t] - ids["recommended"]) for t in RECORD_TILES
               if t != "recommended"}
    check("no orphan ids across tiles", all(v == 0 for v in orphans.values()),
          str(orphans))

    check("every work has an id", all(w.work_id for w in works))
    check("work ids are unique", len({w.work_id for w in works}) == len(works))
    coded = [w for w in works if w.work_id.startswith("WS/")]
    es_keyed = [w for w in works if w.work_id.startswith("ES-")]
    check("sanctioned works keep their WS code", bool(coded),
          f"{len(coded)} coded, {len(es_keyed)} pre-sanction keyed ES-")
    check("pre-sanction ids carry the house (ES-LS-<id>)",
          all(w.work_id.startswith("ES-LS-") for w in es_keyed),
          ", ".join(w.work_id for w in es_keyed[:3]))
    check("payments attach through the expenditure tile's WORK_ID",
          sum(1 for w in works if w.total_paid) > 0,
          f"{sum(1 for w in works if w.total_paid)} works with payments")
    check("category is populated (anomaly peer-group key)",
          all(w.category for w in works))
    check("house is set", {w.house for w in works} == {"LS"})
    check("era is tagged", all(w.era in ("pre2023", "post2023") for w in works))
    check("costs present", sum(1 for w in works if w.estimated_cost) == len(works),
          f"{sum(1 for w in works if w.estimated_cost)} with a cost")
    check("vendors resolved on paid works",
          sum(1 for w in works if w.vendor_name) > 0,
          f"{sum(1 for w in works if w.vendor_name)} with a vendor")
    check("expenditure never exceeds nothing",
          all(w.expenditure is None or w.expenditure >= 0 for w in works))
    return works


def test_portal_fields(tiles: dict[str, list[dict]], works: list) -> None:
    """Fields the portal sends that the CSV exports never had."""
    print("\n[3a] portal-only fields — category, letter, term, agency, vendor id")
    categories = {"Normal/Others", "Repair and Renovation", "Trust and Society",
                  "Bar and Associations"}
    check("every work carries the portal's own category",
          all(w.work_category in categories for w in works),
          str(sorted({str(w.work_category) for w in works})))
    check("and its recommendation letter",
          all(w.letter_no and w.letter_no.startswith("LN/") for w in works))
    check("and the recommending member's term, as ISO dates",
          all(w.term_start and w.term_end and w.term_start < w.term_end
              and len(w.term_end) == 10 for w in works),
          str({(w.term_start, w.term_end) for w in works}))
    paid = [w for w in works if w.payment_count]
    check("every paid work names its implementing agency and vendor id",
          paid and all(w.implementing_agency and w.vendor_id for w in paid),
          f"{len(paid)} paid works; e.g. {paid[0].implementing_agency!r}" if paid else "none")
    check("an unpaid work has neither",
          all(w.implementing_agency is None and w.vendor_id is None
              for w in works if not w.payment_count))
    check("the implementing agency is not the district authority",
          all(w.implementing_agency != w.ia_name for w in paid))

    # One work paid to two different vendors who share a name: the larger one
    # is the primary vendor, and the two are not added together.
    base = next(r for r in tiles["expenditure"] if r.get("WORK_RECOMMENDATION_DTL_ID"))
    code = base["WORK_ID"]
    rows = [{**base, "VENDOR_NAME": "Ajay Kumar", "VENDOR_ID": 11, "FUND_DISBURSED_AMT": 60.0},
            {**base, "VENDOR_NAME": "Ajay Kumar", "VENDOR_ID": 22, "FUND_DISBURSED_AMT": 50.0},
            {**base, "VENDOR_NAME": "Ajay Kumar", "VENDOR_ID": 22, "FUND_DISBURSED_AMT": 30.0},
            {**base, "VENDOR_NAME": "Other Firm", "VENDOR_ID": 33, "FUND_DISBURSED_AMT": 70.0}]
    work = next(w for w in emap.to_works({"expenditure": rows}) if w.work_id == code)
    check("payments add up per vendor id, not per name",
          (work.vendor_id, work.vendor_name) == ("22", "Ajay Kumar"),
          f"{work.vendor_id} {work.vendor_name}")
    rec = next(r for r in tiles["recommended"] if emap.row_code(r) == code)
    bare = {k: v for k, v in rec.items()
            if k not in ("WORK_CATEGORY", "LETTER_NO", "TENURE_START_DATE", "TENURE_END_DATE")}
    filled = next(w for w in emap.to_works({"recommended": [bare], "expenditure": rows})
                  if w.work_id == code)
    mapping = emap.map_shard(tiles)
    listed = [r for r in tiles["expenditure"] if r.get("WORK_RECOMMENDATION_DTL_ID") is not None]
    check("every payment record the report lists becomes one payment row",
          len(mapping.payments) == len(listed) == sum(v["payments"] for v in mapping.listing.values()),
          f"{len(mapping.payments)} rows / {len(listed)} listed")
    by_work = {}
    for p in mapping.payments:
        by_work.setdefault(p["work_id"], []).append(p)
    works_by_id = {w.work_id: w for w in mapping.works}
    check("a work's payment rows add up to its total paid and payment count",
          all(abs(sum(p["amount"] for p in rows) - works_by_id[w].total_paid) < 0.01
              and len(rows) == works_by_id[w].payment_count for w, rows in by_work.items()))
    check("rows are numbered 1..n per work, oldest first",
          all([p["seq"] for p in rows] == list(range(1, len(rows) + 1))
              and [p["paid_on"] for p in rows] == sorted(p["paid_on"] for p in rows)
              for rows in by_work.values()))
    twice = emap.payment_rows("W", [rows[0], rows[0], rows[1]] if len(rows := [
        r for r in tiles["expenditure"] if r.get("WORK_RECOMMENDATION_DTL_ID")]) > 1 else [])
    reordered = emap.payment_rows("W", [rows[1], rows[0], rows[0]])
    check("two identical records are both kept, and the order the report lists them in "
          "does not change their numbers", len(twice) == 3 and twice == reordered)
    check("a field one report lacks is taken from a later report that has it",
          filled.letter_no == emap.squash(base["LETTER_NO"]) and filled.term_end is not None
          and filled.work_category is None, f"{filled.letter_no} {filled.term_end}")


def test_shared_portal_ids(tiles: dict[str, list[dict]]) -> None:
    """The portal's record id is not unique: two different works can share it."""
    print("\n[3b] two different works sharing one portal id are both kept")
    import copy

    base = emap.to_works(tiles)
    paid = next(w for w in base if w.total_paid and w.work_id.startswith("WS/"))
    dtl = int(paid.work_id.rsplit("/", 1)[1])
    donor = next(r for r in tiles["recommended"]
                 if r.get("WORK_RECOMMENDATION_DTL_ID") not in (None, dtl))

    # Rajya Sabha UP, 13 Sep 2026: a sanctioned work and an unrelated work
    # awaiting sanction, same id, same report.
    pending = copy.deepcopy(donor)
    pending.update({"WORK_RECOMMENDATION_DTL_ID": dtl,
                    "ACTIVITY_NAME": "NA-Construction of rooms and halls in school and colleges",
                    "MP_NAME": "Another Member", "WORK_STAGE": "NA",
                    "SANCTION_DATE": None, "SANCTION_AMOUNT": None,
                    "WORK_DESCRIPTION": "An unrelated school work", "Sno": 99999})
    shared = {**tiles, "recommended": tiles["recommended"] + [pending]}
    works = {w.work_id: w for w in emap.to_works(shared)}
    check("both works survive", len(works) == len(base) + 1,
          f"{len(base)} -> {len(works)}")
    other = works.get(f"ES-LS-{dtl}")
    check("the pending work is keyed by house and id", other is not None)
    check("it does not inherit the sanctioned work's stage, sanction or payments",
          other is not None and other.status == "NA" and other.sanction_date is None
          and not other.total_paid and other.vendor_name is None,
          other and f"status={other.status}, paid={other.total_paid}")
    kept = works.get(paid.work_id)
    check("the sanctioned work keeps its own payments",
          kept is not None and kept.total_paid == paid.total_paid
          and kept.vendor_name == paid.vendor_name,
          kept and f"paid={kept.total_paid} vendor={kept.vendor_name}")
    check("a shared id across a sanctioned and a pending work is not a duplicate listing",
          emap.map_shard(shared).duplicates == [])

    # Rajampet (LS) and a Madhya Pradesh Rajya Sabha member, 13 Sep 2026.
    rs = copy.deepcopy(pending)
    rs["HOUSE_OF_PARLIAMENT"] = 1
    ls_id = emap.to_works({"recommended": [pending]})[0].work_id
    rs_id = emap.to_works({"recommended": [rs]})[0].work_id
    check("the same pending id in two houses gives two work ids",
          ls_id != rs_id and rs_id == f"ES-RS-{dtl}", f"{ls_id} / {rs_id}")

    twin = copy.deepcopy(pending)
    twin["WORK_DESCRIPTION"] = "A second, different pending work"
    both = emap.map_shard({**tiles, "recommended": tiles["recommended"] + [pending, twin]})
    ids = {w.work_id for w in both.works}
    check("two different works listed under one key are both kept, as the portal shows both",
          {f"ES-LS-{dtl}", f"ES-LS-{dtl}#2"} <= ids, str(sorted(i for i in ids if "ES-" in i)))
    check("and the double listing is reported",
          any(d["work_id"] == f"ES-LS-{dtl}" and d["distinct_records"] == 2
              for d in both.duplicates), str(both.duplicates)[:120])
    echo = copy.deepcopy(pending)
    echo["Sno"], echo["ATTACH_ID"] = 100000, 42
    listed_twice = emap.map_shard({**tiles, "recommended": tiles["recommended"] + [pending, echo]})
    check("the same record listed twice (row number or attachment differ) is one work",
          sum(1 for w in listed_twice.works if w.work_id.startswith(f"ES-LS-{dtl}")) == 1)
    check("counted twice, as the portal's own count counts it",
          listed_twice.listing[f"ES-LS-{dtl}"]["in_recommended"] == 2)

    # A work sanctioned between two report reads: pending in one report, coded
    # in the next. Matching stays exact, so nothing is attached on a guess; the
    # sanctioned record stands as its own work until a consistent read retires
    # the pending one.
    uncoded = copy.deepcopy(next(r for r in tiles["recommended"]
                                 if r.get("WORK_RECOMMENDATION_DTL_ID") == dtl))
    uncoded["ACTIVITY_NAME"] = "NA-" + (emap.activity_type(uncoded["ACTIVITY_NAME"]) or "")
    raced = {**tiles, "recommended": [r for r in tiles["recommended"]
                                      if r.get("WORK_RECOMMENDATION_DTL_ID") != dtl]
             + [uncoded]}
    split = {w.work_id: w for w in emap.map_shard(raced).works}
    check("a sanction read a moment later never attaches to a pending work by id alone",
          split.get(f"ES-LS-{dtl}") is not None and not split[f"ES-LS-{dtl}"].total_paid
          and split.get(paid.work_id) is not None
          and split[paid.work_id].total_paid == paid.total_paid,
          f"pending paid={split.get(f'ES-LS-{dtl}') and split[f'ES-LS-{dtl}'].total_paid}")


def test_portal_figures(tiles: dict[str, list[dict]]) -> None:
    """ASTRA must reproduce the portal's four tiles from what it maps."""
    print("\n[3c] every portal tile figure is reproduced, and nothing listed is dropped")
    import copy

    mapping = emap.map_shard(tiles)
    figures = emap.portal_figures(mapping.listing,
                                  {w.work_id: w.model_dump() for w in mapping.works})
    real = {t: [r for r in tiles[t] if r.get("WORK_RECOMMENDATION_DTL_ID") is not None]
            for t in RECORD_TILES}
    check("Works Recommended count and rupees equal the report's",
          figures["recommended"] == (len(real["recommended"]), round(sum(
              r["RECOMMENDED_AMOUNT"] or 0 for r in real["recommended"]), 2)),
          str(figures["recommended"]))
    check("Works Sanctioned count and rupees equal the report's",
          figures["sanctioned"] == (len(real["sanctioned"]), round(sum(
              r["SANCTION_AMOUNT"] or 0 for r in real["sanctioned"]), 2)),
          str(figures["sanctioned"]))
    sanction_of = {emap._key(r): r["SANCTION_AMOUNT"] or 0 for r in real["sanctioned"]}
    check("Works Completed is the sanctioned amount of completed works (portal definition)",
          figures["completed"] == (len(real["completed"]), round(sum(
              sanction_of.get(emap._key(r), 0) for r in real["completed"]), 2)),
          str(figures["completed"]))
    check("expenditure is every payment tranche",
          abs(figures["expenditure"][1] - sum(r["FUND_DISBURSED_AMT"] or 0
                                               for r in real["expenditure"])) < 0.01,
          str(figures["expenditure"]))

    # a sanctioned, paid work that no recommended report lists (608 on 13 Sep 2026)
    held_back = next(r for r in real["sanctioned"]
                     if any(e["WORK_RECOMMENDATION_DTL_ID"] == r["WORK_RECOMMENDATION_DTL_ID"]
                            for e in real["expenditure"]))
    missing = {**tiles, "recommended": [r for r in tiles["recommended"]
                                        if emap._key(r) != emap._key(held_back)]}
    m = emap.map_shard(missing)
    work = next((w for w in m.works if w.work_id == emap.row_code(held_back)), None)
    check("a work listed only from the sanctioned report onward is still a work",
          work is not None and work.sanctioned_amount and work.total_paid,
          work and f"sanctioned={work.sanctioned_amount} paid={work.total_paid}")
    check("it is not counted as recommended, exactly as the portal does not",
          m.listing[work.work_id]["in_recommended"] == 0
          and m.listing[work.work_id]["in_sanctioned"] == 1 if work else False)
    after = emap.portal_figures(m.listing, {w.work_id: w.model_dump() for w in m.works})
    check("so sanctioned, completed and expenditure figures are unchanged by its absence",
          after["sanctioned"] == figures["sanctioned"]
          and after["completed"] == figures["completed"]
          and abs(after["expenditure"][1] - figures["expenditure"][1]) < 0.01)

    token = copy.deepcopy(real["recommended"][0])
    token.update({"WORK_RECOMMENDATION_DTL_ID": 999001, "ACTIVITY_NAME": "NA-Test",
                  "RECOMMENDED_AMOUNT": 1.0, "WORK_DESCRIPTION": "test"})
    kept = emap.map_shard({**tiles, "recommended": tiles["recommended"] + [token]})
    check("a one-rupee placeholder the portal lists is kept, not filtered as fake",
          any(w.work_id == "ES-LS-999001" and w.estimated_cost == 1.0 for w in kept.works))


def test_corpus_equivalence(works: list) -> None:
    """The check that actually matters: does the API agree with the CSVs?"""
    print("\n[4] corpus equivalence — API vs the CSV rows already in astra.db")
    if not DB.exists():
        skip("API amounts match the CSV corpus", "data/astra.db not present")
        return
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT work_id, estimated_cost FROM works "
            "WHERE upper(constituency) = ?", (FIX_NAME,)).fetchall()
    finally:
        con.close()
    if not rows:
        skip("API amounts match the CSV corpus", f"no {FIX_NAME} rows in the corpus")
        return

    csv_cost = {r[0]: r[1] for r in rows}
    api_cost = {w.work_id: w.estimated_cost for w in works
                if w.work_id.startswith("WS/")}
    shared = set(csv_cost) & set(api_cost)
    check("work codes overlap", bool(shared),
          f"{len(shared)} shared of {len(csv_cost)} CSV / {len(api_cost)} API coded")

    differing = [wid for wid in shared
                 if csv_cost[wid] is not None and api_cost[wid] is not None
                 and abs(csv_cost[wid] - api_cost[wid]) >= 0.01]
    check("every shared work has an identical recommended amount",
          not differing,
          f"{len(shared) - len(differing)}/{len(shared)} identical"
          + (f"; differing: {differing[:3]}" if differing else ""))

    api_only = set(api_cost) - set(csv_cost)
    check("the API introduces no unknown work codes", not api_only,
          f"{len(api_only)} API-only" if api_only else "none")


def test_rajya_sabha_shape() -> None:
    """Rajya Sabha records carry a membership type where a seat would be."""
    print("\n[4b] Rajya Sabha records — no pseudo-constituency, no SC/ST distortion")
    import pandas as pd
    from astra.agents.compliance import ComplianceAgent
    from astra.config import load_rules

    rs_row = {
        "WORK_RECOMMENDATION_DTL_ID": 900001, "HOUSE_OF_PARLIAMENT": 1,
        "ACTIVITY_NAME": "NA-Construction of community centers and community halls",
        "STATE_NAME": "Bihar", "CONSTITUENCY": "Sitting Rajya Sabha",
        "MP_NAME": "Shri Example Member (2022-28)",
        "IDA_NAME": "PATNA(DISTRICT MAGISTRATE PATNA_IDA)",
        "RECOMMENDATION_DATE": "12-Mar-2025", "RECOMMENDED_AMOUNT": 1500000.0,
        "WORK_STAGE": "Pending for Sanction", "SANCTION_DATE": "NA",
    }
    work = emap.to_works({"recommended": [rs_row]})[0]
    check("a Rajya Sabha work is marked RS", work.house == "RS")
    check("its membership type is not stored as a constituency",
          work.constituency is None, repr(work.constituency))
    check("it carries no reserved-seat flags", (work.is_sc_constituency,
                                                work.is_st_constituency) == (0, 0))

    key = ComplianceAgent._ckey({"constituency": work.constituency,
                                 "mp_name": work.mp_name, "state": work.state})
    other = ComplianceAgent._ckey({"constituency": None,
                                   "mp_name": "Smt Another Member (2024-30)",
                                   "state": work.state})
    check("each RS member is a separate constituency-level entity",
          key != other and key.startswith("Shri Example Member"), key)

    # R-SCST-02: a state where reserved Lok Sabha seats clear the benchmark on
    # their own, and a large block of Rajya Sabha spend that would drag the
    # share below it if it were counted.
    rule = load_rules()["rules"]["scst_reserved_proxy"]
    ls = [{"work_id": f"LS-{i}", "state": "BIHAR", "house": "LS",
           "constituency": "GOPALGANJ(SC)" if i < 3 else f"SEAT {i}",
           "is_sc_constituency": 1 if i < 3 else 0, "is_st_constituency": 0,
           "expenditure": 10_000_000.0} for i in range(10)]
    rs = [{"work_id": f"RS-{i}", "state": "BIHAR", "house": "RS",
           "constituency": None, "is_sc_constituency": 0, "is_st_constituency": 0,
           "expenditure": 10_000_000.0} for i in range(20)]
    agent = ComplianceAgent()
    ls_only = agent._scst_reserved_proxy(pd.DataFrame(ls), rule)
    mixed = agent._scst_reserved_proxy(pd.DataFrame(ls + rs), rule)
    sc = [f for f in mixed if "SC-reserved" in f.summary]
    check("reserved LS seats at 30% of state spend raise no SC finding",
          not [f for f in ls_only if "SC-reserved" in f.summary])
    check("adding Rajya Sabha spend does not manufacture one",
          not sc, sc[0].summary[:90] if sc else "none")

    legacy = [{**r, "house": None} for r in ls]
    check("the CSV corpus, which has no house, is judged exactly as before",
          len(agent._scst_reserved_proxy(pd.DataFrame(legacy), rule)) == len(ls_only))


def test_breaker() -> None:
    print("\n[5] circuit breaker")
    cb = CircuitBreaker(window=8, threshold=0.5, base_seconds=30)
    for _ in range(8):
        cb.record(False)
    check("opens on a sustained failure rate", cb.is_open,
          f"reopens in {cb.opens_in():.0f}s")
    cb.record(True)
    check("a success closes it", not cb.is_open)

    print("      while open: the real error, and a trial every few minutes")
    cb = CircuitBreaker(window=8, threshold=0.5, base_seconds=600, trial_seconds=0.3)
    for _ in range(8):
        cb.record(False, "ReadTimeout: portal did not answer in 45 s")
    status = cb.status()
    check("the last real error is kept, not replaced by 'circuit open'",
          status["open"] and status["last_error"] == "ReadTimeout: portal did not answer in 45 s"
          and status["failing_since"] and status["last_error_at"], json.dumps(status)[:160])
    blocked = False
    try:
        cb.check()
    except CircuitOpen:
        blocked = True
    check("calls are held back while it is open", blocked)
    check("the next attempt is due at the trial, not at the end of the backoff",
          status["next_attempt_at"] is not None
          and (datetime.fromisoformat(status["next_attempt_at"])
               - datetime.fromisoformat(status["checked_at"])).total_seconds() <= 1.0,
          f"{status['checked_at']} -> {status['next_attempt_at']}")
    check("no trial is granted before its interval", cb.allow_trial() is False)
    time.sleep(0.35)
    check("once the interval has passed a trial is granted", cb.allow_trial() is True)
    passed = True
    try:
        cb.check()
    except CircuitOpen:
        passed = False
    check("and a call may go through", passed)
    wait_before = cb.opens_in()
    cb.record(False, "HTTP 503 from /getTilesData")
    still_blocked = False
    try:
        cb.check()
    except CircuitOpen:
        still_blocked = True
    check("a failed trial leaves it open without lengthening the backoff",
          cb.is_open and cb.opens_in() <= wait_before and still_blocked,
          f"{wait_before:.1f}s -> {cb.opens_in():.1f}s")
    check("and updates the error shown", cb.status()["last_error"] == "HTTP 503 from /getTilesData")
    time.sleep(0.35)
    cb.allow_trial()
    cb.record(True)
    closed = cb.status()
    check("a successful trial closes it and clears the failing-since time",
          not cb.is_open and not closed["open"] and closed["failing_since"] is None
          and closed["next_attempt_at"] is None, json.dumps(closed)[:160])


# --------------------------------------------------------------------- live
def test_live(client: EsakshiClient) -> bool:
    print("\n[6] live portal — enumeration, watermark, fetch")
    try:
        states = client.states()
    except SourceError as exc:
        skip("portal reachable", str(exc)[:80])
        return False
    check("36 states enumerate", len(states) == 36, f"{len(states)} states")

    shards = client.shards(houses=(HOUSE_LS,))
    check("543 Lok Sabha constituencies enumerate", len(shards) == 543,
          f"{len(shards)} shards")

    wm = client.watermark(shard_combo(house=HOUSE_LS))
    rec = wm.counts.get("Works Recommended")
    check("national watermark returns a plausible count",
          isinstance(rec, int) and rec > 100_000, f"recommended={rec:,}")
    check("national watermark is not flagged suspicious", not wm.suspicious_zero)

    # 20 random shards must fetch and parse
    sample = random.Random(20260912).sample(shards, 20)
    t0 = time.monotonic()
    rows, failures = 0, []
    for shard in sample:
        try:
            rows += len(client.tile_report(shard.combo, "recommended"))
        except SourceError as exc:
            failures.append(f"{shard.label}: {exc}")
    wall = time.monotonic() - t0
    check("20 random shards fetch and parse", not failures,
          f"{rows:,} rows in {wall:.1f}s"
          + (f"; failures: {failures[:2]}" if failures else ""))

    # an id that does not exist answers 200 with zeros — it must be flagged
    bogus = client.watermark(shard_combo(999, house=HOUSE_LS))
    check("invalid shard id is flagged suspicious_zero, not believed",
          bogus.suspicious_zero,
          f"counts={ {k: v for k, v in list(bogus.counts.items())[:2]} }")

    # a malformed combo returns an HTML 500 — it must raise, not crash
    try:
        client.watermark("not-a-combo")
        check("malformed combo raises SourceError", False, "no error raised")
    except SourceError as exc:
        check("malformed combo raises SourceError", True, f"status={exc.status}")

    # an unknown tile key answers {} — parse it as empty, do not invent rows
    unknown = client.tile_report(shard_combo(12, house=HOUSE_LS), "No Such Tile")
    check("unknown tile key yields no rows", unknown == [])
    return True


# --------------------------------------------------------------------- main
def main() -> int:
    refresh = "--refresh" in sys.argv
    offline_only = "--offline" in sys.argv
    print("=" * 74)
    print("ASTRA Stage A — eSAKSHI client and field mapping")
    print("=" * 74)

    client = None if offline_only else EsakshiClient()
    tiles = load_fixtures()
    if (tiles is None or refresh) and client is not None:
        print(f"\n[0] refreshing fixtures for {FIX_NAME} from the portal")
        try:
            tiles = write_fixtures(client)
            print("   " + ", ".join(f"{k}={len(v)}" for k, v in tiles.items()))
        except SourceError as exc:
            print(f"   could not refresh fixtures: {exc}")

    test_parse_tile()
    test_scalars()
    if tiles is None:
        skip("to_works mapping", "no fixtures and no network")
        skip("corpus equivalence", "no fixtures and no network")
    else:
        works = test_mapping(tiles)
        test_portal_fields(tiles, works)
        test_shared_portal_ids(tiles)
        test_portal_figures(tiles)
        test_corpus_equivalence(works)
    test_rajya_sabha_shape()
    test_breaker()

    if client is not None:
        test_live(client)
        client.close()
    else:
        skip("live portal checks", "--offline requested")

    print("\n" + "=" * 74)
    failed = [r for r in results if r[0] == FAIL]
    passed = sum(1 for r in results if r[0] == PASS)
    skipped = sum(1 for r in results if r[0] == SKIP)
    print(f"  {passed} passed, {len(failed)} failed, {skipped} skipped")
    for _, name, detail in failed:
        print(f"    FAILED: {name} — {detail}")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
