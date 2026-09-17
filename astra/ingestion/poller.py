"""The control loop: sharded polling with hierarchical change detection.

    python -m astra.ingestion.poller                 # run until stopped
    python -m astra.ingestion.poller --once          # one heartbeat cycle
    python -m astra.ingestion.poller --reconcile     # one full record-level sweep
    python -m astra.ingestion.poller --registry      # re-enumerate shards only
    python -m astra.ingestion.poller --analyse       # re-run the analysis now

Why it is shaped this way
-------------------------
The portal has no way to tell us when something changes: no webhook, no feed,
no `updated_since` filter. So we ask. Asking for the whole country never
returns (measured: no response after 45 s), which is why the country is split
into 579 slices — 543 Lok Sabha constituencies plus 36 Rajya Sabha states.

Asking every slice every minute would be 579 requests a minute against a
government service, which is out of the question. What makes it cheap instead
is that the portal's counts are exactly additive: the national total equals the
sum of the states, and each state equals the sum of its constituencies, to the
paisa. So one cheap national question per minute tells us whether anything
moved anywhere, and the same question asked of 36 states and then of one
state's constituencies walks the difference down to the exact slice. At rest
that is **one request a minute**; a typical detection costs about forty.

There is deliberately no learned priority over shards. An earlier design
weighted slices by observed change rate to shave a few seconds off the
descent; it bought roughly six seconds in exchange for a parameter that is
wrong during warm-up and wrong again whenever a quiet state suddenly becomes
active. Sweeping all 36 states costs 7.5 s, so every slice is treated equally.

The counts are a hint, never an authority
-----------------------------------------
Two independent reasons the fast path cannot be the only path:

* The counts and the records do not agree exactly. Each report carries one
  id-less summary row, and the two figures are separate reads — measured
  national Rajya Sabha "Works Completed" at 10,080 against a state sum of
  10,081 across a 13-second sweep. So additivity is asserted with a tolerance
  and used as a consistency check, never as a loop's exit condition.
* If the counts endpoint were ever stale, cached or simply wrong, the heartbeat
  would sleep through a real change and see nothing. Nothing in the fast path
  could detect that.

`reconcile_all()` is the answer to both: once a night it re-reads every slice's
**records** regardless of what the counts say, diffs the id sets against what
we hold, and records whether each slice's count agreed with its records. It
must never be "optimised" into a counts-only sweep — that would inherit exactly
the blindness it exists to cover.

Between the minute and the night
--------------------------------
An edit that moves no figure — a corrected description, a new stage, an
agency's name — is invisible to the heartbeat and would otherwise wait for the
nightly sweep, up to a day. `rolling_reread()` closes most of that gap: during
working hours, in a minute with nothing else to do, it re-reads the one area
whose records were read longest ago. That is five requests a minute, each
about a single area. It stands down in any minute already busy with a change,
a recheck or a failing portal, while the circuit breaker is open, and for
areas read recently by any path. With every minute of a twelve-hour day free,
each area is re-read within about ten hours; minutes lost to changes stretch
that on a busy day, but an area that changed has just been read anyway.

The poller keeps the data current; the risk flags come from the analysis,
which reads the whole corpus. `refresh_analysis()` re-runs it when stored data
has actually changed, at most every `ANALYSIS_EVERY` and after a completed
nightly sweep, so the flags follow the portal within hours rather than waiting
for someone to re-run the pipeline by hand.

At night, photos
----------------
A duplicate match nothing recorded separates is held until evidence decides,
and the portal's photos of completed works are that evidence
(`astra.ingestion.photos`). Checking them needs the database's one writer, so
`photo_checks()` does it here: only in `PHOTO_HOURS`, when offices are not
editing and the rotation is idle, a few held works a minute and at most
`PHOTOS_PER_NIGHT` a night that need the portal, standing down in any minute
busy with a change or while the portal is failing. When the night's checks stop
they count as a change the analysis reads, so one re-run picks them all up.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import PROCESSED_DIR
from . import backfill, instance_lock, shard_cache, validate
from . import esakshi_map as emap
from .esakshi_api import (HOUSE_LS, HOUSE_RS, RECORD_TILES, TILE_KEYS,
                          CircuitOpen, EsakshiClient, Shard, SourceError,
                          run_parallel, shard_combo)

#: Seconds between heartbeats. Works arrive at roughly 20-25 an hour during
#: Indian working hours, i.e. one every few minutes, so a minute already
#: over-samples; going faster multiplies load on the portal for latency that
#: the portal's own data-entry lag dwarfs.
POLL_INTERVAL = float(os.environ.get("ASTRA_POLL_INTERVAL", "60"))
#: Local clock time for the nightly record-level reconciliation, HH:MM, or
#: "off". This is a preferred slot, not a trigger: a sweep that misses its
#: slot runs as soon as the poller is next awake.
RECONCILE_AT = os.environ.get("ASTRA_RECONCILE_AT", "03:00")
#: A failed or partial sweep is not recorded as done; wait this long before
#: trying again, so an unreachable portal is not hammered every minute.
RETRY_GAP = timedelta(minutes=int(os.environ.get("ASTRA_RETRY_GAP_MIN", "30")))
#: Share of shards a sweep must read successfully to count as a real sweep.
#: A sweep run while the circuit breaker is open fails every shard in
#: seconds; recording that as "done" would skip the safety net for a day.
HEALTHY_SWEEP = float(os.environ.get("ASTRA_HEALTHY_SWEEP", "0.9"))
#: How often the shard registry is re-enumerated from the portal.
REGISTRY_EVERY = timedelta(hours=24)
#: Houses to watch: 2 = Lok Sabha, 1 = Rajya Sabha.
HOUSES = tuple(int(h) for h in
               os.environ.get("ASTRA_HOUSES", "2,1").replace(" ", "").split(",") if h)
#: Consecutive failures on one slice before it is escalated by name.
ESCALATE_AFTER = int(os.environ.get("ASTRA_ESCALATE_AFTER", "3"))
#: Kill switch: set to 0 to pin the demo to the cache and stop all polling.
ENABLED = os.environ.get("ASTRA_POLLER_ENABLED", "1") != "0"
#: A work a slice stops listing is retired only if a second consistent read at
#: least this much later still does not list it. The portal has been seen to
#: return a short but self-consistent report for a few minutes.
RETIRE_CONFIRM = timedelta(minutes=int(os.environ.get("ASTRA_RETIRE_CONFIRM_MIN", "10")))
#: Wait before re-reading a slice whose stored figures differ from the portal's.
PARITY_RECHECK = timedelta(minutes=int(os.environ.get("ASTRA_PARITY_RECHECK_MIN", "5")))
#: Re-reads of one slice before leaving a difference to the nightly sweep. The
#: difference stays on the site, named, either way.
MAX_RECHECKS = int(os.environ.get("ASTRA_MAX_RECHECKS", "3"))
#: Slices re-read per heartbeat, so a burst of rechecks cannot stall the loop.
RECHECKS_PER_CYCLE = 10
#: Areas the rotation re-reads per heartbeat, oldest-read first; 0 turns it
#: off. One area costs one watermark and four reports, all about that area.
ROLLING_AREAS = int(os.environ.get("ASTRA_ROLLING_AREAS", "1"))
#: Local hours the rotation runs: "HH:MM-HH:MM" (may wrap past midnight),
#: "always", or "off". Offices edit in the day; the nightly sweep covers the night.
ROLLING_HOURS = os.environ.get("ASTRA_ROLLING_HOURS", "08:00-20:00")
#: An area whose records were read more recently than this, by any path, is
#: not due for the rotation.
ROLLING_MIN_AGE = timedelta(hours=float(os.environ.get("ASTRA_ROLLING_MIN_AGE_H", "3")))
#: Re-run the analysis when stored data changed, at most this often, and after
#: every completed nightly sweep that changed something; 0 turns it off. A run
#: over the full corpus takes about two minutes, and the heartbeat waits for
#: it exactly as it waits for the nightly sweep.
ANALYSIS_EVERY = timedelta(minutes=float(os.environ.get("ASTRA_ANALYSIS_EVERY_MIN", "180")))
#: Local hours the photo check of held duplicate matches runs: "HH:MM-HH:MM"
#: (may wrap past midnight), "always", or "off". Clear of the rotation's hours.
PHOTO_HOURS = os.environ.get("ASTRA_PHOTO_HOURS", "21:00-07:00")
#: Held works a night's photo check may ask the portal about, at two to four
#: requests each; 0 turns it off. A work not yet completed needs no request and
#: is not counted.
PHOTOS_PER_NIGHT = int(os.environ.get("ASTRA_PHOTOS_PER_NIGHT", "300"))
#: Of those, at most this many a minute, so a check never holds up the loop.
PHOTOS_PER_CHECK = int(os.environ.get("ASTRA_PHOTOS_PER_CHECK", "10"))
#: Works not yet completed recorded a minute (no request, one row each).
PHOTOS_UNASKED_PER_CHECK = 200
#: The portal's internal work numbers come from the cached completed reports
#: (about 4 s to read); they are reused for this long.
PHOTO_IDS_EVERY = timedelta(hours=1)

ALERTS_PATH = PROCESSED_DIR / "ingest_alerts.json"
STATUS_PATH = PROCESSED_DIR / "poller_status.json"

#: Lifecycles that mean "this slice still owes us a fetch" — read back on
#: startup so a restart resumes rather than restarts.
PENDING = (validate.DIRTY, validate.FETCHED, validate.RETRY, validate.QUARANTINED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def parse_reconcile_at(at: str | None) -> tuple[int, int] | None:
    """`"03:00"` -> (3, 0). `"off"`, blanks and invalid times disable it."""
    text = (at or "").strip().lower()
    if text in ("", "off", "none", "disabled"):
        return None
    try:
        hour, minute = (int(part) for part in text.split(":"))
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour, minute


def most_recent_slot(now: datetime, at: str | None = RECONCILE_AT) -> datetime | None:
    """The latest scheduled sweep time at or before `now`.

    The sweep is due whenever the last successful one is older than this
    slot. That single rule covers every case: the normal 03:00 run, a slot
    slept through (runs on waking), a heartbeat that ran long across the
    slot (runs on the next loop), several days asleep (runs once, not once
    per missed day), and never having run at all.
    """
    parsed = parse_reconcile_at(at)
    if parsed is None:
        return None
    if now.tzinfo is None:
        now = now.astimezone()
    slot = now.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    return slot


def parse_hours(text: str | None) -> tuple[int, int] | None:
    """`"08:00-20:00"` -> (480, 1200), minutes past local midnight.

    `"always"` is the whole day. `"off"`, blanks, invalid times and an empty
    range disable it.
    """
    value = (text or "").strip().lower()
    if value in ("always", "all day"):
        return 0, 24 * 60
    start_text, dash, end_text = value.partition("-")
    start, end = parse_reconcile_at(start_text), parse_reconcile_at(end_text)
    if not dash or start is None or end is None:
        return None
    start_min, end_min = start[0] * 60 + start[1], end[0] * 60 + end[1]
    return None if start_min == end_min else (start_min, end_min)


def in_hours(now: datetime, hours: tuple[int, int] | None) -> bool:
    """Is `now` inside the window? A naive time is taken as local."""
    if hours is None:
        return False
    if now.tzinfo is None:
        now = now.astimezone()
    minute = now.hour * 60 + now.minute
    start, end = hours
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end          # wraps past midnight


def _same_signature(stored_json: str | None, signature) -> bool:
    """Does a stored watermark signature equal one just read?"""
    if not stored_json:
        return False
    try:
        previous = json.loads(stored_json)
    except ValueError:
        return False
    return previous == json.loads(json.dumps(signature, default=str))


def national_shard_id(house: int) -> str:
    return f"{house}:0:0"


def state_shard_id(house: int, state_id: int) -> str:
    return f"{house}:{state_id}:0"


@dataclass
class CycleReport:
    """What one heartbeat cycle did — logged and written to poller_status.json."""

    started_at: str
    requests: int = 0
    houses_moved: tuple = ()
    states_probed: int = 0
    constituencies_probed: int = 0
    dirty: tuple = ()
    stored: int = 0
    changed: int = 0
    versions: int = 0
    quarantined: tuple = ()
    rolled: tuple = ()
    seconds: float = 0.0
    note: str = ""

    def as_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v)
                for k, v in self.__dict__.items()}


class Poller:
    """Owns the loop, the registry and the queue. The only writer."""

    def __init__(self, client: EsakshiClient | None = None, *,
                 houses: tuple[int, ...] = HOUSES, verbose: bool = True):
        self.client = client or EsakshiClient()
        self.houses = houses
        self.verbose = verbose
        self._stop = False
        #: The photo check's own portal client, made on first use; its circuit
        #: breaker is separate, so a failing attachment service cannot pause
        #: the watch on the portal's figures.
        self.photo_client = None
        self._photo_ids: dict[str, str] | None = None
        self._photo_ids_at: datetime | None = None

    # ------------------------------------------------------------- plumbing
    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[poller {datetime.now().strftime('%H:%M:%S')}] {message}",
                  flush=True)

    def stop(self, *_args) -> None:
        self._stop = True
        self.log("stop requested; finishing the current cycle")

    # ------------------------------------------------------------- registry
    def refresh_registry(self) -> int:
        """Re-enumerate every slice from the portal. Never hard-code ids."""
        shards = self.client.shards(houses=self.houses)
        rows = [{"shard_id": s.shard_id, "house": s.house, "state_id": s.state_id,
                 "constituency_id": s.constituency_id, "state_name": s.state_name,
                 "constituency_name": s.constituency_name} for s in shards]
        db.save_shards(rows)
        db.set_state("last_registry_at", _now())
        self.log(f"registry: {len(rows)} shards "
                 + ", ".join(f"house {h}={sum(1 for s in shards if s.house == h)}"
                             for h in self.houses))
        return len(rows)

    def registry(self, house: int | None = None) -> list[Shard]:
        rows = db.load_shards(house)
        return [Shard(house=r["house"], state_id=r["state_id"],
                      constituency_id=r["constituency_id"],
                      state_name=r["state_name"] or "",
                      constituency_name=r["constituency_name"] or "")
                for r in rows]

    # ------------------------------------------------- watermark comparison
    def _probe(self, shard_id: str, combo: str) -> tuple[bool, object, int | None]:
        """Read a watermark and say whether it moved since we last looked.

        Returns (moved, signature, recommended_count). A slice we have never
        seen counts as moved — that is how a cold start finds everything.
        """
        watermark = self.client.watermark(combo)
        signature = watermark.signature()
        stored = db.get_watermark(shard_id) or {}
        moved = not _same_signature(stored.get("signature_json"), signature)
        return moved, signature, watermark.counts.get("Works Recommended")

    @staticmethod
    def _failed(shard_id: str, exc: BaseException, lifecycle: str) -> int | None:
        """Count a real failure against a slice. A call the circuit breaker held
        back asked the portal nothing, so it is not one: counting it made one
        outage look like every slice failing, minute after minute."""
        if isinstance(exc, CircuitOpen):
            return None
        return db.mark_shard_failure(shard_id, str(exc), lifecycle=lifecycle)

    def _record_probe(self, shard_id: str, signature, count: int | None,
                      lifecycle: str, *, keep_payload: bool = False) -> None:
        # A probe that found nothing moved leaves the records' fingerprint in
        # place, so a later read of the same records can still skip the write.
        db.save_watermark(shard_id, signature=signature, n_records=count,
                          lifecycle=lifecycle, keep_payload=keep_payload)

    # ------------------------------------------------------------- the hunt
    def descend(self, house: int, report: CycleReport) -> list[Shard]:
        """Walk the difference down: states, then the constituencies that moved.

        Every state is probed, not a prioritised subset. The accounting is
        checked afterwards as a consistency assertion, with a tolerance, and a
        mismatch widens the search rather than narrowing it.
        """
        dirty: list[Shard] = []
        states = {}
        for shard in self.registry(house):
            states.setdefault(shard.state_id, []).append(shard)

        # Every state at once, four at a time. All 36 are probed rather than a
        # prioritised subset: measured at 7.5 s, which is not worth a learned
        # parameter that can be wrong.
        state_ids = sorted(states)
        probes = run_parallel(
            state_ids,
            lambda sid_: self._probe(state_shard_id(house, sid_),
                                     shard_combo(sid_, 0, house=house)))
        moved_states = []
        for state_id, probe in zip(state_ids, probes):
            sid = state_shard_id(house, state_id)
            if isinstance(probe, BaseException):
                self._failed(sid, probe, validate.RETRY)
                self.log(f"  state {state_id}: {probe}")
                continue
            moved, signature, count = probe
            report.requests += 1
            report.states_probed += 1
            if moved:
                moved_states.append(state_id)
            self._record_probe(sid, signature, count, validate.IDLE,
                               keep_payload=not moved)

        if not moved_states:
            return dirty

        for state_id in moved_states:
            members = states[state_id]
            # Rajya Sabha slices ARE states: no constituency level to descend to.
            if house == HOUSE_RS:
                dirty.extend(members)
                continue
            probed = run_parallel(
                members, lambda s: self._probe(s.shard_id, s.combo))
            for shard, probe in zip(members, probed):
                if isinstance(probe, BaseException):
                    self._failed(shard.shard_id, probe, validate.RETRY)
                    continue
                moved, signature, count = probe
                report.requests += 1
                report.constituencies_probed += 1
                if moved:
                    dirty.append(shard)
                    db.save_watermark(shard.shard_id, signature=signature,
                                      n_records=count, lifecycle=validate.DIRTY)
                else:
                    self._record_probe(shard.shard_id, signature, count,
                                       validate.IDLE, keep_payload=True)
        return dirty

    # ------------------------------------------------------------ the fetch
    def fetch_shard(self, shard: Shard, *, force: bool = False,
                    attempts: int = 2) -> dict:
        """Pull, gate, map and store one slice. Returns a small outcome dict.

        Never deletes. A slice that cannot be read or does not pass the gates
        keeps whatever records it already had and is marked stale instead.

        A gate failure is retried once before the shard is quarantined. The
        first national sweep showed why: 7 of 543 constituencies came back with
        a report that disagreed with the portal's own count, in both directions
        and by as much as 175 records — and every one of them was perfectly
        consistent again minutes later (24 consecutive re-fetches of two of
        them matched exactly). The portal is occasionally inconsistent under a
        long sustained sweep, so the right response to a mismatch is to ask
        again, and to quarantine only if it persists.
        """
        outcome = {"shard": shard.shard_id, "label": shard.label,
                   "stored": 0, "changed": 0, "versions": 0, "status": "",
                   "reason": ""}
        for attempt in range(1, max(1, attempts) + 1):
            outcome = self._attempt_fetch(shard, force=force,
                                          final=attempt >= attempts)
            if outcome["status"] != "RECHECK":
                return outcome
            self.log(f"  {shard.label}: {outcome['reason'][:90]} — re-reading")
            time.sleep(1.5)
        return outcome

    def _attempt_fetch(self, shard: Shard, *, force: bool,
                       final: bool) -> dict:
        """One pull-gate-store attempt. Returns status 'RECHECK' if worth a retry."""
        outcome = {"shard": shard.shard_id, "label": shard.label,
                   "stored": 0, "changed": 0, "versions": 0, "status": "",
                   "reason": ""}

        def refuse(reason: str, lifecycle: str) -> dict:
            """Quarantine now, or ask for one more read first."""
            if not final:
                return {**outcome, "status": "RECHECK", "reason": reason}
            failures = db.mark_shard_failure(shard.shard_id, reason,
                                             lifecycle=lifecycle)
            self._maybe_escalate(shard, failures, reason)
            return {**outcome, "status": lifecycle, "reason": reason}

        try:
            watermark = self.client.watermark(shard.combo)
            # The four tiles in parallel: exactly the measured concurrency
            # ceiling, so one shard costs about one request's worth of time
            # rather than four.
            fetched = run_parallel(
                list(RECORD_TILES),
                lambda tile: self.client.tile_report(shard.combo, tile))
            for result in fetched:
                if isinstance(result, BaseException):
                    raise result
            tiles = dict(zip(RECORD_TILES, fetched))
        except CircuitOpen as exc:
            # held back, not failed: the slice keeps its place in the queue and
            # its failure count, and is read once the portal answers again
            return {**outcome, "status": validate.RETRY, "reason": str(exc)}
        except SourceError as exc:
            return refuse(str(exc), validate.RETRY)

        digest = validate.payload_hash(tiles)
        stored_row = db.get_watermark(shard.shard_id) or {}
        previous_stored = stored_row.get("n_stored")
        portal_count = watermark.counts.get("Works Recommended")

        if (not force and digest and stored_row.get("payload_sha256") == digest
                and _same_signature(stored_row.get("signature_json"),
                                    watermark.signature())):
            # Nothing in the slice actually changed; do not rewrite it. Both
            # the records and the figures must match: identical records under
            # figures that moved are a report lagging its own count, or a
            # figure no report carries, and only the full path's gates can
            # tell those apart.
            db.save_watermark(shard.shard_id, signature=watermark.signature(),
                              n_records=portal_count, payload_sha256=digest,
                              lifecycle=validate.IDLE, fetched=True)
            outcome.update(status="unchanged")
            return outcome

        # `registered=True`: the poller only ever fetches shards the portal
        # itself enumerated, so an all-zero answer here means a genuinely empty
        # slice rather than a bad id.
        gate = validate.validate_shard(
            tiles, suspicious_zero=watermark.suspicious_zero,
            previous_stored=previous_stored, registered=True)
        if not gate:
            return refuse(gate.reason, validate.QUARANTINED)

        mapping = emap.map_shard(tiles)
        works = mapping.works
        # The portal's Works Recommended count is a count of report rows, so it
        # is compared with rows listed, not with distinct works: a work listed
        # twice is two rows there too.
        listed_rows = sum(v["in_recommended"] for v in mapping.listing.values())
        count_gate = validate.reconcile_count(portal_count, listed_rows)
        if not count_gate:
            return refuse(count_gate.reason, validate.QUARANTINED)

        portal = {tile: [watermark.counts.get(TILE_KEYS[tile]),
                         watermark.totals.get(TILE_KEYS[tile])] for tile in RECORD_TILES}
        # A read is consistent when every tile's count agrees with the report
        # rows. Only a consistent read may mark a work as gone from the portal.
        consistent = not validate.figure_differences(
            portal, emap.portal_figures(mapping.listing,
                                        {w.work_id: w.model_dump() for w in works}),
            counts_only=True)

        try:
            result = db.upsert_works(works, shard_id=shard.shard_id,
                                     movable=db.missing_ids([w.work_id for w in works]))
        except db.WorkIdCollision as exc:
            return refuse(str(exc), validate.QUARANTINED)
        db.replace_payments(shard.shard_id, [w.work_id for w in works], mapping.payments)
        removal = db.apply_listing(shard.shard_id, mapping.listing,
                                   consistent=consistent, confirm_after=RETIRE_CONFIRM)

        # Parity is checked on what was written, read back from the database —
        # not on the mapping — so it proves the stored corpus, not the code.
        stored = emap.portal_figures(*db.stored_listing(shard.shard_id))
        differences = validate.figure_differences(portal, stored)
        awaiting = removal["newly_missing"] or removal["still_missing"]
        recheck = None
        if differences:
            recheck = datetime.now(timezone.utc) + PARITY_RECHECK
        if awaiting:
            confirm = datetime.now(timezone.utc) + RETIRE_CONFIRM
            recheck = min(recheck, confirm) if recheck else confirm
        db.save_parity(shard.shard_id, exact=not differences, portal=portal,
                       stored=stored, differences=differences,
                       duplicates=mapping.duplicates,
                       recheck_after=recheck.isoformat() if recheck else None)
        if removal["retired"]:
            self.log(f"  {shard.label}: {len(removal['retired'])} work(s) no longer "
                     f"listed on the portal, retired: {', '.join(removal['retired'][:3])}")
        if differences:
            self.log(f"  {shard.label}: stored figures differ from the portal "
                     f"({'; '.join(d['tile'] + ' ' + d['measure'] for d in differences)}); "
                     f"re-reading in {PARITY_RECHECK.seconds // 60} min")

        data_changed = bool(result["inserted"] or result["changed"] or removal["retired"]
                            or removal["restored"] or removal["listing_changed"])
        if data_changed:
            # What the analysis reads has changed, so the flags are now behind.
            db.set_state("data_changed_at", _now())

        for tile, rows in tiles.items():
            shard_cache.write(shard.shard_id, tile, rows)
        db.save_watermark(
            shard.shard_id, signature=watermark.signature(),
            n_records=portal_count, payload_sha256=digest,
            lifecycle=validate.STORED, n_stored=len(works),
            count_matched=bool(count_gate.detail.get("matched")), fetched=True)
        # Appended, never replaced: this is the running log of what the portal
        # sent, and replacing it on every store left only the last slice.
        fields = result["versions"]
        parts = []
        if result["inserted"] or result["changed"]:
            parts.append(f"{result['inserted']} new, {result['changed']} updated "
                         f"({fields} field change{'' if fields == 1 else 's'})")
        if removal["retired"]:
            parts.append(f"{len(removal['retired'])} removed from the portal")
        if removal["restored"]:
            parts.append(f"{len(removal['restored'])} listed again")
        if removal["newly_missing"]:
            parts.append(f"{len(removal['newly_missing'])} no longer listed, confirming")
        # Only reads that found something are logged. The nightly sweep and the
        # rotation mostly re-read areas where nothing changed; logging those too
        # would push the real updates out of the capped log the site shows.
        if parts:
            db.append_provenance([{
                "source": f"eSAKSHI API {shard.label}", "mode": "api",
                "table": "works", "rows": len(works), "status": "ok",
                "detail": f"{', '.join(parts)}; portal count {portal_count}; "
                          f"shard {shard.shard_id}",
                "fetched_at": _now()}])
        outcome.update(status=validate.STORED, stored=len(works),
                       data_changed=data_changed,
                       changed=result["changed"], versions=result["versions"],
                       exact=not differences, retired=len(removal["retired"]),
                       awaiting_removal=len(awaiting), restored=len(removal["restored"]))
        return outcome

    # ------------------------------------------------------------ escalation
    def _maybe_escalate(self, shard: Shard, failures: int, reason: str) -> None:
        """A slice missing for days must degrade freshness, not hide inside it."""
        if failures < ESCALATE_AFTER:
            return
        stale = db.stale_shards(min_failures=ESCALATE_AFTER)
        payload = {"generated_at": _now(), "escalate_after": ESCALATE_AFTER,
                   "stale": stale}
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        ALERTS_PATH.write_text(json.dumps(payload, indent=2, default=str),
                               encoding="utf-8")
        self.log(f"  ESCALATED {shard.label} after {failures} consecutive "
                 f"failures: {reason[:100]}")

    # ----------------------------------------------------------- the queue
    def pending_shards(self) -> list[Shard]:
        """Slices still owing a fetch, read back from the database.

        The queue is durable by construction: it is a lifecycle column, so a
        crash or restart resumes from exactly where it stopped.
        """
        by_id = {s.shard_id: s for s in self.registry()}
        out = []
        with db.connect() as con:
            marks = ", ".join("?" * len(PENDING))
            rows = con.execute(
                f"SELECT shard_id FROM shard_watermarks "
                f"WHERE lifecycle IN ({marks})", PENDING)
            for row in rows:
                shard = by_id.get(row["shard_id"])
                if shard is not None:
                    out.append(shard)
        return out

    def drain(self, shards: list[Shard], report: CycleReport) -> None:
        for shard in shards:
            outcome = self.fetch_shard(shard)
            if outcome["status"] == validate.STORED:
                report.stored += outcome["stored"]
                report.changed += outcome["changed"]
                report.versions += outcome["versions"]
                self.log(f"  stored {shard.label}: {outcome['stored']} records, "
                         f"{outcome['changed']} changed, "
                         f"{outcome['versions']} versions")
            elif outcome["status"] in (validate.QUARANTINED, validate.RETRY):
                report.quarantined = report.quarantined + (shard.shard_id,)
                self.log(f"  {outcome['status']} {shard.label}: "
                         f"{outcome['reason'][:120]}")

    # ------------------------------------------------------------ rotation
    def rolling_reread(self, report: CycleReport | None = None,
                       now: datetime | None = None) -> list[str]:
        """Re-read the areas read longest ago, a few a minute, in working hours.

        The heartbeat sees only edits that move a figure. This is what brings
        in the rest — descriptions, stages, agencies, dates — within hours
        rather than at the nightly sweep. An ordinary read in every way: the
        same gates, parity check and history, and an unchanged area writes
        nothing but the time it was read. Returns the slices it read.
        """
        report = report if report is not None else CycleReport(started_at=_now())
        if ROLLING_AREAS <= 0:
            return []
        local = now if now is not None else datetime.now().astimezone()
        if not in_hours(local, parse_hours(ROLLING_HOURS)):
            return []
        breaker = getattr(self.client, "breaker", None)
        if breaker is not None and breaker.is_open:
            return []
        if local.tzinfo is None:
            local = local.astimezone()
        read_before = (local.astimezone(timezone.utc) - ROLLING_MIN_AGE).isoformat()
        due = db.oldest_read_shards(self.houses, read_before=read_before,
                                    limit=ROLLING_AREAS, skip_lifecycles=PENDING)
        if not due:
            return []
        by_id = {s.shard_id: s for s in self.registry()}
        done = []
        for shard_id in due:
            shard = by_id.get(shard_id)
            if shard is None or self._stop:
                continue
            outcome = self.fetch_shard(shard)
            done.append(shard_id)
            db.set_state("last_rolling_at", _now())
            if outcome["status"] == validate.STORED:
                report.stored += outcome["stored"]
                report.changed += outcome["changed"]
                report.versions += outcome["versions"]
                if outcome.get("data_changed"):
                    self.log(f"  rotation found changes in {shard.label}: "
                             f"{outcome['changed']} updated, "
                             f"{outcome['versions']} field changes")
            elif outcome["status"] in (validate.QUARANTINED, validate.RETRY):
                report.quarantined = report.quarantined + (shard_id,)
                self.log(f"  rotation: {outcome['status']} {shard.label}: "
                         f"{outcome['reason'][:120]}")
        report.rolled = report.rolled + tuple(done)
        return done

    # ------------------------------------------------------------ photos
    @staticmethod
    def photo_night(local: datetime, hours: tuple[int, int]) -> str:
        """The night a moment belongs to, named by the date its hours opened:
        with 21:00-07:00, both 23:00 on the 17th and 03:00 on the 18th are the
        night of the 17th."""
        return (local - timedelta(minutes=hours[0])).date().isoformat()

    def _portal_ids(self, now: datetime) -> dict[str, str]:
        from . import photos
        if (self._photo_ids is None or self._photo_ids_at is None
                or now - self._photo_ids_at >= PHOTO_IDS_EVERY):
            self._photo_ids = photos.portal_work_ids()
            self._photo_ids_at = now
        return self._photo_ids

    def photo_results_ready(self) -> bool:
        """Once the night's photo checks stop, those recorded since the flags
        last included them count as a change the analysis reads. One re-run
        then picks up the whole night, rather than one run per few minutes of
        checking. Returns whether it marked them."""
        if not db.get_state("photo_results_pending"):
            return False
        db.set_state("data_changed_at", _now())
        db.set_state("photo_results_pending", "")
        self.log("photo checks recorded; the risk flags include them at the next analysis")
        return True

    def photo_checks(self, report: CycleReport | None = None,
                     now: datetime | None = None) -> Counter:
        """Check the portal's photos of held duplicate matches, a few a minute, at night.

        Inside `PHOTO_HOURS` only, at most `PHOTOS_PER_CHECK` works a minute and
        `PHOTOS_PER_NIGHT` a night that ask the portal (works not yet completed
        are recorded without a request, `PHOTOS_UNASKED_PER_CHECK` a minute).
        It stands down in a minute busy with a change, while the poller's
        breaker is open, and until `RETRY_GAP` after the attachment service
        stopped answering. Returns what the checks found.
        """
        from . import photos

        tally: Counter = Counter()
        hours = parse_hours(PHOTO_HOURS)
        if PHOTOS_PER_NIGHT <= 0 or hours is None:
            return tally
        local = now if now is not None else datetime.now().astimezone()
        if local.tzinfo is None:
            local = local.astimezone()
        utc_now = local.astimezone(timezone.utc)
        if not in_hours(local, hours):
            self.photo_results_ready()
            return tally
        if report is not None and (report.houses_moved or report.dirty or report.quarantined):
            return tally
        breaker = getattr(self.client, "breaker", None)
        if breaker is not None and breaker.is_open:
            return tally
        paused = _parse_ts(db.get_state("photo_paused_until"))
        if paused is not None and utc_now < paused:
            return tally

        night = self.photo_night(local, hours)
        try:
            tonight = json.loads(db.get_state("photo_night") or "{}")
        except ValueError:
            tonight = {}
        asked_before = int(tonight.get("asked") or 0) if tonight.get("night") == night else 0
        room = min(PHOTOS_PER_CHECK, PHOTOS_PER_NIGHT - asked_before)
        due = db.due_photo_checks(recheck_before=photos.recheck_before(utc_now)) if room > 0 else []
        if not due:
            self.photo_results_ready()          # the night's work is done
            return tally

        ids = self._portal_ids(utc_now)
        batch, asking, unasked = [], 0, 0
        for work_id in due:
            if work_id in ids:
                if asking >= room:
                    continue
                asking += 1
            else:
                if unasked >= PHOTOS_UNASKED_PER_CHECK:
                    continue
                unasked += 1
            batch.append(work_id)
            if asking >= room and unasked >= PHOTOS_UNASKED_PER_CHECK:
                break

        if self.photo_client is None:
            self.photo_client = photos.PhotoClient()
        tally = photos.check_works(batch, client=self.photo_client, ids=ids, log=self.log,
                                   progress_every=0, should_stop=lambda: self._stop)
        asked = sum(tally[s] for s in photos.ASKED)
        recorded = asked + tally["not_completed"]
        db.set_state("photo_night", json.dumps({"night": night, "asked": asked_before + asked}))
        if recorded:
            db.set_state("last_photo_check_at", _now())
            if not db.get_state("photo_results_pending"):
                db.set_state("photo_results_pending", _now())
            found = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(tally.items())
                              if k not in ("stopped_early", "photos_fingerprinted"))
            self.log(f"photo check: {recorded} held work(s) ({found}); "
                     f"{asked_before + asked} of {PHOTOS_PER_NIGHT} tonight")
        if tally["stopped_early"]:
            db.set_state("photo_paused_until", (utc_now + RETRY_GAP).isoformat())
            self.log(f"photo checks paused for {RETRY_GAP.total_seconds() / 60:.0f} min")
        elif asked and db.get_state("photo_paused_until"):
            db.set_state("photo_paused_until", "")      # answering again
        return tally

    # ------------------------------------------------------------ one cycle
    def heartbeat(self) -> CycleReport:
        """One minute's work: ask nationally, descend only if something moved."""
        report = CycleReport(started_at=_now())
        clock = time.monotonic()

        # Anything that makes this minute busy keeps the rotation out of it.
        pending = self.pending_shards()
        busy = bool(pending)
        if pending:
            self.log(f"resuming {len(pending)} shard(s) left from earlier")
            self.drain(pending, report)

        # Slices owed a second read: a parity difference, or a work that
        # stopped being listed and must be confirmed gone before it is retired.
        due = db.due_rechecks(_now(), MAX_RECHECKS)
        busy = busy or bool(due)
        if due:
            by_id = {s.shard_id: s for s in self.registry()}
            recheck = [by_id[sid] for sid in due if sid in by_id][:RECHECKS_PER_CYCLE]
            self.log(f"re-reading {len(recheck)} slice(s) to confirm against the portal: "
                     + ", ".join(s.label for s in recheck[:6]))
            for shard in recheck:
                outcome = self.fetch_shard(shard, force=True)
                if outcome["status"] == validate.STORED:
                    report.stored += outcome["stored"]
                    report.changed += outcome["changed"]
                    report.versions += outcome["versions"]

        breaker = getattr(self.client, "breaker", None)
        if breaker is not None and breaker.is_open and hasattr(breaker, "allow_trial"):
            if breaker.allow_trial():
                self.log("portal paused; trying one national check "
                         f"(last error: {getattr(breaker, 'last_error', None)})")
        for house in self.houses:
            nid = national_shard_id(house)
            try:
                moved, signature, count = self._probe(
                    nid, shard_combo(house=house))
            except CircuitOpen as exc:
                report.note = f"portal paused: {exc}"
                busy = True
                continue
            except SourceError as exc:
                db.mark_shard_failure(nid, str(exc), lifecycle=validate.RETRY)
                report.note = f"national probe failed: {exc}"
                self.log(f"national (house {house}): {exc}")
                busy = True
                continue
            report.requests += 1
            if not moved:
                self._record_probe(nid, signature, count, validate.IDLE)
                continue
            busy = True
            self.log(f"national (house {house}) moved -> descending "
                     f"(recommended={count:,})" if count else
                     f"national (house {house}) moved -> descending")
            report.houses_moved = report.houses_moved + (house,)
            dirty = self.descend(house, report)
            report.dirty = report.dirty + tuple(s.shard_id for s in dirty)
            if dirty:
                self.log(f"  {len(dirty)} shard(s) changed: "
                         + ", ".join(s.label for s in dirty[:6])
                         + (" ..." if len(dirty) > 6 else ""))
                self.drain(dirty, report)
            # Record the national watermark only after the descent, so a crash
            # midway leaves it "moved" and the next cycle tries again.
            self._record_probe(nid, signature, count, validate.IDLE)

        if not busy:
            self.rolling_reread(report)

        report.seconds = round(time.monotonic() - clock, 2)
        # In the database rather than only the status file, so the website can
        # say when the portal was last checked without racing a half-written file.
        db.set_state("last_heartbeat_at", _now())
        self._save_portal_status()
        self._write_status(report)
        return report

    def _save_portal_status(self) -> None:
        """Whether the portal is answering, when the breaker tries next, and the
        last real error — for the website, which cannot see this process."""
        status = getattr(getattr(self.client, "breaker", None), "status", None)
        if callable(status):
            db.set_state("portal_status", json.dumps(status(), default=str))

    # ------------------------------------------------------- full sweep
    def reconcile_all(self, house: int | None = None) -> dict:
        """Re-read every slice's RECORDS, whatever the counts say.

        This is the safety net under the fast path, for two reasons: it catches
        edits that move no count, and it is the only thing that would notice a
        counts endpoint gone stale or wrong. It must stay record-level — a
        counts-only sweep would be blind in exactly the same way as the
        heartbeat it is meant to back up.
        """
        # A sweep takes about twenty minutes with no heartbeat in between. The
        # marker lets the website say "full check running" instead of showing
        # a last-check time that looks like the poller has stalled.
        db.set_state("sweep_started_at", _now())
        try:
            return self._reconcile_all(house)
        finally:
            db.set_state("sweep_started_at", "")

    def _reconcile_all(self, house: int | None) -> dict:
        started = time.monotonic()
        shards = self.registry(house)
        summary = {"started_at": _now(), "shards": len(shards), "stored": 0,
                   "unchanged": 0, "changed": 0, "versions": 0,
                   "quarantined": [], "count_mismatch": [], "not_exact": [],
                   "retired": 0, "awaiting_removal": 0}
        self.log(f"full reconciliation over {len(shards)} shards")
        full = house is None
        if full:
            db.set_state("last_reconcile_attempt_at", _now())
        ok_shards = 0
        summary["upper_levels_recorded"] = self._snapshot_upper_levels(
            (house,) if house else self.houses)
        for index, shard in enumerate(shards, 1):
            outcome = self.fetch_shard(shard, force=True)
            if outcome["status"] == validate.STORED:
                summary["stored"] += outcome["stored"]
                summary["changed"] += outcome["changed"]
                summary["versions"] += outcome["versions"]
                summary["retired"] += outcome.get("retired", 0)
                summary["awaiting_removal"] += outcome.get("awaiting_removal", 0)
                if not outcome.get("exact", True):
                    summary["not_exact"].append(shard.shard_id)
                row = db.get_watermark(shard.shard_id) or {}
                if row.get("count_matched") == 0:
                    summary["count_mismatch"].append(shard.shard_id)
            elif outcome["status"] == "unchanged":
                summary["unchanged"] += 1
            else:
                summary["quarantined"].append(shard.shard_id)
            if outcome["status"] in (validate.STORED, "unchanged"):
                ok_shards += 1
            if self.verbose and index % 50 == 0:
                self.log(f"  {index}/{len(shards)} "
                         f"({time.monotonic() - started:.0f}s)")
            if self._stop:
                summary["stopped_early_at"] = index
                break
        summary["seconds"] = round(time.monotonic() - started, 1)
        summary["finished_at"] = _now()
        summary["ok_shards"] = ok_shards
        # Only a whole, healthy sweep counts. A partial one (--house), one
        # stopped early, or one run against an unreachable portal must stay
        # due, or the safety net would be skipped for a day while looking done.
        healthy = (full and "stopped_early_at" not in summary and bool(shards)
                   and ok_shards / len(shards) >= HEALTHY_SWEEP)
        summary["recorded_as_complete"] = healthy
        if healthy:
            db.set_state("last_reconcile_at", summary["finished_at"])
        self.log(f"reconciliation done in {summary['seconds']}s: "
                 f"{summary['stored']:,} records, "
                 f"{summary['changed']} changed, "
                 f"{len(summary['quarantined'])} quarantined, "
                 f"{len(summary['count_mismatch'])} count mismatches, "
                 f"{len(summary['not_exact'])} slices not in exact parity, "
                 f"{summary['retired']} retired"
                 + ("" if healthy else " — NOT recorded as complete; still due"))
        return summary

    def _snapshot_upper_levels(self, houses: tuple[int, ...]) -> int:
        """Record the national and state watermarks a sweep does not fetch.

        A sweep stores a watermark for every shard it reads, but the heartbeat
        compares at two levels above that too — the national figure and, for
        the Lok Sabha, each state. Leaving those unrecorded made the first
        heartbeat after the first national sweep treat them as never seen and
        descend into all 543 constituencies: 617 requests where one was due,
        and it would have happened again after every nightly sweep.

        Taken at the START of the sweep, deliberately. If they were recorded at
        the end, a change landing on a shard the sweep had already read would
        be absorbed into the national figure, the heartbeat would see nothing
        moved, and the change would go unnoticed until the next night. Taken at
        the start, the same change leaves the national figure different from
        what we stored, so the next heartbeat descends and finds it. The worst
        case is one unnecessary descent right after a busy sweep.
        """
        recorded = 0
        for house in houses:
            try:
                _moved, signature, count = self._probe(
                    national_shard_id(house), shard_combo(house=house))
                self._record_probe(national_shard_id(house), signature, count,
                                   validate.IDLE)
                recorded += 1
            except SourceError as exc:
                self.log(f"  national snapshot (house {house}) failed: {exc}")
            if house == HOUSE_RS:
                continue           # Rajya Sabha shards ARE the states: the sweep
                                   # stores those watermarks itself
            state_ids = sorted({s.state_id for s in self.registry(house)})
            probes = run_parallel(
                state_ids,
                lambda sid_, h=house: self._probe(state_shard_id(h, sid_),
                                                  shard_combo(sid_, 0, house=h)))
            for state_id, probe in zip(state_ids, probes):
                if isinstance(probe, BaseException):
                    continue
                _moved, signature, count = probe
                self._record_probe(state_shard_id(house, state_id), signature,
                                   count, validate.IDLE)
                recorded += 1
        return recorded

    # ------------------------------------------------------------- schedule
    def reconcile_due(self, now: datetime | None = None,
                      at: str | None = RECONCILE_AT) -> bool:
        """Is the nightly sweep owed? Decided from the database, not memory.

        This replaces an exact-minute check that silently skipped the whole
        safety net whenever the machine slept through 03:00 or a heartbeat
        happened to run long across it.
        """
        now = now or datetime.now().astimezone()
        slot = most_recent_slot(now, at)
        if slot is None:
            return False
        last_ok = _parse_ts(db.get_state("last_reconcile_at"))
        if last_ok is not None and last_ok >= slot:
            return False
        last_try = _parse_ts(db.get_state("last_reconcile_attempt_at"))
        if last_try is not None and now - last_try < RETRY_GAP:
            return False
        return True

    def analysis_due(self, now: datetime | None = None, *,
                     after_sweep: bool = False) -> bool:
        """Are the risk flags behind the stored data, and is a re-run allowed?

        Only a change the analysis reads counts: new, edited, removed or
        restored works, a change in which reports list them, or a night's photo
        checks of held matches (`photo_results_ready`). A re-read that found
        nothing never triggers a run. Decided from the database, so a restart
        does not forget a change the flags have not caught up with.
        """
        if ANALYSIS_EVERY <= timedelta(0):
            return False
        changed = _parse_ts(db.get_state("data_changed_at"))
        if changed is None:
            return False
        covered = _parse_ts(db.get_state("analysis_covers_changes_at"))
        if covered is not None and covered >= changed:
            return False
        now = now or datetime.now(timezone.utc)
        last_ok = _parse_ts(db.get_state("last_analysis_at"))
        last_try = _parse_ts(db.get_state("last_analysis_attempt_at"))
        if (last_try is not None and (last_ok is None or last_try > last_ok)
                and now - last_try < RETRY_GAP):
            return False
        if after_sweep or last_ok is None:
            return True
        return now - last_ok >= ANALYSIS_EVERY

    def note_unrecorded_analysis(self) -> bool:
        """Mark flags from before analysis runs were recorded as behind.

        A database analysed by an earlier version, or by the switch-over
        before any change was recorded, has flags but no record of which
        stored changes they include. Treating them as behind costs one
        re-run and guarantees they cover everything stored. Returns whether
        it did so.
        """
        if db.get_state("last_analysis_at") or db.get_state("data_changed_at"):
            return False
        with db.connect() as con:
            if con.execute("SELECT 1 FROM flags LIMIT 1").fetchone() is None:
                return False
        db.set_state("data_changed_at", _now())
        self.log("risk flags predate recorded analysis runs; recomputing them once")
        return True

    def refresh_analysis(self, reason: str = "") -> dict | None:
        """Re-run the analysis over the corpus as it now stands.

        Rebuilds the fund positions derived from the works, recomputes every
        flag and keeps the review decisions people recorded. Returns the
        pipeline summary, or None if it failed; a failure leaves the previous
        flags in place and is retried after `RETRY_GAP`.
        """
        # Deferred: the analysis stack is heavy and no other mode needs it.
        from ..pipeline import run_pipeline

        db.set_state("last_analysis_attempt_at", _now())
        # Lets the website say the flags are being recomputed, in the same way
        # as the sweep marker.
        db.set_state("analysis_started_at", _now())
        self.log("recomputing risk flags" + (f" ({reason})" if reason else ""))
        try:
            summary = run_pipeline(verbose=False)
        except Exception as exc:
            self.log(f"analysis error: {type(exc).__name__}: {exc}")
            return None
        finally:
            db.set_state("analysis_started_at", "")
        self.log(f"risk flags recomputed in {summary['seconds']}s: "
                 f"{summary['flags']:,} flags, {summary['alerts']:,} alerts")
        return summary

    def registry_due(self, now: datetime | None = None) -> bool:
        """Wall-clock and persisted, so sleep and restarts cannot postpone it."""
        now = now or datetime.now(timezone.utc)
        last_ok = _parse_ts(db.get_state("last_registry_at"))
        if last_ok is not None and now - last_ok < REGISTRY_EVERY:
            return False
        last_try = _parse_ts(db.get_state("last_registry_attempt_at"))
        if last_try is not None and now - last_try < RETRY_GAP:
            return False
        return True

    # ------------------------------------------------------------ the loop
    def _write_status(self, report: CycleReport) -> None:
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(
            {"last_cycle": report.as_dict(),
             "poll_interval_seconds": POLL_INTERVAL,
             "reconcile_at": RECONCILE_AT,
             "rolling": {"areas_per_check": ROLLING_AREAS, "hours": ROLLING_HOURS,
                         "min_age_hours": ROLLING_MIN_AGE.total_seconds() / 3600},
             "analysis_every_minutes": ANALYSIS_EVERY.total_seconds() / 60,
             "photos": {"hours": PHOTO_HOURS, "per_night": PHOTOS_PER_NIGHT,
                        "per_check": PHOTOS_PER_CHECK},
             "last_reconcile_at": db.get_state("last_reconcile_at"),
             "last_registry_at": db.get_state("last_registry_at"),
             "houses": list(self.houses),
             "portal": json.loads(db.get_state("portal_status") or "null"),
             "watermarks": db.watermark_summary(),
             "cache": shard_cache.usage()}, indent=2, default=str),
            encoding="utf-8")

    def run_forever(self) -> None:
        if not ENABLED:
            self.log("ASTRA_POLLER_ENABLED=0 — not polling; serving the cache")
            return
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        if not db.load_shards():
            self.refresh_registry()
        self.note_unrecorded_analysis()
        self.log(f"watching every {POLL_INTERVAL:.0f}s; nightly sweep at "
                 f"{RECONCILE_AT}; houses {self.houses}; rotation "
                 f"{ROLLING_AREAS} area(s) a check, {ROLLING_HOURS}; analysis "
                 f"at most every {ANALYSIS_EVERY.total_seconds() / 60:.0f} min; photo checks "
                 f"{PHOTOS_PER_NIGHT} held works a night, {PHOTOS_PER_CHECK} a check, {PHOTO_HOURS}")
        while not self._stop:
            cycle_start = time.monotonic()
            report = None
            try:
                report = self.heartbeat()
                if report.houses_moved or report.dirty:
                    self.log(f"cycle: {report.requests} requests, "
                             f"{len(report.dirty)} shards fetched, "
                             f"{report.stored} records, {report.seconds}s")
            except Exception as exc:                 # a cycle must never kill it
                self.log(f"cycle error: {type(exc).__name__}: {exc}")

            if self.registry_due():
                db.set_state("last_registry_attempt_at", _now())
                try:
                    self.refresh_registry()
                except SourceError as exc:
                    self.log(f"registry refresh failed: {exc}")

            swept = sweep_ran = False
            if self.reconcile_due():
                sweep_ran = True
                last = db.get_state("last_reconcile_at")
                self.log("nightly sweep is due "
                         + (f"(last completed {last})" if last else "(never completed)"))
                try:
                    swept = bool(self.reconcile_all().get("recorded_as_complete"))
                except Exception as exc:
                    self.log(f"reconciliation error: {type(exc).__name__}: {exc}")

            # A sweep takes the minute and more; the photos wait for the next one.
            if not self._stop and not sweep_ran and report is not None:
                try:
                    self.photo_checks(report)
                except Exception as exc:
                    self.log(f"photo check error: {type(exc).__name__}: {exc}")

            if not self._stop and self.analysis_due(after_sweep=swept):
                self.refresh_analysis("after the nightly sweep" if swept
                                      else "portal data changed")

            # jittered, so restarts do not synchronise onto the same second
            elapsed = time.monotonic() - cycle_start
            nap = max(1.0, POLL_INTERVAL - elapsed) * (0.9 + random.random() / 5)
            deadline = time.monotonic() + nap
            while time.monotonic() < deadline and not self._stop:
                time.sleep(0.5)
        self.client.close()
        self.log("stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ASTRA eSAKSHI poller")
    parser.add_argument("--once", action="store_true",
                        help="run a single heartbeat cycle and exit")
    parser.add_argument("--reconcile", action="store_true",
                        help="run one full record-level sweep and exit")
    parser.add_argument("--registry", action="store_true",
                        help="re-enumerate shards from the portal and exit")
    parser.add_argument("--analyse", action="store_true",
                        help="re-run the analysis over the stored data and exit")
    parser.add_argument("--house", type=int, choices=(HOUSE_LS, HOUSE_RS),
                        help="restrict to one house")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    # Every mode writes the corpus, so every mode takes the lock — a one-off
    # --once beside a running poller would double-log the same change too.
    lock = instance_lock.WriterLock()
    if not lock.acquire(role="poller"):
        note = instance_lock.holder(lock.path) or {}
        print(f"[poller] another poller is already running on {db.DB_PATH.name} "
              f"(pid {note.get('pid', '?')}, {note.get('role', 'poller')}, since "
              f"{note.get('since', '?')}). It is keeping the data current; "
              f"not starting a second one.", file=sys.stderr, flush=True)
        return instance_lock.ALREADY_RUNNING
    try:
        # A sweep killed mid-run (closed window, power cut) cannot clear its own
        # marker. Holding the lock proves no sweep is running now.
        db.set_state("sweep_started_at", "")
        db.set_state("analysis_started_at", "")
        houses = (args.house,) if args.house else HOUSES
        poller = Poller(houses=houses, verbose=not args.quiet)
        if backfill.pending():
            # Before any read: a slice read first would log each field it
            # fills in as an edit, and an unchanged slice is never rewritten.
            try:
                backfill.run_pending(log=poller.log)
            except Exception as exc:              # never blocks polling
                poller.log(f"completing stored works from the cache failed: "
                           f"{type(exc).__name__}: {exc}")

        if args.analyse:
            summary = poller.refresh_analysis("requested")
            if summary:
                print(json.dumps({k: v for k, v in summary.items() if k != "router_trace"},
                                 indent=2, default=str))
            return 0 if summary else 1

        if args.registry:
            poller.refresh_registry()
            return 0
        if not db.load_shards():
            poller.refresh_registry()
        if args.reconcile:
            summary = poller.reconcile_all(args.house)
            print(json.dumps(summary, indent=2, default=str))
            return 0
        if args.once:
            report = poller.heartbeat()
            print(json.dumps(report.as_dict(), indent=2, default=str))
            return 0
        poller.run_forever()
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
