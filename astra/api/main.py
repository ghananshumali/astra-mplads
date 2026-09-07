"""ASTRA API — authority-tiered decision-support endpoints.

Tiers scope BOTH the data slice and the framing (tier_views come from the
orchestrator's synthesis, not from filtered SQL):

  GET /flags/mp?constituency=&state=       — MP: own constituency, mp-framed
  GET /flags/district?district=&state=     — DA: execution queue
  GET /flags/state?state=                  — SNA: cross-district patterns
  GET /flags/ministry                      — MoSPI: national exception rates
  GET /flags/case/{flag_id}?tier=          — one case, fully explained for a tier
  POST /flags/{flag_id}/feedback           — human-in-the-loop review action
  GET /meta/router-trace                   — why each agent ran / was skipped
  GET /meta/data-source                    — dual-mode ingestion provenance:
                                             which source supplied this batch
                                             (live vs offline official CSV) and
                                             how current it is vs the live
                                             eSAKSHI portal

Full RBAC/audit-logging is a stage-2 roadmap item; tier here is an explicit
path segment so the demo can show all four synthesized framings side by side.
"""
from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .. import HUMAN_REVIEW_DISCLAIMER, PLATFORM_NAME, PLATFORM_TAGLINE, __version__
from .. import db
from ..config import PROCESSED_DIR

app = FastAPI(title=f"{PLATFORM_NAME} API", description=PLATFORM_TAGLINE, version=__version__)


def _tier_payload(flags: list[dict], tier: str, aggregate: dict | None = None) -> dict:
    return {
        "tier": tier,
        "disclaimer": HUMAN_REVIEW_DISCLAIMER,
        "count": len(flags),
        "aggregate": aggregate or {},
        "flags": [
            {
                "flag_id": f["flag_id"],
                "entity_label": f["entity_label"],
                "entity_type": f["entity_type"],
                "state": f["state"],
                "district": f["district"],
                "constituency": f["constituency"],
                "era": f["era"],
                "risk_score": f["risk_score"],
                "alert": f["alert"],
                "review_status": f["review_status"],
                "narrative": f["narrative"],
                "tier_view": f["tier_views"].get(tier, f["narrative"]),
                # structured, plain-language brief for this authority
                "display_title": f.get("display_title"),
                "primary_signal": f.get("primary_signal"),
                "brief": (f.get("tier_briefs") or {}).get(tier),
                "findings": f["findings"],
            }
            for f in flags
        ],
    }


@app.get("/")
def root():
    return {"platform": PLATFORM_NAME, "tagline": PLATFORM_TAGLINE,
            "version": __version__, "disclaimer": HUMAN_REVIEW_DISCLAIMER}


@app.get("/flags/mp")
def flags_mp(constituency: str, state: str | None = None):
    flags = [f for f in db.load_flags()
             if (f["constituency"] or "").lower() == constituency.lower()
             and (not state or (f["state"] or "").lower() == state.lower())]
    return _tier_payload(flags, "mp")


@app.get("/flags/district")
def flags_district(district: str, state: str | None = None):
    flags = [f for f in db.load_flags()
             if (f["district"] or "").lower() == district.lower()
             and (not state or (f["state"] or "").lower() == state.lower())]
    return _tier_payload(flags, "district")


@app.get("/flags/state")
def flags_state(state: str):
    flags = [f for f in db.load_flags() if (f["state"] or "").lower() == state.lower()]
    by_district: dict[str, int] = {}
    for f in flags:
        by_district[f["district"] or "unattributed"] = by_district.get(f["district"] or "unattributed", 0) + 1
    return _tier_payload(flags, "state", {"flags_by_district": by_district})


@app.get("/flags/ministry")
def flags_ministry(min_score: float = 0):
    flags = [f for f in db.load_flags() if f["risk_score"] >= min_score]
    by_state: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for f in flags:
        by_state[f["state"] or "unattributed"] = by_state.get(f["state"] or "unattributed", 0) + 1
        for fd in f["findings"]:
            by_rule[fd["rule_id"]] = by_rule.get(fd["rule_id"], 0) + 1
    return _tier_payload(flags[:200], "ministry",
                         {"flags_by_state": by_state, "findings_by_rule": by_rule,
                          "total_alerts": sum(1 for f in flags if f["alert"])})


@app.get("/flags/case/{flag_id}")
def flag_case(flag_id: str, tier: str = "district"):
    """One case with its authority-specific brief and all supporting evidence."""
    if tier not in ("mp", "district", "state", "ministry"):
        raise HTTPException(400, "tier must be mp | district | state | ministry")
    f = db.get_flag(flag_id)
    if not f:
        raise HTTPException(404, f"no such case: {flag_id}")
    return {
        "flag_id": f["flag_id"],
        "display_title": f.get("display_title"),
        "entity_id": f["entity_id"],
        "entity_type": f["entity_type"],
        "state": f["state"], "district": f["district"],
        "constituency": f["constituency"], "era": f["era"],
        "risk_score": f["risk_score"], "alert": f["alert"],
        "review_status": f["review_status"],
        "primary_signal": f.get("primary_signal"),
        "brief": (f.get("tier_briefs") or {}).get(tier),
        "all_tier_briefs": f.get("tier_briefs"),
        "findings": f["findings"],
        "narrative": f["narrative"],
        "disclaimer": HUMAN_REVIEW_DISCLAIMER,
    }


class FeedbackIn(BaseModel):
    action: str            # confirmed | false_positive | under_review
    authority_tier: str    # mp | district | state | ministry
    note: str = ""


@app.post("/flags/{flag_id}/feedback")
def flag_feedback(flag_id: str, body: FeedbackIn):
    if body.action not in ("confirmed", "false_positive", "under_review"):
        raise HTTPException(400, "action must be confirmed | false_positive | under_review")
    db.record_feedback(flag_id, body.action, body.authority_tier, body.note)
    return {"ok": True, "flag_id": flag_id, "recorded": body.action,
            "note": "Feedback feeds threshold recalibration (stage-2 retraining loop)."}


@app.get("/meta/data-source")
def data_source():
    """Provenance of the analysed batch under the dual-mode ingestion strategy."""
    p = PROCESSED_DIR / "ingest_meta.json"
    prov = db.load_provenance()
    payload = {
        "provenance": prov.to_dict(orient="records") if not prov.empty else [],
    }
    if p.exists():
        meta = json.loads(p.read_text(encoding="utf-8"))
        payload.update({
            "mode_requested": meta.get("mode_requested"),
            "mode_resolved": meta.get("mode_resolved"),
            "works": meta.get("works"),
            "fundflows": meta.get("fundflows"),
            "work_eras": meta.get("eras"),
            "fundflow_eras": meta.get("flow_eras"),
            "freshness_vs_live_portal": meta.get("freshness"),
            "live_portal_totals": meta.get("live_tiles"),
            "ingested_at": meta.get("ingested_at"),
        })
    return payload


@app.get("/meta/router-trace")
def router_trace():
    p = PROCESSED_DIR / "run_meta.json"
    if not p.exists():
        raise HTTPException(404, "pipeline has not run yet")
    return json.loads(p.read_text(encoding="utf-8"))
