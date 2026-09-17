"""Stage C gate: the control loop.

    python tests/test_poller.py            # offline: a scripted fake portal
    python tests/test_poller.py --live     # also probe the real portal once

The offline group drives the real `Poller` against a fake client that replays
the Aligarh fixtures, so the descent, the gates, the escalation and the restart
behaviour are all exercised with no network and no risk. The live group only
confirms the real portal still answers the way the loop assumes.

Everything runs against a throwaway database in a temp directory.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="astra-stage-c-"))
os.environ["ASTRA_DB_PATH"] = str(_TMP / "scratch.db")
os.environ["ASTRA_PROCESSED_DIR"] = str(_TMP / "processed")
os.environ["ASTRA_SHARD_CACHE"] = str(_TMP / "shards")
os.environ["ASTRA_ESCALATE_AFTER"] = "3"
# The rotation depends on the time of day, so it is off unless a test turns it
# on; [13] and [14] exercise it and the analysis re-run explicitly.
os.environ["ASTRA_ROLLING_AREAS"] = "0"
# [16] drives the nightly photo check with a fake portal; no pause between requests.
os.environ["ASTRA_PHOTO_PAUSE_SECONDS"] = "0"

from astra import db                                            # noqa: E402
from astra.ingestion import poller as pmod                      # noqa: E402
from astra.ingestion import validate                            # noqa: E402
from astra.ingestion.esakshi_api import (                       # noqa: E402
    HOUSE_LS, RECORD_TILES, CircuitBreaker, CircuitOpen, EsakshiClient, SourceError,
    Watermark,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "esakshi"
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


# ------------------------------------------------------------- a fake portal
class FakePortal:
    """A scripted eSAKSHI: three states, two constituencies in the one that moves.

    Counts are kept exactly additive, because the whole descent depends on
    that property and a fake that cheated on it would prove nothing.
    """

    STATES = [{"STATE_NAME": "UTTAR PRADESH", "STATE_ID": 33},
              {"STATE_NAME": "GOA", "STATE_ID": 12},
              {"STATE_NAME": "KERALA", "STATE_ID": 36}]
    CONSTITUENCIES = {33: [{"ID": 418, "CAPTION": "ALIGARH"},
                           {"ID": 419, "CAPTION": "BARABANKI(SC)"}],
                      12: [{"ID": 105, "CAPTION": "SOUTH GOA"}],
                      36: [{"ID": 500, "CAPTION": "KOLLAM"}]}

    def __init__(self, tiles: dict[str, list[dict]]):
        self.tiles = tiles
        self.n = {"2:0:0": 30, "2:33:0": 20, "2:33:418": 10, "2:33:419": 10,
                  "2:12:0": 5, "2:12:105": 5, "2:36:0": 5, "2:36:500": 5}
        self.calls: list[tuple[str, str]] = []
        self.fail_shards: set[str] = set()
        self.truncate_shards: set[str] = set()
        self.truncate_once: set[str] = set()
        self.zero_shards: set[str] = set()
        #: exact reports for one slice, served verbatim, with the watermark
        #: computed from them the way the portal computes its tiles
        self.shard_tiles: dict[str, dict[str, list[dict]]] = {}
        #: counts the portal claims regardless of its reports (an inconsistent read)
        self.claimed_counts: dict[str, dict[str, int]] = {}
        #: portal ids the shared reports no longer list
        self.dropped: set = set()
        #: how many shared recommended rows a slice's report is cut from
        self.slice_n: dict[str, int] = {}
        self.breaker = _NullBreaker()

    # -- the interface Poller uses
    def states(self):
        return list(self.STATES)

    def constituencies(self, state_id):
        return list(self.CONSTITUENCIES.get(int(state_id), []))

    def shards(self, houses=(HOUSE_LS,)):
        from astra.ingestion.esakshi_api import Shard
        out = []
        for st in self.STATES:
            for con in self.CONSTITUENCIES[st["STATE_ID"]]:
                out.append(Shard(HOUSE_LS, st["STATE_ID"], con["ID"],
                                 st["STATE_NAME"], con["CAPTION"]))
        return out

    def _shard_id(self, combo: str) -> str:
        state, const, _mp, house = combo.split(",")[:4]
        return f"{house}:{state}:{const}"

    def _report(self, shard_id: str) -> dict[str, list[dict]]:
        """The four reports a slice lists, before any simulated fault.

        The later reports list only works the slice's recommended report
        holds, as on the portal; otherwise every unlisted payment in the shared
        fixture would become a work of its own.
        """
        if shard_id in self.shard_tiles:
            return self.shard_tiles[shard_id]
        want = self.slice_n.get(shard_id, self.n.get(shard_id, 0))
        rec = [r for r in self.tiles["recommended"][:want]
               if r.get("WORK_RECOMMENDATION_DTL_ID") not in self.dropped]
        held = {r.get("WORK_RECOMMENDATION_DTL_ID") for r in rec}
        out = {"recommended": rec}
        for tile in ("sanctioned", "completed", "expenditure"):
            out[tile] = [r for r in self.tiles.get(tile) or []
                         if r.get("WORK_RECOMMENDATION_DTL_ID") in held]
        return out

    @staticmethod
    def figures(tiles: dict[str, list[dict]]) -> dict[str, tuple]:
        """The portal's four tiles, computed straight from raw rows.

        Deliberately independent of `esakshi_map.portal_figures`, so a parity
        check against it tests the mapping and the storage, not itself.
        """
        from astra.ingestion import esakshi_map as emap
        real = {t: [r for r in tiles.get(t) or []
                    if r.get("WORK_RECOMMENDATION_DTL_ID") is not None]
                for t in RECORD_TILES}
        sanctioned = {(r["WORK_RECOMMENDATION_DTL_ID"], emap.row_code(r)):
                      r.get("SANCTION_AMOUNT") or 0.0 for r in real["sanctioned"]}
        return {
            "recommended": (len(real["recommended"]),
                            sum(r.get("RECOMMENDED_AMOUNT") or 0.0 for r in real["recommended"])),
            "sanctioned": (len(real["sanctioned"]),
                           sum(r.get("SANCTION_AMOUNT") or 0.0 for r in real["sanctioned"])),
            "completed": (len(real["completed"]),
                          sum(sanctioned.get((r["WORK_RECOMMENDATION_DTL_ID"], emap.row_code(r)), 0.0)
                              for r in real["completed"])),
            "expenditure": (None, sum(r.get("FUND_DISBURSED_AMT") or 0.0
                                      for r in real["expenditure"])),
        }

    def watermark(self, combo: str) -> Watermark:
        from astra.ingestion.esakshi_api import TILE_KEYS
        shard_id = self._shard_id(combo)
        self.calls.append(("watermark", shard_id))
        if shard_id in self.fail_shards:
            raise SourceError("HTTP 500", path="/getTilesData", status=500)
        wm = Watermark(combo=combo, fetched_at="now")
        if shard_id in self.shard_tiles:
            figures = self.figures(self.shard_tiles[shard_id])
            wm.counts = {TILE_KEYS[t]: n for t, (n, _) in figures.items() if n is not None}
            wm.totals = {TILE_KEYS[t]: total for t, (_, total) in figures.items()}
        else:
            count = 0 if shard_id in self.zero_shards else self.n.get(shard_id, 0)
            wm.counts = {"Works Recommended": count}
            # The portal's total is the sum of the amounts it lists. A fake that
            # invented one would fail the parity check for the wrong reason.
            listed = [r for r in self._report(shard_id)["recommended"]
                      if r.get("WORK_RECOMMENDATION_DTL_ID") is not None][:count]
            wm.totals = {"Works Recommended": float(sum(
                r.get("RECOMMENDED_AMOUNT") or 0 for r in listed))}
        wm.counts.update(self.claimed_counts.get(shard_id, {}))
        wm.suspicious_zero = shard_id in self.zero_shards
        return wm

    def tile_report(self, combo: str, tile: str) -> list[dict]:
        shard_id = self._shard_id(combo)
        self.calls.append(("report", shard_id))
        if shard_id in self.fail_shards:
            raise SourceError("HTTP 500", path="/getTilesReportData", status=500)
        report = self._report(shard_id)
        rows = report[tile]
        if shard_id in self.shard_tiles:
            return list(rows)
        short = shard_id in self.truncate_shards
        if tile == "recommended" and not short and shard_id in self.truncate_once:
            # short on the first read only, correct from then on — exactly
            # what the first national sweep saw on 7 of 543 constituencies
            self.truncate_once.discard(shard_id)
            short = True
        if short:
            kept = report["recommended"][:max(1, len(report["recommended"]) // 4)]
            if tile == "recommended":
                rows = kept
            elif shard_id in self.truncate_shards:
                held = {r.get("WORK_RECOMMENDATION_DTL_ID") for r in kept}
                rows = [r for r in rows if r.get("WORK_RECOMMENDATION_DTL_ID") in held]
        return list(rows)

    def close(self):
        pass

    # -- test helpers
    def bump(self, shard_id: str, by: int = 1) -> None:
        """Move a constituency and carry the change up, keeping sums exact."""
        house, state, _const = shard_id.split(":")
        self.n[shard_id] += by
        self.n[f"{house}:{state}:0"] += by
        self.n[f"{house}:0:0"] += by

    def report_calls(self) -> list[str]:
        return [s for kind, s in self.calls if kind == "report"]


class _NullBreaker:
    is_open = False

    def check(self):
        return None

    def record(self, ok):
        return None

    def opens_in(self):
        return 0.0


def load_tiles():
    tiles = {}
    for tile in RECORD_TILES:
        path = FIXTURES / f"aligarh_{tile}.json"
        if not path.exists():
            return None
        tiles[tile] = json.loads(path.read_text(encoding="utf-8"))
    return tiles


def fresh_poller(portal) -> pmod.Poller:
    p = pmod.Poller(client=portal, houses=(HOUSE_LS,), verbose=False)
    p.refresh_registry()
    return p


# ---------------------------------------------------------------- the tests
def test_quiet_cycle(tiles) -> None:
    print("\n[1] a quiet minute costs one request")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    poller.heartbeat()                        # cold start: everything looks moved
    portal.calls.clear()
    report = poller.heartbeat()
    check("second cycle issues exactly one request", report.requests == 1,
          f"requests={report.requests}, calls={portal.calls}")
    check("nothing was fetched", report.dirty == (), str(report.dirty))
    check("no states were probed", report.states_probed == 0)


def test_descent(tiles) -> None:
    print("\n[2] a change is localised to the exact constituency")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    poller.heartbeat()
    portal.calls.clear()

    portal.bump("2:33:418", 1)
    report = poller.heartbeat()

    check("the national probe noticed", report.houses_moved == (HOUSE_LS,),
          str(report.houses_moved))
    check("all three states were probed", report.states_probed == 3,
          f"{report.states_probed} states")
    check("only the moved state's constituencies were probed",
          report.constituencies_probed == 2,
          f"{report.constituencies_probed} constituencies")
    check("exactly the changed shard was fetched", report.dirty == ("2:33:418",),
          str(report.dirty))
    check("records were stored", report.stored > 0, f"{report.stored} records")
    check("untouched shards were never fetched",
          portal.report_calls() == ["2:33:418"] * len(RECORD_TILES),
          str(sorted(set(portal.report_calls()))))

    print("\n[3] an unchanged shard is not rewritten")
    portal.calls.clear()
    poller.heartbeat()
    check("a repeat cycle fetches nothing", portal.report_calls() == [],
          str(portal.report_calls()))
    portal.bump("2:33:418", 1)
    again = poller.heartbeat()
    check("but a further change is fetched again", again.dirty == ("2:33:418",))


def test_hash_skip(tiles) -> None:
    print("\n[4] identical content is detected by hash, not rewritten")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    shard = next(s for s in poller.registry() if s.shard_id == "2:33:418")
    first = poller.fetch_shard(shard)
    check("first fetch stores", first["status"] == validate.STORED, str(first))
    with db.connect() as con:
        stored = con.execute("SELECT COUNT(*), SUM(amount) FROM payments WHERE shard_id = ?",
                             (shard.shard_id,)).fetchone()
        listed = con.execute("SELECT SUM(payments) FROM work_listing WHERE shard_id = ?",
                             (shard.shard_id,)).fetchone()[0]
        paid = con.execute("SELECT SUM(total_paid) FROM works").fetchone()[0]
    check("and keeps every payment record the slice lists, adding up to the works",
          stored[0] == listed > 0 and abs(stored[1] - paid) < 0.01,
          f"{stored[0]} payments, {listed} listed")
    second = poller.fetch_shard(shard)
    check("second fetch is skipped as unchanged", second["status"] == "unchanged",
          str(second))
    forced = poller.fetch_shard(shard, force=True)
    check("force re-reads it anyway", forced["status"] == validate.STORED,
          str(forced))
    check("re-reading identical content logs no new history",
          forced["versions"] == 0, str(forced))


def test_gates_in_the_loop(tiles) -> None:
    print("\n[5] the gates protect the corpus inside the loop")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    shard = next(s for s in poller.registry() if s.shard_id == "2:33:418")
    poller.fetch_shard(shard)
    stored_before = db.works_in_shard("2:33:418")
    check("a baseline is stored", len(stored_before) == 10, str(len(stored_before)))

    portal.truncate_shards.add("2:33:418")
    portal.bump("2:33:418", 1)
    outcome = poller.fetch_shard(shard)
    check("a truncated response is quarantined",
          outcome["status"] == validate.QUARANTINED, outcome["reason"][:90])
    check("the previously stored records are untouched",
          db.works_in_shard("2:33:418") == stored_before)
    portal.truncate_shards.clear()

    portal.zero_shards.add("2:33:419")
    other = next(s for s in poller.registry() if s.shard_id == "2:33:419")
    zero_outcome = poller.fetch_shard(other)
    check("an all-zero watermark is quarantined, not believed",
          zero_outcome["status"] == validate.QUARANTINED,
          zero_outcome["reason"][:90])
    portal.zero_shards.clear()


def test_transient_recovery(tiles) -> None:
    print("\n[5b] a transient short response is re-read, not quarantined")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    shard = next(s for s in poller.registry() if s.shard_id == "2:33:418")

    portal.truncate_once.add("2:33:418")
    outcome = poller.fetch_shard(shard)
    check("a one-off short read is retried and then stored",
          outcome["status"] == validate.STORED, str(outcome))
    check("the retried shard is not left with a failure counter",
          (db.get_watermark("2:33:418") or {}).get("consecutive_failures") == 0)

    portal.truncate_shards.add("2:33:419")
    other = next(s for s in poller.registry() if s.shard_id == "2:33:419")
    persistent = poller.fetch_shard(other)
    check("a persistent short read is quarantined after the retry",
          persistent["status"] == validate.QUARANTINED,
          persistent["reason"][:80])
    portal.truncate_shards.clear()


def test_genuinely_empty(tiles) -> None:
    print("\n[5c] a registered slice that is really empty is accepted")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    # four union territories have no Rajya Sabha members at all, and the first
    # national sweep found a Lok Sabha constituency with no works; all answer
    # all-zero and all are telling the truth
    portal.zero_shards.add("2:12:105")
    portal.n["2:12:105"] = 0
    shard = next(s for s in poller.registry() if s.shard_id == "2:12:105")
    outcome = poller.fetch_shard(shard)
    check("an enumerated empty shard stores zero records rather than quarantining",
          outcome["status"] == validate.STORED and outcome["stored"] == 0,
          str(outcome))
    row = db.get_watermark("2:12:105")
    check("it is recorded as reconciled, not as a failure",
          row["count_matched"] == 1 and row["consecutive_failures"] == 0,
          f"matched={row['count_matched']} failures={row['consecutive_failures']}")

    print("      and an id the portal never enumerated is still refused")
    from astra.ingestion.esakshi_api import Shard as _S
    bogus = _S(HOUSE_LS, 999, 999, "NOWHERE", "NOWHERE")
    portal.zero_shards.add("2:999:999")
    refused = validate.guard_zero([], suspicious_zero=True, registered=False)
    check("an unregistered all-zero shard is refused", not refused,
          refused.reason[:80])
    check("a registered shard that previously held records is refused too",
          not validate.guard_zero([], suspicious_zero=True, registered=True,
                                  previous_stored=10))


def test_failure_and_escalation(tiles) -> None:
    print("\n[6] failures retry, then escalate by name")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    shard = next(s for s in poller.registry() if s.shard_id == "2:33:419")
    portal.fail_shards.add("2:33:419")

    for _ in range(3):
        outcome = poller.fetch_shard(shard)
    check("a failing shard is marked for retry",
          outcome["status"] == validate.RETRY, outcome["reason"][:60])
    row = db.get_watermark("2:33:419")
    check("failures accumulate on the shard",
          row["consecutive_failures"] == 3, str(row["consecutive_failures"]))

    alerts = pmod.ALERTS_PATH
    check("an alerts file is written", alerts.exists(), str(alerts))
    payload = json.loads(alerts.read_text(encoding="utf-8"))
    named = [s for s in payload["stale"] if s["shard_id"] == "2:33:419"]
    check("the alert names the place", bool(named)
          and named[0]["constituency_name"] == "BARABANKI(SC)",
          json.dumps(named[:1], default=str)[:120])

    portal.fail_shards.clear()
    poller.fetch_shard(shard)
    row = db.get_watermark("2:33:419")
    check("a success clears the counter and the staleness",
          row["consecutive_failures"] == 0 and row["stale_since"] is None)


def test_restart_resumes(tiles) -> None:
    print("\n[7] a restart resumes from the durable queue")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    poller.heartbeat()

    # simulate a crash between marking a shard dirty and fetching it
    db.save_watermark("2:33:418", signature=["stale"], n_records=99,
                      lifecycle=validate.DIRTY)
    pending = [s.shard_id for s in pmod.Poller(client=portal, houses=(HOUSE_LS,),
                                               verbose=False).pending_shards()]
    check("the interrupted shard is still queued", "2:33:418" in pending,
          str(pending))

    restarted = pmod.Poller(client=portal, houses=(HOUSE_LS,), verbose=False)
    report = restarted.heartbeat()
    check("the restart fetches it without being told",
          report.stored > 0 or "2:33:418" not in
          [s.shard_id for s in restarted.pending_shards()],
          f"stored={report.stored}")


def test_reconcile(tiles) -> None:
    print("\n[8] the nightly sweep reads records, not counts")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    poller.heartbeat()
    portal.calls.clear()

    # a silent edit: content changes while every count stays identical
    summary = poller.reconcile_all()
    fetched = set(portal.report_calls())
    check("every registered shard was read at record level",
          fetched == {s.shard_id for s in poller.registry()},
          f"{len(fetched)} of {len(poller.registry())}")
    check("the sweep reports its own timing and counts",
          summary["shards"] == 4 and summary["seconds"] >= 0,
          json.dumps({k: summary[k] for k in ("shards", "stored", "changed")}))
    check("count reconciliation is recorded per shard",
          all((db.get_watermark(s.shard_id) or {}).get("count_matched") is not None
              for s in poller.registry()))

    print("\n[9] a silent edit is only caught by the sweep")
    edited = [dict(r) for r in tiles["recommended"]]
    edited[0] = {**edited[0], "WORK_DESCRIPTION": "CORRECTED DESCRIPTION"}
    portal.tiles = {**tiles, "recommended": edited}
    quiet = poller.heartbeat()
    check("the heartbeat sees nothing (no count moved)", quiet.dirty == (),
          str(quiet.dirty))
    swept = poller.reconcile_all()
    check("the sweep finds the edit", swept["changed"] >= 1,
          f"changed={swept['changed']}, versions={swept['versions']}")


def test_after_sweep(tiles) -> None:
    print("\n[8b] the first heartbeat after a sweep is quiet")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    # a sweep on a cold database, with no heartbeat before it — exactly the
    # situation after a first deployment or after the nightly job
    summary = poller.reconcile_all()
    check("the sweep records the national and state watermarks too",
          summary["upper_levels_recorded"] == 1 + 3,
          f"recorded {summary['upper_levels_recorded']} upper-level watermarks")
    portal.calls.clear()
    report = poller.heartbeat()
    check("the next heartbeat costs one request, not a full descent",
          report.requests == 1 and report.states_probed == 0,
          f"requests={report.requests}, states probed={report.states_probed}")

    print("\n[8c] a change landing mid-sweep is still caught")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    original = poller.fetch_shard

    def fetch_then_change(shard, **kwargs):
        outcome = original(shard, **kwargs)
        # the portal changes a slice moments AFTER the sweep has read it
        if shard.shard_id == "2:33:418" and not getattr(poller, "_bumped", False):
            poller._bumped = True
            portal.bump("2:33:418", 1)
        return outcome

    poller.fetch_shard = fetch_then_change
    poller.reconcile_all()
    poller.fetch_shard = original
    report = poller.heartbeat()
    check("the heartbeat after the sweep notices the national figure moved",
          report.houses_moved == (HOUSE_LS,), str(report.houses_moved))
    check("and fetches exactly the slice that changed behind the sweep",
          report.dirty == ("2:33:418",), str(report.dirty))


def test_schedule(tiles) -> None:
    print("\n[8d] the schedule survives sleep, restarts and a busy loop")
    from datetime import datetime, timedelta, timezone
    IST = timezone(timedelta(hours=5, minutes=30))

    def at(day: int, hh: int, mm: int) -> datetime:
        return datetime(2026, 9, day, hh, mm, tzinfo=IST)

    def stamp(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat()

    check("03:00 parses", pmod.parse_reconcile_at("03:00") == (3, 0))
    check("'off' disables the sweep", pmod.parse_reconcile_at("off") is None)
    check("an impossible time disables it rather than crashing",
          pmod.parse_reconcile_at("99:99") is None)
    check("the slot before 03:00 is yesterday's",
          pmod.most_recent_slot(at(13, 2, 0), "03:00") == at(12, 3, 0))
    check("the slot after 03:00 is today's",
          pmod.most_recent_slot(at(13, 8, 0), "03:00") == at(13, 3, 0))

    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    check("a sweep that has never run is due",
          poller.reconcile_due(now=at(13, 14, 0), at="03:00"))

    # the bug this fixes: the laptop slept from 01:00 to 08:00, straight
    # through the 03:00 slot
    db.set_state("last_reconcile_at", stamp(at(12, 3, 10)))
    check("sleeping through 03:00 leaves the sweep due on waking at 08:00",
          poller.reconcile_due(now=at(13, 8, 0), at="03:00"))
    check("but it is not due before today's slot has arrived",
          not poller.reconcile_due(now=at(13, 2, 0), at="03:00"))
    check("a heartbeat that ran long across 03:00 still finds it due at 03:02",
          poller.reconcile_due(now=at(13, 3, 2), at="03:00"))

    db.set_state("last_reconcile_at", stamp(at(9, 3, 10)))
    check("three days asleep makes it due", poller.reconcile_due(now=at(13, 8, 0),
                                                                 at="03:00"))

    summary = poller.reconcile_all()
    check("a healthy full sweep is recorded as complete",
          summary["recorded_as_complete"] is True
          and db.get_state("last_reconcile_at") is not None,
          f"ok_shards={summary['ok_shards']}/{summary['shards']}")
    real_now = datetime.now().astimezone()
    check("so it runs once, not once per missed day",
          not poller.reconcile_due(now=real_now, at="03:00"))

    restarted = pmod.Poller(client=portal, houses=(HOUSE_LS,), verbose=False)
    check("a restarted poller remembers it, because it lives in the database",
          not restarted.reconcile_due(now=real_now, at="03:00"))

    print("\n[8e] a sweep that did not really happen is not recorded as done")
    db.set_state("last_reconcile_at", stamp(at(9, 3, 10)))
    for shard in poller.registry():
        portal.fail_shards.add(shard.shard_id)
    failed = poller.reconcile_all()
    portal.fail_shards.clear()
    check("a sweep against an unreachable portal is not marked complete",
          failed["recorded_as_complete"] is False,
          f"ok_shards={failed['ok_shards']}/{failed['shards']}")
    check("the last good sweep time is left untouched",
          db.get_state("last_reconcile_at") == stamp(at(9, 3, 10)))
    just_after = datetime.now().astimezone() + timedelta(minutes=5)
    check("it is not retried every minute (retry gap)",
          not poller.reconcile_due(now=just_after, at="03:00"))
    check("but it is due again once the retry gap has passed",
          poller.reconcile_due(now=just_after + pmod.RETRY_GAP, at="03:00"))

    db.set_state("last_reconcile_at", stamp(at(9, 3, 10)))
    partial = poller.reconcile_all(house=HOUSE_LS)
    check("a single-house sweep does not count as the nightly sweep",
          partial["recorded_as_complete"] is False)

    print("\n[8f] the registry refresh is wall-clock, not a sleeping monotonic clock")
    db.set_state("last_registry_at", stamp(datetime.now(IST)))
    check("a fresh registry is not due",
          not poller.registry_due(now=datetime.now(timezone.utc)))
    db.set_state("last_registry_at",
                 stamp(datetime.now(IST) - timedelta(hours=25)))
    check("a registry last refreshed 25 hours ago is due, sleep or no sleep",
          poller.registry_due(now=datetime.now(timezone.utc)))
    poller.refresh_registry()
    check("refreshing it records the time",
          not poller.registry_due(now=datetime.now(timezone.utc)))


def test_kill_switch(tiles) -> None:
    print("\n[10] the kill switch and the status file")
    check("ASTRA_POLLER_ENABLED is honoured at import",
          pmod.ENABLED is True, "enabled by default in this run")
    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    poller.heartbeat()
    check("a status file is written for the dashboard",
          pmod.STATUS_PATH.exists(), str(pmod.STATUS_PATH))
    status = json.loads(pmod.STATUS_PATH.read_text(encoding="utf-8"))
    check("status carries the interval, houses and watermark summary",
          status["poll_interval_seconds"] == pmod.POLL_INTERVAL
          and status["houses"] == [HOUSE_LS]
          and "registered_shards" in status["watermarks"],
          json.dumps(status["watermarks"], default=str)[:100])


def test_freshness_endpoint(tiles) -> None:
    print("\n[11] /meta/freshness degrades rather than hides")
    from fastapi.testclient import TestClient
    from astra.api.main import app

    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    poller.heartbeat()
    poller.reconcile_all()

    client = TestClient(app)
    ok = client.get("/meta/freshness").json()
    check("a healthy sweep reports ok", ok["status"] == "ok", json.dumps(ok)[:140])
    check("it reports reconciled shards as a percentage",
          ok["reconciled_pct"] is not None, str(ok["reconciled_pct"]))
    check("it reports when the safety net last ran",
          ok["last_full_reconciliation"] is not None
          and ok["reconciliation_overdue"] is False,
          f"{ok['hours_since_reconciliation']}h ago")

    # a count off by the tolerated single record is reported, not alarmed on
    db.save_watermark("2:36:500", n_records=5, lifecycle=validate.STORED,
                      n_stored=6, count_matched=False, fetched=True)
    skew = client.get("/meta/freshness").json()
    check("a tolerated one-record skew is counted but does not degrade status",
          skew["status"] == "ok" and skew["count_mismatched"] >= 1,
          f"status={skew['status']}, mismatched={skew['count_mismatched']}")

    from datetime import datetime, timedelta, timezone
    db.set_state("last_reconcile_at",
                 (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat())
    overdue = client.get("/meta/freshness").json()
    check("a safety net that has not run in 30 hours degrades the status",
          overdue["status"] == "degraded" and overdue["reconciliation_overdue"],
          f"status={overdue['status']}, {overdue['hours_since_reconciliation']}h")
    poller.reconcile_all()

    shard = next(s for s in poller.registry() if s.shard_id == "2:33:419")
    portal.fail_shards.add("2:33:419")
    for _ in range(3):
        poller.fetch_shard(shard)
    degraded = client.get("/meta/freshness").json()
    check("a stale shard degrades the status",
          degraded["status"] == "degraded", degraded["status"])
    check("the stale shard is named, not averaged away",
          any(s["place"] == "BARABANKI(SC)" for s in degraded["stale"]),
          json.dumps(degraded["stale"], default=str)[:160])
    check("the response never leaks a full record dump",
          "works" not in degraded and len(json.dumps(degraded)) < 6000)


def test_single_writer(tiles) -> None:
    print("\n[11b] only one writer may run against a database")
    from fastapi.testclient import TestClient
    from astra.api.main import app
    from astra.ingestion import instance_lock, router

    client = TestClient(app)
    check("with no poller, the site reports it as not running",
          client.get("/meta/freshness").json()["poller_running"] is False)

    rival = instance_lock.WriterLock()
    check("the lock sits beside the scratch database, not the real one",
          rival.path.parent == Path(os.environ["ASTRA_DB_PATH"]).parent,
          str(rival.path))
    check("a first holder takes the lock", rival.acquire(role="poller"))
    try:
        check("the site now reports the poller as running",
              client.get("/meta/freshness").json()["poller_running"] is True)
        code = pmod.main(["--once", "--quiet"])
        check("a second poller refuses to start, with its own exit code",
              code == instance_lock.ALREADY_RUNNING, f"exit {code}")
        try:
            router.ingest("offline", verbose=False)
            refused = False
        except RuntimeError as exc:
            refused = "poller is running" in str(exc)
        check("a bulk re-ingest refuses to run under a live poller", refused)
        note = instance_lock.holder()
        check("the holder is named for the refusal message",
              note and note.get("pid") == os.getpid(), str(note))
    finally:
        rival.release()
    check("releasing frees it for the next poller", not instance_lock.is_held())


def test_live_status(tiles) -> None:
    print("\n[11c] the website can tell a live, a sweeping and a stopped poller apart")
    from fastapi.testclient import TestClient
    from astra.api.main import app
    from astra.ingestion import instance_lock

    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    client = TestClient(app)
    before = client.get("/meta/freshness").json()
    check("no check is claimed before the first heartbeat",
          before["last_check_at"] is None and before["last_update"] is None)

    poller.heartbeat()
    after = client.get("/meta/freshness").json()
    check("a heartbeat records when the portal was last checked",
          after["last_check_at"] is not None, str(after["last_check_at"]))
    check("the latest stored update is named by place",
          (after["last_update"] or {}).get("area", "").startswith(("ALIGARH", "BARABANKI",
                                                                    "SOUTH GOA", "KOLLAM")),
          json.dumps(after["last_update"])[:120])

    lock = instance_lock.WriterLock()
    lock.acquire()
    try:
        seen = {}
        original = pmod.Poller._reconcile_all

        def spy(self, house):
            seen["during"] = client.get("/meta/freshness").json()
            return original(self, house)

        pmod.Poller._reconcile_all = spy
        try:
            poller.reconcile_all()
        finally:
            pmod.Poller._reconcile_all = original
        during = seen.get("during") or {}
        check("a running sweep is reported as in progress",
              during.get("sweep_in_progress") is True, str(during.get("sweep_started_at")))
        check("and cleared when it ends",
              client.get("/meta/freshness").json()["sweep_in_progress"] is False)
    finally:
        lock.release()

    db.set_state("sweep_started_at", "2026-01-01T00:00:00+00:00")
    check("a marker left by a killed sweep is ignored once nothing holds the lock",
          client.get("/meta/freshness").json()["sweep_in_progress"] is False)


def test_recent_updates(tiles) -> None:
    print("\n[11d] the change log shows what the portal really changed")
    import copy
    from fastapi.testclient import TestClient
    from astra.api.main import app

    moved = copy.deepcopy(tiles)
    portal = FakePortal(moved)
    poller = fresh_poller(portal)
    shard = next(s for s in poller.registry() if s.shard_id == "2:33:418")
    other = next(s for s in poller.registry() if s.shard_id == "2:12:105")
    poller.fetch_shard(shard)
    check("a store that brings new works is logged",
          len(db.load_provenance()) == 1, f"{len(db.load_provenance())} rows")
    # The fake's slices share the fixture's rows, so this one holds nothing new.
    poller.fetch_shard(other)
    check("a read that found nothing new is not logged, so re-reads cannot "
          "crowd out real updates", len(db.load_provenance()) == 1,
          f"{len(db.load_provenance())} rows")

    row = moved["recommended"][0]
    old_text = row["WORK_DESCRIPTION"]
    row["WORK_DESCRIPTION"] = old_text + " (revised)"
    outcome = poller.fetch_shard(shard, force=True)
    check("an edited work is stored as a change", outcome["changed"] >= 1, str(outcome))
    check("each store is appended to the ledger, not written over it",
          len(db.load_provenance()) == 2, f"{len(db.load_provenance())} rows")

    client = TestClient(app)
    body = client.get("/meta/recent-updates?limit=5").json()
    stores = body["stores"]
    check("the newest store comes first, with what it held",
          stores and stores[0]["area"].startswith("ALIGARH")
          and "updated" in stores[0]["detail"], json.dumps(stores[:1])[:160])
    change = next((c for c in body["changes"]
                   if any(f["field"] == "description" for f in c["fields"])), None)
    check("the field-level edit is listed with its old and new value",
          change is not None
          and any(f["old_value"] == old_text and f["new_value"].endswith("(revised)")
                  for f in change["fields"]),
          json.dumps(change, default=str)[:160] if change else "no change listed")
    check("the change names its place and member",
          change is not None and change["place"] == "ALIGARH" and change["mp_name"],
          f"{change and change['place']} / {change and change['mp_name']}")

    db.append_provenance([{"source": f"eSAKSHI API X{i}", "mode": "api",
                           "table": "works", "rows": i, "status": "ok"}
                          for i in range(8)], keep=5)
    check("the running log is trimmed to its cap", len(db.load_provenance()) == 5,
          f"{len(db.load_provenance())} rows")


def test_data_source_live(tiles) -> None:
    print("\n[11e] the data-source ledger describes the live corpus, from the database")
    from fastapi.testclient import TestClient
    from astra.api.main import app
    from astra.config import PROCESSED_DIR

    portal = FakePortal(tiles)
    poller = fresh_poller(portal)
    poller.reconcile_all()
    meta = PROCESSED_DIR / "ingest_meta.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"mode_resolved": "api", "mode_requested": "api",
                                "works": 1, "eras": {"stale": 1}}), encoding="utf-8")
    try:
        body = TestClient(app).get("/meta/data-source").json()
        with db.connect() as con:
            stored = con.execute("SELECT COUNT(*) FROM works").fetchone()[0]
        check("work counts are read from the database, not the ingestion note",
              body["works"] == stored and "stale" not in body["work_eras"],
              f"api={body['works']} db={stored} eras={body['work_eras']}")
        ledger = {r["source"]: r for r in body["provenance"]}
        ls = ledger.get("eSAKSHI portal — Lok Sabha")
        check("the ledger has one row per real source, not per slice stored",
              ls is not None and not any(s.startswith("eSAKSHI API") for s in ledger),
              ", ".join(ledger)[:160])
        check("the Lok Sabha row carries its slice health",
              ls is not None and "4 constituencies" in ls["detail"]
              and ls["rows"] == body["houses"].get("LS"),
              ls and ls["detail"])
        check("it is flagged live", body["live"] is True)

        client = TestClient(app)
        fresh = client.get("/meta/freshness").json()
        parity = fresh["parity"]
        ls = parity["national"].get("LS", {})
        check("every slice is reported in exact parity with the portal",
              parity["exact_slices"] == parity["registered_slices"] == 4
              and fresh["status"] == "ok",
              f"{parity['exact_slices']} of {parity['registered_slices']}, {fresh['status']}")
        check("national figures carry portal and stored side by side, per tile",
              set(ls) == {"recommended", "sanctioned", "completed", "expenditure"}
              and all(slot["exact"] for slot in ls.values()), json.dumps(ls)[:160])
        with db.connect() as con:
            con.execute("UPDATE shard_parity SET exact = 0, differences_json = ? "
                        "WHERE shard_id = '2:36:500'",
                        (json.dumps([{"tile": "sanctioned", "measure": "count",
                                      "portal": 5, "stored": 4}]),))
        off = client.get("/meta/freshness").json()
        check("a slice that differs from the portal degrades the status and is named",
              off["status"] == "degraded" and off["parity"]["exceptions"]
              and off["parity"]["exceptions"][0]["place"] == "KOLLAM",
              json.dumps(off["parity"]["exceptions"])[:160])
    finally:
        meta.unlink(missing_ok=True)


def _parity(shard_id: str) -> dict:
    with db.connect() as con:
        row = con.execute("SELECT * FROM shard_parity WHERE shard_id = ?",
                          (shard_id,)).fetchone()
    return dict(row) if row else {}


def _slice(tiles, rows: list[dict], state="Uttar Pradesh",
           constituency="ALIGARH") -> dict[str, list[dict]]:
    """Reports for one slice holding exactly `rows`, with their later reports."""
    import copy
    rec = []
    for row in rows:
        row = copy.deepcopy(row)
        row.update({"STATE_NAME": state, "CONSTITUENCY": constituency})
        rec.append(row)
    held = {r["WORK_RECOMMENDATION_DTL_ID"] for r in rec}
    out = {"recommended": rec}
    for tile in ("sanctioned", "completed", "expenditure"):
        out[tile] = []
        for row in tiles.get(tile) or []:
            if row.get("WORK_RECOMMENDATION_DTL_ID") in held:
                row = copy.deepcopy(row)
                row.update({"STATE_NAME": state, "CONSTITUENCY": constituency})
                out[tile].append(row)
    return out


def _work_id(row: dict) -> str:
    from astra.ingestion import esakshi_map as emap
    return emap.work_id_for(str(row["WORK_RECOMMENDATION_DTL_ID"]), emap.row_code(row), "LS")


def _alone(portal, tiles, keep: str) -> None:
    """Give every other slice an empty report, so no two slices list one work."""
    for shard_id in ("2:33:418", "2:33:419", "2:12:105", "2:36:500"):
        if shard_id != keep:
            portal.shard_tiles.setdefault(shard_id, _slice(tiles, []))


def test_duplicate_listings(tiles) -> None:
    print("\n[11f] works the portal lists more than once are mirrored, not refused")
    import copy
    donor = [r for r in tiles["recommended"] if r.get("WORK_RECOMMENDATION_DTL_ID")][:8]
    first, second = copy.deepcopy(donor[0]), copy.deepcopy(donor[0])
    for row, text in ((first, "first pending work"), (second, "second pending work")):
        row.update({"WORK_RECOMMENDATION_DTL_ID": 1740, "ACTIVITY_NAME": "NA-Installing hand pumps",
                    "WORK_DESCRIPTION": text})
    echo = copy.deepcopy(donor[1])
    echo["Sno"] = 9999
    portal = FakePortal(tiles)
    portal.shard_tiles["2:33:418"] = _slice(tiles, [first, second] + donor[1:] + [echo])
    poller = fresh_poller(portal)
    shard = next(s for s in poller.registry() if s.shard_id == "2:33:418")
    outcome = poller.fetch_shard(shard)
    stored = db.works_in_shard("2:33:418")
    check("two different works sharing one id are both stored",
          outcome["status"] == validate.STORED
          and {"ES-LS-1740", "ES-LS-1740#2"} <= stored, str(sorted(stored))[:120])
    with db.connect() as con:
        listed = con.execute("SELECT in_recommended FROM work_listing WHERE work_id = ?",
                             (_work_id(donor[1]),)).fetchone()
    check("the same record listed twice is one work counted twice, as the portal counts it",
          listed is not None and listed[0] == 2, str(listed and listed[0]))
    parity = _parity("2:33:418")
    check("the slice is in exact parity with the portal's own tiles",
          parity.get("exact") == 1, parity.get("differences_json"))
    check("the duplicate listings are recorded for inspection",
          len(json.loads(parity.get("duplicates_json") or "[]")) == 2,
          parity.get("duplicates_json", "")[:140])

    print("      and a work id already held by another state is still refused")
    portal = FakePortal(copy.deepcopy(tiles))
    poller = fresh_poller(portal)
    aligarh = next(s for s in poller.registry() if s.shard_id == "2:33:418")
    goa = next(s for s in poller.registry() if s.shard_id == "2:12:105")
    poller.fetch_shard(aligarh)
    for row in portal.tiles["recommended"]:
        row["STATE_NAME"] = "Goa"
    moved = poller.fetch_shard(goa)
    check("a slice whose works are still listed elsewhere is quarantined, not merged",
          moved["status"] == validate.QUARANTINED
          and "would overwrite a different work" in moved["reason"],
          moved["reason"][:120])


def test_removal_lifecycle(tiles) -> None:
    print("\n[11g] a work the portal stops listing is retired only once confirmed")
    from datetime import datetime, timedelta, timezone
    rows = [r for r in tiles["recommended"] if r.get("WORK_RECOMMENDATION_DTL_ID")][:10]
    portal = FakePortal(tiles)
    portal.shard_tiles["2:33:418"] = _slice(tiles, rows)
    _alone(portal, tiles, "2:33:418")
    poller = fresh_poller(portal)
    shard = next(s for s in poller.registry() if s.shard_id == "2:33:418")
    poller.fetch_shard(shard)
    gone = rows[3]
    gone_id = _work_id(gone)
    check("the work to be dropped is stored first", gone_id in db.works_in_shard("2:33:418"),
          gone_id)
    without = _slice(tiles, [r for r in rows if r is not gone])

    # a short read the portal itself disagrees with: it still claims ten
    portal.shard_tiles["2:33:418"] = without
    portal.claimed_counts["2:33:418"] = {"Works Recommended": 10}
    short = poller.fetch_shard(shard, force=True)
    with db.connect() as con:
        marked = con.execute("SELECT COUNT(*) FROM missing_works").fetchone()[0]
    check("an inconsistent read (count ten, nine records) marks nothing as gone",
          short["status"] == validate.STORED and marked == 0,
          f"status={short['status']}, missing={marked}")

    # the portal really drops it: its count and its records agree on nine
    portal.claimed_counts.clear()
    first = poller.fetch_shard(shard, force=True)
    check("a consistent read without it records it as awaiting removal",
          first.get("awaiting_removal") == 1 and gone_id in db.works_in_shard("2:33:418"),
          str(first))
    parity = _parity("2:33:418")
    check("the slice already reads as exact against the portal",
          parity.get("exact") == 1, parity.get("differences_json"))
    check("and a confirming re-read is scheduled", parity.get("recheck_after") is not None)

    again = poller.fetch_shard(shard, force=True)
    check("a second read inside the confirmation window does not retire it",
          again.get("retired") == 0 and gone_id in db.works_in_shard("2:33:418"), str(again))

    earlier = (datetime.now(timezone.utc) - pmod.RETIRE_CONFIRM - timedelta(minutes=1)).isoformat()
    with db.connect() as con:
        con.execute("UPDATE missing_works SET first_missing_at = ?", (earlier,))
        con.execute("UPDATE shard_parity SET recheck_after = ? WHERE shard_id = '2:33:418'",
                    (earlier,))
    portal.calls.clear()
    poller.heartbeat()
    check("the heartbeat re-reads a slice whose confirmation is due",
          "2:33:418" in portal.report_calls(), str(sorted(set(portal.report_calls()))))
    with db.connect() as con:
        stored = con.execute("SELECT COUNT(*) FROM works WHERE work_id = ?", (gone_id,)).fetchone()[0]
        kept = con.execute("SELECT row_json FROM retired_works WHERE work_id = ?", (gone_id,)).fetchone()
        logged = con.execute("SELECT new_value FROM work_versions WHERE work_id = ? "
                             "AND field = 'listing'", (gone_id,)).fetchall()
    check("once confirmed it leaves the corpus", stored == 0)
    check("it is kept whole among retired works", kept is not None and gone_id in kept[0])
    check("and its history records the removal",
          [r[0] for r in logged] == ["no longer listed on the portal"], str(logged))
    check("the corpus now matches the portal's nine",
          len(db.works_in_shard("2:33:418")) == 9 and _parity("2:33:418").get("exact") == 1,
          f"{len(db.works_in_shard('2:33:418'))} stored")

    portal.shard_tiles["2:33:418"] = _slice(tiles, rows)
    back = poller.fetch_shard(shard, force=True)
    with db.connect() as con:
        still_retired = con.execute("SELECT COUNT(*) FROM retired_works").fetchone()[0]
        logged = [r[0] for r in con.execute(
            "SELECT new_value FROM work_versions WHERE work_id = ? AND field = 'listing' "
            "ORDER BY observed_at", (gone_id,))]
    check("a work the portal lists again comes back",
          back.get("restored") == 1 and gone_id in db.works_in_shard("2:33:418")
          and still_retired == 0, str(back))
    check("and its history says so", logged[-1:] == ["listed again"], str(logged))


def test_move_between_slices(tiles) -> None:
    print("\n[11h] a work the portal moves to another slice is followed, not refused")
    import copy
    from datetime import datetime, timedelta, timezone
    rows = [r for r in tiles["recommended"] if r.get("WORK_RECOMMENDATION_DTL_ID")][:10]
    portal = FakePortal(tiles)
    portal.shard_tiles["2:33:418"] = _slice(tiles, rows)
    portal.shard_tiles["2:12:105"] = _slice(tiles, [], state="Goa", constituency="SOUTH GOA")
    _alone(portal, tiles, "2:33:418")
    poller = fresh_poller(portal)
    aligarh = next(s for s in poller.registry() if s.shard_id == "2:33:418")
    goa = next(s for s in poller.registry() if s.shard_id == "2:12:105")
    poller.fetch_shard(aligarh)
    poller.fetch_shard(goa)
    moving = rows[0]
    moved_id = _work_id(moving)

    portal.shard_tiles["2:33:418"] = _slice(tiles, rows[1:])
    poller.fetch_shard(aligarh, force=True)
    portal.shard_tiles["2:12:105"] = _slice(tiles, [moving], state="Goa", constituency="SOUTH GOA")
    arrived = poller.fetch_shard(goa, force=True)
    with db.connect() as con:
        row = con.execute("SELECT state, constituency FROM works WHERE work_id = ?",
                          (moved_id,)).fetchone()
        pending = con.execute("SELECT COUNT(*) FROM missing_works").fetchone()[0]
        history = {r[0] for r in con.execute(
            "SELECT field FROM work_versions WHERE work_id = ?", (moved_id,))}
    check("the destination slice stores it instead of refusing",
          arrived["status"] == validate.STORED and row is not None
          and row["state"] == "GOA", f"{arrived['status']} {row and dict(row)}")
    check("it is no longer awaiting removal", pending == 0, f"{pending} pending")
    check("the move is in its history", {"state", "constituency"} <= history, str(history))

    earlier = (datetime.now(timezone.utc) - pmod.RETIRE_CONFIRM - timedelta(minutes=1)).isoformat()
    with db.connect() as con:
        con.execute("UPDATE shard_parity SET recheck_after = ?", (earlier,))
    poller.fetch_shard(aligarh, force=True)
    with db.connect() as con:
        retired = con.execute("SELECT COUNT(*) FROM retired_works").fetchone()[0]
    check("and a later read of its old slice does not retire it", retired == 0)
    check("both slices are in exact parity",
          _parity("2:33:418").get("exact") == 1 and _parity("2:12:105").get("exact") == 1)


def test_later_report_only(tiles) -> None:
    print("\n[11i] a work listed only in a later report is stored and counted")
    import copy
    rows = [r for r in tiles["recommended"] if r.get("WORK_RECOMMENDATION_DTL_ID")][:10]
    portal = FakePortal(tiles)
    reports = _slice(tiles, rows)
    sanctioned_only = next((r for r in reports["sanctioned"]), None)
    check("the fixture has a sanctioned work to hold back", sanctioned_only is not None)
    if sanctioned_only is None:
        return
    dtl = sanctioned_only["WORK_RECOMMENDATION_DTL_ID"]
    # as on the portal on 13 Sep 2026: sanctioned, paid, yet in no recommended report
    reports["recommended"] = [r for r in reports["recommended"]
                              if r["WORK_RECOMMENDATION_DTL_ID"] != dtl]
    portal.shard_tiles["2:33:418"] = reports
    _alone(portal, tiles, "2:33:418")
    poller = fresh_poller(portal)
    shard = next(s for s in poller.registry() if s.shard_id == "2:33:418")
    outcome = poller.fetch_shard(shard)
    code = _work_id(sanctioned_only)
    with db.connect() as con:
        work = con.execute("SELECT sanction_date, sanctioned_amount, total_paid FROM works "
                           "WHERE work_id = ?", (code,)).fetchone()
        listing = con.execute("SELECT in_recommended, in_sanctioned FROM work_listing "
                              "WHERE work_id = ?", (code,)).fetchone()
    check("the work is stored with its sanction", outcome["status"] == validate.STORED
          and work is not None and work["sanctioned_amount"], f"{outcome['status']} {work and dict(work)}")
    check("it is listed as sanctioned but not recommended",
          listing is not None and tuple(listing) == (0, 1), str(listing and tuple(listing)))
    parity = _parity("2:33:418")
    portal_side = json.loads(parity.get("portal_json") or "{}")
    check("every tile, count and rupees, equals the portal's",
          parity.get("exact") == 1, parity.get("differences_json"))
    check("including sanctioned, completed and expenditure, not just the headline",
          all(portal_side.get(t, [None, None])[1] is not None
              for t in ("sanctioned", "completed", "expenditure")), str(portal_side)[:160])


class _OpenBreaker(_NullBreaker):
    is_open = True

    def check(self):
        raise CircuitOpen("breaker open")


SLICES = {"2:33:418": ("Uttar Pradesh", "ALIGARH"),
          "2:33:419": ("Uttar Pradesh", "BARABANKI(SC)"),
          "2:12:105": ("Goa", "SOUTH GOA"),
          "2:36:500": ("Kerala", "KOLLAM")}


def _distinct_slices(portal, tiles) -> None:
    """Give each slice its own works, as on the real portal, where no two
    slices list one work. The shared fixture would make every store touch
    every slice's listing."""
    rows = [r for r in tiles["recommended"] if r.get("WORK_RECOMMENDATION_DTL_ID")]
    per = len(rows) // len(SLICES)
    for i, (shard_id, (state, place)) in enumerate(SLICES.items()):
        portal.shard_tiles[shard_id] = _slice(tiles, rows[i * per:(i + 1) * per],
                                              state=state, constituency=place)


