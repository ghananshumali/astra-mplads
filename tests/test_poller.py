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

    def watermark(self, combo: str) -> Watermark:
        shard_id = self._shard_id(combo)
        self.calls.append(("watermark", shard_id))
        if shard_id in self.fail_shards:
            raise SourceError("HTTP 500", path="/getTilesData", status=500)
        count = 0 if shard_id in self.zero_shards else self.n.get(shard_id, 0)
        wm = Watermark(combo=combo, fetched_at="now")
        wm.counts = {"Works Recommended": count}
        wm.totals = {"Works Recommended": float(count) * 1000}
        wm.suspicious_zero = shard_id in self.zero_shards
        return wm

    def tile_report(self, combo: str, tile: str) -> list[dict]:
        shard_id = self._shard_id(combo)
        self.calls.append(("report", shard_id))
        if shard_id in self.fail_shards:
            raise SourceError("HTTP 500", path="/getTilesReportData", status=500)
        rows = self.tiles[tile]
        if tile == "recommended":
            want = self.n.get(shard_id, 0)
            if shard_id in self.truncate_shards:
                want = max(1, want // 4)
            elif shard_id in self.truncate_once:
                # short on the first read only, correct from then on — exactly
                # what the first national sweep saw on 7 of 543 constituencies
                self.truncate_once.discard(shard_id)
                want = max(1, want // 4)
            rows = rows[:want]
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
                   test_freshness_endpoint):
            db.init_db(force=True)
            with db.connect() as con:
                for table in ("works", "shards", "shard_watermarks",
                              "work_versions", "provenance",
                              "poller_state"):
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
