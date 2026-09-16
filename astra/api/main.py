"""ASTRA API — authority-tiered decision-support endpoints.

Tiers scope BOTH the data slice and the framing (tier_views come from the
orchestrator's synthesis, not from filtered SQL):

  GET /flags/mp?constituency=&state=       — MP: own constituency, mp-framed
  GET /flags/district?district=&state=     — DA: execution queue
  GET /flags/state?state=                  — SNA: cross-district patterns
  GET /flags/ministry                      — MoSPI: national exception rates
  GET /flags/case/{flag_id}?tier=          — one case, fully explained for a tier
  GET /flags/case/{flag_id}/synthesis      — LLM synthesis + constrained action
                                             plan for a tier (falls back to the
                                             deterministic brief automatically)
  GET /flags/case/{flag_id}/actions?tier=  — the RBAC allow-list for that tier
  GET /meta/llm                            — LLM provider status (never the key)
  POST /flags/{flag_id}/feedback           — human-in-the-loop review action
  GET /meta/router-trace                   — why each agent ran / was skipped
  GET /meta/data-source                    — where the corpus came from: the
                                             live eSAKSHI portal, or an offline
                                             official CSV batch, with counts
                                             read from the database
  GET /meta/freshness                      — live sync health: poller running,
                                             last portal check, stale slices
  GET /meta/recent-updates                 — what the portal changed most
                                             recently, slice and field level

Language: the case endpoints, the case list and the pipeline page take
`lang` (en | hi, default en). English is what the analysis stored and is
returned unchanged; Hindi is rendered from the same stored findings through
config/locales (astra/locale), so both state the same facts and figures.

Full RBAC/audit-logging is a stage-2 roadmap item; tier here is an explicit
path segment so the demo can show all four synthesized framings side by side.
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .. import HUMAN_REVIEW_DISCLAIMER, PLATFORM_NAME, PLATFORM_TAGLINE, __version__
import pandas as pd

from .. import data_contract, db, explain, locale, rbac, synthesis
from ..config import PROCESSED_DIR
from ..explain import AGENT_LABEL

#: languages the case text can be read in; anything else is refused
Lang = Query("en", pattern="^(" + "|".join(locale.LANGUAGES) + ")$",
             description="Language of the case text: " + ", ".join(locale.LANGUAGES))
from ..ingestion import instance_lock

app = FastAPI(title=f"{PLATFORM_NAME} API", description=PLATFORM_TAGLINE, version=__version__)

# The React client is served from a different origin in development and from
# the same origin once built.
#
# A fixed list of dev origins is fragile: Vite increments its port when 5173 is
# busy (5174, 5175, ...), and a preflight from an origin that is not on the list
# makes Starlette reject OPTIONS with "400 Disallowed CORS origin". The browser
# then reports only a generic network failure, which is hard to diagnose.
#
# So in development any loopback origin is accepted, on any port, over http or
# https. Setting ASTRA_CORS_ORIGINS switches to an explicit allow-list, which is
# what a real deployment should do.
_CORS_ENV = os.environ.get("ASTRA_CORS_ORIGINS", "").strip()
_CORS_KWARGS: dict = (
    {"allow_origins": [o.strip() for o in _CORS_ENV.split(",") if o.strip()]}
    if _CORS_ENV
    else {"allow_origin_regex": r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"}
)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    **_CORS_KWARGS,
)


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
def flag_case(flag_id: str, tier: str = "district", lang: str = Lang):
    """One case with its authority-specific brief and all supporting evidence.

    In a language other than English every finding also carries `explained`:
    its headline, plain explanation and guideline clause in that language. The
    finding itself (summary and details) is the audit record and stays as the
    agent wrote it.
    """
    if tier not in ("mp", "district", "state", "ministry"):
        raise HTTPException(400, "tier must be mp | district | state | ministry")
    f = db.get_flag(flag_id)
    if not f:
        raise HTTPException(404, f"no such case: {flag_id}")
    english = lang == locale.DEFAULT
    return {
        "flag_id": f["flag_id"],
        "display_title": explain.display_title(f, lang),
        "entity_id": f["entity_id"],
        "entity_type": f["entity_type"],
        "state": f["state"], "district": f["district"],
        "constituency": f["constituency"], "era": f["era"],
        "risk_score": f["risk_score"], "alert": f["alert"],
        "review_status": f["review_status"],
        "primary_signal": _primary_signal(f, lang),
        "lang": lang,
        "brief": explain.brief_for(f, tier, lang),
        "all_tier_briefs": (f.get("tier_briefs") if english else
                            {t: explain.brief_for(f, t, lang) for t in explain.TIERS}),
        "findings": f["findings"] if english else [
            {**fd, "explained": _explained(fd, lang)} for fd in f["findings"]],
        "narrative": f["narrative"],
        "disclaimer": HUMAN_REVIEW_DISCLAIMER,
    }


def _explained(finding: dict, lang: str) -> dict:
    h = explain.humanize(finding, lang)
    return {"headline": h["headline"], "plain": h["plain"], "metric": h["metric"],
            "benchmark": h["benchmark"], "clause": h["clause"]}


def _primary_signal(f: dict, lang: str) -> str | None:
    if lang == locale.DEFAULT or not f.get("findings"):
        return f.get("primary_signal")
    return explain.build_brief(f, "district", lang=lang)["primary_risk"]


@app.get("/flags/case/{flag_id}/synthesis")
def flag_synthesis(flag_id: str, tier: str = "district", use_llm: bool = True,
                   lang: str = Lang):
    """Authority-specific synthesis with a constrained action plan.

    Always returns a usable body: `source` is "groq" when the LLM produced it
    and "deterministic" when the template path did, with `fallback_reason`
    explaining why. The risk score and level are copied from the deterministic
    pipeline after generation and can never be altered by the model.
    """
    if tier not in rbac.ROLES:
        raise HTTPException(400, f"tier must be one of {list(rbac.ROLES)}")
    f = db.get_flag(flag_id)
    if not f:
        raise HTTPException(404, f"no such case: {flag_id}")
    work = None
    if f["entity_type"] == "work":
        df = db.read_df("works", "work_id = ?", (f["entity_id"],))
        work = df.iloc[0].to_dict() if not df.empty else None
    return synthesis.synthesise_cached(f, tier, work, use_llm=use_llm, lang=lang)


@app.get("/flags/case/{flag_id}/actions")
def flag_actions(flag_id: str, tier: str = "district", lang: str = Lang):
    """The deterministic RBAC allow-list for this case and authority.

    This is the authoritative set the synthesis layer is constrained to; it is
    computed from role, risk level and the evidence actually found, with no
    model involvement.
    """
    if tier not in rbac.ROLES:
        raise HTTPException(400, f"tier must be one of {list(rbac.ROLES)}")
    f = db.get_flag(flag_id)
    if not f:
        raise HTTPException(404, f"no such case: {flag_id}")
    actions = rbac.allowed_actions(tier, f["risk_score"], f["findings"])
    return {
        "flag_id": flag_id,
        "tier": tier,
        "tier_label": rbac.role_label(tier, lang),
        "risk_score": f["risk_score"],
        "evidence_present": sorted(rbac.evidence_flags(f["findings"])),
        "allowed_actions": actions,
        "constraint_notice": rbac.constraint_notice(lang),
    }


@app.get("/meta/llm")
def llm_meta():
    """LLM provider status. The API key is never included in this response."""
    return synthesis.llm_status()


@app.get("/meta/freshness")
def freshness_meta():
    """Per-shard ingestion freshness, with staleness that degrades the figure.

    Deliberately not a single "last updated" label. A slice we have failed to
    read for days must lower what this reports and be named, not be quietly
    dropped from an average that then looks healthy. `stale` carries the
    offenders by place name; `reconciled_pct` counts only slices whose stored
    record count actually matched the portal's own counter for them.
    """
    summary = db.watermark_summary()
    stale = db.stale_shards(min_failures=3)
    registered = summary.get("registered_shards") or 0
    reconciled = summary.get("reconciled") or 0
    last_sweep = db.get_state("last_reconcile_at")
    sweep_age_h = None
    if last_sweep:
        from datetime import datetime, timezone
        stamp = datetime.fromisoformat(last_sweep)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        sweep_age_h = round(
            (datetime.now(timezone.utc) - stamp).total_seconds() / 3600, 1)
    # The safety net not having run is itself a freshness problem: the
    # heartbeat cannot see edits that move no count. Allow a day plus slack.
    sweep_overdue = bool(registered) and (sweep_age_h is None or sweep_age_h > 26)
    # A count that differs from the portal by the tolerated single record is
    # reported, but does not degrade the status: the tolerance exists because
    # that skew is expected, and a permanent warning would be ignored.
    # Parity is the claim that matters most: every slice's stored figures equal
    # the portal's own tiles. A slice that differs degrades the status and is
    # named, even while a re-read is pending.
    parity = db.parity_summary()
    alerts = _read_json(PROCESSED_DIR / "ingest_alerts.json") or {}
    poller = _read_json(PROCESSED_DIR / "poller_status.json") or {}
    # Whether the poller is running is answered by its lock, not by how recent
    # the last check looks: a nightly sweep runs for twenty minutes with no
    # heartbeat, and a crashed poller can leave a recent-looking timestamp.
    running = instance_lock.is_held()
    owner = instance_lock.holder() if running else None
    sweep_started = db.get_state("sweep_started_at") or None
    stores = db.recent_stores(1)
    # Whether the portal is answering, from the poller's circuit breaker. Only
    # meaningful while the poller runs: a stopped poller's last word is history.
    portal = _portal_status(running)
    status = "no data" if not registered else (
        "degraded" if stale or sweep_overdue or parity["exception_count"]
        or (portal and portal["open"]) else "ok")
    return {
        "rolling": _rolling_status(poller, running),
        "analysis": _analysis_status(poller, running),
        "parity": parity,
        "poller_running": running,
        "poller_since": (owner or {}).get("since"),
        "last_check_at": db.get_state("last_heartbeat_at"),
        "poll_interval_seconds": poller.get("poll_interval_seconds"),
        "reconcile_at": poller.get("reconcile_at"),
        "sweep_in_progress": bool(running and sweep_started),
        "sweep_started_at": sweep_started if running else None,
        "last_update": ({"at": stores[0]["fetched_at"],
                         "area": _area_label(stores[0]["source"]),
                         "detail": stores[0]["detail"]} if stores else None),
        "status": status,
        "registered_shards": registered,
        "reconciled_shards": reconciled,
        "reconciled_pct": round(100 * reconciled / registered, 2)
        if registered else None,
        "quarantined": summary.get("quarantined") or 0,
        "count_mismatched": summary.get("mismatched") or 0,
        "never_fetched": summary.get("never_fetched") or 0,
        "last_full_reconciliation": last_sweep,
        "hours_since_reconciliation": sweep_age_h,
        "reconciliation_overdue": sweep_overdue,
        "oldest_shard_fetch": summary.get("oldest_fetch"),
        "newest_shard_fetch": summary.get("newest_fetch"),
        "portal": portal,
        "stale": [{
            "shard_id": s["shard_id"],
            # national | state (a state-level check) | area (a registered slice)
            "scope": s.get("scope", "area"),
            "place": (None if s.get("scope") == "national"
                      else s.get("constituency_name") or s.get("state_name")),
            "state": s.get("state_name"),
            "house": "LS" if s.get("house") == 2 else "RS",
            "consecutive_failures": s.get("consecutive_failures"),
            "stale_since": s.get("stale_since"),
            "last_fetch": s.get("fetched_at"),
            "last_error": s.get("last_error"),
        } for s in stale],
        "alerts_written_at": alerts.get("generated_at"),
        "poller": poller.get("last_cycle"),
    }


@app.get("/meta/ops")
def ops_meta():
    """Unattended running: the supervisor, its processes, backups and alerts.

    Read from `data/processed/ops_state.json`, which the supervisor rewrites
    every minute (`astra.ops`). Empty-handed, not an error, when it has never
    run: the site is then being run by hand.
    """
    from ..ops import backup, state
    from ..ops.supervisor import LOCK_PATH
    ops = state.load()
    supervisor = ops.get("supervisor") or {}
    running = instance_lock.is_held(LOCK_PATH)
    copies = backup.backups()
    return {
        "supervisor_running": running,
        "started_at": supervisor.get("started_at") if running else None,
        "last_tick_at": supervisor.get("last_tick_at"),
        "stopped_at": None if running else supervisor.get("stopped_at"),
        "keep_awake": supervisor.get("keep_awake") if running else None,
        "services": supervisor.get("services") if running else None,
        "conditions": [{"key": k, **v} for k, v in (ops.get("alerts") or {}).items()],
        "last_alert": ops.get("alerts_last_sent"),
        "backup": {**(ops.get("backup") or {}), "kept": len(copies),
                   "newest": copies[0] if copies else None},
    }


def _portal_status(running: bool) -> dict | None:
    """The poller's view of the portal: open breaker, next attempt, last error."""
    raw = db.get_state("portal_status")
    try:
        portal = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(portal, dict):
        return None
    portal["open"] = bool(portal.get("open")) and running
    return portal