def _read_hours_ago(hours_by_shard: dict[str, float]) -> None:
    """Pretend each slice's records were last read this many hours ago."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    with db.connect() as con:
        for shard_id, hours in hours_by_shard.items():
            con.execute("UPDATE shard_watermarks SET fetched_at = ? WHERE shard_id = ?",
                        ((now - timedelta(hours=hours)).isoformat(), shard_id))


def _rotation(areas: int = 1, hours: str = "always", min_age_h: float = 3.0) -> None:
    from datetime import timedelta
    pmod.ROLLING_AREAS, pmod.ROLLING_HOURS = areas, hours
    pmod.ROLLING_MIN_AGE = timedelta(hours=min_age_h)


def _count(table: str) -> int:
    with db.connect() as con:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_rotation(tiles) -> None:
    print("\n[13] the rotation re-reads the area read longest ago, gently")
    from datetime import datetime, timedelta
    from fastapi.testclient import TestClient
    from astra.api.main import app
    from astra.ingestion.esakshi_api import TILE_KEYS

    wide = {sid: 10 for sid in SLICES}
    saved = (pmod.ROLLING_AREAS, pmod.ROLLING_HOURS, pmod.ROLLING_MIN_AGE)
    try:
        check("working hours parse, including a window across midnight",
              pmod.parse_hours("08:00-20:00") == (480, 1200)
              and pmod.parse_hours("22:00-06:00") == (1320, 360)
              and pmod.parse_hours("always") == (0, 1440)
              and all(pmod.parse_hours(v) is None
                      for v in ("off", "", "9-5", "25:00-06:00", "10:00-10:00")))
        night = datetime(2026, 9, 14, 23, 30).astimezone()
        check("and a time is tested against them the way a clock reads",
              pmod.in_hours(night, (1320, 360))
              and pmod.in_hours(night.replace(hour=5, minute=59), (1320, 360))
              and not pmod.in_hours(night.replace(hour=12), (1320, 360))
              and not pmod.in_hours(night.replace(hour=20, minute=0), (480, 1200))
              and pmod.in_hours(night.replace(hour=8, minute=0), (480, 1200)))

        portal = FakePortal(tiles)
        _distinct_slices(portal, tiles)
        poller = fresh_poller(portal)
        poller.reconcile_all()
        _rotation()
        _read_hours_ago({"2:33:418": 1, "2:33:419": 7, "2:12:105": 5, "2:36:500": 9})
        history, logged = _count("work_versions"), len(db.load_provenance())
        changed_at = db.get_state("data_changed_at")
        portal.calls.clear()
        first = poller.rolling_reread()
        check("it reads one area: the one read longest ago", first == ["2:36:500"], str(first))
        check("at five requests, every one about that area",
              len(portal.calls) == 5 and {sid for _, sid in portal.calls} == {"2:36:500"},
              str(portal.calls))
        check("an unchanged area adds no history, no log entry and no analysis trigger",
              _count("work_versions") == history and len(db.load_provenance()) == logged
              and db.get_state("data_changed_at") == changed_at)
        second, third = poller.rolling_reread(), poller.rolling_reread()
        check("each further minute moves on to the next-oldest area",
              second == ["2:33:419"] and third == ["2:12:105"], f"{second} {third}")
        portal.calls.clear()
        check("an area read in the last three hours is not due, so a fresh corpus "
              "costs nothing", poller.rolling_reread() == [] and not portal.calls,
              str(portal.calls))

        _read_hours_ago(wide)
        soon = datetime.now().astimezone()
        _rotation(hours=f"{soon + timedelta(hours=1):%H:%M}-{soon + timedelta(hours=2):%H:%M}")
        check("outside its hours it reads nothing",
              poller.rolling_reread() == [] and not portal.calls, str(portal.calls))
        _rotation(areas=0)
        off_by_count = poller.rolling_reread()
        _rotation(hours="off")
        check("and it can be switched off by count or by hours",
              off_by_count == [] and poller.rolling_reread() == [] and not portal.calls)
        _rotation()
        portal.breaker = _OpenBreaker()
        check("it stands down while the circuit breaker is open",
              poller.rolling_reread() == [] and not portal.calls, str(portal.calls))
        portal.breaker = _NullBreaker()

        goa = next(s for s in poller.registry() if s.shard_id == "2:12:105")
        portal.fail_shards.add("2:12:105")
        poller.fetch_shard(goa)
        portal.fail_shards.clear()
        _read_hours_ago({"2:33:418": 1, "2:33:419": 1, "2:36:500": 1, "2:12:105": 12})
        portal.calls.clear()
        check("an area already waiting for a retry is left to that queue",
              poller.rolling_reread() == [] and not portal.calls, str(portal.calls))
        poller.fetch_shard(goa)

        print("      an edit that moves no figure")
        row = portal.shard_tiles["2:33:418"]["recommended"][0]
        old_text = row["WORK_DESCRIPTION"]
        row["WORK_DESCRIPTION"] = old_text + " (corrected)"
        _rotation(areas=0)
        portal.calls.clear()
        quiet = poller.heartbeat()
        check("is invisible to the minute check on its own",
              quiet.requests == 1 and not quiet.dirty and quiet.changed == 0,
              f"requests={quiet.requests} dirty={quiet.dirty} changed={quiet.changed}")
        _rotation()
        _read_hours_ago({"2:33:418": 10, "2:33:419": 1, "2:12:105": 1, "2:36:500": 1})
        caught = poller.heartbeat()
        check("and is picked up by the rotation in a quiet minute",
              caught.rolled == ("2:33:418",) and caught.changed == 1,
              f"rolled={caught.rolled} changed={caught.changed}")
        check("stored as a field change with its old and new text",
              any(v["field"] == "description" and v["old_value"] == old_text
                  and (v["new_value"] or "").endswith("(corrected)")
                  for v in db.work_versions(_work_id(row))))
        check("which tells the analysis the data changed",
              db.get_state("data_changed_at") not in (None, "", changed_at))

        print("      busy minutes")
        _read_hours_ago(wide)
        portal.n["2:0:0"] += 1
        portal.n["2:33:0"] += 1
        busy = poller.heartbeat()
        check("a minute in which a figure moved is not used for the rotation",
              busy.houses_moved == (HOUSE_LS,) and busy.rolled == (),
              f"moved={busy.houses_moved} rolled={busy.rolled}")
        check("an area the descent found unchanged keeps its records fingerprint",
              (db.get_watermark("2:33:419") or {}).get("payload_sha256"))
        history = _count("work_versions")
        portal.calls.clear()
        calm = poller.heartbeat()
        check("the next quiet minute is used, for one area and one national check",
              len(calm.rolled) == 1 and calm.requests == 1
              and len(portal.calls) == 1 + 5, f"rolled={calm.rolled} calls={portal.calls}")
        check("and that area, unchanged, is not rewritten",
              calm.stored == 0 and _count("work_versions") == history,
              f"stored={calm.stored}")

        kollam = next(s for s in poller.registry() if s.shard_id == "2:36:500")
        portal.claimed_counts["2:36:500"] = {TILE_KEYS["sanctioned"]: 999}
        lagging = poller.fetch_shard(kollam)
        portal.claimed_counts.clear()
        check("identical records under figures that moved are not skipped as unchanged",
              lagging["status"] != "unchanged", str(lagging)[:140])

        _rotation(hours="08:00-20:00")
        poller.heartbeat()
        rolling = TestClient(app).get("/meta/freshness").json().get("rolling") or {}
        check("the site shows the rotation as the poller runs it",
              rolling.get("enabled") is True and rolling.get("hours") == "08:00-20:00"
              and rolling.get("areas_per_check") == 1 and rolling.get("last_at")
              and rolling.get("active_now") is False, json.dumps(rolling))
    finally:
        pmod.ROLLING_AREAS, pmod.ROLLING_HOURS, pmod.ROLLING_MIN_AGE = saved


def test_analysis_refresh(tiles) -> None:
    print("\n[14] the risk flags follow the data, and only when it changed")
    from datetime import datetime, timedelta, timezone
    from fastapi.testclient import TestClient
    import astra.pipeline as pipeline_mod
    from astra.agents.orchestrator import Orchestrator
    from astra.api.main import app
    from astra.ingestion import esakshi_map as emap
    from astra.ingestion import instance_lock

    portal = FakePortal(tiles)
    _distinct_slices(portal, tiles)
    poller = fresh_poller(portal)
    check("no analysis is due before anything is stored", not poller.analysis_due())
    check("and an empty database has no old flags to catch up on",
          not poller.note_unrecorded_analysis() and not poller.analysis_due())
    poller.heartbeat()
    changed_at = db.get_state("data_changed_at")
    check("storing new works marks the data as changed", bool(changed_at), str(changed_at))
    check("so an analysis is due, one never having run", poller.analysis_due())

    summary = poller.refresh_analysis("test")
    check("it analyses the stored corpus and saves the flags",
          summary is not None and summary["works"] == _count("works")
          and summary["flags"] == _count("flags") and summary["flags"] > 0,
          summary and f"works={summary['works']} flags={summary['flags']}")
    check("it records which stored changes the flags include",
          db.get_state("analysis_covers_changes_at") == changed_at
          and db.get_state("last_analysis_at"))
    check("its in-progress marker is cleared", not db.get_state("analysis_started_at"))
    check("with nothing new, no further run is due", not poller.analysis_due())
    with db.connect() as con:
        live_rows = con.execute("SELECT COUNT(*), SUM(works_count) FROM fundflows "
                                "WHERE source = 'esakshi_api'").fetchone()
        dated = con.execute("SELECT COUNT(*) FROM works WHERE fy IS NOT NULL").fetchone()[0]
    check("fund positions are built from the stored works",
          live_rows[0] > 0 and live_rows[1] == dated, f"{tuple(live_rows)} vs {dated} dated works")

    poller.reconcile_all()
    check("re-reading every area and finding nothing new does not mark the data changed",
          db.get_state("data_changed_at") == changed_at)

    print("      a change, the interval and the nightly sweep")
    aligarh = next(s for s in poller.registry() if s.shard_id == "2:33:418")
    row = next(r for r in portal.shard_tiles["2:33:418"]["recommended"]
               if emap._FY_IN_CODE.search(emap.row_code(r) or ""))
    row["RECOMMENDED_AMOUNT"] = (row.get("RECOMMENDED_AMOUNT") or 0) + 50000
    with db.connect() as con:
        before_total = con.execute("SELECT SUM(recommended) FROM fundflows "
                                   "WHERE source = 'esakshi_api'").fetchone()[0]
    poller.fetch_shard(aligarh)
    newer = db.get_state("data_changed_at")
    check("an edited amount marks the data changed again", newer and newer != changed_at)
    now = datetime.now(timezone.utc)
    check("within the interval since the last run it waits", not poller.analysis_due(now))
    check("unless a nightly sweep has just completed",
          poller.analysis_due(now, after_sweep=True))
    check("and once the interval has passed it is due",
          poller.analysis_due(now + pmod.ANALYSIS_EVERY + timedelta(minutes=1)))
    saved_every = pmod.ANALYSIS_EVERY
    pmod.ANALYSIS_EVERY = timedelta(0)
    check("it can be switched off", not poller.analysis_due(now, after_sweep=True))
    pmod.ANALYSIS_EVERY = saved_every

    print("      review decisions and pre-2023 fund rows")
    with db.connect() as con:
        top = [dict(r) for r in con.execute(
            "SELECT flag_id, created_at FROM flags ORDER BY risk_score DESC, flag_id LIMIT 2")]
        con.execute("INSERT INTO fundflows (row_id, source, era, state, fy, expenditure) "
                    "VALUES ('FF-HISTORY-1', 'MPLADS 17th Lok Sabha (2019-2024)', 'pre2023', "
                    "'UTTAR PRADESH', '2019-2020', 12345.0)")
    decided, midrun = top[0]["flag_id"], top[1]["flag_id"]
    db.record_feedback(decided, "confirmed", "district", "checked on site")
    original_run = Orchestrator.run

    def run_then_review(self, works, flows, context=None):
        flags = original_run(self, works, flows, context)
        db.record_feedback(midrun, "under_review", "state", "recorded mid-run")
        return flags

    lock = instance_lock.WriterLock()
    lock.acquire()
    client = TestClient(app)
    seen = {}
    original_pipeline = pipeline_mod.run_pipeline

    def spy(**kwargs):
        seen["during"] = client.get("/meta/freshness").json().get("analysis")
        return original_pipeline(**kwargs)

    Orchestrator.run = run_then_review
    pipeline_mod.run_pipeline = spy
    try:
        second = poller.refresh_analysis("test")
    finally:
        Orchestrator.run = original_run
        pipeline_mod.run_pipeline = original_pipeline
    try:
        during = seen.get("during") or {}
        check("while it runs the site says the flags are being recomputed",
              during.get("in_progress") is True and during.get("changes_waiting") is True,
              json.dumps(during))
        after = client.get("/meta/freshness").json().get("analysis") or {}
        check("and afterwards when, with no change left waiting",
              after.get("in_progress") is False and after.get("last_at")
              and after.get("changes_waiting") is False, json.dumps(after))
    finally:
        lock.release()
    with db.connect() as con:
        flags = {r["flag_id"]: dict(r) for r in con.execute(
            "SELECT flag_id, review_status, reviewer_note, created_at FROM flags")}
        history = con.execute("SELECT expenditure FROM fundflows "
                              "WHERE row_id = 'FF-HISTORY-1'").fetchone()
        after_total = con.execute("SELECT SUM(recommended) FROM fundflows "
                                  "WHERE source = 'esakshi_api'").fetchone()[0]
    check("the re-run completed", second is not None)
    check("a review decision survives the re-run",
          flags.get(decided, {}).get("review_status") == "confirmed"
          and flags[decided]["reviewer_note"] == "checked on site",
          str(flags.get(decided)))
    check("so does one recorded while the analysis was computing",
          flags.get(midrun, {}).get("review_status") == "under_review", str(flags.get(midrun)))
    check("a case keeps the time it was first flagged",
          flags.get(decided, {}).get("created_at") == top[0]["created_at"])
    check("the fund positions follow the edited amount",
          round((after_total or 0) - (before_total or 0)) == 50000,
          f"{before_total} -> {after_total}")
    check("while pre-2023 fund rows are left exactly as they were",
          history is not None and history[0] == 12345.0)

    print("      members with no constituency, as in the Rajya Sabha")
    with db.connect() as con:
        con.execute("UPDATE works SET constituency = NULL WHERE UPPER(state) = 'GOA'")
    seen_flows = {}

    def capture(self, works, flows, context=None):
        seen_flows["constituency"] = list(flows.loc[flows["source"] == "esakshi_api",
                                                    "constituency"])
        return original_run(self, works, flows, context)

    Orchestrator.run = capture
    try:
        pipeline_mod.run_pipeline(verbose=False)
    finally:
        Orchestrator.run = original_run
    values = seen_flows.get("constituency") or []
    check("the analysis sees a missing constituency as missing, not as the text 'nan'",
          any(v is None for v in values)
          and all(v is None or isinstance(v, str) for v in values),
          str(sorted({type(v).__name__ for v in values})))
    with db.connect() as con:
        named = [r[0] for r in con.execute("SELECT entity_id FROM flags")]
    check("so no case is named after a 'nan' constituency",
          not any(str(name).startswith("nan|") for name in named))

    print("      a failing run")

    def broken(**kwargs):
        raise RuntimeError("simulated failure")

    flag_count = _count("flags")
    row["RECOMMENDED_AMOUNT"] += 1000
    poller.fetch_shard(aligarh)
    pipeline_mod.run_pipeline = broken
    try:
        failed = poller.refresh_analysis("test")
    finally:
        pipeline_mod.run_pipeline = original_pipeline
    now = datetime.now(timezone.utc)
    check("returns nothing and leaves the flags in place",
          failed is None and _count("flags") == flag_count)
    check("clears its marker even so", not db.get_state("analysis_started_at"))
    check("is not retried straight away, even after a sweep",
          not poller.analysis_due(now + timedelta(minutes=5), after_sweep=True))
    check("but is retried once the retry gap and interval have passed",
          poller.analysis_due(now + max(pmod.RETRY_GAP, pmod.ANALYSIS_EVERY)
                              + timedelta(minutes=1)))

    print("      flags from before runs were recorded")
    for key in ("last_analysis_at", "data_changed_at", "analysis_covers_changes_at",
                "last_analysis_attempt_at"):
        db.set_state(key, "")
    check("flags with no recorded run are treated as behind, once",
          poller.note_unrecorded_analysis() and poller.analysis_due()
          and not poller.note_unrecorded_analysis())

    with db.connect() as con:
        con.execute("UPDATE works SET source = 'esakshi_csv'")
        kept = con.execute("SELECT COUNT(*), SUM(recommended) FROM fundflows "
                           "WHERE source = 'esakshi_api'").fetchone()
    pipeline_mod.run_pipeline(verbose=False)
    with db.connect() as con:
        still = con.execute("SELECT COUNT(*), SUM(recommended) FROM fundflows "
                            "WHERE source = 'esakshi_api'").fetchone()
    check("a corpus not read from the portal keeps its fund positions untouched",
          tuple(kept) == tuple(still), f"{tuple(kept)} -> {tuple(still)}")


class _BreakerPortal(FakePortal):
    """The fake portal behind a real circuit breaker, as EsakshiClient uses one:
    every call asks the breaker first and reports how it went."""

    def _guarded(self, call, *args):
        self.breaker.check()
        try:
            out = call(*args)
        except SourceError as exc:
            self.breaker.record(False, str(exc))
            raise
        self.breaker.record(True)
        return out

    def watermark(self, combo):
        return self._guarded(super().watermark, combo)

    def tile_report(self, combo, tile):
        return self._guarded(super().tile_report, combo, tile)


def _open_breaker(portal, error: str, trial_due: bool = False) -> None:
    import time as _time
    breaker = CircuitBreaker(window=8, threshold=0.5, base_seconds=1800, trial_seconds=300)
    for _ in range(8):
        breaker.record(False, error)
    if trial_due:
        breaker._last_trial = _time.monotonic() - 301
    portal.breaker = breaker


def test_portal_pause(tiles) -> None:
    print("\n[15] a portal that stops answering: paused, named, and retried")
    from fastapi.testclient import TestClient
    from astra.api.main import app
    from astra.ingestion import instance_lock

    portal = _BreakerPortal(tiles)
    portal.breaker = CircuitBreaker()
    poller = fresh_poller(portal)
    poller.heartbeat()
    client = TestClient(app)
    goa = next(s for s in poller.registry() if s.shard_id == "2:12:105")
    db.save_watermark("2:12:105", lifecycle=validate.DIRTY)

    print("      while the breaker is open")
    _open_breaker(portal, "ReadTimeout: portal did not answer in 45 s")
    portal.calls.clear()
    for _ in range(4):
        report = poller.heartbeat()
    national = db.get_watermark("2:0:0") or {}
    area = db.get_watermark("2:12:105") or {}
    check("nothing is asked of the portal", not portal.calls, str(portal.calls[:4]))
    check("the minutes it held back are not counted as failures",
          not national.get("consecutive_failures") and not area.get("consecutive_failures"),
          f"national={national.get('consecutive_failures')} area={area.get('consecutive_failures')}")
    check("a slice owed a read keeps its place in the queue",
          area.get("lifecycle") == validate.DIRTY and goa in poller.pending_shards(),
          str(area.get("lifecycle")))
    check("the cycle says it is paused", report.note.startswith("portal paused"), report.note)
    saved = json.loads(db.get_state("portal_status") or "{}")
    check("the real error is saved for the website, not 'circuit open'",
          saved.get("open") and saved.get("last_error") == "ReadTimeout: portal did not answer in 45 s"
          and saved.get("failing_since") and saved.get("next_attempt_at"), json.dumps(saved)[:160])

    lock = instance_lock.WriterLock()
    lock.acquire()
    try:
        fresh = client.get("/meta/freshness").json()
    finally:
        lock.release()
    check("the website is told the portal is not answering, and why",
          fresh["status"] == "degraded" and (fresh.get("portal") or {}).get("open") is True
          and "ReadTimeout" in (fresh["portal"].get("last_error") or ""),
          json.dumps(fresh.get("portal"))[:160])
    stopped = client.get("/meta/freshness").json()
    check("but not once the poller has stopped: its last word is history",
          (stopped.get("portal") or {}).get("open") is False)

    print("      a trial every few minutes")
    _open_breaker(portal, "HTTP 503 from /getTilesData", trial_due=True)
    portal.calls.clear()
    report = poller.heartbeat()
    check("once a trial is due, the national check is tried",
          ("watermark", "2:0:0") in portal.calls, str(portal.calls[:4]))
    check("the portal answering closes the breaker", not portal.breaker.is_open)
    portal.calls.clear()
    poller.heartbeat()
    check("and the next minute resumes the queue it held back",
          ("report", "2:12:105") in portal.calls
          and (db.get_watermark("2:12:105") or {}).get("lifecycle") != validate.DIRTY,
          str(portal.calls[:8]))
    check("and the website hears the portal is back",
          json.loads(db.get_state("portal_status") or "{}").get("open") is False)

    _open_breaker(portal, "HTTP 503 from /getTilesData", trial_due=True)
    portal.fail_shards.add("2:0:0")
    wait = portal.breaker.opens_in()
    portal.calls.clear()
    poller.heartbeat()
    check("a trial that fails is one real failure, and the wait does not grow",
          (db.get_watermark("2:0:0") or {}).get("consecutive_failures") == 1
          and portal.breaker.opens_in() <= wait
          and portal.calls.count(("watermark", "2:0:0")) == 1, str(portal.calls))
    portal.calls.clear()
    poller.heartbeat()
    check("and the next minute waits for the next trial", not portal.calls, str(portal.calls))
    portal.fail_shards.clear()
    portal.breaker = CircuitBreaker()

    print("      failing checks are named for what they are")
    for sid in ("2:0:0", "2:0:0", "2:0:0", "2:33:0", "2:33:0", "2:33:0"):
        db.mark_shard_failure(sid, "HTTP 500", lifecycle=validate.RETRY)
    stale = {s["shard_id"]: s for s in client.get("/meta/freshness").json()["stale"]}
    national, state = stale.get("2:0:0") or {}, stale.get("2:33:0") or {}
    check("the national check is a Lok Sabha national check, not an unnamed '(RS)'",
          national.get("scope") == "national" and national.get("house") == "LS"
          and national.get("place") is None, json.dumps(national)[:160])
    check("a state-level check carries its state's name",
          state.get("scope") == "state" and state.get("house") == "LS"
          and state.get("place") == "UTTAR PRADESH", json.dumps(state)[:160])


class _FakePhotoPortal:
    """The two attachment calls, scripted: a work number maps to its files."""

    def __init__(self, files: dict[str, list[tuple[str, str]]], image: bytes):
        self.files, self.image = files, image
        self.calls: list[tuple[str, str]] = []
        self.down = False

    def attachments(self, portal_work_id: str):
        self.calls.append(("list", portal_work_id))
        if self.down:
            raise CircuitOpen("attachment service paused")
        return self.files.get(portal_work_id, [])

    def attachment(self, attach_id: str) -> bytes:
        self.calls.append(("file", attach_id))
        return self.image


def test_photo_checks(tiles) -> None:
    print("\n[16] held duplicate matches get their photos checked at night, gently")
    import io
    from datetime import datetime, timedelta
    from fastapi.testclient import TestClient
    from PIL import Image
    from astra.api.main import app

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (120, 90, 30)).save(buf, "JPEG")
    photos_portal = _FakePhotoPortal({
        "101": [("a101", "p1.jpg")], "102": [("a102", "p2.jpg")],
        "103": [("a103", "q1 completion.pdf")],
        "201": [("a201", "b1.jpg")], "202": [], "203": [("a203", "b3.jpeg")], "204": [],
        "301": [("a301", "late.jpg")]}, buf.getvalue())
    with db.connect() as con:
        for table in ("duplicate_groups", "photo_checks", "work_attachments"):
            con.execute(f"DELETE FROM {table}")
    db.replace_duplicate_groups([
        {"group_id": "pair:aaa", "kind": "pair", "work_ids": ["P1", "P2"]},
        {"group_id": "pair:bbb", "kind": "pair", "work_ids": ["Q1", "Q2"]},
        {"group_id": "batch:ccc", "kind": "batch",
         "work_ids": ["B1", "B2", "B3", "B4", "N1", "N2", "N3"]}])
    ids = {"P1": "101", "P2": "102", "Q1": "103", "B1": "201", "B2": "202", "B3": "203", "B4": "204"}

    saved = (pmod.PHOTO_HOURS, pmod.PHOTOS_PER_NIGHT, pmod.PHOTOS_PER_CHECK,
             pmod.PHOTOS_UNASKED_PER_CHECK)
    try:
        pmod.PHOTO_HOURS, pmod.PHOTOS_PER_NIGHT, pmod.PHOTOS_PER_CHECK = "21:00-07:00", 5, 3
        pmod.PHOTOS_UNASKED_PER_CHECK = 2
        portal = FakePortal(tiles)
        poller = pmod.Poller(client=portal, houses=(HOUSE_LS,), verbose=False)
        poller.photo_client = photos_portal
        night = datetime(2026, 9, 17, 23, 0).astimezone()
        poller._photo_ids, poller._photo_ids_at = ids, night.astimezone(pmod.timezone.utc)

        check("a night is named by the date its hours began",
              pmod.Poller.photo_night(night, (1260, 420)) == "2026-09-17"
              and pmod.Poller.photo_night(night.replace(day=18, hour=3), (1260, 420)) == "2026-09-17"
              and pmod.Poller.photo_night(night.replace(day=18, hour=21), (1260, 420)) == "2026-09-18")

        noon = night.replace(hour=12)
        check("outside its hours it asks the portal nothing",
              not poller.photo_checks(now=noon) and not photos_portal.calls
              and not _count("photo_checks"))
        busy = pmod.CycleReport(started_at=pmod._now(), dirty=("2:33:418",))
        check("in a minute busy with a portal change it stands down",
              not poller.photo_checks(busy, now=night) and not photos_portal.calls)
        portal.breaker = _OpenBreaker()
        check("and while the portal's figures are not answering",
              not poller.photo_checks(now=night) and not photos_portal.calls)
        portal.breaker = _NullBreaker()
        pmod.PHOTOS_PER_NIGHT = 0
        off_by_count = poller.photo_checks(now=night)
        pmod.PHOTOS_PER_NIGHT, pmod.PHOTO_HOURS = 5, "off"
        check("it can be switched off by count or by hours",
              not off_by_count and not poller.photo_checks(now=night) and not photos_portal.calls)
        pmod.PHOTO_HOURS = "21:00-07:00"
        poller._stop = True
        check("a stop requested before a work is checked leaves it for later",
              not sum(poller.photo_checks(now=night).values()) and not photos_portal.calls
              and not _count("photo_checks"))
        poller._stop = False

        changed_before = db.get_state("data_changed_at")
        first = poller.photo_checks(now=night)
        asked = [pid for kind, pid in photos_portal.calls if kind == "list"]
        check("at night it checks a few held works a minute, pairs first",
              asked == ["101", "102", "103"], str(photos_portal.calls))
        check("fetching only photos, never the certificate",
              [a for kind, a in photos_portal.calls if kind == "file"] == ["a101", "a102"])
        with db.connect() as con:
            statuses = dict(con.execute("SELECT work_id, status FROM photo_checks").fetchall())
        check("works not yet completed are recorded without a request, a few a minute",
              statuses == {"P1": "photos", "P2": "photos", "Q1": "documents",
                           "Q2": "not_completed", "N1": "not_completed"}, str(statuses))
        check("the night's count covers only works that asked the portal",
              json.loads(db.get_state("photo_night")) == {"night": "2026-09-17", "asked": 3}
              and first["not_completed"] == 2, db.get_state("photo_night"))
        check("while checks go on, the analysis is not triggered yet",
              db.get_state("photo_results_pending") and db.get_state("data_changed_at") == changed_before)

        photos_portal.calls.clear()
        poller.photo_checks(now=night + timedelta(minutes=1))
        check("the next minute takes what is left of the night's allowance",
              [pid for kind, pid in photos_portal.calls if kind == "list"] == ["201", "202"]
              and json.loads(db.get_state("photo_night"))["asked"] == 5, str(photos_portal.calls))
        photos_portal.calls.clear()
        poller.photo_checks(now=night + timedelta(minutes=2))
        check("with the allowance spent it asks nothing more tonight", not photos_portal.calls)
        check("and the night's checks become a change the analysis reads, once",
              not db.get_state("photo_results_pending")
              and db.get_state("data_changed_at") not in (None, "", changed_before)
              and poller.analysis_due())

        tomorrow = night + timedelta(days=1)
        poller._photo_ids_at = tomorrow.astimezone(pmod.timezone.utc)
        photos_portal.calls.clear()
        poller.photo_checks(now=tomorrow)
        check("the next night starts a new allowance and finishes the batch",
              [pid for kind, pid in photos_portal.calls if kind == "list"] == ["203", "204"]
              and json.loads(db.get_state("photo_night")) == {"night": "2026-09-18", "asked": 2},
              str(photos_portal.calls))
        check("a checked work is not asked about again",
              not db.due_photo_checks(recheck_before=(tomorrow - timedelta(days=14)).isoformat()))
        marked = db.get_state("data_changed_at")
        poller.photo_checks(now=tomorrow.replace(hour=8) + timedelta(days=1))
        check("checks left waiting when the hours end are handed to the analysis too",
              not db.get_state("photo_results_pending") and db.get_state("data_changed_at") != marked)

        print("      a failing attachment service")
        db.replace_duplicate_groups([{"group_id": "pair:ddd", "kind": "pair", "work_ids": ["L1", "L2"]}])
        later = tomorrow + timedelta(hours=1)
        poller._photo_ids = {**ids, "L1": "301", "L2": "302"}
        poller._photo_ids_at = later.astimezone(pmod.timezone.utc)
        photos_portal.down, photos_portal.calls = True, []
        tally = poller.photo_checks(now=later)
        check("the run stops at the first refusal and pauses",
              tally["stopped_early"] == 1 and len(photos_portal.calls) == 1
              and db.get_state("photo_paused_until"), str(photos_portal.calls))
        photos_portal.down, photos_portal.calls = False, []
        check("nothing is asked until the pause is over",
              not poller.photo_checks(now=later + timedelta(minutes=5)) and not photos_portal.calls)
        poller.photo_checks(now=later + pmod.RETRY_GAP + timedelta(minutes=1))
        check("and then it carries on",
              [pid for kind, pid in photos_portal.calls if kind == "list"] == ["301", "302"],
              str(photos_portal.calls))

        print("      what the website shows")
        poller._write_status(pmod.CycleReport(started_at=pmod._now()))
        shown = TestClient(app).get("/meta/freshness").json().get("photos") or {}
        check("the data page reports the settings, the night and the progress",
              shown.get("enabled") and shown.get("hours") == "21:00-07:00" and shown.get("per_night") == 5
              and shown.get("held_works") == 2 and shown.get("checked") == 2
              and (shown.get("last_night") or {}).get("night") == "2026-09-18"
              and shown.get("last_at"), json.dumps(shown)[:300])
    finally:
        (pmod.PHOTO_HOURS, pmod.PHOTOS_PER_NIGHT, pmod.PHOTOS_PER_CHECK,
         pmod.PHOTOS_UNASKED_PER_CHECK) = saved


def test_live() -> None:
    print("\n[12] live portal — the loop's assumptions still hold")
    client = EsakshiClient()
    try:
        national = client.watermark("0,0,0,2")
    except (SourceError, CircuitOpen) as exc:
        skip("live portal reachable", str(exc)[:70])
        return
    recommended = national.counts.get("Works Recommended")
    check("the national watermark answers with a count",
          isinstance(recommended, int) and recommended > 100_000,
          f"recommended={recommended:,}")

    states = client.states()
    total = 0
    for st in states[:6]:
        wm = client.watermark(f"{st['STATE_ID']},0,0,2")
        total += wm.counts.get("Works Recommended") or 0
    check("state watermarks are cheap and answer", total > 0,
          f"first 6 states hold {total:,} works")
    client.close()


# ----------------------------------------------------------------------- main
def main() -> int:
    tiles = load_tiles()
    print("=" * 74)
    print("ASTRA Stage C — the poller")
    print(f"scratch database: {os.environ['ASTRA_DB_PATH']}")
    print("=" * 74)
    if tiles is None:
        print("\nFixtures missing — run: python tests/test_esakshi.py --refresh")
        return 1
    try:
        for fn in (test_quiet_cycle, test_descent, test_hash_skip,
                   test_gates_in_the_loop, test_transient_recovery,
                   test_genuinely_empty, test_failure_and_escalation,
                   test_restart_resumes, test_reconcile, test_after_sweep,
                   test_schedule, test_kill_switch,
                   test_freshness_endpoint, test_single_writer,
                   test_live_status, test_recent_updates,
                   test_data_source_live, test_duplicate_listings,
                   test_removal_lifecycle, test_move_between_slices,
                   test_later_report_only, test_rotation,
                   test_analysis_refresh, test_portal_pause, test_photo_checks):
            db.init_db(force=True)
            with db.connect() as con:
                for table in ("works", "shards", "shard_watermarks",
                              "work_versions", "provenance", "poller_state",
                              "work_listing", "missing_works", "retired_works",
                              "shard_parity", "flags", "fundflows", "feedback",
                              "payments"):
                    con.execute(f"DELETE FROM {table}")
            fn(tiles)
        if "--live" in sys.argv:
            test_live()
        else:
            skip("live portal checks", "pass --live to include them")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

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
