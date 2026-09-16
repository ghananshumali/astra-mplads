"""Running unattended: backups, health, alerts and the supervisor.

    python tests/test_ops.py

Offline and isolated: a scratch database, scratch backup, log and state
directories, and the alert channel limited to the log file, so no notification
is shown and nothing leaves the machine. Real child processes are started and
stopped to test restarts and the job object.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="astra-ops-"))
os.environ["ASTRA_DB_PATH"] = str(_TMP / "scratch.db")
os.environ["ASTRA_PROCESSED_DIR"] = str(_TMP / "processed")
os.environ["ASTRA_BACKUP_DIR"] = str(_TMP / "backups")
os.environ["ASTRA_LOG_DIR"] = str(_TMP / "logs")
os.environ["ASTRA_ALERT_CHANNELS"] = "log"
os.environ["ASTRA_SHARD_CACHE"] = str(_TMP / "shards")

from astra import db                                           # noqa: E402
from astra.ingestion import instance_lock                      # noqa: E402
from astra.ops import backup, health, notify, state            # noqa: E402
from astra.ops.supervisor import Service, Supervisor, _Job, _kill_tree  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []
UTC = timezone.utc
STATE = _TMP / "processed" / "ops_state.json"


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                         text=True).stdout if os.name == "nt" else ""
    return str(pid) in out


def seed_db() -> None:
    db.init_db(force=True)
    with db.connect() as con:
        con.execute("DELETE FROM works")
        con.executemany("INSERT INTO works (work_id, source, state) VALUES (?, 'esakshi_api', 'X')",
                        [(f"W{i}",) for i in range(50)])


# ------------------------------------------------------------------ backups
def test_backup() -> None:
    print("\n[1] backups: a checked copy, the newest kept, the rest never touched")
    seed_db()
    directory = Path(os.environ["ASTRA_BACKUP_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    manual = directory / "astra.db.bak-by-hand"
    manual.write_bytes(b"kept")
    t0 = datetime(2026, 9, 16, 21, 0, tzinfo=UTC)
    first = backup.take(keep=2, state_path=STATE, now=t0)
    check("a backup is a complete database under its final name",
          first["ok"] and first["works"] == 50 and Path(first["file"]).name == "astra-20260916-210000.db"
          and not list(directory.glob("*.part")), json.dumps(first)[:160])
    copy = sqlite3.connect(first["file"])
    check("and reads back", copy.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 50)
    copy.close()
    for hours in (1, 2):
        backup.take(keep=2, state_path=STATE, now=t0 + timedelta(hours=hours))
    names = [b["name"] for b in backup.backups()]
    check("only the newest two are kept, and a hand-made copy is left alone",
          names == ["astra-20260916-230000.db", "astra-20260916-220000.db"] and manual.exists(),
          str(names))
    saved = state.load(STATE)["backup"]
    check("the last success is recorded", saved["last_ok_at"] == (t0 + timedelta(hours=2)).isoformat()
          and saved["last_error"] is None)

    failed = backup.take(source=_TMP / "missing.db", keep=2, state_path=STATE,
                         now=t0 + timedelta(hours=3))
    saved = state.load(STATE)["backup"]
    check("a failure is returned and recorded, not raised, and the last success is kept",
          not failed["ok"] and "no database" in failed["error"]
          and saved["last_error_at"] == (t0 + timedelta(hours=3)).isoformat()
          and saved["last_ok_at"] == (t0 + timedelta(hours=2)).isoformat())

    local = datetime(2026, 9, 17, 3, 0).astimezone()
    fresh_state = _TMP / "due.json"
    check("with no backup yet, it is due", backup.due(local, at="02:30", state_path=fresh_state))
    state.update({"backup": {"last_ok_at": (local - timedelta(minutes=10)).isoformat()}}, fresh_state)
    check("after today's backup, it is not", not backup.due(local, at="02:30", state_path=fresh_state))
    later = local + timedelta(days=1)
    check("the next day's slot makes it due again", backup.due(later, at="02:30", state_path=fresh_state))
    state.update({"backup": {"last_ok_at": (local - timedelta(minutes=10)).isoformat(),
                             "last_attempt_at": (later - timedelta(minutes=5)).isoformat()}},
                 fresh_state)
    check("a failed attempt waits for the retry gap", not backup.due(later, at="02:30",
                                                                      state_path=fresh_state))
    check("'off' turns it off", not backup.due(later, at="off", state_path=fresh_state))


def test_restore() -> None:
    print("\n[2] restore: checked, refused while a poller writes, the old file kept aside")
    newest = Path(backup.backups()[0]["path"])
    target = _TMP / "restored" / "astra.db"
    target.parent.mkdir()
    target.write_bytes(b"not a database")
    lock = instance_lock.WriterLock(instance_lock.lock_path(target))
    lock.acquire(role="poller")
    try:
        backup.restore(newest, target=target)
        refused = False
    except RuntimeError:
        refused = True
    finally:
        lock.release()
    check("refused while a poller holds the database", refused)
    aside = backup.restore(newest, target=target)
    check("then restored, with the previous file moved aside, not deleted",
          aside.read_bytes() == b"not a database"
          and sqlite3.connect(target).execute("SELECT COUNT(*) FROM works").fetchone()[0] == 50)
    stray = _TMP / "other.db"
    shutil.copy2(newest, stray)
    try:
        backup.restore(stray, target=target)
        named = False
    except ValueError:
        named = True
    check("a file not named as a backup is refused", named)


# ------------------------------------------------------------------ health
def test_health() -> None:
    print("\n[3] health: what is wrong, from what the processes recorded")
    seed_db()
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    with db.connect() as con:
        con.execute("DELETE FROM poller_state")
        con.execute("DELETE FROM shard_watermarks")
    db.set_state("last_heartbeat_at", (now - timedelta(minutes=2)).isoformat())
    db.set_state("last_reconcile_at", (now - timedelta(hours=3)).isoformat())
    ops = {"backup": {"last_ok_at": (now - timedelta(hours=5)).isoformat()}}
    services = {"api": {"expected": True, "healthy": True}, "web": {"expected": True, "healthy": True},
                "poller": {"expected": True, "healthy": True}}
    lock = instance_lock.WriterLock(instance_lock.lock_path(db.DB_PATH))
    lock.acquire(role="poller")
    try:
        clean = health.assess(now=now, services=services, ops=ops)
        check("a healthy system raises nothing", clean == [], str([c.key for c in clean]))

        db.set_state("last_heartbeat_at", (now - timedelta(minutes=40)).isoformat())
        db.set_state("portal_status", json.dumps({"open": True, "last_error": "ReadTimeout",
                                                  "failing_since": (now - timedelta(minutes=45)).isoformat()}))
        db.save_watermark("2:0:0", lifecycle="RETRY")
        for _ in range(3):
            db.mark_shard_failure("2:0:0", "ReadTimeout", lifecycle="RETRY")
        with db.connect() as con:                  # the wall clock, not this test's `now`
            con.execute("UPDATE shard_watermarks SET checked_at = ?",
                        ((now - timedelta(minutes=40)).isoformat(),))
        down ={**services, "api": {"expected": True, "healthy": False, "detail": "see api.log"}}
        keys = {c.key: c for c in health.assess(now=now, services=down, ops={"backup": {
            "last_ok_at": (now - timedelta(hours=40)).isoformat()}})}
        check("a stalled poller, an unreachable portal, failing checks, a silent API and an old "
              "backup are each named",
              {"checks_stalled", "portal_unreachable", "checks_failing", "api_down",
               "backup_overdue"} <= set(keys) and "national check" in keys["checks_failing"].detail
              and "ReadTimeout" in keys["portal_unreachable"].detail, str(sorted(keys)))
        db.set_state("sweep_started_at", now.isoformat())
        check("a long sweep is not a stall", "checks_stalled" not in
              {c.key for c in health.assess(now=now, services=services, ops=ops)})
        db.set_state("sweep_started_at", "")
        db.set_state("last_heartbeat_at", (now - timedelta(minutes=84)).isoformat())
        fresh = {**services, "poller": {"expected": True, "healthy": True,
                                        "started_at": (now - timedelta(minutes=1)).isoformat()}}
        check("after downtime, a poller started a minute ago is not stalled by its old last check",
              "checks_stalled" not in {c.key for c in health.assess(now=now, services=fresh, ops=ops)})
        slow = {**services, "poller": {"expected": True, "healthy": True,
                                       "started_at": (now - timedelta(minutes=20)).isoformat()}}
        stalled = {c.key: c for c in health.assess(now=now, services=slow, ops=ops)}
        check("a poller started 20 min ago that has not checked yet is stalled, and says so",
              "checks_stalled" in stalled
              and "has not completed a check yet" in stalled["checks_stalled"].detail,
              stalled["checks_stalled"].detail if "checks_stalled" in stalled else "")
        with db.connect() as con:
            con.execute("UPDATE shard_watermarks SET checked_at = ?",
                        ((now - timedelta(minutes=1)).isoformat(),))
        check("a long first check, still reading areas, is not a stall",
              "checks_stalled" not in {c.key for c in health.assess(now=now, services=slow, ops=ops)})
    finally:
        lock.release()
    keys = {c.key for c in health.assess(now=now, services=services, ops=ops)}
    check("with no poller running, portal checks are reported stopped, and its portal state "
          "is not believed", "poller_stopped" in keys and "portal_unreachable" not in keys,
          str(sorted(keys)))
    failed = {"backup": {"last_ok_at": (now - timedelta(hours=5)).isoformat(),
                         "last_error_at": (now - timedelta(hours=1)).isoformat(), "last_error": "disk"}}
    check("a backup that failed after the last success is reported",
          "backup_failed" in {c.key for c in health.assess(now=now, services={}, ops=failed)})


# ------------------------------------------------------------------ alerts
def test_notifier() -> None:
    print("\n[4] alerts: after the grace period, reminded, and cleared")
    sent: list[tuple[str, str]] = []

    def sender(title, body, *, channels=None, now=None):
        sent.append((title, body))
        return {"fake": "sent"}

    path = _TMP / "alerts.json"
    notifier = notify.Notifier(state_path=path, sender=sender, remind=timedelta(hours=12))
    t0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    api = health.Condition("api_down", "critical", "The site's data service is not answering",
                           "see api.log", grace_minutes=5)
    notifier.update([api], now=t0)
    check("nothing within the grace period", sent == [])
    notifier.update([api], now=t0 + timedelta(minutes=6))
    check("announced once it lasts", len(sent) == 1 and sent[0][0] == "ASTRA needs attention"
          and "not answering" in sent[0][1], str(sent))
    notifier.update([api], now=t0 + timedelta(hours=1))
    check("not repeated every minute", len(sent) == 1)
    notifier.update([api], now=t0 + timedelta(hours=13))
    check("reminded after twelve hours", len(sent) == 2 and sent[1][0].endswith("(still)"))
    blip = health.Condition("parity_differs", "warning", "1 area differs", "", grace_minutes=120)
    notifier.update([blip], now=t0 + timedelta(hours=13, minutes=1))
    check("recovery is announced; a blip that clears within its grace never is",
          len(sent) == 3 and sent[2][0] == "ASTRA recovered" and "not answering" in sent[2][1])
    notifier.update([], now=t0 + timedelta(hours=13, minutes=2))
    check("and its clearing says nothing either", len(sent) == 3)

    def broken(title, body, now):
        raise RuntimeError("no network")

    notify.SENDERS["broken"] = broken
    try:
        result = notify.send("t", "b", channels=["log", "broken"], now=t0)
    finally:
        notify.SENDERS.pop("broken")
    log_text = (Path(os.environ["ASTRA_LOG_DIR"]) / "alerts.log").read_text(encoding="utf-8")
    check("a failing channel is recorded and does not stop the others",
          result["log"] == "sent" and "no network" in result["broken"]
          and "alert channel failed" in log_text, json.dumps(result))


# ------------------------------------------------------------------ processes
def test_service() -> None:
    print("\n[5] restarts: backoff, a second poller, stopping the whole tree")
    logs = _TMP / "service-logs"
    crash = Service("crash", [sys.executable, "-c", "import sys; print('boom'); sys.exit(1)"],
                    cwd=_TMP, log_dir=logs, first_backoff=0.3, max_backoff=1.2, stable_after=60)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        crash.poll()
        time.sleep(0.05)
    starts = len(crash.starts)
    check("a crashing process is restarted, more slowly each time, up to the cap",
          3 <= starts <= 8 and crash.backoff == 1.2 and crash.last_exit == 1,
          f"{starts} starts, backoff {crash.backoff}")
    check("its output goes to its log", "boom" in (logs / "crash.log").read_text(encoding="utf-8"))
    crash.stop()

    second = Service("poller", [sys.executable, "-c", "import sys; sys.exit(3)"], cwd=_TMP,
                     log_dir=logs, first_backoff=0.3, max_backoff=50)
    second.poll()
    second.proc.wait(timeout=20)
    second.poll()
    check("a poller refused because another holds the database waits the long interval",
          second.last_exit == 3 and second.next_start - time.monotonic() > 40
          and second.backoff == 0.3)

    code = ("import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(p.pid, flush=True); time.sleep(60)")
    tree = Service("tree", [sys.executable, "-c", code], cwd=_TMP, log_dir=logs)
    tree.poll()
    grandchild = None
    for _ in range(100):
        text = (logs / "tree.log").read_text(encoding="utf-8").split()
        numbers = [t for t in text if t.isdigit()]
        if numbers:
            grandchild = int(numbers[-1])
            break
        time.sleep(0.1)
    child = tree.proc.pid
    tree.stop()
    time.sleep(1)
    check("stopping a process stops what it started", grandchild is not None
          and not alive(child) and not alive(grandchild), f"child {child}, grandchild {grandchild}")


def test_job_object() -> None:
    print("\n[6] the job object: a supervisor that dies takes its processes with it")
    if os.name != "nt":
        check("job objects are Windows-only", True)
        return
    script = (
        "import subprocess, sys, os\n"
        "sys.path.insert(0, %r)\n"
        "from astra.ops.supervisor import _Job\n"
        "job = _Job()\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(p.pid, job.add(p.pid), flush=True)\n"
        "os._exit(1)\n" % str(ROOT))
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         timeout=60).stdout.split()
    pid, added = int(out[0]), out[1] == "True"
    time.sleep(1.5)
    check("the process is in the job, and gone once the supervisor is killed",
          added and not alive(pid), f"pid {pid}, added {added}, alive {alive(pid)}")
    if alive(pid):
        _kill_tree(pid)


def test_tick_and_api() -> None:
    print("\n[7] the minute's work is recorded and served at /meta/ops")
    seed_db()
    sent: list = []
    notifier = notify.Notifier(state_path=STATE, sender=lambda t, b, **k: sent.append(t) or {})
    sup = Supervisor(web=False, poller=False, api=False, services=[], notifier=notifier,
                     state_path=STATE, backups=False)
    sup.started_at = datetime.now(UTC).isoformat()
    record = sup.tick()
    saved = state.load(STATE)["supervisor"]
    check("a tick records the processes and the conditions it found",
          saved["last_tick_at"] == record["last_tick_at"] and saved["services"] == {}
          and isinstance(saved["conditions"], list), json.dumps(saved)[:200])
    stop = _TMP / "stop.request"
    quick = Service("sleeper", [sys.executable, "-c", "import time; time.sleep(60)"], cwd=_TMP,
                    log_dir=_TMP / "service-logs")
    runner = Supervisor(web=False, poller=False, api=False, services=[quick], notifier=notifier,
                        state_path=STATE, backups=False)
    import threading
    thread = threading.Thread(target=runner.run, kwargs={"max_seconds": 30, "stop_path": stop})
    started = time.monotonic()
    thread.start()
    for _ in range(50):
        if quick.running:
            break
        time.sleep(0.1)
    pid = quick.proc.pid if quick.proc else None
    stop.write_text("now")
    thread.join(timeout=20)
    check("a stop request ends the supervisor cleanly, its processes with it",
          not thread.is_alive() and time.monotonic() - started < 15 and pid and not alive(pid)
          and not stop.exists() and state.load(STATE)["supervisor"]["stopped_at"],
          f"pid {pid}, {time.monotonic() - started:.1f}s")
    from fastapi.testclient import TestClient
    from astra.api.main import app
    body = TestClient(app).get("/meta/ops").json()
    check("/meta/ops reports the supervisor as not running, and the backups kept",
          body["supervisor_running"] is False and body["backup"]["kept"] == 2
          and body["backup"]["newest"]["name"] == "astra-20260916-230000.db",
          json.dumps(body)[:240])


def main() -> int:
    print("=" * 74)
    print("ASTRA unattended running — backups, health, alerts, supervisor")
    print(f"scratch: {_TMP}")
    print("=" * 74)
    try:
        for fn in (test_backup, test_restore, test_health, test_notifier, test_service,
                   test_job_object, test_tick_and_api):
            try:
                fn()
            except Exception as exc:                   # noqa: BLE001
                check(f"{fn.__name__} ran", False, f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 74)
    failed = [r for r in results if r[0] == FAIL]
    print(f"  {len(results) - len(failed)} passed, {len(failed)} failed")
    for _, name, detail in failed:
        print(f"    FAILED: {name} — {detail}")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
