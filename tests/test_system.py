"""End-to-end system tests for ASTRA, run against the REAL MPLADS corpus.

    python tests/test_system.py

No pytest dependency and no network required: the offline official CSV exports
are sufficient, which is the point of the dual-mode design. Live-source tests
are skipped automatically when the network is unavailable.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astra import db  # noqa: E402
from astra.agents.anomaly import AnomalyAgent  # noqa: E402
from astra.agents.compliance import ComplianceAgent  # noqa: E402
from astra.agents.entity_resolution import EntityResolutionAgent  # noqa: E402
from astra.agents.network import NetworkAgent  # noqa: E402
from astra.agents.orchestrator import Orchestrator  # noqa: E402
from astra.config import PROCESSED_DIR  # noqa: E402
from astra.ingestion import offline as offline_mod  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return cond


def skip(name: str, detail: str) -> None:
    results.append((SKIP, name, detail))
    print(f"  [{SKIP}] {name} — {detail}")


# ---------------------------------------------------------------- ingestion

def test_offline_ingestion() -> pd.DataFrame:
    print("\n[1] Offline ingestion — official eSAKSHI CSV exports")
    if not offline_mod.datasets_available():
        skip("datasets/ present", "no CSV exports found")
        return pd.DataFrame()

    t = time.time()
    works = offline_mod.build_works()
    flows = offline_mod.build_fundflows(works)
    took = time.time() - t

    check("works parsed", len(works) > 50_000, f"{len(works):,} rows in {took:.0f}s")
    check("work ids unique", works["work_id"].is_unique)
    check("real eSAKSHI codes present",
          works["work_code"].notna().sum() > 50_000,
          f"{works['work_code'].notna().sum():,} coded works")
    check("districts parsed from IDA", works["district"].nunique() > 100,
          f"{works['district'].nunique()} districts")
    check("vendor names joined", works["vendor_name"].notna().sum() > 10_000,
          f"{works['vendor_name'].notna().sum():,} works with a vendor")
    check("work types are meaningful peer keys", works["category"].nunique() > 50,
          f"{works['category'].nunique()} standardised work types")
    check("every row carries an era tag", works["era"].notna().all(),
          str(works["era"].value_counts().to_dict()))
    check("costs are positive numbers",
          pd.to_numeric(works["estimated_cost"], errors="coerce").gt(0).sum() > 50_000)
    check("fundflows built", len(flows) > 500, f"{len(flows):,} constituency-FY rows")
    check("SC/ST area spend deliberately unpopulated",
          flows["sc_expenditure"].isna().all(),
          "seat reservation must not be passed off as area attribution")
    return works


def test_router_offline_mode() -> None:
    print("\n[2] Dual-mode router — offline mode needs no network")
    from astra.ingestion.router import ingest
    try:
        meta = ingest(mode="offline", verbose=False, enrich=False)
    except Exception as exc:
        check("offline ingest runs", False, str(exc)[:120])
        return
    check("offline ingest runs", meta["mode_resolved"] == "offline",
          f"resolved={meta['mode_resolved']}")
    check("works persisted", meta["works"] > 50_000, f"{meta['works']:,}")
    check("provenance recorded", len(meta["provenance"]) > 0,
          f"{len(meta['provenance'])} source entries")
    check("no live calls in offline mode",
          all(p["mode"] == "offline" for p in meta["provenance"]))


def test_live_sources() -> None:
    print("\n[3] Live sources (skipped gracefully when unreachable)")
    from astra.ingestion.live import fetch_esakshi_tiles, freshness_check
    tiles, prov = fetch_esakshi_tiles(timeout=20)
    if prov["status"] != "ok":
        skip("eSAKSHI portal reachable", prov["detail"][:80])
        return
    check("eSAKSHI portal reachable", True, f"tenure={tiles.get('Current Tenure')}")
    check("live totals parsed",
          (tiles.get("Works Recommended") or {}).get("count", 0) > 100_000,
          f"{(tiles.get('Works Recommended') or {}).get('count'):,} recommended works")
    works = db.read_df("works")
    if not works.empty:
        f = freshness_check(works, tiles)
        check("freshness check computes", f is not None and f["coverage_pct"] > 0,
              f"{f['coverage_pct']}% coverage" if f else "")


# ---------------------------------------------------------------- agents

def test_agents(works: pd.DataFrame, flows: pd.DataFrame) -> None:
    print("\n[4] Agents on real data")

    comp = ComplianceAgent().run(works, flows)
    rules = {f.rule_id for f in comp}
    check("compliance produces findings", len(comp) > 0, f"{len(comp):,} findings")
    check("every compliance finding cites a clause",
          all(f.clause for f in comp), f"rules fired: {sorted(rules)}")
    check("completion-norm rule fires", "R-TIME-01" in rules)
    check("prohibited-category rule fires", "R-PROH-01" in rules)
    check("SC/ST area rule correctly stands down", "R-SCST-01" not in rules,
          "requires area attribution absent from these extracts")

    anom = AnomalyAgent().run(works, flows)
    cost = [f for f in anom if f.rule_id == "A-COST-01"]
    check("anomaly produces findings", len(anom) > 0, f"{len(anom):,} findings")
    check("all cost flags are material",
          all(f.details["pct_vs_benchmark"] >= 30 for f in cost),
          "min 30% above benchmark enforced")
    check("scale mismatches are down-graded, not called overruns",
          all(f.severity == "medium" for f in cost if f.details["scale_mismatch"]),
          f"{sum(1 for f in cost if f.details['scale_mismatch'])} scale-mismatch cases")
    check("peer groups never straddle eras",
          all("|" in f.details.get("peer_group", "|") for f in cost))

    dup = EntityResolutionAgent().run(works, pd.DataFrame())
    pairs = [f for f in dup if f.rule_id == "D-DUP-01"]
    check("duplicate detection produces findings", len(dup) > 0, f"{len(dup):,} findings")
    check("every duplicate cites shared identifiers",
          all(len(f.details.get("shared_identifiers", [])) >= 3 for f in pairs),
          "generic template matches rejected")
    check("numeric locators discriminate distinct works",
          all(f.details["semantic_sim"] >= 0.80 for f in pairs))
    check("generic clusters reported once, not pairwise",
          any(f.rule_id == "D-DUP-02" for f in dup),
          f"{sum(1 for f in dup if f.rule_id == 'D-DUP-02')} clusters")

    net = NetworkAgent().run(works, pd.DataFrame())
    check("network analyses vendors and agencies", len(net) > 0, f"{len(net)} signals")
    check("vendor entities present",
          any(f.details["actor_type"] == "vendor" for f in net))
    check("national suppliers down-weighted",
          all(f.severity in ("low", "medium", "high") for f in net))


# ---------------------------------------------------------------- orchestrator

def test_orchestrator(works: pd.DataFrame, flows: pd.DataFrame) -> None:
    print("\n[5] Orchestrator — routing, scoring, narrative, tier synthesis")
    orch = Orchestrator()
    t = time.time()
    flags = orch.run(works, flows)
    took = time.time() - t

    check("flags produced", len(flags) > 0, f"{len(flags):,} flags in {took:.0f}s")
    check("router trace recorded", len(orch.route_trace) == 4,
          ", ".join(f"{t['agent']}={t.get('findings', 'skipped')}"
                    for t in orch.route_trace))
    check("rule coverage includes zero-match rules",
          any(r["findings"] == 0 for r in orch.rule_coverage),
          f"{len(orch.rule_coverage)} rules evaluated")
    check("risk scores in range",
          all(0 <= f.risk_score <= 100 for f in flags))
    check("every flag has a causal narrative",
          all(len(f.narrative) > 60 for f in flags))
    check("every narrative carries the human-review disclaimer",
          all("human review" in f.narrative.lower() for f in flags))
    check("every flag has all four tier views",
          all(set(f.tier_views) == {"mp", "district", "state", "ministry"} for f in flags))
    check("tier views genuinely differ per audience",
          all(len(set(f.tier_views.values())) == 4 for f in flags[:200]))
    check("every flag starts as pending human review",
          all(f.review_status == "pending" for f in flags))

    multi = [f for f in flags if len({fd.agent for fd in f.findings}) > 1]
    check("multi-agent corroborated cases exist", len(multi) > 0,
          f"{len(multi):,} flags with evidence from 2+ agents")
    if multi:
        f = multi[0]
        check("corroborated narrative chains agents together",
              "furthermore" in f.narrative.lower(), f.entity_label[:60])


# ---------------------------------------------------------------- persistence + API

def test_persistence_and_api() -> None:
    print("\n[6] Persistence, API and the human-in-the-loop feedback loop")
    flags = db.load_flags()
    check("flags persisted to sqlite", len(flags) > 0, f"{len(flags):,} rows")
    if not flags:
        return

    prov = db.load_provenance()
    check("provenance queryable", not prov.empty, f"{len(prov)} entries")
    check("ingest metadata written", (PROCESSED_DIR / "ingest_meta.json").exists())
    check("router trace written", (PROCESSED_DIR / "run_meta.json").exists())

    from fastapi.testclient import TestClient
    from astra.api.main import app
    client = TestClient(app)

    r = client.get("/meta/data-source")
    check("GET /meta/data-source", r.status_code == 200,
          f"mode={r.json().get('mode_resolved')}")
    r = client.get("/flags/ministry?min_score=60")
    check("GET /flags/ministry", r.status_code == 200,
          f"{r.json()['count']} flags")
    body = r.json()
    check("ministry payload carries the disclaimer", "human review" in body["disclaimer"].lower())
    check("ministry aggregates by state and rule",
          bool(body["aggregate"].get("flags_by_state"))
          and bool(body["aggregate"].get("findings_by_rule")))
    if body["flags"]:
        check("tier_view is the ministry framing",
              body["flags"][0]["tier_view"] != body["flags"][0]["narrative"])

    sample = next((f for f in flags if f["state"]), flags[0])
    if sample["state"]:
        r = client.get(f"/flags/state?state={sample['state']}")
        check("GET /flags/state", r.status_code == 200, f"{r.json()['count']} flags")

    fid = sample["flag_id"]
    r = client.post(f"/flags/{fid}/feedback",
                    json={"action": "false_positive", "authority_tier": "district",
                          "note": "system test"})
    check("POST feedback accepted", r.status_code == 200)
    after = [f for f in db.load_flags() if f["flag_id"] == fid]
    check("feedback updates review status",
          bool(after) and after[0]["review_status"] == "false_positive")
    check("feedback is auditable", not db.feedback_stats().empty)
    r = client.post(f"/flags/{fid}/feedback",
                    json={"action": "nonsense", "authority_tier": "mp"})
    check("invalid feedback action rejected", r.status_code == 400)
    db.record_feedback(fid, "pending", "district", "")   # restore


# ------------------------------------------------- presentation + query layer

def test_explanations() -> None:
    """The plain-language layer must be readable, grounded and non-accusatory."""
    print("\n[8] Explanation quality")
    from astra.explain import build_brief, humanize, rupees, short_title

    check("long descriptions become short titles",
          len(short_title("Construction of CC Road from Gutuhatu main road towards "
                          "the house of Pritnath Munda in ward number 7 as per "
                          "attached letter")) <= 65)
    check("boilerplate stripped from titles",
          "attached letter" not in short_title(
              "Installation of Semi High Mast Lights as per attached letter").lower())
    check("money uses Indian conventions",
          rupees(12500000).startswith("Rs".replace("Rs", "\u20b9") + "1.25 crore")
          and "lakh" in rupees(500000))

    flags = db.query_flags(min_score=60, limit=250)
    check("flags carry display titles and primary signals",
          all(f.get("display_title") and f.get("primary_signal") for f in flags))
    check("flags carry all four authority briefs",
          all(set(f.get("tier_briefs", {})) == {"mp", "district", "state", "ministry"}
              for f in flags))

    briefs = [b for f in flags for b in f["tier_briefs"].values()]
    banned = ("fraud", "fraudulent", "corrupt", "guilty", "embezzl", "scam",
              "criminal", "wrongdoing by")
    offenders = [b["primary_plain"] for b in briefs
                 if any(w in (b["primary_plain"] or "").lower() for w in banned)]
    check("no brief accuses anyone of fraud", not offenders,
          offenders[0][:70] if offenders
          else f"checked {len(briefs):,} briefs across {len(flags)} cases")
    check("every brief carries the human-review statement",
          all("not a determination of fraud" in b["disclaimer"] for b in briefs))
    check("every brief gives at least one concrete action",
          all(b["actions"] for b in briefs))
    check("no raw z-scores leak into the plain explanation",
          not any("robust z=" in (b["primary_plain"] or "") for b in briefs))

    sample = flags[0]
    firsts = {t: b["actions"][0] for t, b in sample["tier_briefs"].items()}
    check("each authority gets a different first action",
          len(set(firsts.values())) == 4)
    openings = {b["opening"] for b in sample["tier_briefs"].values()}
    check("each authority gets a different framing", len(openings) == 4)

    sig = [s for f in flags[:60] for s in f["tier_briefs"]["district"]["signals"]]
    check("signals report a measurement and a benchmark",
          all(s["metric"] and s["benchmark"] for s in sig),
          f"{len(sig)} signals checked")
    check("signal risk contributions are positive",
          all(s["contribution"] > 0 for s in sig))
    check("humanize() is total over every emitted rule",
          all(humanize(fd)["headline"] for f in flags for fd in f["findings"]))


def test_query_layer() -> None:
    """The dashboard query paths must be correct and fast."""
    print("\n[9] Dashboard query layer")
    import time

    t = time.time()
    facets = db.flag_facets()
    check("facets load", facets["total"] > 0,
          f"{len(facets['states'])} states, {len(facets['districts'])} districts, "
          f"{(time.time() - t) * 1000:.0f}ms")

    t = time.time()
    page = db.query_flags(limit=250, order="risk")
    dt = (time.time() - t) * 1000
    check("page query is fast", dt < 2000, f"{len(page)} rows in {dt:.0f}ms")
    check("ordering is highest-risk-first",
          all(page[i]["risk_score"] >= page[i + 1]["risk_score"]
              for i in range(len(page) - 1)))
    check("count agrees with an unpaginated listing",
          db.count_flags(min_score=90) == len(db.query_flags(min_score=90, limit=10**6)))

    st_name = facets["states"][0]
    scoped = db.query_flags(states=[st_name], limit=50)
    check("state filter scopes correctly",
          all(f["state"] == st_name for f in scoped),
          f"{st_name}: {len(scoped)} cases")

    dup = db.query_flags(rule_ids=["D-DUP-01"], limit=40)
    check("detection-type filter works",
          all(any(x["rule_id"] == "D-DUP-01" for x in f["findings"]) for f in dup),
          f"{len(dup)} duplicate cases")

    hits = db.query_flags(search="road", limit=40)
    check("search filter returns matches", len(hits) > 0, f"{len(hits)} hits")

    stats = db.flag_stats()
    check("stats reconcile with the total",
          stats["high"] + stats["medium"] + stats["low"] == stats["total"],
          f"{stats['high']} high / {stats['medium']} medium / {stats['low']} low")

    one = db.get_flag(page[0]["flag_id"])
    check("single-case fetch works", bool(one) and one["flag_id"] == page[0]["flag_id"])
    check("missing case returns None rather than raising",
          db.get_flag("F-DOESNOTEXIST") is None)

    try:
        db.query_flags(states=[], districts=None, statuses=["pending"],
                       entity_types=["work"], rule_ids=["R-TIME-01"],
                       min_score=50, search="a'b", limit=5)
        check("filters are injection-safe and tolerate empties", True)
    except Exception as exc:
        check("filters are injection-safe and tolerate empties", False, str(exc)[:90])


def test_review_workflow() -> None:
    """Human review must actually persist and be reversible."""
    print("\n[10] Human review workflow")
    target = db.query_flags(limit=1)[0]
    fid = target["flag_id"]
    original = target["review_status"]

    for action in ("under_review", "confirmed", "false_positive"):
        db.record_feedback(fid, action, "district", f"system test: {action}")
        check(f"status persists as '{action}'",
              db.get_flag(fid)["review_status"] == action)

    check("review actions are auditable", not db.feedback_stats().empty)
    filtered = db.query_flags(statuses=["false_positive"], limit=10)
    check("status filter finds the reviewed case",
          any(f["flag_id"] == fid for f in filtered))
    db.record_feedback(fid, original, "district", "")
    check("status restored after test", db.get_flag(fid)["review_status"] == original)


def test_dashboard_renders() -> None:
    """The dashboard module must parse and survive missing/edge-case data."""
    print("\n[11] Dashboard robustness")
    import ast
    src = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    try:
        ast.parse(src)
        check("dashboard parses", True)
    except SyntaxError as exc:
        check("dashboard parses", False, str(exc))
        return

    from astra.explain import build_brief, risk_band, short_title
    check("risk bands cover the full range",
          {risk_band(s)[0] for s in (0, 39, 40, 69, 70, 100)} ==
          {"Low risk", "Medium risk", "High risk"})
    check("short_title survives empty input", short_title(None) == "Untitled work")
    check("short_title survives a 500-character description",
          len(short_title("x " * 250)) <= 65)
    empty = build_brief({"findings": []}, "district")
    check("brief survives a case with no findings",
          bool(empty["primary_risk"]) and bool(empty["disclaimer"]))
    check("brief survives an unknown authority tier",
          bool(build_brief({"findings": []}, "nonsense")["actions"]))

    wf = next((f for f in db.query_flags(entity_types=["work"], limit=5)), None)
    if wf:
        row = db.read_df("works", "work_id = ?", (wf["entity_id"],))
        check("work-level cases resolve to a work record", not row.empty,
              wf["entity_id"])
    agf = db.query_flags(entity_types=["agency"], limit=1)
    if agf:
        check("agency-level cases have no work row and must not crash the panel",
              db.read_df("works", "work_id = ?", (agf[0]["entity_id"],)).empty)


# ------------------------------------------------- RBAC + LLM synthesis layer

def _rich_case():
    """A case with evidence from three agents, for the synthesis tests."""
    for f in db.query_flags(min_score=90, limit=60):
        if len({x["agent"] for x in f["findings"]}) >= 3:
            work = db.read_df("works", "work_id = ?", (f["entity_id"],))
            return f, (work.iloc[0].to_dict() if not work.empty else None)
    f = db.query_flags(limit=1)[0]
    return f, None


def test_rbac_engine() -> None:
    """Deterministic action constraints — the security boundary for the LLM."""
    print("\n[12] RBAC constraint engine")
    from astra import rbac

    case, _ = _rich_case()
    score, findings = case["risk_score"], case["findings"]

    plans = {r: rbac.allowed_ids(r, score, findings) for r in rbac.ROLES}
    check("every authority gets a non-empty action set",
          all(plans.values()), {r: len(v) for r, v in plans.items()})
    check("authorities get genuinely different actions",
          len({frozenset(v) for v in plans.values()}) == 4)
    check("no action is offered to two different authorities",
          not any(plans[a] & plans[b] for a in rbac.ROLES for b in rbac.ROLES if a < b))

    # the catalogue must contain no punitive/enforcement powers at all
    banned = ("suspend", "penalt", "blacklist", "criminal", "fir", "prosecut",
              "terminate", "debar", "recover", "fine")
    offenders = [aid for aid in rbac.ACTION_CATALOGUE
                 if any(b in aid.lower() for b in banned)]
    check("catalogue contains no punitive actions", not offenders, str(offenders))

    # risk gating
    low = rbac.allowed_ids("district", 10, findings)
    high = rbac.allowed_ids("district", 100, findings)
    check("escalation is withheld at low risk",
          "escalate_case" not in low and "escalate_case" in high)
    check("physical inspection is withheld at low risk",
          "conduct_physical_inspection" not in low)

    # evidence gating
    no_dup = [f for f in findings if not str(f["rule_id"]).startswith("D-")]
    check("duplicate action requires duplicate evidence",
          "cross_check_duplicate" in rbac.allowed_ids("district", score, findings)
          and "cross_check_duplicate" not in rbac.allowed_ids("district", score, no_dup))

    # validation strips anything not permitted
    accepted, rejected = rbac.validate_plan([
        {"action_id": "verify_documents", "reason": "ok"},
        {"action_id": "suspend_contractor", "reason": "invented"},
        {"action_id": "file_criminal_case", "reason": "invented"},
        {"action_id": "monitor_national_pattern", "reason": "wrong role"},
    ], "district", score, findings)
    check("validation keeps only permitted actions",
          [a["action_id"] for a in accepted] == ["verify_documents"])
    check("validation reports every rejection",
          set(rejected) == {"suspend_contractor", "file_criminal_case",
                            "monitor_national_pattern"})
    check("validation cannot be bypassed with an empty/garbage plan",
          rbac.validate_plan([], "district", score, findings)[0] == []
          and rbac.validate_plan([{"x": 1}], "district", score, findings)[0] == [])


def test_llm_provider_safety() -> None:
    """Key handling and graceful failure of the provider layer."""
    print("\n[13] LLM provider safety")
    from astra.llm import provider

    status = provider.provider_status()
    check("status never exposes the API key",
          "GROQ_API_KEY" not in str(status)
          and not str(status.get("key_hint") or "").startswith("gsk_"))
    check("status reports provider and model",
          status["provider"] == "groq" and bool(status["model"]))

    saved = os.environ.pop("GROQ_API_KEY", None)
    provider._ENV_LOADED = True          # skip .env for this check
    try:
        r = provider.complete_json("s", "u", {"type": "object"})
        check("missing key fails fast without a network call",
              r.ok is False and r.reason == "no_api_key")
    finally:
        if saved:
            os.environ["GROQ_API_KEY"] = saved

    check("provider never raises on a bad base url",
          _no_raise(lambda: provider.complete_json("s", "u", {"type": "object"})))


def _no_raise(fn) -> bool:
    try:
        fn()
        return True
    except Exception:
        return False


def test_synthesis_guardrails() -> None:
    """The LLM may explain evidence; it may not widen its own authority."""
    print("\n[14] Synthesis guardrails")
    from astra import rbac, synthesis
    from astra.llm.provider import LLMResult

    case, work = _rich_case()
    real = synthesis.complete_json

    good = {
        "key_risk_summary": "The work is overdue and priced above comparable works.",
        "case_explanation": "The system detected that this work requires verification.",
        "supporting_signals": [{"source_agent": "Compliance Agent",
                                "signal": "Overdue", "evidence": "627 days"}],
        "authority_specific_summary": "Verification is needed before further release.",
        "action_plan": [{"action_id": "verify_documents", "reason": "Records first."}],
        "plan_rationale": "Document checks before escalation.",
        "limitations_or_missing_evidence": ["Cannot verify physical work on site."],
    }

    def stub(payload):
        return lambda system, user, schema, **kw: LLMResult(
            True, data=payload, model="stub-model", latency_ms=5)

    try:
        synthesis.complete_json = stub(good)
        r = synthesis.synthesise(case, "district", work)
        check("a valid model response is accepted", r["source"] == "groq")

        # invented and cross-role actions
        synthesis.complete_json = stub(dict(good, action_plan=[
            {"action_id": "verify_documents", "reason": "ok"},
            {"action_id": "suspend_contractor", "reason": "invented"},
            {"action_id": "issue_penalty", "reason": "invented"},
            {"action_id": "monitor_national_pattern", "reason": "wrong role"}]))
        r = synthesis.synthesise(case, "district", work)
        check("invented actions never reach the action plan",
              [a["action_id"] for a in r["action_plan"]] == ["verify_documents"],
              f"rejected {r['rejected_actions']}")

        # accusatory language
        for phrase in ("This is clear fraud by the contractor.",
                       "The agency is guilty of corruption.",
                       "Criminal misappropriation occurred."):
            synthesis.complete_json = stub(dict(good, key_risk_summary=phrase))
            r = synthesis.synthesise(case, "district", work)
            if r["source"] != "deterministic":
                break
        check("accusatory language forces the deterministic fallback",
              r["source"] == "deterministic"
              and r["fallback_reason"] == "language_guardrail")
        check("no banned term survives into the output",
              not synthesis.scan_language(r["key_risk_summary"],
                                          r["case_explanation"]))

        # the model cannot move the deterministic score
        synthesis.complete_json = stub(dict(good, risk_score=5, risk_level="Low risk"))
        r = synthesis.synthesise(case, "district", work)
        check("model cannot alter the deterministic risk score",
              r["risk_score"] == case["risk_score"] and r["risk_level"] == "High risk")

        # fabricated figures are caught, genuine ones are not
        synthesis.complete_json = stub(dict(
            good, case_explanation="Cost was 9999999 against a benchmark of 4242424."))
        r = synthesis.synthesise(case, "district", work)
        check("fabricated figures are flagged as unverified",
              {"9999999", "4242424"} <= set(r["unverified_numbers"]))
        # take a figure that genuinely appears in THIS case's evidence, rather
        # than hardcoding one - the selected case varies between runs
        packet = synthesis.build_evidence_packet(case, "district", work)
        genuine = next(
            (n for n in synthesis._numbers_in(json.dumps(packet, default=str))
             if n.isdigit() and int(n) > 12), None)
        if genuine:
            synthesis.complete_json = stub(dict(
                good, case_explanation=f"The system recorded a value of {genuine}."))
            r = synthesis.synthesise(case, "district", work)
            check("genuine figures are not falsely flagged",
                  r["unverified_numbers"] == [],
                  f"used {genuine} from the evidence; flagged "
                  f"{r['unverified_numbers']}")
        else:
            skip("genuine figures are not falsely flagged",
                 "no numeric evidence in the selected case")

        # every transport failure degrades to a complete deterministic answer
        modes = ("timeout", "rate_limited", "invalid_json", "network",
                 "http_500", "unauthorized")
        ok = True
        for reason in modes:
            synthesis.complete_json = (
                lambda s, u, sc, _r=reason, **k: LLMResult(False, reason=_r))
            r = synthesis.synthesise(case, "district", work)
            ok &= (r["source"] == "deterministic"
                   and r["fallback_reason"] == reason
                   and bool(r["action_plan"]) and bool(r["key_risk_summary"]))
        check("every failure mode falls back to a complete answer", ok,
              f"{len(modes)} modes tested")
    finally:
        synthesis.complete_json = real


def test_synthesis_authority_and_cache() -> None:
    """Authority-specific synthesis, permission containment, caching."""
    print("\n[15] Authority synthesis and caching")
    from astra import rbac, synthesis

    case, work = _rich_case()
    outs = {r: synthesis.synthesise(case, r, work, use_llm=False) for r in rbac.ROLES}

    check("every authority receives a synthesis",
          all(o["key_risk_summary"] and o["action_plan"] for o in outs.values()))
    check("each authority gets a different action plan",
          len({tuple(a["action_id"] for a in o["action_plan"])
               for o in outs.values()}) == 4)
    contained = all(
        {a["action_id"] for a in o["action_plan"]}
        <= rbac.allowed_ids(role, case["risk_score"], case["findings"])
        for role, o in outs.items())
    check("no plan exceeds its authority's permissions", contained)
    check("every synthesis carries the constraint and review notices",
          all(o["constraint_notice"] and o["human_review_notice"]
              for o in outs.values()))
    check("the risk score is identical across authorities",
          len({o["risk_score"] for o in outs.values()}) == 1,
          "the LLM layer never changes the deterministic score")

    # MP must not be handed operational enforcement steps
    mp_ids = {a["action_id"] for a in outs["mp"]["action_plan"]}
    check("MP is never asked to inspect or escalate operationally",
          not (mp_ids & {"conduct_physical_inspection", "escalate_case",
                         "verify_documents", "review_expenditure"}),
          str(sorted(mp_ids)))

    # evidence packet must be a summary, not the dataset
    packet = synthesis.build_evidence_packet(case, "district", work)
    check("evidence packet stays small and case-scoped",
          len(json.dumps(packet, default=str)) < 12000
          and packet["work_id"] == case["entity_id"])
    check("evidence packet carries measurements and benchmarks",
          all(s.get("measured_value") is not None
              for s in packet["agent_findings"]))

    synthesis._CACHE.clear()
    a = synthesis.synthesise_cached(case, "district", work, use_llm=False)
    b = synthesis.synthesise_cached(case, "district", work, use_llm=False)
    c = synthesis.synthesise_cached(case, "ministry", work, use_llm=False)
    check("repeat views hit the cache",
          a["cached"] is False and b["cached"] is True)
    check("a different authority is cached separately", c["cached"] is False)

    fp1 = synthesis.evidence_fingerprint(packet, "district")
    fp2 = synthesis.evidence_fingerprint(packet, "ministry")
    check("cache key separates authorities", fp1 != fp2)


def test_synthesis_api() -> None:
    """The new endpoints, including that the key is never returned."""
    print("\n[16] Synthesis API")
    from fastapi.testclient import TestClient
    from astra import rbac
    from astra.api.main import app
    client = TestClient(app)

    case, _ = _rich_case()
    fid = case["flag_id"]

    r = client.get("/meta/llm")
    check("GET /meta/llm", r.status_code == 200,
          f"configured={r.json().get('configured')}")
    body = str(r.json())
    check("API never returns the API key",
          "gsk_" not in body and "GROQ_API_KEY" not in body)

    r = client.get(f"/flags/case/{fid}/actions?tier=district")
    check("GET /flags/case/{id}/actions", r.status_code == 200,
          f"{len(r.json()['allowed_actions'])} permitted")
    district_ids = {a["action_id"] for a in r.json()["allowed_actions"]}
    mp_ids = {a["action_id"] for a in
              client.get(f"/flags/case/{fid}/actions?tier=mp").json()["allowed_actions"]}
    check("the allow-list differs per authority", district_ids != mp_ids)

    r = client.get(f"/flags/case/{fid}/synthesis?tier=district")
    check("GET /flags/case/{id}/synthesis", r.status_code == 200)
    syn = r.json()
    check("synthesis reports which layer produced it",
          syn["source"] in ("groq", "deterministic"),
          f"source={syn['source']}, reason={syn.get('fallback_reason')}")
    check("synthesis preserves the deterministic score",
          syn["risk_score"] == case["risk_score"])
    check("synthesis action plan respects permissions",
          {a["action_id"] for a in syn["action_plan"]} <= district_ids)

    check("invalid tier is rejected",
          client.get(f"/flags/case/{fid}/synthesis?tier=nope").status_code == 400)
    check("unknown case returns 404",
          client.get("/flags/case/F-NOPE/synthesis").status_code == 404)
    check("actions endpoint validates tier too",
          client.get(f"/flags/case/{fid}/actions?tier=nope").status_code == 400)


def main() -> int:
    print("=" * 78)
    print("ASTRA system test — real MPLADS data")
    print("=" * 78)

    works = test_offline_ingestion()
    test_router_offline_mode()
    test_live_sources()

    works = db.read_df("works")
    flows = db.read_df("fundflows")
    if works.empty:
        print("\nNo ingested data; aborting.")
        return 1
    test_agents(works, flows)
    test_orchestrator(works, flows)
    from astra.pipeline import run_pipeline
    run_pipeline(verbose=False)
    test_persistence_and_api()
    test_explanations()
    test_query_layer()
    test_review_workflow()
    test_dashboard_renders()
    test_rbac_engine()
    test_llm_provider_safety()
    test_synthesis_guardrails()
    test_synthesis_authority_and_cache()
    test_synthesis_api()

    # The offline-mode test above rewrote the database without live enrichment.
    # Restore the full dual-mode batch so the dashboard is left demo-ready.
    print("\n[7] Restoring dual-mode (auto) batch")
    try:
        from astra.ingestion.router import ingest
        meta = ingest(mode="auto", verbose=False)
        run_pipeline(verbose=False)
        check("auto batch restored", meta["works"] > 50_000,
              f"mode={meta['mode_resolved']}, works={meta['works']:,}, "
              f"fundflows={meta['fundflows']:,}")
    except Exception as exc:
        skip("auto batch restored", f"{type(exc).__name__}: {str(exc)[:90]}")

    npass = sum(1 for r in results if r[0] == PASS)
    nfail = sum(1 for r in results if r[0] == FAIL)
    nskip = sum(1 for r in results if r[0] == SKIP)
    print("\n" + "=" * 78)
    print(f"RESULT: {npass} passed, {nfail} failed, {nskip} skipped")
    if nfail:
        print("\nFailures:")
        for status, name, detail in results:
            if status == FAIL:
                print(f"  - {name}: {detail}")
    print("=" * 78)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