def _stamp(value: str | None):
    """An ISO time from the poller's state, or None."""
    from datetime import datetime, timezone
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _rolling_status(poller: dict, running: bool) -> dict | None:
    """The rotation's settings, as the running poller reported them.

    None until a poller has written its status: the settings live in the
    poller's environment, not the API's, so the API does not guess them.
    """
    from datetime import datetime
    from ..ingestion.poller import in_hours, parse_hours
    settings = poller.get("rolling")
    if not settings:
        return None
    hours = parse_hours(settings.get("hours"))
    enabled = bool(settings.get("areas_per_check")) and hours is not None
    return {
        "enabled": enabled,
        "active_now": bool(running and enabled
                           and in_hours(datetime.now().astimezone(), hours)),
        "areas_per_check": settings.get("areas_per_check"),
        "hours": settings.get("hours"),
        "min_age_hours": settings.get("min_age_hours"),
        "last_at": db.get_state("last_rolling_at") or None,
    }


def _analysis_status(poller: dict, running: bool) -> dict:
    """When the risk flags were last recomputed, and whether data has moved since."""
    started = db.get_state("analysis_started_at") or None
    changed = _stamp(db.get_state("data_changed_at"))
    covered = _stamp(db.get_state("analysis_covers_changes_at"))
    return {
        "last_at": db.get_state("last_analysis_at") or None,
        # A marker left by a poller killed mid-run means nothing once the
        # lock is free, exactly as for the sweep.
        "in_progress": bool(running and started),
        "started_at": started if running else None,
        "changes_waiting": bool(changed and (covered is None or covered < changed)),
        "every_minutes": poller.get("analysis_every_minutes"),
    }


