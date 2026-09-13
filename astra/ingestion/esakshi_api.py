"""eSAKSHI REST client — the only component in ASTRA that talks to the portal.

The MPLADS dashboard at mplads.mospi.gov.in/digigov/dashboard.html does not
serve spreadsheets; it asks a small REST service behind the page for whichever
slice the visitor selected. That service is public, unauthenticated, and takes
filters — whole country, one state, or one constituency — which is what makes
incremental ingestion possible at all. The CSV exports in `datasets/` are this
same service's output, saved by the browser's DataTables `csvHtml5` button.

Addressing
----------
Every call is scoped by a `combo` string::

    "<stateId>,<constituencyId>,<mpId>,<house>[,<tenureId>]"

`0` in any position means "all"; `house` is 2 for Lok Sabha and 1 for Rajya
Sabha. `shard_combo()` builds these; ids come from `states()` / constituencies().

Two questions, very different costs
-----------------------------------
`watermark()`   -> six counts and six rupee totals for a slice. 0.3 s for a
                   state or constituency, 3.4-5.6 s nationally. This is the
                   change detector.
`tile_report()` -> the records themselves. ~100 rows / 0.6 s for one
                   constituency. A national request never returns at all
                   (measured: no response after 45 s), which is precisely why
                   the pipeline shards.

Measured behaviour this client is built around (12 Sep 2026)
------------------------------------------------------------
* No server-side cache: repeat national calls took 3.45 / 5.61 / 4.17 s. Each
  call runs a live aggregate query, so the data is current and every request
  costs the portal real work. Be frugal.
* Concurrency tops out at four: 1 worker 1.71 calls/s, 2 -> 3.08, 4 -> 4.79.
  Past four, latency inflates without throughput. MAX_WORKERS is a cap, not a
  target.
* No `ETag`, no `Last-Modified`, `Pragma: no-cache`, no gzip — conditional
  requests are impossible, so change detection has to be ours.
* `Content-Type` claims ISO-8859-1 while the bytes are UTF-8. Always go through
  `response.json()`; `response.text` yields mojibake.
* `Access-Control-Allow-Origin` is the portal's own origin, so a browser cannot
  call this. Server-side only.
* Sticky `JSESSIONID` / `ROUTEID` / WAF cookies: reuse one Session per thread.
* An **invalid shard id returns HTTP 200 with every count zero** and no error.
  `watermark()` and `tile_report()` therefore never treat emptiness as fact —
  they hand back a `suspicious_zero` marker for `validate.guard_zero()` to act
  on. This is the failure mode that would otherwise silently delete a district.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://mplads.mospi.gov.in/rest/PreLoginDashboardData"

#: Tile keys the portal accepts, and the response key each one comes back under.
TILE_KEYS = {
    "recommended": "Works Recommended",
    "sanctioned": "Works Sanctioned",
    "completed": "Works Completed",
    "expenditure": "Expenditure on Completed and On-going Works as on Date",
    "allocated": "Allocated Limit for Hon'ble MPs",
    "calamity": "Amount consented for Calamity",
}

#: The four that carry work-level records and drive the pipeline.
RECORD_TILES = ("recommended", "sanctioned", "completed", "expenditure")

HOUSE_LS, HOUSE_RS = 2, 1

#: Hard ceiling on concurrent requests against the portal. Measured: no
#: throughput gained above four, and we are a guest on a government service.
MAX_WORKERS = int(os.environ.get("ASTRA_API_WORKERS", "4"))
#: (connect, read). The read budget is generous because a large state's report
#: legitimately takes 15 s; a national one never returns and must time out.
TIMEOUT = (5.0, 45.0)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://mplads.mospi.gov.in",
    "Referer": "https://mplads.mospi.gov.in/digigov/dashboard.html",
    "User-Agent": _UA,
}


class SourceError(RuntimeError):
    """The portal could not answer. Carries enough context to requeue a shard."""

    def __init__(self, message: str, *, path: str = "", body: object = None,
                 status: int | None = None):
        super().__init__(message)
        self.path, self.body, self.status = path, body, status


class CircuitOpen(SourceError):
    """The breaker is open: we are deliberately not calling the portal."""


# --------------------------------------------------------------- circuit breaker
class CircuitBreaker:
    """Stops calling a failing portal instead of hammering it.

    Opens once the failure rate over the last `window` outcomes exceeds
    `threshold`, then stays open for a backoff that doubles on each further
    failure, jittered, capped at `cap_seconds`. A single success closes it.
    """

    def __init__(self, window: int = 20, threshold: float = 0.5,
                 base_seconds: float = 30.0, cap_seconds: float = 1800.0):
        self.window, self.threshold = window, threshold
        self.base, self.cap = base_seconds, cap_seconds
        self._outcomes: list[bool] = []
        self._open_until = 0.0
        self._consecutive_opens = 0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def opens_in(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - time.monotonic())

    def record(self, ok: bool) -> None:
        with self._lock:
            self._outcomes.append(ok)
            del self._outcomes[:-self.window]
            if ok:
                self._open_until = 0.0
                self._consecutive_opens = 0
                return
            if len(self._outcomes) < max(4, self.window // 4):
                return                      # too few samples to judge
            rate = 1 - sum(self._outcomes) / len(self._outcomes)
            if rate > self.threshold:
                self._consecutive_opens += 1
                wait = min(self.cap, self.base * 2 ** (self._consecutive_opens - 1))
                self._open_until = time.monotonic() + wait * (0.75 + random.random() / 2)
                self._outcomes.clear()

    def check(self) -> None:
        if self.is_open:
            raise CircuitOpen(
                f"circuit open for another {self.opens_in():.0f}s; "
                f"serving cached data")


# ------------------------------------------------------------------------ shards
@dataclass(frozen=True)
class Shard:
    """One pollable slice: a Lok Sabha constituency, or a Rajya Sabha state."""

    house: int
    state_id: int
    constituency_id: int = 0
    state_name: str = ""
    constituency_name: str = ""

    @property
    def shard_id(self) -> str:
        return f"{self.house}:{self.state_id}:{self.constituency_id}"

    @property
    def combo(self) -> str:
        return shard_combo(self.state_id, self.constituency_id, house=self.house)

    @property
    def label(self) -> str:
        where = self.constituency_name or self.state_name or str(self.state_id)
        return f"{where} ({'LS' if self.house == HOUSE_LS else 'RS'})"


def shard_combo(state_id: int = 0, constituency_id: int = 0, mp_id: int = 0,
                house: int = HOUSE_LS, tenure_id: int | None = None) -> str:
    parts = [state_id, constituency_id, mp_id, house]
    if tenure_id is not None:
        parts.append(tenure_id)
    return ",".join(str(int(p)) for p in parts)


def national_combo(house: int = HOUSE_LS) -> str:
    return shard_combo(house=house)


# ------------------------------------------------------------------------ client
def _parse_amount(text: str) -> float | None:
    """'\xa057,64,99,83,550.11' -> 5764998355011.0 ; None when unparseable."""
    cleaned = "".join(c for c in str(text) if c.isdigit() or c == ".")
    if not cleaned or cleaned == ".":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


@dataclass
class Watermark:
    """The cheap fingerprint of a slice: what the portal says is in there."""

    combo: str
    counts: dict[str, int | None] = field(default_factory=dict)
    totals: dict[str, float | None] = field(default_factory=dict)
    tenure: str | None = None
    fetched_at: str = ""
    suspicious_zero: bool = False

    def signature(self) -> tuple:
        """Everything a change would have to move. Compared, never trusted."""
        keys = sorted(set(self.counts) | set(self.totals))
        return tuple((k, self.counts.get(k), self.totals.get(k)) for k in keys)


class EsakshiClient:
    """Thread-safe: each thread gets its own Session, so cookies stay sticky."""

    def __init__(self, base: str = BASE, timeout: tuple[float, float] = TIMEOUT,
                 breaker: CircuitBreaker | None = None, retries: int = 3):
        self.base, self.timeout = base.rstrip("/"), timeout
        self.breaker = breaker if breaker is not None else CircuitBreaker()
        self._retries = retries
        self._local = threading.local()

    # ------------------------------------------------------------- transport
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(_HEADERS)
            retry = Retry(
                total=self._retries, connect=self._retries, read=self._retries,
                backoff_factor=1.5, status_forcelist=(500, 502, 503, 504),
                allowed_methods=frozenset(["POST", "GET"]), raise_on_status=False,
                respect_retry_after_header=True,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_maxsize=MAX_WORKERS,
                                  pool_connections=MAX_WORKERS)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            self._local.session = s
        return s

    def close(self) -> None:
        s = getattr(self._local, "session", None)
        if s is not None:
            s.close()
            self._local.session = None

    def _post(self, path: str, body: dict) -> object:
        """POST and decode. Raises SourceError for anything that is not JSON."""
        self.breaker.check()
        url = f"{self.base}{path}"
        try:
            r = self.session().post(url, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            self.breaker.record(False)
            raise SourceError(f"{type(exc).__name__}: {str(exc)[:160]}",
                              path=path, body=body) from exc
        if r.status_code != 200:
            # A malformed combo comes back as an HTML 500 error page.
            self.breaker.record(False)
            raise SourceError(f"HTTP {r.status_code}", path=path, body=body,
                              status=r.status_code)
        try:
            # Deliberately r.json(): the header claims ISO-8859-1 but the bytes
            # are UTF-8, so r.text would mojibake every Indic name.
            payload = r.json()
        except ValueError as exc:
            self.breaker.record(False)
            raise SourceError(f"non-JSON response ({len(r.content)} bytes)",
                              path=path, body=body, status=r.status_code) from exc
        self.breaker.record(True)
        return payload

    # -------------------------------------------------------- id enumeration
    def states(self) -> list[dict]:
        """[{'STATE_NAME': 'Goa', 'STATE_ID': 12}, ...] — 36 rows."""
        rows = self._post("/getStateData", {})
        if not isinstance(rows, list) or not rows:
            raise SourceError("getStateData returned no states", path="/getStateData")
        return rows

    def constituencies(self, state_id: int) -> list[dict]:
        """[{'ID': 418, 'CAPTION': 'ALIGARH'}, ...] for one state."""
        rows = self._post("/getConstituencyData", {"id": str(int(state_id))})
        return rows if isinstance(rows, list) else []

    def tenures(self, house: int = HOUSE_LS) -> list[dict]:
        rows = self._post("/getTenureData", {"uname": national_combo(house)})
        return rows if isinstance(rows, list) else []

    def shards(self, houses: tuple[int, ...] = (HOUSE_LS, HOUSE_RS)) -> list[Shard]:
        """Enumerate every pollable slice, freshly, from the portal itself.

        Never hard-code ids: constituencies are reorganised and new union
        territories appear (Ladakh and Telangana both carry ids today).

        Lok Sabha shards are constituencies (543). Rajya Sabha members are not
        tied to a constituency, so RS shards are states (36).
        """
        out: list[Shard] = []
        states = self.states()
        for house in houses:
            for st in states:
                sid, sname = int(st["STATE_ID"]), str(st.get("STATE_NAME") or "")
                if house == HOUSE_RS:
                    out.append(Shard(house, sid, 0, sname, ""))
                    continue
                for con in self.constituencies(sid):
                    cid = con.get("ID")
                    if cid is None:
                        continue
                    out.append(Shard(house, sid, int(cid), sname,
                                     str(con.get("CAPTION") or "")))
        return out

    # ------------------------------------------------------------- watermark
    def watermark(self, combo: str) -> Watermark:
        """Counts and rupee totals for a slice — the change detector.

        An unknown id yields HTTP 200 with every figure zero, so an all-zero
        answer is flagged `suspicious_zero` rather than believed.
        """
        payload = self._post("/getTilesData", {"uname": combo})
        if not isinstance(payload, dict):
            raise SourceError("getTilesData did not return an object",
                              path="/getTilesData", body={"uname": combo})
        wm = Watermark(combo=combo,
                       fetched_at=datetime.now(timezone.utc).isoformat())
        for key, value in payload.items():
            if key == "Current Tenure":
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    wm.tenure = value[0].get("CAPTION")
                continue
            if not (isinstance(value, list) and value):
                continue
            texts = [str(v) for v in value]
            # Count tiles lead with a bare integer; amount-only tiles do not.
            head = texts[0].replace(",", "").strip()
            if head.isdigit():
                wm.counts[key] = int(head)
                wm.totals[key] = _parse_amount(texts[1]) if len(texts) > 1 else None
            else:
                wm.counts[key] = None
                wm.totals[key] = _parse_amount(texts[0])
        if not wm.counts and not wm.totals:
            raise SourceError("getTilesData returned no tiles",
                              path="/getTilesData", body={"uname": combo})
        wm.suspicious_zero = all(not c for c in wm.counts.values()) and \
            all(not t for t in wm.totals.values())
        return wm

    # ---------------------------------------------------------- tile records
    def tile_report(self, combo: str, tile: str) -> list[dict]:
        """Work-level records for one slice and one tile.

        `tile` is a key of TILE_KEYS. Two response quirks are handled here:
        the value under the response key is itself a JSON *string* needing a
        second decode, and a slice with nothing in it answers with the
        degenerate `[{"Total_Amt": 0.0}]` shape rather than an empty list.
        """
        key = TILE_KEYS.get(tile, tile)
        payload = self._post("/getTilesReportData", {"combo": combo, "key": key})
        if not isinstance(payload, dict):
            raise SourceError("getTilesReportData did not return an object",
                              path="/getTilesReportData",
                              body={"combo": combo, "key": key})
        return parse_tile(payload, combo=combo, key=key)


def run_parallel(jobs: list, worker, *, workers: int = MAX_WORKERS) -> list:
    """Map `worker` over `jobs`, at most `workers` at a time, in order.

    The concurrency ceiling is the whole point: measured against the portal,
    one worker manages 1.71 calls/s, two 3.08, four 4.79, and beyond four the
    per-call latency inflates without buying throughput. Exceptions are
    returned in place rather than raised, so one bad shard cannot abandon a
    sweep half-done.
    """
    if not jobs:
        return []
    if workers <= 1 or len(jobs) == 1:
        out = []
        for job in jobs:
            try:
                out.append(worker(job))
            except Exception as exc:                 # noqa: BLE001 — returned
                out.append(exc)
        return out

    from concurrent.futures import ThreadPoolExecutor

    def guarded(job):
        try:
            return worker(job)
        except Exception as exc:                     # noqa: BLE001 — returned
            return exc

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        return list(pool.map(guarded, jobs))


def parse_tile(payload: dict, *, combo: str = "", key: str = "") -> list[dict]:
    """Flatten a getTilesReportData payload into a list of record dicts.

    Kept module-level and free of I/O so it can be unit-tested against saved
    fixtures with no network.
    """
    rows: list[dict] = []
    for _response_key, value in payload.items():
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise SourceError(f"tile value was not JSON: {str(exc)[:80]}",
                                  path="/getTilesReportData",
                                  body={"combo": combo, "key": key}) from exc
        if not isinstance(value, list):
            continue
        # "no records" is expressed as a single {"Total_Amt": 0.0} row.
        if len(value) == 1 and isinstance(value[0], dict) \
                and set(value[0]) == {"Total_Amt"}:
            continue
        rows.extend(r for r in value if isinstance(r, dict))
    return rows
