"""Photo evidence for held duplicate matches: fingerprints of the portal's work photos.

    python -m astra.ingestion.photos --status
    python -m astra.ingestion.photos --limit 100          # check held works not yet checked
    python -m astra.ingestion.photos --works WS/MP.../1 WS/MP.../2

The running poller does this itself at night (`Poller.photo_checks`, see
`ASTRA_PHOTO_HOURS`); the commands are for a database no poller is writing.

Why it exists
-------------
A duplicate match that nothing in the record separates is held outside the risk
score (`astra.agents.entity_resolution`). The portal keeps photos uploaded
against a completed work, and one photo file recorded for two works claimed as
separate assets is evidence the record cannot give. Photos that only look alike
are shown for a person to compare, and different photos suggest two sites
without proving it: a second photo can always be taken.

What it does, and does not
--------------------------
* Only works the last analysis held (`duplicate_groups`) are looked at, pairs
  before batches.
* The portal finds a work's photos by its internal work number, which only the
  completed-works report carries. It is read from the raw response cache, with
  no request; a work not yet completed is recorded as such and looked at again
  later.
* One request lists a work's attachments, and one fetches each photo, at most
  `MAX_PHOTOS_PER_WORK`, one at a time with a pause between requests, through a
  circuit breaker that stops the run if the portal stops answering.
* Only a fingerprint is kept, never the image: a 64-bit difference hash across
  rows and another down columns (a re-saved or resized copy stays within a few
  bits; two different photos differ in about half), and the file's SHA-256.
* Documents (PDFs) are recorded by name and never fetched or compared. In the
  17 Sep 2026 sample most completed works had a PDF and no photo (47 of 94), and
  three held pairs and one batch of 15 carried byte-identical PDFs; the one read
  was a collector's fund-withdrawal order listing four water-tanker works in
  different villages, uploaded against each of them. One paper legitimately
  covers several works, so a shared document is not evidence; a site photo is.
* The photo files carry no GPS or camera metadata (checked on 16 Sep 2026).
  Some show a GPS camera stamp drawn into the picture itself; reading it would
  need text recognition, which is not built, so there is no location check.
* One writer per database: the commands refuse while a poller holds the
  database, like `router.ingest()`; the poller, being that writer, runs the
  same check in its quiet hours instead.

Statuses: `photos` (at least one photo fingerprinted), `documents` (no photo,
only PDFs), `no_photo` (completed, nothing attached),
`not_completed` (no internal work number yet), `failed` (the portal did not
answer for this work).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .. import db
from ..config import SHARD_CACHE_DIR
from ..photo_hash import fingerprint
from . import instance_lock
from .esakshi_api import CircuitBreaker, CircuitOpen, EsakshiClient, SourceError
from .esakshi_map import work_code
from .validate import ungzip_json

#: Where the attachment list is served (the dashboard's own base).
LIST_BASE = "https://mplads.mospi.gov.in/rest/PreLoginDashboardData"
#: Where one attachment is served.
ATTACHMENT_BASE = "https://mplads.mospi.gov.in/rest/PreLoginCitizenWorkRcmdRest"
#: The attachment kind the portal's work page shows as photos.
PHOTO_FLAG = 3
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
#: Completion certificates, orders and similar papers: recorded by name only.
DOCUMENT_SUFFIXES = (".pdf",)
MAX_PHOTOS_PER_WORK = int(os.environ.get("ASTRA_PHOTOS_PER_WORK", "3"))
#: Seconds between two requests: this runs beside a portal other people use.
PAUSE_SECONDS = float(os.environ.get("ASTRA_PHOTO_PAUSE_SECONDS", "1.0"))
#: A work found without a photo, or not completed, is looked at again after this.
RECHECK_AFTER = timedelta(days=float(os.environ.get("ASTRA_PHOTO_RECHECK_DAYS", "14")))


# --------------------------------------------------------------- portal ids
def portal_work_ids(root: Path | None = None) -> dict[str, str]:
    """work code -> the portal's internal work number, from every cached
    completed-works report. No request is made."""
    base = Path(root or SHARD_CACHE_DIR)
    out: dict[str, str] = {}
    for path in base.rglob("completed.json.gz"):
        try:
            rows = ungzip_json(path.read_bytes())
        except (OSError, ValueError):
            continue
        if isinstance(rows, dict):                       # {"tile": "<json string>"} shape
            rows = next(iter(rows.values()), [])
            rows = json.loads(rows) if isinstance(rows, str) else rows
        for row in rows if isinstance(rows, list) else []:
            code = work_code(row.get("ACTIVITY_NAME"))
            internal = row.get("WORK_ID")
            if code and internal is not None and str(internal).strip().isdigit():
                out[code] = str(int(internal))
    return out


# --------------------------------------------------------------- checking
class PhotoClient:
    """The two portal calls a photo check needs, sharing one circuit breaker."""

    def __init__(self, breaker: CircuitBreaker | None = None):
        breaker = breaker or CircuitBreaker()
        self.lists = EsakshiClient(base=LIST_BASE, breaker=breaker)
        self.files = EsakshiClient(base=ATTACHMENT_BASE, breaker=breaker)

    def attachments(self, portal_work_id: str) -> list[tuple[str, str]]:
        """[(attach id, file name)] recorded against a work."""
        data = self.lists._post("/getAttachIdsbyFlag",
                                {"json": {"FLAG": PHOTO_FLAG, "WORK_ID": int(portal_work_id)}})
        if not isinstance(data, list) or not data:
            return []
        ids = data[0].get("ATTACH_ID") or []
        names = data[0].get("FILE_NAME") or []
        if isinstance(ids, str):
            ids, names = [ids], [names]
        return [(str(i), str(n)) for i, n in zip(ids, names)]

    def attachment(self, attach_id: str) -> bytes:
        data = self.files._post("/getAttachmentById", {"id": attach_id})
        if not isinstance(data, list) or not data or not data[0].get("URL"):
            raise SourceError("attachment came back empty", path="/getAttachmentById")
        return base64.b64decode(data[0]["URL"])


def check_works(work_ids: list[str], *, client: PhotoClient | None = None,
                ids: dict[str, str] | None = None, pause: float = PAUSE_SECONDS,
                log: Callable[[str], None] = print, progress_every: int = 25,
                should_stop: Callable[[], bool] | None = None) -> Counter:
    """Fingerprint the photos of each work and record the check. Stops early,
    keeping what it recorded, if the portal stops answering or `should_stop`
    says so; each work's check is saved on its own, so nothing is half-written."""
    client = client or PhotoClient()
    ids = portal_work_ids() if ids is None else ids
    tally: Counter = Counter()

    def rest():
        if pause:
            time.sleep(pause)

    for n, wid in enumerate(work_ids, 1):
        if should_stop is not None and should_stop():
            break
        internal = ids.get(wid)
        if internal is None:
            db.save_photo_check(wid, portal_work_id=None, status="not_completed")
            tally["not_completed"] += 1
            continue
        try:
            listed = client.attachments(internal)
            rest()
            images = [(a, name) for a, name in listed if name.lower().endswith(IMAGE_SUFFIXES)]
            documents = [(a, name) for a, name in listed if name.lower().endswith(DOCUMENT_SUFFIXES)]
            found, unreadable = [], 0
            for attach_id, name in images[:MAX_PHOTOS_PER_WORK]:
                raw = client.attachment(attach_id)
                rest()
                try:
                    found.append({"attach_id": attach_id, "file_name": name, "kind": "photo",
                                  "sha256": hashlib.sha256(raw).hexdigest(), **fingerprint(raw)})
                except Exception:                              # noqa: BLE001 - not an image after all
                    unreadable += 1
            # Documents are listed by name only, never fetched or compared: see above.
            found += [{"attach_id": attach_id, "file_name": name, "kind": "document"}
                      for attach_id, name in documents]
            kinds = {a["kind"] for a in found}
            status = "photos" if "photo" in kinds else "documents" if kinds else "no_photo"
            db.save_photo_check(wid, portal_work_id=internal, status=status,
                                attachments=len(listed), found=found,
                                error=f"{unreadable} unreadable image(s)" if unreadable else None)
            tally[status] += 1
            tally["photos_fingerprinted"] += sum(1 for a in found if a["kind"] == "photo")
        except CircuitOpen as exc:
            log(f"photo check stopped: the portal is not answering ({exc})")
            tally["stopped_early"] += 1
            break
        except SourceError as exc:
            db.save_photo_check(wid, portal_work_id=internal, status="failed", error=str(exc)[:300])
            tally["failed"] += 1
            rest()
        if progress_every and n % progress_every == 0:
            log(f"  {n}/{len(work_ids)} works checked: {dict(tally)}")
    return tally


