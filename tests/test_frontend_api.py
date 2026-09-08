"""Contract tests for the endpoints the React frontend consumes.

Run against the in-process app (no server needed):

    python tests/test_frontend_api.py

These assert the SHAPE the TypeScript client in frontend/src/api/types.ts
expects. If a field is renamed or dropped in the backend, this fails here
rather than as a blank panel in the browser.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from astra import db, rbac  # noqa: E402
from astra.api.main import app  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []
client = TestClient(app)


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def has_keys(obj: dict, keys: set[str]) -> tuple[bool, str]:
    missing = keys - set(obj)
    return (not missing), (f"missing {sorted(missing)}" if missing else "")


def main() -> int:
    print("=" * 78)
    print("ASTRA frontend API contract tests")
    print("=" * 78)

    # ------------------------------------------------------------- CORS
    print("\n[1] Browser access")
    r = client.options(
        "/cases",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    check("CORS preflight allows the Vite dev origin",
          r.status_code in (200, 204)
          and r.headers.get("access-control-allow-origin") == "http://localhost:5173",
          r.headers.get("access-control-allow-origin", "no header"))

    # Regression: Vite increments its port when 5173 is busy. A fixed origin
    # list made those preflights fail with "400 Disallowed CORS origin", which
    # the browser reports only as a generic network error.
    drifted = ["http://localhost:5174", "http://127.0.0.1:5174",
               "http://localhost:5175", "http://127.0.0.1:4173"]
    bad = [o for o in drifted
           if client.options("/cases",
                             headers={"Origin": o,
                                      "Access-Control-Request-Method": "GET"}
                             ).status_code not in (200, 204)]
    check("preflight survives a Vite port change", not bad,
          f"rejected {bad}" if bad else f"{len(drifted)} fallback ports accepted")

    # ...but the policy must not become open to the whole internet.
    hostile = ["http://evil.example.com", "https://attacker.net:5173",
               "http://localhost.evil.com"]
    leaked = [o for o in hostile
              if client.options("/cases",
                                headers={"Origin": o,
                                         "Access-Control-Request-Method": "GET"}
                                ).status_code in (200, 204)]
    check("non-loopback origins are still rejected", not leaked,
          f"wrongly allowed {leaked}" if leaked else "3 hostile origins blocked")

    # ------------------------------------------------------- case listing
    print("\n[2] Case list")
    r = client.get("/cases?limit=5")
    check("GET /cases", r.status_code == 200)
    body = r.json()
    ok, detail = has_keys(body, {"total", "limit", "offset", "cases", "disclaimer"})
    check("list envelope shape", ok, detail)
    check("list is capped by limit", len(body["cases"]) <= 5,
          f"{len(body['cases'])} rows of {body['total']:,}")

    if not body["cases"]:
        print("\nNo cases in the database; run the pipeline first.")
        return 1
    case = body["cases"][0]
    ok, detail = has_keys(case, {
        "flag_id", "display_title", "entity_id", "entity_type", "state",
        "district", "constituency", "era", "risk_score", "alert",
        "review_status", "primary_signal", "agents", "rule_ids", "finding_count",
    })
    check("case summary shape matches the TS type", ok, detail)
    check("summary omits heavy fields from list payloads",
          "findings" not in case and "tier_briefs" not in case,
          "keeps the list light")

    # filters
    r = client.get("/cases?limit=3&min_score=70")
    check("min_score filter applies",
          all(c["risk_score"] >= 70 for c in r.json()["cases"]))
    r = client.get("/cases?limit=3&order=risk")
    scores = [c["risk_score"] for c in r.json()["cases"]]
    check("risk ordering is descending", scores == sorted(scores, reverse=True))
    r = client.get("/cases?limit=3&order=risk_asc")
    scores = [c["risk_score"] for c in r.json()["cases"]]
    check("ascending order is supported", scores == sorted(scores))
    r = client.get("/cases?limit=200&offset=0")
    first = {c["flag_id"] for c in r.json()["cases"]}
    r2 = client.get("/cases?limit=200&offset=200")
    check("pagination returns a distinct page",
          not (first & {c["flag_id"] for c in r2.json()["cases"]}))
    r = client.get("/cases?limit=5&rule_ids=D-DUP-01")
    check("rule filter narrows results",
          all("D-DUP-01" in c["rule_ids"] for c in r.json()["cases"]))
    r = client.get("/cases?limit=5&search=zzzznotarealthing")
    check("no-match search returns an empty page, not an error",
          r.status_code == 200 and r.json()["total"] == 0)

    # ------------------------------------------------------- case detail
    print("\n[3] Case detail and synthesis")
    fid = case["flag_id"]
    r = client.get(f"/flags/case/{fid}?tier=district")
    check("GET /flags/case/{id}", r.status_code == 200)
    detail_body = r.json()
    ok, missing = has_keys(detail_body, {
        "flag_id", "entity_id", "entity_type", "risk_score", "review_status",
        "brief", "all_tier_briefs", "findings", "narrative", "disclaimer",
    })
    check("case detail shape matches the TS type", ok, missing)
    brief = detail_body["brief"]
    ok, missing = has_keys(brief or {}, {
        "tier", "tier_label", "lens", "opening", "primary_risk",
        "primary_plain", "signals", "actions", "closing", "disclaimer",
    })
    check("tier brief shape matches the TS type", ok, missing)
    if brief and brief["signals"]:
        ok, missing = has_keys(brief["signals"][0], {
            "rule_id", "agent", "agent_label", "severity", "contribution",
            "share_pct", "headline", "plain", "metric", "benchmark",
        })
        check("brief signal shape matches the TS type", ok, missing)
    check("all four tier briefs are present",
          set(detail_body["all_tier_briefs"]) == set(rbac.ROLES))

    r = client.get(f"/flags/case/{fid}/synthesis?tier=district")
    check("GET synthesis", r.status_code == 200)
    syn = r.json()
    ok, missing = has_keys(syn, {
        "source", "fallback_reason", "risk_score", "risk_level",
        "key_risk_summary", "case_explanation", "supporting_signals",
        "authority_specific_summary", "action_plan", "plan_rationale",
        "limitations_or_missing_evidence", "rejected_actions",
        "unverified_numbers", "constraint_notice", "human_review_notice",
    })
    check("synthesis shape matches the TS type", ok, missing)
    check("synthesis declares its source",
          syn["source"] in ("groq", "deterministic"),
          f"source={syn['source']}, reason={syn.get('fallback_reason')}")
    if syn["action_plan"]:
        ok, missing = has_keys(syn["action_plan"][0],
                               {"action_id", "label", "detail", "stage", "reason"})
        check("action shape matches the TS type", ok, missing)
        check("every action carries a grounded reason",
              all(a["reason"] for a in syn["action_plan"]))
        check("no action reason is an ungrounded placeholder",
              not any("the flagged concern" in a["reason"]
                      for a in syn["action_plan"]),
              "fallback reasons must cite a real signal")

    # authority differentiation, which the UI relies on
    plans = {}
    for tier in rbac.ROLES:
        s = client.get(f"/flags/case/{fid}/synthesis?tier={tier}").json()
        plans[tier] = [a["action_id"] for a in s["action_plan"]]
        allowed = client.get(f"/flags/case/{fid}/actions?tier={tier}").json()
        permitted = {a["action_id"] for a in allowed["allowed_actions"]}
        check(f"{tier} plan stays within its permissions",
              set(plans[tier]) <= permitted,
              f"{len(plans[tier])} actions")
    check("all four authorities receive different plans",
          len({tuple(v) for v in plans.values()}) == 4)

    # ------------------------------------------------------- analytics
    print("\n[4] Analytics endpoints")
    r = client.get("/stats")
    st = r.json()
    ok, missing = has_keys(st, {
        "total", "high", "medium", "low", "under_review", "closed",
        "bands", "corpus",
    })
    check("GET /stats shape", r.status_code == 200 and ok, missing)
    check("stats bands reconcile with the total",
          st["high"] + st["medium"] + st["low"] == st["total"])
    ok, missing = has_keys(st["corpus"], {"works", "districts", "states"})
    check("corpus counts present", ok, missing)

    r = client.get("/meta/facets")
    fc = r.json()
    ok, missing = has_keys(fc, {"states", "districts", "constituencies",
                                "status_counts", "total", "rules"})
    check("GET /meta/facets shape", r.status_code == 200 and ok, missing)
    check("facets drive the filter controls",
          len(fc["states"]) > 0 and len(fc["rules"]) > 0,
          f"{len(fc['states'])} states, {len(fc['rules'])} rules")

    r = client.get("/analytics/states")
    rows = r.json()
    check("GET /analytics/states", r.status_code == 200 and isinstance(rows, list))
    if rows:
        ok, missing = has_keys(rows[0],
                               {"state", "flags", "high_risk", "alerts", "avg_risk"})
        check("state row shape", ok, missing)

    r = client.get("/analytics/districts?limit=5")
    rows = r.json()
    check("GET /analytics/districts", r.status_code == 200 and len(rows) <= 5)
    if rows:
        ok, missing = has_keys(rows[0],
                               {"state", "district", "flags", "high_risk", "avg_risk"})
        check("district row shape", ok, missing)

    r = client.get("/analytics/detections")
    rows = r.json()
    check("GET /analytics/detections", r.status_code == 200)
    if rows:
        ok, missing = has_keys(rows[0], {"rule_id", "title", "count"})
        check("detection row shape", ok, missing)
        check("detections are titled, not bare rule ids",
              all(d["title"] != d["rule_id"] for d in rows))

    # --------------------------------------------------------- records
    print("\n[5] Record lookups")
    work_case = next(
        (c for c in client.get("/cases?limit=40&entity_types=work").json()["cases"]),
        None)
    if work_case:
        r = client.get(f"/works/{work_case['entity_id']}")
        check("GET /works/{id}", r.status_code == 200)
        ok, missing = has_keys(r.json(), {
            "work_id", "description", "category", "state", "district",
            "sanctioned_amount", "status",
        })
        check("work record shape matches the TS type", ok, missing)
    check("unknown work returns 404",
          client.get("/works/NOT-A-REAL-WORK").status_code == 404)

    r = client.get("/meta/pipeline")
    ok, missing = has_keys(r.json(), {"router_trace", "rule_coverage"})
    check("GET /meta/pipeline shape", r.status_code == 200 and ok, missing)

    r = client.get("/meta/data-source")
    check("GET /meta/data-source", r.status_code == 200 and "provenance" in r.json())

    r = client.get("/meta/llm")
    check("GET /meta/llm never leaks the key",
          r.status_code == 200 and "gsk_" not in r.text)

    # ------------------------------------------------- review round trip
    print("\n[6] Human review round trip")
    fid = case["flag_id"]
    original = client.get(f"/flags/case/{fid}?tier=district").json()["review_status"]
    r = client.post(f"/flags/{fid}/feedback",
                    json={"action": "under_review", "authority_tier": "district",
                          "note": "frontend contract test"})
    check("POST feedback accepted", r.status_code == 200)
    check("status is reflected on the next read",
          client.get(f"/flags/case/{fid}?tier=district").json()["review_status"]
          == "under_review")
    check("status filter finds the reviewed case",
          any(c["flag_id"] == fid for c in
              client.get("/cases?limit=200&statuses=under_review").json()["cases"]))
    check("reopening is not an API action",
          client.post(f"/flags/{fid}/feedback",
                      json={"action": "pending", "authority_tier": "district"})
          .status_code == 400,
          "the API exposes only the three real review decisions")
    # restore through the data layer, since 'pending' is a reset rather than a
    # decision an authority takes
    db.record_feedback(fid, original, "district", "")
    check("status restored for the next run",
          client.get(f"/flags/case/{fid}?tier=district").json()["review_status"]
          == original)

    npass = sum(1 for r in results if r[0] == PASS)
    nfail = sum(1 for r in results if r[0] == FAIL)
    print("\n" + "=" * 78)
    print(f"RESULT: {npass} passed, {nfail} failed")
    if nfail:
        for status, name, detail in results:
            if status == FAIL:
                print(f"  - {name}: {detail}")
    print("=" * 78)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
