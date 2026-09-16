"""Is ASTRA working? The conditions a person should hear about.

Each check reads what the running processes already record (the poller's
schedule in `poller_state`, the writer lock, the watermarks, the parity table,
the backup record) and returns a condition when something is wrong. A
condition carries a grace period: most problems fix themselves (a slice
re-read five minutes later, a portal back after a blip, a process restarted by
the supervisor), and an alert for every blip would soon be ignored. The
notifier (`astra.ops.notify`) only speaks once a condition has lasted longer
than its grace.

Nothing here writes to the database or talks to the portal.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import DB_PATH

#: No minute check for this long, while no sweep or analysis is running.
STALLED_AFTER = timedelta(minutes=15)
#: The portal's breaker open for this long.
PORTAL_DOWN_AFTER = timedelta(minutes=30)
#: The nightly sweep older than this.
SWEEP_OVERDUE_AFTER = timedelta(hours=26)
#: No successful backup for this long.
BACKUP_OVERDUE_AFTER = timedelta(hours=36)
#: Free disk below this.
DISK_LOW_BYTES = 5 * 1024 ** 3


@dataclass
class Condition:
    key: str
    severity: str            # "warning" | "critical"
    title: str
    detail: str
    grace_minutes: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _stamp(value) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes / 60
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} days"


def assess(*, now: datetime | None = None, services: dict | None = None,
           ops: dict | None = None, db_path: Path | None = None) -> list[Condition]:
    """Every condition that holds right now.

    `services` is the supervisor's view of its processes: {name: {"expected":
    bool, "healthy": bool, "detail": str}}. `ops` is the operations state
    (`astra.ops.state`), for the backup record.
    """
    from .. import db
    from ..ingestion import instance_lock

    now = now or datetime.now(timezone.utc)
    out: list[Condition] = []
    services = services or {}
    ops = ops or {}
    db_path = Path(db_path or DB_PATH)

    for name, title in (("api", "The site's data service is not answering"),
                        ("web", "The website is not being served")):
        svc = services.get(name) or {}
        if svc.get("expected") and not svc.get("healthy"):
            out.append(Condition(f"{name}_down", "critical", title,
                                 svc.get("detail") or "", grace_minutes=5))

    running = instance_lock.is_held(instance_lock.lock_path(db_path))
    poller = services.get("poller") or {}
    if poller.get("expected") and not running:
        out.append(Condition("poller_stopped", "critical", "Portal checks have stopped",
                             poller.get("detail") or "No poller is running, so the data "
                             "is no longer kept current.", grace_minutes=10))

    try:
        heartbeat = _stamp(db.get_state("last_heartbeat_at"))
        busy = bool(db.get_state("sweep_started_at") or db.get_state("analysis_started_at"))
        # The heartbeat is written when a check ends, and the first check after
        # downtime re-reads every area that moved meanwhile, so it can run for
        # minutes: areas being read are activity too. A poller that has only just
        # started is not stalled by an old heartbeat either.
        with db.connect() as con:
            area = _stamp(con.execute("SELECT MAX(checked_at) FROM shard_watermarks").fetchone()[0])
        launched = _stamp(poller.get("started_at"))
        since = max((s for s in (heartbeat, area, launched) if s), default=None) if heartbeat else None
        if running and not busy and since and now - since > STALLED_AFTER:
            out.append(Condition("checks_stalled", "warning", "Portal checks are not running",
                                 f"The poller is running but its last check was "
                                 f"{_ago(now - since)} ago." if since != launched else
                                 f"The poller started {_ago(now - since)} ago and has not "
                                 f"completed a check yet."))

        portal = json.loads(db.get_state("portal_status") or "null") or {}
        since = _stamp(portal.get("failing_since"))
        if running and portal.get("open") and since and now - since > PORTAL_DOWN_AFTER:
            out.append(Condition("portal_unreachable", "warning",
                                 "The eSAKSHI portal is not answering",
                                 f"Failing for {_ago(now - since)}. Last error: "
                                 f"{str(portal.get('last_error') or 'unknown')[:160]}"))

        stale = db.stale_shards(min_failures=3)
        if stale:
            names = [("national check" if s.get("scope") == "national" else
                      s.get("constituency_name") or s.get("state_name") or s["shard_id"])
                     for s in stale]
            out.append(Condition("checks_failing", "warning",
                                 f"{len(stale)} portal check(s) keep failing",
                                 ", ".join(names[:5]) + (" and more" if len(names) > 5 else ""),
                                 grace_minutes=30))

        parity = db.parity_summary()
        if parity.get("exception_count"):
            out.append(Condition("parity_differs", "warning",
                                 f"{parity['exception_count']} area(s) differ from the portal",
                                 "Stored figures do not match the portal's own tiles after "
                                 "re-reads.", grace_minutes=120))

        sweep = _stamp(db.get_state("last_reconcile_at"))
        if running and (sweep is None or now - sweep > SWEEP_OVERDUE_AFTER):
            out.append(Condition("sweep_overdue", "warning", "The nightly full check is overdue",
                                 "Never completed." if sweep is None else
                                 f"Last completed {_ago(now - sweep)} ago.",
                                 grace_minutes=60))

        last_ok = _stamp(db.get_state("last_analysis_at"))
        last_try = _stamp(db.get_state("last_analysis_attempt_at"))
        if running and last_try and (last_ok is None or last_try > last_ok) \
                and not db.get_state("analysis_started_at"):
            out.append(Condition("analysis_failing", "warning",
                                 "Recomputing the risk flags failed",
                                 "The site shows flags from the last successful run; see "
                                 "data/logs/poller.log.", grace_minutes=0))
    except Exception as exc:                             # the database itself
        out.append(Condition("database_unreadable", "critical", "The database cannot be read",
                             f"{type(exc).__name__}: {str(exc)[:200]}", grace_minutes=5))

    backup = ops.get("backup") or {}
    ok_at = _stamp(backup.get("last_ok_at"))
    error_at = _stamp(backup.get("last_error_at"))
    started = _stamp((ops.get("supervisor") or {}).get("first_started_at"))
    if error_at and (ok_at is None or error_at > ok_at):
        out.append(Condition("backup_failed", "warning", "The daily backup failed",
                             str(backup.get("last_error") or "")[:200]))
    elif (ok_at and now - ok_at > BACKUP_OVERDUE_AFTER) or \
            (ok_at is None and started and now - started > BACKUP_OVERDUE_AFTER):
        out.append(Condition("backup_overdue", "warning", "No recent backup",
                             "Never taken." if ok_at is None else
                             f"Last backup {_ago(now - ok_at)} ago."))

    try:
        free = shutil.disk_usage(db_path.parent).free
        if free < DISK_LOW_BYTES:
            out.append(Condition("disk_low", "warning", "Disk space is low",
                                 f"{free / 1024 ** 3:.1f} GB free beside the database."))
    except OSError:
        pass
    return out