#: Check outcomes that asked the portal (a work not yet completed needs no request).
ASKED = ("photos", "documents", "no_photo", "failed")


def recheck_before(now: datetime | None = None) -> str:
    """Checks older than this, that found nothing to compare, are due again."""
    return ((now or datetime.now(timezone.utc)) - RECHECK_AFTER).isoformat()


def run(limit: int | None = None, *, work_ids: list[str] | None = None,
        log: Callable[[str], None] = print) -> Counter:
    """Check the held works that are due (or the ones named)."""
    if instance_lock.is_held():
        raise RuntimeError("a poller holds this database, and checks these photos itself at "
                           "night (ASTRA_PHOTO_HOURS); stop it before a manual photo check "
                           "(one writer per database)")
    due = work_ids if work_ids else db.due_photo_checks(recheck_before=recheck_before(), limit=limit)
    log(f"photo check: {len(due)} work(s) to look at")
    started = time.monotonic()
    tally = check_works(due, log=log)
    log(f"photo check done in {time.monotonic() - started:.0f}s: {dict(tally)}")
    return tally


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fingerprint portal photos of held duplicate matches")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="what the checks have found so far")
    group.add_argument("--limit", type=int, help="check at most this many due works")
    group.add_argument("--works", nargs="+", help="check these works now")
    args = parser.parse_args(argv)
    if args.status:
        try:
            last_night = json.loads(db.get_state("photo_night") or "null")
        except ValueError:
            last_night = None
        print(json.dumps({**db.photo_check_summary(),
                          "last_night": last_night,
                          "last_check_at": db.get_state("last_photo_check_at") or None,
                          "waiting_for_analysis": bool(db.get_state("photo_results_pending"))},
                         indent=2))
        return 0
    try:
        run(args.limit, work_ids=args.works)
    except RuntimeError as exc:
        print(f"[ASTRA] {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
