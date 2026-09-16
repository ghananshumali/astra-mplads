"""The four gates a shard must pass before anything is written.

Each gate exists because of a specific measured behaviour of the eSAKSHI
portal, not as generic defensiveness:

`assert_contract`
    The portal renames and adds fields without notice. A missing *required*
    field means the record can no longer be mapped, so the shard is
    quarantined and its previous rows are kept. Extra fields are reported but
    never fatal — the Rajya Sabha completed tile legitimately carries two more
    (`FILE_STATUS`, `ATTACH_ID`) than the Lok Sabha one.

`guard_zero`
    Asking about a shard id that does not exist returns **HTTP 200 with every
    count zero and no error at all**. An empty answer is therefore treated as a
    fault, never as "this constituency has no works". Removal requires two
    separate full sweeps to agree, which is the poller's job, not this gate's.

`reconcile_count`
    What we stored for a shard must equal what the portal's own counter says is
    in it. This is the self-audit behind the freshness claim, and it is also
    the only independent check on the counts endpoint: a shard whose count and
    records disagree is recorded, so systematic divergence shows up as a
    pattern rather than a surprise. Note the two never line up exactly on row
    counts — each report carries one id-less summary row (Aligarh returns 104
    rows against a count of 103) — so the comparison is made on mapped records,
    not raw rows, and allows a small tolerance for read skew.

`atomic_upsert`
    Enforced in `db.upsert_works()`, which writes one shard per transaction so
    a half-finished download cannot half-apply.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field

#: Fields each record tile must carry for `esakshi_map.to_works()` to work.
#: Deliberately a required subset, not an exact set: the portal may add
#: columns, and different houses return slightly different shapes.
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "recommended": frozenset({
        "WORK_RECOMMENDATION_DTL_ID", "ACTIVITY_NAME", "STATE_NAME",
        "CONSTITUENCY", "MP_NAME", "IDA_NAME", "RECOMMENDATION_DATE",
        "RECOMMENDED_AMOUNT", "WORK_STAGE", "HOUSE_OF_PARLIAMENT",
    }),
    "sanctioned": frozenset({
        "WORK_RECOMMENDATION_DTL_ID", "SANCTION_DATE", "SANCTION_AMOUNT",
        "WORK_STAGE",
    }),
    "completed": frozenset({
        "WORK_RECOMMENDATION_DTL_ID", "ACTUAL_END_DATE", "ACTUAL_AMOUNT",
    }),
    "expenditure": frozenset({
        "WORK_RECOMMENDATION_DTL_ID", "VENDOR_NAME", "FUND_DISBURSED_AMT",
        "EXPENDITURE_DATE", "WORK_STATUS",
    }),
}

#: Lifecycle values stored in `shard_watermarks.lifecycle`.
IDLE, SUSPECT, DIRTY, FETCHED, VALIDATED, STORED = (
    "IDLE", "SUSPECT", "DIRTY", "FETCHED", "VALIDATED", "STORED")
QUARANTINED, RETRY = "QUARANTINED", "RETRY"

#: Records may differ from the portal's own count by this much before the shard
#: is held back. One is expected: the reports carry an id-less summary row, and
#: counts and records are not read atomically.
COUNT_TOLERANCE = 1


@dataclass
class GateResult:
    ok: bool
    lifecycle: str
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def __bool__(self) -> bool:                # so `if gate:` reads naturally
        return self.ok


# ------------------------------------------------------------------ gate 1
def assert_contract(tile: str, rows: list[dict]) -> GateResult:
    """Every required field for `tile` must appear somewhere in `rows`.

    Checked against the union of keys rather than the first row, because the
    id-less summary row the portal appends carries only a subset.
    """
    required = REQUIRED_FIELDS.get(tile)
    if required is None:
        return GateResult(True, VALIDATED, "no contract declared for this tile")
    if not rows:
        # Emptiness is guard_zero's decision, not the contract's.
        return GateResult(True, VALIDATED, "no rows to check")
    seen: set[str] = set()
    for row in rows:
        seen.update(row)
    missing = sorted(required - seen)
    extra = sorted(seen - required)
    if missing:
        return GateResult(False, QUARANTINED,
                          f"{tile}: missing required field(s) {missing}",
                          {"missing": missing, "extra": extra})
    return GateResult(True, VALIDATED, "", {"extra": extra})


# ------------------------------------------------------------------ gate 2
def guard_zero(rows: list[dict], *, suspicious_zero: bool = False,
               previous_stored: int | None = None,
               registered: bool = False) -> GateResult:
    """Decide whether an empty or shrunken answer may be believed.

    An all-zero response is ambiguous on its own: it is what the portal returns
    both for a shard id that does not exist and for a slice that genuinely
    holds nothing. The discrimination is not in the response, it is in where
    the id came from — an id the portal itself enumerated through
    `getStateData` / `getConstituencyData` is valid by construction.

    So `registered` decides. Four union territories (Lakshadweep, Andaman and
    Nicobar, Ladakh, Dadra and Nagar Haveli and Daman and Diu) have no Rajya
    Sabha members at all, and a first national sweep found a Lok Sabha
    constituency with no works either; all of those answer all-zero and all of
    them are telling the truth.

    Refused:
      * an all-zero watermark for an id we did not enumerate — a bad id,
      * an all-zero watermark where we previously held records — a regression,
      * no records where we previously held some,
      * a collapse to under a tenth of what we held.
    """
    n = len(rows)
    if suspicious_zero:
        if not registered:
            return GateResult(False, QUARANTINED,
                              "all-zero watermark for a shard id that is not in "
                              "the registry: an unknown id, never an empty slice",
                              {"rows": n, "previous_stored": previous_stored})
        if previous_stored:
            return GateResult(False, QUARANTINED,
                              f"all-zero watermark for a registered shard that "
                              f"previously held {previous_stored} records",
                              {"rows": n, "previous_stored": previous_stored})
        return GateResult(True, VALIDATED,
                          "registered shard is genuinely empty (no members or "
                          "no works)",
                          {"rows": n, "previous_stored": previous_stored,
                           "empty": True})
    if n == 0:
        if previous_stored:
            return GateResult(False, QUARANTINED,
                              f"no records returned where {previous_stored} were "
                              f"previously stored",
                              {"rows": 0, "previous_stored": previous_stored})
        return GateResult(True, VALIDATED, "shard is genuinely empty",
                          {"rows": 0, "previous_stored": previous_stored})
    if previous_stored and n * 10 < previous_stored:
        return GateResult(False, QUARANTINED,
                          f"record count collapsed from {previous_stored} to {n}",
                          {"rows": n, "previous_stored": previous_stored})
    return GateResult(True, VALIDATED, "", {"rows": n,
                                            "previous_stored": previous_stored})


# ------------------------------------------------------------------ gate 3
def reconcile_count(portal_count: int | None, mapped: int, *,
                    tolerance: int = COUNT_TOLERANCE) -> GateResult:
    """Compare what we mapped against the portal's own count for the shard.

    A mismatch inside `tolerance` is recorded but allowed: the reports include
    one id-less summary row, and the count and the records are separate reads.
    Anything larger means we did not get the whole slice.
    """
    if portal_count is None:
        return GateResult(True, VALIDATED, "portal reported no count",
                          {"portal": None, "mapped": mapped, "matched": None})
    delta = mapped - portal_count
    matched = delta == 0
    if abs(delta) <= tolerance:
        return GateResult(True, VALIDATED, "" if matched else
                          f"within tolerance ({delta:+d})",
                          {"portal": portal_count, "mapped": mapped,
                           "delta": delta, "matched": matched})
    return GateResult(False, QUARANTINED,
                      f"mapped {mapped} records against a portal count of "
                      f"{portal_count} ({delta:+d})",
                      {"portal": portal_count, "mapped": mapped,
                       "delta": delta, "matched": False})


def figure_differences(portal: dict, stored: dict, *,
                       counts_only: bool = False) -> list[dict]:
    """Where ASTRA's four tile figures differ from the portal's, if anywhere.

    Both sides map tile -> [count, rupees]; `None` on the portal side means it
    did not report that figure (expenditure has no count). Rupees are compared
    to within one rupee: the portal's totals are floating-point sums too.
    """
    out = []
    for tile, (p_count, p_total) in portal.items():
        s_count, s_total = stored.get(tile) or (None, None)
        if p_count is not None and tile != "expenditure" and p_count != s_count:
            out.append({"tile": tile, "measure": "count", "portal": p_count,
                        "stored": s_count})
        if (not counts_only and p_total is not None
                and abs((s_total or 0.0) - p_total) > 1.0):
            out.append({"tile": tile, "measure": "rupees", "portal": p_total,
                        "stored": s_total})
    return out


# -------------------------------------------------------------- orchestration
def validate_shard(tiles: dict[str, list[dict]], *, suspicious_zero: bool = False,
                   portal_count: int | None = None, mapped: int | None = None,
                   previous_stored: int | None = None,
                   registered: bool = False) -> GateResult:
    """Run the pre-write gates in order and return the first failure.

    `mapped` is the number of `Work` records built from `tiles`; pass it to
    include the count reconciliation. Callers that have not mapped yet can omit
    it and reconcile after `to_works()`.
    """
    for tile, rows in tiles.items():
        result = assert_contract(tile, rows)
        if not result:
            return result

    zero = guard_zero(tiles.get("recommended") or [],
                      suspicious_zero=suspicious_zero,
                      previous_stored=previous_stored,
                      registered=registered)
    if not zero:
        return zero

    if mapped is not None:
        return reconcile_count(portal_count, mapped)
    return GateResult(True, VALIDATED, "", zero.detail)


# --------------------------------------------------------------- fingerprints
def payload_hash(tiles: dict[str, list[dict]]) -> str:
    """Stable SHA-256 of a shard's records, so an unchanged shard is not rewritten.

    The portal sends no `ETag` or `Last-Modified` and sets `Pragma: no-cache`,
    so conditional requests are impossible and the fingerprint has to be ours.
    `Sno` is excluded: it is a presentation row number that shifts whenever
    anything is inserted upstream, and including it would make every response
    look changed.

    Rows are sorted on their serialised form rather than on the work id. The
    expenditure tile carries several payment tranches per work, so a work id is
    not a unique key there and sorting on it would leave the order of tied rows
    up to however the portal happened to return them.
    """
    digest = hashlib.sha256()
    for tile in sorted(tiles):
        digest.update(tile.encode())
        rows = sorted(
            json.dumps({k: v for k, v in row.items() if k != "Sno"},
                       sort_keys=True, default=str)
            for row in tiles[tile]
        )
        digest.update("\n".join(rows).encode())
    return digest.hexdigest()


def gzip_json(obj: object) -> bytes:
    """Compact gzipped JSON. Measured 15.5x on a real tile response."""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                     default=str).encode("utf-8")
    return gzip.compress(raw, compresslevel=6)


def ungzip_json(blob: bytes) -> object:
    return json.loads(gzip.decompress(blob).decode("utf-8"))