def _read_json(path) -> dict | None:
    """A status file the poller rewrites every minute. A read that lands
    mid-write must cost one stale answer, never a 500."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _area_label(source: str | None) -> str:
    """ "eSAKSHI API BHARATPUR(SC) (LS)" -> "BHARATPUR(SC) (LS)" """
    return (source or "").removeprefix("eSAKSHI API ").strip()


@app.get("/meta/recent-updates")
def recent_updates(limit: int = Query(10, ge=1, le=50)):
    """What the portal changed most recently.

    `stores` is the poller's log of slices it re-read because their contents
    changed, which includes newly recommended works. `changes` is the
    field-level history of existing works, old value and new, exactly as
    observed. Neither is an inference.
    """
    return {
        "stores": [{"area": _area_label(s["source"]), "records": s["rows"],
                    "detail": s["detail"], "at": s["fetched_at"]}
                   for s in db.recent_stores(limit)],
        "changes": db.recent_changes(limit),
    }


def _case_summary(f: dict, lang: str = locale.DEFAULT) -> dict:
    """List-view projection of a case. Heavy fields stay out of list payloads."""
    return {
        "flag_id": f["flag_id"],
        "display_title": explain.display_title(f, lang),
        "entity_id": f["entity_id"],
        "entity_type": f["entity_type"],
        "state": f["state"],
        "district": f["district"],
        "constituency": f["constituency"],
        "era": f["era"],
        "risk_score": f["risk_score"],
        "alert": f["alert"],
        "review_status": f["review_status"],
        "primary_signal": _primary_signal(f, lang),
        "agents": sorted({x["agent"] for x in f.get("findings", [])}),
        "rule_ids": sorted({x["rule_id"] for x in f.get("findings", [])}),
        "finding_count": len(f.get("findings", [])),
    }


@app.get("/cases")
def list_cases(
    states: list[str] | None = Query(None),
    districts: list[str] | None = Query(None),
    constituencies: list[str] | None = Query(None),
    entity_types: list[str] | None = Query(None),
    statuses: list[str] | None = Query(None),
    rule_ids: list[str] | None = Query(None),
    min_score: float = 0,
    search: str = "",
    order: str = "risk",
    limit: int = 50,
    offset: int = 0,
    lang: str = Lang,
):
    """Filtered, sorted, paginated case list — wraps db.query_flags/count_flags."""
    filters = dict(states=states, districts=districts,
                   constituencies=constituencies, entity_types=entity_types,
                   statuses=statuses, rule_ids=rule_ids,
                   min_score=min_score, search=search)
    rows = db.query_flags(order=order, limit=min(limit, 200), offset=offset, **filters)
    return {
        "total": db.count_flags(**filters),
        "limit": limit,
        "offset": offset,
        "disclaimer": HUMAN_REVIEW_DISCLAIMER,
        "cases": [_case_summary(f, lang) for f in rows],
    }


@app.get("/stats")
def stats(
    states: list[str] | None = Query(None),
    districts: list[str] | None = Query(None),
    constituencies: list[str] | None = Query(None),
    min_score: float = 0,
):
    """Headline counts and risk-band distribution — wraps db.flag_stats."""
    with db.connect() as con:
        works = con.execute("SELECT COUNT(*) FROM works").fetchone()[0]
        districts_n = con.execute(
            "SELECT COUNT(DISTINCT district) FROM works "
            "WHERE district IS NOT NULL").fetchone()[0]
        states_n = con.execute(
            "SELECT COUNT(DISTINCT state) FROM works "
            "WHERE state IS NOT NULL").fetchone()[0]
    out = db.flag_stats(states=states, districts=districts,
                        constituencies=constituencies, min_score=min_score)
    out["corpus"] = {"works": works, "districts": districts_n, "states": states_n}
    return out


@app.get("/meta/facets")
def facets():
    """Distinct filter values for the UI controls — wraps db.flag_facets."""
    f = db.flag_facets()
    run = {}
    p = PROCESSED_DIR / "run_meta.json"
    if p.exists():
        run = json.loads(p.read_text(encoding="utf-8"))
    f["rules"] = [
        {"rule_id": c["rule_id"], "title": c["title"], "count": c["findings"]}
        for c in run.get("rule_coverage", [])
    ]
    f["agents"] = list(AGENT_LABEL.items())
    return f


@app.get("/analytics/states")
def analytics_states():
    """Per-state flag counts and mean risk — wraps db.state_risk_summary."""
    return db.state_risk_summary().to_dict(orient="records")


@app.get("/analytics/districts")
def analytics_districts(state: str | None = None, limit: int = 25):
    """Per-district rollup — wraps db.district_risk_summary."""
    return db.district_risk_summary(state, limit).to_dict(orient="records")


@app.get("/analytics/detections")
def analytics_detections(
    states: list[str] | None = Query(None),
    districts: list[str] | None = Query(None),
    constituencies: list[str] | None = Query(None),
):
    """Detection-type frequency — wraps db.rule_histogram_scoped."""
    hist = db.rule_histogram_scoped(states=states, districts=districts,
                                    constituencies=constituencies)
    titles = {}
    p = PROCESSED_DIR / "run_meta.json"
    if p.exists():
        titles = {c["rule_id"]: c["title"]
                  for c in json.loads(p.read_text(encoding="utf-8"))
                  .get("rule_coverage", [])}
    return [{"rule_id": k, "title": titles.get(k, k), "count": v}
            for k, v in sorted(hist.items(), key=lambda kv: -kv[1])]


@app.get("/works/{work_id:path}")
def work_record(work_id: str):
    """One work row from the canonical table. Read-only."""
    df = db.read_df("works", "work_id = ?", (work_id,))
    if df.empty:
        raise HTTPException(404, f"no such work: {work_id}")
    row = df.iloc[0].to_dict()
    return {k: (None if pd.isna(v) else v) for k, v in row.items()}


@app.get("/payments")
def payments(work_id: str):
    """One work's payment records, oldest first, each marked with what its dates
    say against the work's record. Read-only.

    A query parameter rather than a path: work codes contain slashes, and
    `/works/{work_id:path}` would take the whole path.
    """
    from ..agents.payments import mark
    rows = pd.DataFrame(db.work_payments(work_id), columns=list(db.PAYMENT_COLUMNS))
    work = db.read_df("works", "work_id = ?", (work_id,))
    marked = mark(rows, work) if not rows.empty else rows
    records = []
    for r in marked.to_dict("records"):
        records.append({k: (None if not isinstance(v, (list, dict)) and pd.isna(v) else
                            (v.item() if hasattr(v, "item") else v))
                        for k, v in r.items()})
    sanctioned = (float(work.iloc[0]["sanctioned_amount"])
                  if not work.empty and pd.notna(work.iloc[0]["sanctioned_amount"]) else None)
    total = float(rows["amount"].sum()) if not rows.empty else 0.0
    return {
        "work_id": work_id,
        "payments": records,
        "summary": {
            "count": len(records),
            "total": total,
            "sanctioned_amount": sanctioned,
            "share_of_sanctioned": round(total / sanctioned, 3) if sanctioned else None,
            "vendors": int(rows["vendor_id"].fillna(rows["vendor_name"]).nunique()) if not rows.empty else 0,
            "in_progress": int((rows["status"] == "Payment In-Progress").sum()) if not rows.empty else 0,
            "first_paid_on": rows["paid_on"].min() if not rows.empty else None,
            "last_paid_on": rows["paid_on"].max() if not rows.empty else None,
            "after_completion": (int(marked["days_after_completion"].notna().sum())
                                 if not rows.empty else 0),
            "repeated": int((marked["repeats"] > 1).sum()) if not rows.empty else 0,
        },
    }


@app.get("/agencies/works")
def agency_works(entity_id: str | None = None, name: str | None = None, limit: int = 40):
    """Works of a network case's actor. Read-only.

    `entity_id` is the case's entity: `vendor:<portal vendor id>` (or
    `vendor:<name>` for a corpus without vendor ids), `agency:<implementing
    agency>`, or a district authority's name. `name` is the older lookup by
    name, kept for callers that have only a name.
    """
    if entity_id:
        kind, sep, key = entity_id.partition(":")
        if sep and kind == "vendor":
            where, params = ("vendor_id = ? OR (vendor_id IS NULL AND UPPER(vendor_name) = ?)",
                             (key, key.upper()))
        elif sep and kind == "agency":
            where, params = "UPPER(implementing_agency) = ?", (key.upper(),)
        else:
            where, params = "ia_name = ?", (entity_id,)
    elif name:
        where, params = ("UPPER(ia_name) = ? OR UPPER(vendor_name) = ? "
                         "OR UPPER(implementing_agency) = ?", (name.upper(),) * 3)
    else:
        raise HTTPException(400, "give entity_id or name")
    df = db.read_df("works", where, params).head(limit)
    cols = ["work_id", "district", "state", "category", "sanctioned_amount",
            "status", "vendor_name", "vendor_id", "implementing_agency", "ia_name"]
    df = df[[c for c in cols if c in df.columns]]
    return [{k: (None if pd.isna(v) else v) for k, v in r.items()}
            for r in df.to_dict(orient="records")]


@app.get("/meta/pipeline")
def pipeline_meta(lang: str = Lang):
    """Router trace + rule coverage from the last pipeline run. Read-only."""
    p = PROCESSED_DIR / "run_meta.json"
    if not p.exists():
        return {"router_trace": [], "rule_coverage": [], "ran_at": None}
    meta = json.loads(p.read_text(encoding="utf-8"))
    if lang == locale.DEFAULT:
        return meta
    unchecked = {why: para for para, why in data_contract.UNCHECKABLE.items()}
    for entry in meta.get("router_trace") or []:
        entry["reason"] = locale.rerender(lang, entry.get("reason"), ("router.ready", "router.skipped"))
    for entry in meta.get("rule_coverage") or []:
        why = entry.get("stood_down")
        if why in unchecked and locale.has(lang, f"unchecked.{unchecked[why]}"):
            entry["stood_down"] = locale.text(lang, f"unchecked.{unchecked[why]}")
        elif why:
            entry["stood_down"] = locale.rerender(
                lang, why, ("stood_down.years", "stood_down.history", "stood_down.no_edits",
                            "stood_down.no_category", "stood_down.no_payments"))
    return meta


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
    """Where the analysed corpus came from, and how current it is.

    Counts come from the database rather than the ingestion note, because on
    the live corpus the poller keeps changing them after ingestion ends. For
    the live corpus the ledger is also built from the database: the stored
    provenance there is the poller's running log (see /meta/recent-updates),
    not a description of the corpus. A CSV batch keeps its own ledger.
    """
    meta = _read_json(PROCESSED_DIR / "ingest_meta.json") or {}
    db.init_db()
    with db.connect() as con:
        work_rows = con.execute(
            "SELECT era, house, COUNT(*) AS n FROM works GROUP BY era, house").fetchall()
        flow_rows = con.execute(
            "SELECT source, era, COUNT(*) AS n FROM fundflows "
            "GROUP BY source, era").fetchall()

    def tally(rows, key):
        out: dict[str, int] = {}
        for r in rows:
            out[r[key] or "unknown"] = out.get(r[key] or "unknown", 0) + r["n"]
        return out

    houses = tally(work_rows, "house")
    flow_sources = tally(flow_rows, "source")
    live = meta.get("mode_resolved") == "api"
    if live:
        ledger = _live_ledger(houses, flow_sources, meta.get("ingested_at"))
    else:
        prov = db.load_provenance()
        ledger = prov.to_dict(orient="records") if not prov.empty else []

    return {
        "mode_requested": meta.get("mode_requested"),
        "mode_resolved": meta.get("mode_resolved"),
        "live": live,
        "works": sum(r["n"] for r in work_rows),
        "houses": houses,
        "fundflows": sum(r["n"] for r in flow_rows),
        "work_eras": tally(work_rows, "era"),
        "fundflow_eras": tally(flow_rows, "era"),
        "freshness_vs_live_portal": meta.get("freshness"),
        "live_portal_totals": meta.get("live_tiles"),
        "ingested_at": meta.get("ingested_at"),
        "provenance": ledger,
    }


def _live_ledger(houses: dict, flow_sources: dict, built_at: str | None) -> list[dict]:
    """One row per real source of the live corpus, with its current health."""
    health = db.shard_summary_by_house()
    rows = []
    for code, key, label, unit in ((2, "LS", "Lok Sabha", "constituencies"),
                                   (1, "RS", "Rajya Sabha", "states")):
        h = health.get(code)
        if not h:
            continue
        trouble = h["quarantined"] + h["stale"]
        rows.append({
            "source": f"eSAKSHI portal — {label}",
            "mode": "api", "table_name": "works",
            "rows": houses.get(key, 0),
            "status": "attention" if trouble else "ok",
            "detail": (f"{h['shards']} {unit} read record by record from "
                       f"mplads.mospi.gov.in; {h['exact']} match the portal on every "
                       f"figure (works recommended, sanctioned and completed, and "
                       f"expenditure, in count and rupees)"
                       + (f"; {trouble} need attention" if trouble else "")),
            "fetched_at": h["newest_fetch"],
        })
    for source, n in sorted(flow_sources.items()):
        if source == "esakshi_api":
            rows.append({
                "source": "Fund positions from eSAKSHI work records",
                "mode": "derived", "table_name": "fundflows", "rows": n,
                "status": "ok",
                "detail": ("Aggregated per MP and financial year from the live work "
                           "records when the corpus was built"),
                "fetched_at": built_at,
            })
        else:
            rows.append({
                "source": f"data.opencity.in — {source}",
                "mode": "open data", "table_name": "fundflows", "rows": n,
                "status": "ok",
                "detail": "Pre-2023 fund positions from the OpenCity CKAN datastore",
                "fetched_at": None,
            })
    return rows


@app.get("/meta/router-trace")
def router_trace():
    p = PROCESSED_DIR / "run_meta.json"
    if not p.exists():
        raise HTTPException(404, "pipeline has not run yet")
    return json.loads(p.read_text(encoding="utf-8"))
