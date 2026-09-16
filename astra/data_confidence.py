"""How far each finding's underlying record can be trusted right now.

Every stored record passed the ingestion gates — a read the portal disagreed
with is never stored — so the question is not whether a record was accepted,
but whether its area is healthy at the moment of analysis:

  * its stored figures differ from the portal's own tiles (parity);
  * its latest read failed, or it has been failing for some time;
  * works it used to list have disappeared and are awaiting confirmation.

A work-level finding from such an area is marked `data_confidence: reduced`
with the reasons, and stays visible. Hiding it would make the corpus look
cleaner than it is; scoring it as certain would overstate the evidence. A
reviewer decides, with the caveat in front of them.

The time since an area was last re-read is deliberately not a reason: an
unchanged area's figures are confirmed every minute by the national check,
and a laptop that was closed for a day would otherwise mark everything
reduced until the next sweep.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import load_rules
from .locale import text
from .schemas import Finding

VERIFIED = "verified"
REDUCED = "reduced"


def _stamp(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def area_confidence(health: pd.DataFrame, now: datetime | None = None) -> dict[str, tuple[str, list[str]]]:
    """{shard_id: (level, reasons)} from `db.area_health()`."""
    if health is None or health.empty:
        return {}
    now = now or datetime.now(timezone.utc)
    cfg = load_rules().get("data_confidence") or {}
    stale_after = timedelta(hours=float(cfg.get("stale_after_hours", 36)))
    out: dict[str, tuple[str, list[str]]] = {}
    for row in health.to_dict("records"):
        reasons = []
        if row.get("exact") is not None and not pd.isna(row.get("exact")) and int(row["exact"]) == 0:
            reasons.append(text("en", "confidence.reason.parity_differs"))
        if row.get("exact") is None or pd.isna(row.get("exact")):
            reasons.append(text("en", "confidence.reason.unchecked"))
        failures = row.get("consecutive_failures")
        if failures is not None and not pd.isna(failures) and int(failures) > 0:
            reasons.append(text("en", "confidence.reason.failed", count=int(failures)))
        since = _stamp(row.get("stale_since"))
        if since is not None and now - since > stale_after:
            reasons.append(text("en", "confidence.reason.stale", since=f"{since:%d %b %Y %H:%M}"))
        if row.get("lifecycle") in ("QUARANTINED", "RETRY"):
            reasons.append(text("en", "confidence.reason.held_back"))
        missing = row.get("awaiting_removal")
        if missing is not None and not pd.isna(missing) and int(missing) > 0:
            reasons.append(text("en", "confidence.reason.missing", count=int(missing)))
        out[str(row["shard_id"])] = (REDUCED if reasons else VERIFIED, reasons)
    return out


def annotate(findings: list[Finding], works: pd.DataFrame,
             health: pd.DataFrame | None) -> dict[str, int]:
    """Mark each work-level finding with its area's confidence.

    No-op for a corpus without area health (the CSV path). Returns counts by
    level for the run record.
    """
    levels = area_confidence(health)
    if not levels or works.empty or "shard_id" not in works.columns:
        return {}
    shard_of = dict(zip(works["work_id"].astype(str), works["shard_id"]))
    counts: dict[str, int] = {}
    for finding in findings:
        if finding.entity_type != "work":
            continue
        shard = shard_of.get(str(finding.entity_id))
        level, reasons = levels.get(str(shard), (None, None)) if shard is not None else (None, None)
        if level is None:
            continue
        finding.details["data_confidence"] = level
        if reasons:
            finding.details["data_confidence_reasons"] = reasons
        counts[level] = counts.get(level, 0) + 1
    return counts
