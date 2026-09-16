"""The analysis layer: guideline rules, data contract, revisions, confidence, peers.

    python tests/test_analysis_rules.py

Offline, on synthetic works whose right answer is known. Every guideline rule
is checked against the paragraph of the MPLADS Guidelines (1 April 2023) it
cites, including the cases the 2023 text permits, which earlier versions of the
rules flagged. Nothing touches the real database.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="astra-analysis-"))
os.environ["ASTRA_DB_PATH"] = str(_TMP / "scratch.db")
os.environ["ASTRA_PROCESSED_DIR"] = str(_TMP / "processed")

import pandas as pd                                                  # noqa: E402
import yaml                                                          # noqa: E402

from astra import data_confidence, data_contract as contract, db    # noqa: E402
from astra.agents.anomaly import AnomalyAgent                        # noqa: E402
from astra.agents.compliance import ComplianceAgent                  # noqa: E402
from astra.agents.network import NetworkAgent                        # noqa: E402
from astra.agents.payments import PaymentAgent, mark                 # noqa: E402
from astra.agents.orchestrator import Orchestrator                   # noqa: E402
from astra.agents.revisions import RevisionAgent                     # noqa: E402
from astra.config import load_guidelines, load_rules                 # noqa: E402
from astra.explain import humanize                                   # noqa: E402
from astra.rbac import evidence_keys                                 # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []
TODAY = date(2026, 9, 13)


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def work(work_id, **fields):
    base = {"work_id": work_id, "source": "esakshi_api", "era": "post2023", "state": "UTTAR PRADESH",
            "district": "SITAPUR", "constituency": "SITAPUR", "mp_name": "SHRI A", "house": "LS",
            "category": "Community hall", "work_type": "Community hall",
            "description": "Construction of community hall at Rampur village", "fy": "2025-26",
            "recommended_date": "2025-06-01", "sanction_date": "2025-07-01", "completion_date": None,
            "status": "Sanction", "estimated_cost": 500000.0, "sanctioned_amount": 500000.0,
            "expenditure": None, "total_paid": None, "payment_count": None,
            "ia_name": "SITAPUR(DISTRICT MAGISTRATE SITAPUR_IDA)", "vendor_name": None,
            "lat": None, "lon": None, "in_recommended": 1, "in_sanctioned": 1,
            "in_completed": 0, "payments": 0, "is_sc_constituency": 0, "is_st_constituency": 0}
    base.update(fields)
    return base


def frame(rows) -> pd.DataFrame:
    return contract.enrich(pd.DataFrame(rows))


def by_rule(findings, rule_id, entity_type=None):
    return [f for f in findings if f.rule_id == rule_id
            and (entity_type is None or f.entity_type == entity_type)]


# ---------------------------------------------------------------------------
def test_guideline_index() -> None:
    print("\n[1] every rule names its paragraph and what kind of rule it is")
    rules = load_rules()
    paras = load_guidelines()["paras"]
    ids: dict[str, dict] = {}

    def walk(node):
        if isinstance(node, dict):
            if "id" in node and "title" in node:
                ids[node["id"]] = node
            for v in node.values():
                walk(v)
    walk(rules)
    check("every rule declares its basis",
          all(r.get("basis") for r in ids.values()),
          ", ".join(k for k, r in ids.items() if not r.get("basis")))
    missing = [f"{k}:{r['para']}" for k, r in ids.items()
               if r.get("para") and r["para"] != "5.2" and r["para"] not in paras]
    check("every cited paragraph is in the guidelines index", not missing, ", ".join(missing))
    check("a rule that enforces a guideline always cites one",
          all(r.get("para") for r in ids.values() if r.get("basis") == "guideline"))
    proh = rules["rules"]["prohibited_works"]
    terms = {**proh["asset_patterns"], **proh["activity_patterns"]}
    check("each negative-list term maps to a 2023 paragraph",
          all(str(p) in paras for p in terms.values()), str(terms))
    check("repair, renovation and office buildings are no longer on the negative list",
          not any(t in terms for t in ("repair of", "renovation of", "office building",
                                       "memorial", "statue")))
    check("the minimum work value is Rs 2.5 lakh, per para 3.2.9",
          rules["rules"]["cost_ceiling"]["min_work_cost"] == 250000
          and paras["3.2.9"]["amounts"]["normal_minimum"] == 250000)


def test_data_contract() -> None:
    print("\n[2] the analysis reads fields the way ingestion built them")
    rows = frame([
        work("W1", status="Physical Inspection", in_completed=1, completion_date="2025-12-01"),
        work("W2", status="Work Completed", in_completed=0, completion_date=None),
        work("ES-LS-1", status="Pending for Sanction", sanction_date=None, in_sanctioned=0),
    ]).set_index("work_id")
    check("completion comes from the completed report, not the stage text",
          rows.loc["W1", "is_completed"] and not rows.loc["W2", "is_completed"])
    check("a pending work is not sanctioned", not rows.loc["ES-LS-1", "is_sanctioned"])
    check("financial years end on 31 March",
          contract.fy_has_ended("2025-26", date(2026, 4, 1))
          and not contract.fy_has_ended("2026-27", TODAY)
          and contract.financial_year_of(TODAY) == "2026-27")
    check("a Rajya Sabha member is keyed by name, not a 'nan' constituency",
          contract.mp_key({"constituency": float("nan"), "mp_name": "SHRI B", "state": "BIHAR"})
          == "SHRI B|BIHAR")
    facts = contract.check(frame([work("A", estimated_cost=100.0, sanctioned_amount=120.0),
                                  work("B", lat=26.1)]))
    check("a change on the portal breaks the matching fact",
          not facts["recommended_equals_sanctioned_amount"]["holds"]
          and not facts["no_coordinates"]["holds"], json.dumps(facts)[:160])
    facts = contract.check(frame([
        work("V1", vendor_id="11", vendor_name="AJAY KUMAR", term_end="2029-06-03"),
        work("V2", vendor_id="22", vendor_name="AJAY KUMAR", term_end="2029-06-03"),
        work("V3", vendor_id="22", vendor_name="A KUMAR", term_end="2026-01-01")]))
    check("a vendor id under two names, and a member whose term has ended, are measured",
          not facts["vendor_id_has_one_name"]["holds"]
          and "1 names used by several" in facts["vendor_id_has_one_name"]["observed"]
          and not facts["members_in_office"]["holds"],
          json.dumps({k: facts[k] for k in ("vendor_id_has_one_name", "members_in_office")}))


def test_compliance_rules() -> None:
    print("\n[3] guideline rules, including what the 2023 text permits")
    agent = ComplianceAgent(today=TODAY)
    cfg = load_rules()["rules"]
    works = frame([
        work("DONE", status="Physical Inspection", in_completed=1,
             sanction_date="2024-01-01", completion_date="2024-06-01"),
        work("LATE", status="Work partially Completed", sanction_date="2025-01-01"),
    ])
    late = by_rule(agent._completion_norm(works, cfg["completion_norm"]), "R-TIME-01")
    check("a completed work read as 'Physical Inspection' is not overdue, and an open one is",
          [f.entity_id for f in late] == ["LATE"] and late[0].details["incomplete"],
          str([(f.entity_id, f.details.get("incomplete")) for f in late]))

    works = frame([
        work("SMALL", sanctioned_amount=200000.0, estimated_cost=200000.0),
        work("PENDING-SMALL", sanction_date=None, status="Pending for Sanction",
             sanctioned_amount=100000.0, estimated_cost=100000.0),
        work("NORMAL", sanctioned_amount=300000.0, estimated_cost=300000.0),
        work("BIG", sanctioned_amount=30000000.0, estimated_cost=30000000.0),
    ])
    cost = agent._cost_ceiling(works, cfg["cost_ceiling"])
    small = by_rule(cost, "R-COST-01", "work")
    check("a sanction below Rs 2.5 lakh is found; a pending recommendation is not",
          {f.entity_id for f in small} == {"SMALL", "BIG"}, str([f.entity_id for f in small]))
    floor = next(f for f in small if f.entity_id == "SMALL")
    check("at low severity, as context, citing para 3.2.9 and the recorded-reasons exception",
          floor.severity == "low" and floor.details["standalone"] is False
          and "3.2.9" in floor.clause and "reasons" in floor.summary)
    big = next(f for f in small if f.entity_id == "BIG")
    check("the large-work marker says it is not a scheme limit",
          big.details["basis"] == "heuristic" and "no maximum" in big.summary.lower())
    many = frame([work(f"S{i}", sanctioned_amount=150000.0, estimated_cost=150000.0) for i in range(12)]
                 + [work("OK1", sanctioned_amount=400000.0)])
    summary = by_rule(agent._cost_ceiling(many, cfg["cost_ceiling"]), "R-COST-01", "agency")
    check("a district authority with many small sanctions gets one summary case",
          len(summary) == 1 and summary[0].details["works"] == 12
          and summary[0].details["out_of"] == 13, summary and summary[0].summary)

    texts = {
        "REPAIR": "Repair of community hall at Rampur",
        "RENOV": "Renovation of pond near panchayat bhawan",
        "OFFICE": "Construction of Government office building for post office",
        "TEMPLE": "Construction of temple",
        "GATE": "Construction of Swagat Dwar at village entrance",
        "UPKEEP": "Maintenance of public park for one year",
        "LANDMARK": "Construction of community hall near Hanuman temple",
    }
    found = agent._prohibited(frame([work(k, description=v) for k, v in texts.items()]),
                              cfg["prohibited_works"])
    paras = {f.entity_id: f.details.get("para") for f in found}
    check("repair, renovation and a government office are permitted (para 5.1.9, portal category)",
          not {"REPAIR", "RENOV", "OFFICE"} & set(paras), str(paras))
    check("a temple, a swagat dwar and routine maintenance cite 5.2.11, 5.2.12 and 5.2.1",
          paras.get("TEMPLE") == "5.2.11" and paras.get("GATE") == "5.2.12"
          and paras.get("UPKEEP") == "5.2.1", str(paras))
    check("a temple used as a landmark is not flagged", "LANDMARK" not in paras)

    pending = [work(f"P{i}", sanction_date=None, status="Pending for Sanction",
                    recommended_date="2026-06-01") for i in range(11)]
    pending += [work("FRESH", sanction_date=None, status="Pending for Sanction",
                     recommended_date="2026-09-01"),
                work("PAIR", sanction_date=None, status="NA", recommended_date="2025-01-01")]
    slow = [work(f"D{i}", recommended_date="2025-01-01", sanction_date="2025-05-01") for i in range(20)]
    timeline = agent._sanction_timeline(frame(pending + slow), cfg["sanction_timeline"])
    waiting = by_rule(timeline, "R-SANC-01", "work")
    check("recommendations waiting beyond 45 days are found; recent ones and portal 'NA' records are not",
          {f.entity_id for f in waiting} == {f"P{i}" for i in range(11)},
          str(sorted(f.entity_id for f in waiting)))
    check("each says the count is an upper bound (no model code of conduct calendar, para 3.2.6)",
          all(f.details["upper_bound"] and "code of conduct" in f.summary for f in waiting))
    district = by_rule(timeline, "R-SANC-02")
    check("a district authority whose sanctions typically exceed 45 days is found once",
          len(district) == 1 and district[0].details["median_days"] == 120
          and district[0].entity_type == "agency", district and district[0].summary)

    repairs = [work(f"R{i}", description=f"Repair of school building {i}", estimated_cost=2000000.0,
                    sanctioned_amount=2000000.0) for i in range(3)]
    capped = agent._repair_cap(frame(repairs), cfg["repair_cap"])
    check("repair works above Rs 50 lakh in a year for one MP are found (para 5.1.9)",
          len(capped) == 1 and capped[0].details["total"] == 6000000.0, capped and capped[0].summary)
    pair = [work("R0", description="Repair of school building", estimated_cost=3000000.0),
            work("ES-LS-9", description="Repair of school building", estimated_cost=3000000.0,
                 status="NA", sanction_date=None)]
    check("the portal's pending 'NA' copy of a sanctioned work is not added twice",
          not agent._repair_cap(frame(pair), cfg["repair_cap"]))

    classed = [work(f"C{i}", description=f"Boundary wall at school {i}", estimated_cost=2000000.0,
                    work_category="Repair and Renovation") for i in range(3)]
    plain = [work(f"N{i}", work_category="Normal/Others") for i in range(3)]
    capped = agent._repair_cap(frame(classed + plain), cfg["repair_cap"])
    check("a work the portal classes as repair and renovation counts without saying repair",
          len(capped) == 1 and capped[0].severity == "medium"
          and capped[0].details["portal_category_works"] == 3
          and capped[0].details["described_only_works"] == 0, capped and capped[0].summary)
    mixed = ([work("C0", description="Boundary wall", estimated_cost=3000000.0,
                   work_category="Repair and Renovation"),
              work("D0", description="Repair of panchayat bhawan", estimated_cost=3000000.0,
                   work_category="Normal/Others")])
    capped = agent._repair_cap(frame(mixed), cfg["repair_cap"])
    check("over the cap only once works described as repair are added: reported, at low severity",
          len(capped) == 1 and capped[0].severity == "low"
          and capped[0].details["described_only_total"] == 3000000.0
          and "under another category" in capped[0].summary, capped and capped[0].summary)
    legacy = agent._repair_cap(frame(repairs), cfg["repair_cap"])
    check("a corpus without the portal category is judged as before",
          len(legacy) == 1 and legacy[0].severity == "medium"
          and "portal_category_works" not in legacy[0].details)

    trusts = [work(f"T{i}", estimated_cost=2000000.0, work_category="Trust and Society")
              for i in range(3)]
    grants = by_rule(agent._trust_cap(frame(trusts), cfg["trust_cap"]), "R-TRUST-01")
    check("societies and trusts above Rs 50 lakh in a year for one MP are found (para 6.2.6.2)",
          len(grants) == 1 and grants[0].details["total"] == 6000000.0
          and grants[0].entity_type == "constituency" and "6.2.6.2" in grants[0].clause,
          grants and grants[0].summary)
    under = trusts[:2] + [work("T9", estimated_cost=2000000.0, work_category="Trust and Society",
                               fy="2024-25", recommended_date="2024-06-01"),
                          work("ES-LS-8", estimated_cost=2000000.0, work_category="Trust and Society",
                               status="NA", sanction_date=None)]
    check("Rs 40 lakh in the year, another year's grant and the portal's 'NA' copy stay under the cap",
          not agent._trust_cap(frame(under), cfg["trust_cap"]))
    years = [work("Y1", estimated_cost=3000000.0, work_category="Trust and Society",
                  fy="2025-26", recommended_date="2025-03-12", sanction_date="2025-05-01"),
             work("Y2", estimated_cost=3000000.0, work_category="Trust and Society",
                  fy="2025-26", recommended_date="2025-09-23", sanction_date="2025-10-01")]
    check("the limit counts the year a work was recommended, not the sanction year in its code",
          not agent._trust_cap(frame(years), cfg["trust_cap"])
          and not agent._repair_cap(frame([{**w, "work_category": "Repair and Renovation"}
                                           for w in years]), cfg["repair_cap"]))
    check("recommendation years run April to March, falling back to the code's year",
          contract.recommendation_fy(frame(years + [work("Y3", recommended_date=None, fy="2023-24")]))
          .tolist() == ["2024-25", "2025-26", "2023-24"])
    agent.notes = {}
    check("without the portal category the rule stands down and says why",
          not agent._trust_cap(frame([work("X")]), cfg["trust_cap"])
          and "category" in agent.notes.get("R-TRUST-01", ""), agent.notes.get("R-TRUST-01"))

    flows = pd.DataFrame([
        {"row_id": f"F{i}", "source": "esakshi_api", "state": "BIHAR", "constituency": "PATNA",
         "mp_name": "SHRI C", "fy": fy, "released": 50000000.0, "expenditure": spent,
         "utilization_pct": None, "era": "post2023"}
        for i, (fy, spent) in enumerate((("2024-25", 5e6), ("2025-26", 6e6), ("2026-27", 1e6)))])
    agent.notes = {}
    pile = agent._pileup(flows, cfg["unspent_pileup"])
    check("with fewer than three ended financial years the pile-up rule stands down, and says why",
          not pile and "completed financial years" in agent.notes.get("R-PILE-01", ""),
          agent.notes.get("R-PILE-01"))
    later = ComplianceAgent(today=date(2027, 6, 1))
    check("once three years have ended it evaluates, ignoring none of them",
          len(later._pileup(flows, cfg["unspent_pileup"])) == 1)


def test_orchestration() -> None:
    print("\n[4] context findings, rule coverage and district authorities")
    orch = Orchestrator()
    orch.agents[0].today = TODAY
    works = frame([
        work("ONLY-SMALL", sanctioned_amount=200000.0, estimated_cost=200000.0,
             sanction_date="2026-06-01", description="Installation of hand pump at Kheri"),
        work("SMALL-AND-LATE", sanctioned_amount=200000.0, estimated_cost=200000.0,
             sanction_date="2024-01-01"),
    ])
    flags = orch.run(works, pd.DataFrame(), {})
    cases = {f.entity_id: f for f in flags}
    check("a context finding alone opens no case", "ONLY-SMALL" not in cases, str(list(cases)))
    late = cases.get("SMALL-AND-LATE")
    check("it joins a case that exists for another reason, adding nothing to the score",
          late is not None and {f.rule_id for f in late.findings} >= {"R-TIME-01", "R-COST-01"}
          and late.risk_score == load_rules()["risk_score"]["weights"]["high"],
          late and f"score {late.risk_score}, {[f.rule_id for f in late.findings]}")
    coverage = {r["rule_id"]: r for r in orch.rule_coverage}
    check("rule coverage records each rule's basis and why a rule stood down",
          coverage["R-TIME-01"]["basis"] == "guideline"
          and coverage["A-PEER-01"]["basis"] == "statistical"
          and "stood_down" in coverage["V-REV-01"], json.dumps(coverage.get("V-REV-01")))
    check("a district authority is labelled as one, not as an agency",
          orch._label("agency", "SITAPUR(DISTRICT MAGISTRATE SITAPUR_IDA)", {"ia_name": "SITAPUR(DISTRICT MAGISTRATE SITAPUR_IDA)"})
          == "District authority: Sitapur")


def test_network() -> None:
    print("\n[4b] networks: vendors by portal id, implementing agencies, district authorities")
    agent = NetworkAgent()
    districts = ["SITAPUR", "LUCKNOW", "KANPUR", "AGRA", "MEERUT", "BAREILLY"]
    # Six vendors called Ajay Kumar, one district each: one name, six vendors.
    namesakes = [work(f"A{i}{j}", district=d, vendor_name="AJAY KUMAR", vendor_id=str(100 + i),
                      category=f"Type {j}")
                 for i, d in enumerate(districts) for j in range(5)]
    found = agent.run(frame(namesakes), pd.DataFrame())
    check("namesake vendors in one district each are not one vendor across six",
          not [f for f in found if f.details["actor_type"] == "vendor"],
          str([f.entity_id for f in found]))
    by_name = agent.run(frame(namesakes).drop(columns=["vendor_id"]), pd.DataFrame())
    check("without vendor ids the name is the only key, as before",
          [f.entity_id for f in by_name if f.details["actor_type"] == "vendor"] == ["vendor:AJAY KUMAR"])

    roaming = [work(f"R{i}", district=districts[i % 6], vendor_name="AJAY KUMAR", vendor_id="777",
                    category=f"Type {i}") for i in range(6)]
    found = agent.run(frame(namesakes + roaming), pd.DataFrame())
    vendors = [f for f in found if f.details["actor_type"] == "vendor"]
    check("one vendor id working across six districts is found, keyed by its id",
          [f.entity_id for f in vendors] == ["vendor:777"]
          and vendors[0].details["vendor_id"] == "777"
          and vendors[0].details["same_name_vendor_ids"] == 6
          and "counted separately" in vendors[0].summary, vendors and vendors[0].summary)

    agency = [work(f"G{i}", district=districts[i % 6], vendor_name=f"FIRM {i}", vendor_id=str(i),
                   implementing_agency="RURAL ENGINEERING DEPARTMENT") for i in range(12)]
    found = agent.run(frame(agency), pd.DataFrame())
    check("a state agency executing works across districts is not a spread signal",
          not [f for f in found if f.details["actor_type"] == "agency"])

    authority = "SITAPUR(DISTRICT MAGISTRATE SITAPUR_IDA)"
    peers = [work(f"P{i}", ia_name=f"PEER {i % 3}(DISTRICT MAGISTRATE PEER_IDA)",
                  district=f"PEER {i % 3}", estimated_cost=500000.0 + i * 1000, sanctioned_amount=None)
             for i in range(30)]
    dear = [work(f"E{i}", ia_name=authority, estimated_cost=5000000.0 + i * 1000,
                 sanctioned_amount=None, implementing_agency="PWD SITAPUR") for i in range(6)]
    found = agent.run(frame(peers + dear), pd.DataFrame())
    ids = {f.details["actor_type"]: f.entity_id for f in found}
    check("a district authority's network finding joins the authority's own case",
          ids.get("district_authority") == authority, str(ids))
    check("an implementing agency whose works price high is found under its own name",
          ids.get("agency") == "agency:PWD SITAPUR", str(ids))

    orch = Orchestrator()
    vendor = vendors[0]
    meta = {"actor_type": "vendor", "actor": "AJAY KUMAR", "vendor_id": "777"}
    check("a vendor case is titled by name and labelled with its portal id",
          orch._display_title("agency", vendor.entity_id, meta) == "Ajay Kumar"
          and orch._label("agency", vendor.entity_id, meta)
          == "Vendor: Ajay Kumar (portal vendor id 777)")
    check("an implementing agency is labelled as one",
          orch._label("agency", "agency:PWD SITAPUR", {"actor_type": "agency", "actor": "PWD SITAPUR"})
          == "Implementing agency: Pwd Sitapur")


def pay(work_id, seq, paid_on, amount, vendor="V1", status="Payment Success"):
    return {"work_id": work_id, "seq": seq, "shard_id": "2:1:1", "paid_on": paid_on,
            "amount": amount, "vendor_id": vendor, "vendor_name": f"FIRM {vendor}",
            "implementing_agency": "PWD", "status": status}


def test_payments() -> None:
    print("\n[4c] payment records: before sanction, long after completion, recorded twice")
    works = frame([
        work("EARLY", sanction_date="2025-07-01", total_paid=300000.0, payment_count=2),
        work("LATE", sanction_date="2025-01-01", completion_date="2025-06-30", in_completed=1,
             total_paid=1000000.0, payment_count=3),
        work("TWICE", total_paid=1150000.0, payment_count=4),
        work("BRICKS", total_paid=60000.0, payment_count=2),
        work("CLEAN", completion_date="2026-01-31", in_completed=1, total_paid=100000.0,
             payment_count=2),
    ])
    payments = pd.DataFrame([
        pay("EARLY", 1, "2025-06-15", 100000.0), pay("EARLY", 2, "2025-08-01", 200000.0),
        pay("LATE", 1, "2025-05-01", 600000.0), pay("LATE", 2, "2025-09-01", 100000.0),
        pay("LATE", 3, "2026-03-28", 300000.0),
        pay("TWICE", 1, "2025-09-01", 500000.0), pay("TWICE", 2, "2025-09-01", 500000.0),
        pay("TWICE", 3, "2025-09-01", 100000.0), pay("TWICE", 4, "2025-09-01", 50000.0, vendor="V2"),
        pay("BRICKS", 1, "2025-09-01", 30000.0), pay("BRICKS", 2, "2025-09-01", 30000.0),
        pay("CLEAN", 1, "2025-08-01", 50000.0), pay("CLEAN", 2, "2026-02-15", 50000.0),
    ])
    marked = mark(payments, works).set_index(["work_id", "seq"])
    check("each payment is marked against its work's dates",
          marked.loc[("EARLY", 1), "before_sanction"]
          and marked.loc[("LATE", 3), "days_after_completion"] == 271
          and pd.isna(marked.loc[("LATE", 1), "days_after_completion"])
          and marked.loc[("LATE", 3), "year_end_week"]
          and marked.loc[("TWICE", 1), "repeats"] == 2 and marked.loc[("TWICE", 3), "repeats"] == 1,
          str(marked.loc[("LATE", 3)].to_dict()))

    agent = PaymentAgent()
    agent.context = {"payments": payments}
    found = agent.run(works, pd.DataFrame())
    ids = {(f.rule_id, f.entity_id) for f in found}
    check("a payment dated before sanction is found, at high severity",
          ("P-SEQ-01", "EARLY") in ids
          and by_rule(found, "P-SEQ-01")[0].severity == "high"
          and by_rule(found, "P-SEQ-01")[0].details["earliest"] == "2025-06-15", str(ids))
    late = by_rule(found, "P-LATE-01")
    check("payments more than 180 days after completion are found (para 11.2), and one "
          "63 days after is not",
          [f.entity_id for f in late] == ["LATE"] and late[0].details["late_payments"] == 1
          and late[0].details["share_of_paid"] == 0.3 and late[0].severity == "low"
          and late[0].clause and "11.2" in late[0].clause, late and late[0].summary)
    twice = {f.entity_id: f for f in by_rule(found, "P-DUP-01")}
    check("the same amount to the same vendor on one day, recorded twice, is found; "
          "a Rs 5 lakh repeat is medium, a Rs 30,000 one low",
          set(twice) == {"TWICE", "BRICKS"}
          and twice["TWICE"].severity == "medium" and twice["BRICKS"].severity == "low"
          and twice["TWICE"].details["extra_records"] == 1
          and twice["TWICE"].details["repeated_amount"] == 500000.0, str(
              {k: (f.severity, f.details["repeated_amount"]) for k, f in twice.items()}))
    check("nothing on a clean work, and a repeat's finding says the payment orders decide",
          not any(f.entity_id == "CLEAN" for f in found)
          and all("payment orders" in f.summary for f in by_rule(found, "P-DUP-01")))
    idle = PaymentAgent()
    check("without payment records the module stands down and says why",
          not idle.run(works, pd.DataFrame()) and "payment records" in idle.notes.get("P-DUP-01", ""))
    facts = contract.check(works, payments)
    check("the contract measures that payments add up to works and follow sanction",
          facts["payments_add_up_to_works"]["holds"]
          and not facts["payments_follow_sanction"]["holds"],
          json.dumps({k: facts[k] for k in ("payments_add_up_to_works", "payments_follow_sanction")}))


def test_revisions() -> None:
    print("\n[5] revisions after sanction, not progress")
    agent = RevisionAgent()
    works = frame([
        work("SANCTIONED", sanction_date="2026-01-10"),
        work("PENDING", sanction_date=None, status="Pending for Sanction"),
        work("SAME-DAY", sanction_date="2026-09-01"),
    ])

    def edit(work_id, field, old, new, observed="2026-09-12T10:00:00+00:00"):
        return {"work_id": work_id, "observed_at": observed, "field": field,
                "old_value": old, "new_value": new, "shard_id": "2:33:418"}

    versions = pd.DataFrame([
        edit("SANCTIONED", "description", "Hall at Rampur", "Hall at Sitapur"),
        edit("PENDING", "description", "Hall at Rampur", "Hall at Sitapur"),
        edit("SAME-DAY", "description", "Hall at Rampur", "Hall at Kheri", "2026-09-02T10:00:00+00:00"),
        edit("SAME-DAY", "sanction_date", None, "2026-09-01", "2026-09-02T10:00:00+00:00"),
        edit("SANCTIONED", "sanctioned_amount", "1000000", "1250000"),
        edit("SANCTIONED", "estimated_cost", "1000000", "1250000"),
        edit("SANCTIONED", "vendor_name", "ALPHA TRADERS", "BETA WORKS"),
        edit("SANCTIONED", "status", "Sanction", "Vendor Identification"),
        edit("SANCTIONED", "total_paid", "400000", "250000"),
        edit("PENDING", "vendor_name", None, "GAMMA"),
        edit("SANCTIONED", "ia_name", "SITAPUR IDA", "SITAPUR IDA "),
    ])
    retired = pd.DataFrame([
        {"work_id": "GONE-PAID", "shard_id": "2:33:418", "retired_at": "2026-09-12T11:00:00+00:00",
         "first_missing_at": "2026-09-12T10:00:00+00:00",
         "record_json": json.dumps({"work_id": "GONE-PAID", "total_paid": 90000.0, "state": "UP"})},
        {"work_id": "GONE-UNPAID", "shard_id": "2:33:418", "retired_at": "2026-09-12T11:00:00+00:00",
         "first_missing_at": "2026-09-12T10:00:00+00:00",
         "record_json": json.dumps({"work_id": "GONE-UNPAID", "total_paid": None})},
    ])
    agent.context = {"versions": versions, "retired": retired}
    found = agent.run(works, pd.DataFrame())
    ids = {(f.rule_id, f.entity_id) for f in found}
    check("a description changed after sanction is found (para 3.2.15)",
          ("V-REV-01", "SANCTIONED") in ids)
    check("not before sanction, and not when it coincides with the sanction itself",
          ("V-REV-01", "PENDING") not in ids and ("V-REV-01", "SAME-DAY") not in ids, str(sorted(ids)))
    amount = next((f for f in found if f.rule_id == "V-AMT-01"), None)
    check("a 25% increase after sanction is one finding at medium severity",
          amount is not None and amount.severity == "medium" and amount.details["pct_change"] == 25.0
          and sum(1 for f in found if f.rule_id == "V-AMT-01") == 1)
    vendor = next((f for f in found if f.rule_id == "V-VEN-01"), None)
    check("a vendor swap with no new payment is reported as a relabel, at medium",
          vendor is not None and vendor.severity == "medium" and not vendor.details["with_new_payment"])
    check("a payment reversal and a paid work removed from the portal are found; an unpaid one is not",
          ("V-PAY-01", "SANCTIONED") in ids and ("V-LIST-01", "GONE-PAID") in ids
          and ("V-LIST-01", "GONE-UNPAID") not in ids)
    check("progress, a first vendor and whitespace-only edits are never revisions",
          not any(f.entity_id == "PENDING" for f in found)
          and ("V-IA-01", "SANCTIONED") not in ids, str(sorted(ids)))
    check("every finding states the history limits",
          all("polling began" in f.details["history_note"] for f in found))
    check("the frequency indicator stands down on a short history",
          "V-FREQ-01" in agent.notes and "history" in agent.notes["V-FREQ-01"], agent.notes.get("V-FREQ-01"))

    bulk = pd.DataFrame([edit(f"W{i}", "category", "Old type", "New type") for i in range(60)])
    many = frame([work(f"W{i}") for i in range(60)])
    agent.context = {"versions": bulk, "retired": pd.DataFrame()}
    check("a work type renamed on many works at once is the portal's vocabulary, not a revision",
          not agent.run(many, pd.DataFrame()))
    agent.context = {}
    check("with no observed edits every revision rule stands down with a reason",
          not agent.run(works, pd.DataFrame()) and len(agent.notes) >= 6)


def test_data_confidence() -> None:
    print("\n[6] data confidence follows the area's health")
    from astra.schemas import Finding
    health = pd.DataFrame([
        {"shard_id": "OK", "exact": 1, "lifecycle": "IDLE", "consecutive_failures": 0,
         "stale_since": None, "fetched_at": "2026-09-01T00:00:00+00:00", "awaiting_removal": 0},
        {"shard_id": "OFF", "exact": 0, "lifecycle": "STORED", "consecutive_failures": 0,
         "stale_since": None, "fetched_at": None, "awaiting_removal": 2},
        {"shard_id": "FAILING", "exact": 1, "lifecycle": "RETRY", "consecutive_failures": 3,
         "stale_since": "2026-09-01T00:00:00+00:00", "fetched_at": None, "awaiting_removal": 0},
    ])
    levels = data_confidence.area_confidence(health)
    check("a healthy area is verified, even if it was last re-read long ago",
          levels["OK"] == ("verified", []))
    check("parity differences, removals awaiting confirmation and failing reads reduce confidence",
          levels["OFF"][0] == "reduced" and len(levels["OFF"][1]) == 2
          and levels["FAILING"][0] == "reduced" and len(levels["FAILING"][1]) == 3,
          json.dumps({k: v[1] for k, v in levels.items()})[:200])
    works = pd.DataFrame([{"work_id": "A", "shard_id": "OK"}, {"work_id": "B", "shard_id": "OFF"}])

    def finding(eid):
        return Finding(agent="compliance", rule_id="R-TIME-01", rule_title="t", severity="medium",
                       entity_type="work", entity_id=eid, summary="s")

    fs = [finding("A"), finding("B")]
    counts = data_confidence.annotate(fs, works, health)
    check("findings are marked, not hidden", counts == {"verified": 1, "reduced": 1}
          and fs[1].details["data_confidence"] == "reduced")
    brief = humanize(fs[1].model_dump())
    check("and the plain-language explanation tells the reviewer to verify first",
          "Data confidence is reduced" in brief["plain"]
          and brief["actions"][0].startswith("Verify the record"))
    check("a corpus without area health is left unmarked",
          data_confidence.annotate([finding("A")], works, pd.DataFrame()) == {})


def test_peer_profile() -> None:
    print("\n[7] peer profile: several measures at once, named; Isolation Forest only corroborates")
    import numpy as np
    rng = np.random.default_rng(3)
    rows = []
    for i in range(40):
        rec = pd.Timestamp("2025-04-01") + pd.Timedelta(days=int(rng.integers(0, 30)))
        san = rec + pd.Timedelta(days=int(rng.integers(30, 50)))
        com = san + pd.Timedelta(days=int(rng.integers(80, 120)))
        rows.append(work(f"N{i}", recommended_date=str(rec.date()), sanction_date=str(san.date()),
                         completion_date=str(com.date()), in_completed=1, total_paid=480000.0,
                         payment_count=int(rng.integers(2, 4))))
    rows.append(work("BOTH", recommended_date="2024-04-01", sanction_date="2025-06-01",
                     completion_date="2027-01-01", in_completed=1, total_paid=480000.0, payment_count=3))
    rows.append(work("ONE", recommended_date="2025-04-01", sanction_date="2025-05-10",
                     completion_date="2027-01-01", in_completed=1, total_paid=480000.0, payment_count=3))
    agent = AnomalyAgent()
    found = agent._peer_profile(frame(rows), load_rules()["anomaly"]["peer_profile"])
    ids = {f.entity_id for f in found}
    check("a work extreme on two measures is found, and one extreme on a single measure is not",
          ids == {"BOTH"}, str(ids))
    measures = {m["measure"] for m in found[0].details["measures"]} if found else set()
    check("the finding names the measures with their typical values",
          measures == {"days_to_sanction", "days_to_complete"}
          and all("peer_median" in m for m in found[0].details["measures"]), str(measures))
    small = agent._peer_profile(frame(rows[:10] + rows[-2:]), load_rules()["anomaly"]["peer_profile"])
    check("a peer group smaller than the minimum is not scored", not small)


def test_run_record() -> None:
    print("\n[8] each analysis run is recorded")
    from astra.pipeline import run_pipeline
    db.init_db(force=True)
    rows = [work(f"W{i}", sanction_date="2024-01-01") for i in range(3)]
    db.replace_df("works", pd.DataFrame(rows).reindex(columns=list(db.WORK_COLUMNS)))
    summary = run_pipeline(verbose=False)
    run = db.analysis_runs(1)[0]
    check("a run row names the rules file, the guidelines edition and what it produced",
          run["flags"] == summary["flags"] and len(run["rules_sha256"]) == 64
          and "2023" in run["guidelines"] and run["rule_coverage"], json.dumps(
              {k: run[k] for k in ("flags", "guidelines", "code_version")}))
    check("with the data-contract facts measured on that corpus",
          "recommended_equals_sanctioned_amount" in run["contract"], str(list(run["contract"])))
    check("and the provisions the data cannot check",
          "5.4.1" in run["summary"]["unchecked_provisions"])
    unchecked = run["summary"]["unchecked_provisions"]
    check("the societies-and-trusts entry names only the part not checked",
          "1 crore" in unchecked["6.2.6.2"] and "is checked" in unchecked["6.2.6.2"],
          unchecked["6.2.6.2"])


def test_explanations() -> None:
    print("\n[9] every new rule has a plain explanation and never speaks of fraud")
    samples = [
        {"rule_id": "R-SANC-01", "details": {"days_waiting": 90}},
        {"rule_id": "R-SANC-01", "details": {"district_summary": True, "works": 12, "share": 0.5}},
        {"rule_id": "R-SANC-02", "details": {"sanctioned_works": 40, "median_days": 80, "over_limit_share": 0.7}},
        {"rule_id": "R-REPAIR-01", "details": {"fy": "2025-26", "total": 6e6, "cap": 5e6, "works": 3}},
        {"rule_id": "R-REPAIR-01", "details": {"fy": "2025-26", "total": 6e6, "cap": 5e6, "works": 3,
                                               "portal_category_works": 2, "portal_category_total": 4e6,
                                               "described_only_works": 1, "described_only_total": 2e6}},
        {"rule_id": "R-TRUST-01", "details": {"fy": "2025-26", "total": 6e6, "cap": 5e6, "works": 3}},
        {"rule_id": "P-SEQ-01", "details": {"payments": 1, "amount": 1e5, "sanction_date": "2025-07-01",
                                            "earliest": "2025-06-15"}},
        {"rule_id": "P-LATE-01", "details": {"late_payments": 1, "late_amount": 3e5, "min_days": 180,
                                             "max_days": 271, "completion_date": "2025-06-30",
                                             "share_of_paid": 0.3}},
        {"rule_id": "P-DUP-01", "details": {"sets": 1, "extra_records": 1, "repeated_amount": 5e5,
                                            "share_of_paid": 0.43, "largest": {
                                                "amount": 5e5, "vendor_name": "FIRM V1",
                                                "paid_on": "2025-09-01", "times": 2}}},
        {"rule_id": "N-NET-01", "details": {"actor_type": "vendor", "works": 6, "districts": 6,
                                            "overrun_share": 0, "same_name_vendor_ids": 12}},
        {"rule_id": "R-COST-01", "details": {"district_summary": True, "works": 12, "out_of": 20, "share": 0.6}},
        {"rule_id": "V-REV-01", "details": {"changes": [{"field": "description"}], "observed_at": "2026-09-12"}},
        {"rule_id": "V-AMT-01", "details": {"old_amount": 1e6, "new_amount": 1.25e6, "change": 2.5e5, "pct_change": 25}},
        {"rule_id": "V-VEN-01", "details": {"old": "A", "new": "B"}},
        {"rule_id": "V-PAY-01", "details": {"old_total_paid": 4e5, "new_total_paid": 2.5e5, "reduction": 1.5e5}},
        {"rule_id": "V-LIST-01", "details": {"total_paid": 9e4}},
        {"rule_id": "A-PEER-01", "details": {"peers": 40, "measures": [{"label": "time to sanction"}]}},
    ]
    briefs = [humanize({**s, "severity": "low", "summary": "x", "rule_title": "t"}) for s in samples]
    check("each has its own headline and actions",
          all(b["headline"] not in ("t", "") and b["actions"] for b in briefs),
          str([b["headline"] for b in briefs if b["headline"] == "t"]))
    plain = {s["rule_id"]: b["plain"] for s, b in zip(samples, briefs)}
    repair = [b["plain"] for s, b in zip(samples, briefs)
              if s["rule_id"] == "R-REPAIR-01" and "portal_category_works" in s["details"]]
    check("the explanations name the portal's own evidence",
          "recorded on the portal" in repair[0] and "6.2.6.2" in plain["R-TRUST-01"]
          and "12 other vendor id" in plain["N-NET-01"] and "11.2" in plain["P-LATE-01"]
          and "recorded 2 times" in plain["P-DUP-01"], plain["P-DUP-01"])
    check("none uses the words fraud, corruption or wrongdoing",
          not any(w in (b["plain"] + b["headline"]).lower() for b in briefs
                  for w in ("fraud", "corrupt", "wrongdoing")))
    context = humanize({"rule_id": "R-COST-01", "severity": "low", "summary": "x", "rule_title": "t",
                        "details": {"floor": 250000, "cost": 1e5, "standalone": False}})
    check("a context finding shows no contribution to the score", context["contribution"] == 0)
    check("revision and timeline rules map to the actions they justify",
          evidence_keys("R-SANC-01", "compliance") == ["delay", "compliance"]
          and "cost_anomaly" in evidence_keys("V-AMT-01", "revisions")
          and "compliance" in evidence_keys("V-REV-01", "revisions"))


def main() -> int:
    print("=" * 74)
    print("ASTRA analysis layer — guideline rules, contract, revisions, confidence, peers")
    print("=" * 74)
    try:
        for fn in (test_guideline_index, test_data_contract, test_compliance_rules,
                   test_orchestration, test_network, test_payments, test_revisions,
                   test_data_confidence,
                   test_peer_profile, test_run_record, test_explanations):
            fn()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 74)
    failed = [r for r in results if r[0] == FAIL]
    print(f"  {len(results) - len(failed)} passed, {len(failed)} failed")
    for _, name, detail in failed:
        print(f"    FAILED: {name} — {detail}")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
