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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astra.ingestion import esakshi_map as emap             # noqa: E402
from astra.ingestion.esakshi_api import (                   # noqa: E402
    HOUSE_LS, RECORD_TILES, CircuitBreaker, EsakshiClient, SourceError,
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


def test_breaker() -> None:
    print("\n[5] circuit breaker")
    cb = CircuitBreaker(window=8, threshold=0.5, base_seconds=30)
    for _ in range(8):
        cb.record(False)
    check("opens on a sustained failure rate", cb.is_open,
          f"reopens in {cb.opens_in():.0f}s")
    cb.record(True)
    check("a success closes it", not cb.is_open)


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
        test_corpus_equivalence(works)
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
