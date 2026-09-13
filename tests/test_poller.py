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

from astra import db                                            # noqa: E402
from astra.ingestion import poller as pmod                      # noqa: E402
from astra.ingestion import validate                            # noqa: E402
from astra.ingestion.esakshi_api import (                       # noqa: E402
    HOUSE_LS, RECORD_TILES, CircuitOpen, EsakshiClient, SourceError, Watermark,
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
    poller.fetch_shard(other)
    check("each store is appended to the ledger, not written over it",
          len(db.load_provenance()) == 2, f"{len(db.load_provenance())} rows")

    row = moved["recommended"][0]
    old_text = row["WORK_DESCRIPTION"]
    row["WORK_DESCRIPTION"] = old_text + " (revised)"
    outcome = poller.fetch_shard(shard, force=True)
    check("an edited work is stored as a change", outcome["changed"] >= 1, str(outcome))

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
                   test_later_report_only):
            db.init_db(force=True)
            with db.connect() as con:
                for table in ("works", "shards", "shard_watermarks",
                              "work_versions", "provenance", "poller_state",
                              "work_listing", "missing_works", "retired_works",
                              "shard_parity"):
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
