"""Case text in Hindi: complete, faithful to the English, and still guarded.

    python tests/test_case_language.py

The backend writes every sentence about a case from config/locales/<lang>.yaml.
This checks that the Hindi catalogue matches English key for key and
placeholder for placeholder; that every explanation renders in Hindi with the
same figures and paragraph numbers as its English; that English output is
exactly what the analysis stores; that the AI synthesis keeps its guardrails
in Hindi (forbidden vocabulary, a reply in the wrong language, Devanagari
numerals); and that the API serves both languages. Reads data/astra.db
read-only where it exists; the rest is offline.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from astra import data_confidence, data_contract, explain, locale, rbac, synthesis  # noqa: E402
from astra.agents.anomaly import _PEER_MEASURES  # noqa: E402
from astra.config import DB_PATH, load_guidelines, load_rules  # noqa: E402
from astra.llm.provider import LLMResult  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
#: translations of English that lives outside en.yaml (see the header of hi.yaml)
EXTERNAL = ("clause.rule.", "clause.para.", "measure.", "field.", "rule.", "unchecked.")
#: the only templates allowed to name the forbidden vocabulary: they deny it
DENIALS = {"brief.disclaimer"}


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def skip(name: str, why: str) -> None:
    results.append((SKIP, name, why))
    print(f"  [{SKIP}] {name} — {why}")


def numbers(text: str) -> set[str]:
    return synthesis._numbers_in(text)


def english_words(text: str) -> int:
    return len(re.findall(r"\b[A-Za-z]{3,}\b", re.sub(r"\{[^}]*\}", " ", text)))


# ------------------------------------------------------------ synthetic findings

def finding(rule_id: str, agent: str, details: dict, severity: str = "high",
            clause: str | None = None) -> dict:
    return {"agent": agent, "rule_id": rule_id, "rule_title": rule_id, "clause": clause,
            "severity": severity, "entity_type": "work", "entity_id": "WS/MP1/2025-2026/1",
            "summary": f"{rule_id} summary as the agent wrote it", "details": details}


REASONS = [locale.text("en", "confidence.reason.missing", count=113),
           locale.text("en", "confidence.reason.failed", count=2)]

SAMPLES = [
    finding("R-TIME-01", "compliance", {"days_elapsed": 482, "incomplete": True, "max_days": 365},
            clause=load_rules()["rules"]["completion_time"]["clause"]
            if "completion_time" in load_rules()["rules"] else None),
    finding("R-TIME-01", "compliance", {"days_elapsed": 390, "incomplete": False}),
    finding("R-PROH-01", "compliance", {"matched_pattern": "Maintenance of", "match_type": "activity",
                                        "para": "5.2.1"}),
    finding("R-PROH-01", "compliance", {"matched_pattern": "mandir", "match_type": "asset",
                                        "para": "5.2.11"}),
    finding("R-COST-01", "compliance", {"district_summary": True, "works": 86, "out_of": 126,
                                        "share": 0.68}, severity="low"),
    finding("R-COST-01", "compliance", {"cost": 216400, "floor": 250000, "standalone": False},
            severity="low"),
    finding("R-COST-01", "compliance", {"cost": 49000000, "ceiling": 25000000}),
    finding("R-PILE-01", "compliance", {"years": 3, "cum_util_pct": 22.4,
                                        "entitlement_total": 150000000}),
    finding("R-SPIKE-01", "compliance", {"spike_pct": 88.0, "spike_fy": "2024-25",
                                         "dormant_avg_pct": 12.5}),
    finding("R-SCST-02", "compliance", {"kind": "SC", "state": "KARNATAKA", "pct": 14.4,
                                        "benchmark_pct": 15.0}),
    finding("R-SANC-01", "compliance", {"district_summary": True, "works": 139, "share": 0.95}),
    finding("R-SANC-01", "compliance", {"days_waiting": 353}),
    finding("R-SANC-02", "compliance", {"sanctioned_works": 126, "median_days": 129,
                                        "over_limit_share": 1.0}),
    finding("R-REPAIR-01", "compliance", {"fy": "2025-26", "total": 5481298, "works": 7,
                                          "cap": 5000000}),
    finding("R-REPAIR-01", "compliance", {"fy": "2025-26", "total": 7200000, "works": 10,
                                          "cap": 5000000, "portal_category_works": 7,
                                          "portal_category_total": 5100000,
                                          "described_only_works": 3,
                                          "described_only_total": 2100000}),
    finding("R-TRUST-01", "compliance", {"fy": "2025-26", "total": 10000000, "works": 20,
                                         "cap": 5000000}),
    finding("P-SEQ-01", "payments", {"payments": 2, "amount": 250000, "sanction_date": "2025-07-01",
                                     "earliest": "2025-06-15"}),
    finding("P-LATE-01", "payments", {"late_payments": 3, "late_amount": 1250000, "min_days": 180,
                                      "max_days": 271, "completion_date": "2025-06-30",
                                      "share_of_paid": 0.28}, severity="low"),
    finding("P-DUP-01", "payments", {"sets": 2, "extra_records": 3, "repeated_amount": 5000000,
                                     "share_of_paid": 0.5, "largest": {
                                         "amount": 5000000, "vendor_name": "Om Enterprises",
                                         "paid_on": "2025-09-01", "times": 2}}),
    finding("V-REV-01", "revisions", {"changes": [{"field": "description"}, {"field": "district"}],
                                      "observed_at": "2026-09-14T10:00:00+00:00"}),
    finding("V-AMT-01", "revisions", {"change": -162547, "old_amount": 540000, "new_amount": 377453,
                                      "pct_change": -30.1, "near_year_end": True}),
    finding("V-AMT-01", "revisions", {"change": 60000, "old_amount": 540000, "new_amount": 600000,
                                      "pct_change": 11.1}),
    finding("V-IA-01", "revisions", {"old": "SITAPUR", "new": "LAKHIMPUR"}),
    finding("V-VEN-01", "revisions", {"old": "HSH Interlock", "new": "Moon Enterprises",
                                      "with_new_payment": True}),
    finding("V-VEN-01", "revisions", {"old": "BDO SURANI", "new": "BDO DEHRA"}),
    finding("V-PAY-01", "revisions", {"old_total_paid": 800000, "new_total_paid": 600000,
                                      "reduction": 200000}),
    finding("V-LIST-01", "revisions", {"total_paid": 450000}),
    finding("V-FREQ-01", "revisions", {"revisions": 6, "peer_average": 1.4}),
    finding("A-PEER-01", "anomaly", {"peers": 322, "measures": [
        {"measure": m, "label": _PEER_MEASURES[m][0]} for m in ("days_to_sanction", "paid_share")]}),
    finding("A-COST-01", "anomaly", {"cost": 216400, "benchmark": 26150, "pct_vs_benchmark": 728,
                                     "peers": 6960, "peer_group": "UTTAR PRADESH|Street lights"}),
    finding("A-COST-01", "anomaly", {"cost": 8000000, "benchmark": 5000, "scale_mismatch": True,
                                     "ratio_vs_benchmark": 1600, "peers": 146}),
    finding("A-EXP-01", "anomaly", {"expenditure": 1015451, "fy": "2025-26"}),
    finding("D-DUP-01", "entity_resolution", {
        "portal_record_pair": True, "pending_work_id": "NA-Construction of shed",
        "sanctioned_work_id": "WS/MP1/2025-2026/7", "pending_stage": "Pending for Sanction",
        "recommended_date": "2025-06-01", "semantic_sim": 0.93}, severity="low"),
    finding("D-DUP-01", "entity_resolution", {
        "semantic_sim": 1.0, "pair_work_id": "WS/MP195/2023-2024/23344",
        "evidence_strength": "strong", "same_sanction_amount": True, "this_cost": 106850}),
    finding("D-DUP-01", "entity_resolution", {
        "held": True, "semantic_sim": 1.0, "pair_work_id": "WS/MP18335/2024-2025/150941",
        "evidence_strength": "held", "same_sanction_amount": True, "this_cost": 499206,
        "shared_payee": "GP Kapisda B", "photo_check": "different_photos"}, severity="low"),
    finding("D-DUP-01", "entity_resolution", {
        "held": True, "semantic_sim": 1.0, "pair_work_id": "WS/MP18235/2025-2026/254515",
        "evidence_strength": "held", "photo_check": "look_alike",
        "this_file": "1786525591620.jpeg", "other_file": "1786526642613.jpeg"}, severity="low"),
    finding("D-DUP-01", "entity_resolution", {
        "photo_match": True, "semantic_sim": 0.95, "pair_work_id": "WS/MP18178/2024-2025/174398",
        "evidence_strength": "same photo file", "photo_check": "same_file",
        "photo_cluster": ["WS/MP18178/2025-2026/202071", "WS/MP18178/2024-2025/174398",
                          "WS/MP18178/2024-2025/174399"]}, severity="critical"),
    finding("D-DUP-01", "entity_resolution", {
        "batch": True, "held": True, "batch_size": 6, "batch_mp": "RADHE SHYAM RATHIYA",
        "batch_total": 2994230, "batch_letters": 1, "batch_payees": 5, "evidence_strength": "batch",
        "same_payee_groups": [{"payee": "GP Kapisda B", "work_ids": [
            "WS/MP18335/2024-2025/150940", "WS/MP18335/2024-2025/150941"]}]}, severity="low"),
    finding("D-DUP-02", "entity_resolution", {
        "cluster_size": 10, "district": "NANDURBAR", "total_cost": 1700000,
        "normalised_description": "install 2 set of street light"}),
    finding("N-NET-01", "network", {"actor_type": "vendor", "works": 13, "districts": 7,
                                    "overrun_share": 0.65, "likely_national_supplier": True}),
    finding("N-NET-01", "network", {"actor_type": "vendor", "works": 6, "districts": 6,
                                    "overrun_share": 0, "vendor_id": "54018",
                                    "same_name_vendor_ids": 4}),
    finding("N-NET-01", "network", {"actor_type": "agency", "works": 26, "districts": 1,
                                    "overrun_share": 0.65}),
    finding("N-NET-01", "network", {"actor_type": "district_authority", "works": 102,
                                    "districts": 1, "overrun_share": 0.6}),
    finding("R-TIME-01", "compliance", {"days_elapsed": 617, "incomplete": True,
                                        "data_confidence": "reduced",
                                        "data_confidence_reasons": REASONS}),
]


def rendered(signal: dict) -> str:
    return " ".join([signal["headline"], signal["plain"], signal["metric"] or "",
                     signal["benchmark"] or "", *signal["actions"]])


# ----------------------------------------------------------------------- tests

def test_catalogues() -> None:
    print("\n[1] The Hindi catalogue matches English")
    en, hi = locale.catalogue("en"), locale.catalogue("hi")
    missing = sorted(set(en) - set(hi))
    check("every English template has a Hindi one", not missing, f"{len(en)} templates; missing {missing[:5]}")
    stray = sorted(k for k in set(hi) - set(en) if not k.startswith(EXTERNAL))
    check("Hindi has no templates English lacks (beyond the named external texts)", not stray, str(stray[:5]))

    def fields(value) -> list[set[str]]:
        items = value if isinstance(value, list) else [value]
        return [locale.placeholders(v) for v in items]

    wrong = [k for k in en if k in hi and fields(en[k]) != fields(hi[k])]
    check("every {placeholder} kept exactly, and lists keep their length", not wrong, str(wrong[:5]))
    off_script = [k for k, v in hi.items()
                  for item in (v if isinstance(v, list) else [v])
                  if (english_words(en[k] if isinstance(en.get(k), str) else item) >= 1
                      or k.startswith(EXTERNAL)) and not DEVANAGARI.search(item)]
    check("every Hindi text is written in Devanagari", not off_script, str(off_script[:5]))
    banned = [k for lang in ("en", "hi") for k, v in locale.catalogue(lang).items() if k not in DENIALS
              and synthesis.scan_language(*(v if isinstance(v, list) else [v]))]
    check("no template uses the forbidden vocabulary, in either language", not banned, str(banned[:5]))

    rules, ids = load_rules(), []

    def walk(node, out):
        if isinstance(node, dict):
            if node.get("id"):
                out.append(node)
            for v in node.values():
                walk(v, out)
        elif isinstance(node, list):
            for v in node:
                walk(v, out)
    walk(rules, ids)
    need = {f"clause.rule.{r['id']}" for r in ids if r.get("clause")} \
        | {f"clause.para.{p}" for p in load_guidelines()["paras"]} \
        | {f"rule.{r['id']}" for r in ids} \
        | {f"measure.{m}" for m in _PEER_MEASURES} \
        | {f"field.{f}" for f in data_contract.PLACE_FIELDS + data_contract.AMOUNT_FIELDS} \
        | {f"unchecked.{p}" for p in data_contract.UNCHECKABLE}
    absent = sorted(k for k in need if k not in hi)
    check("Hindi covers every rule clause, guideline paragraph, rule title, measure, field and "
          "unchecked provision", not absent, f"{len(need)} needed; missing {absent[:5]}")


def test_explanations() -> None:
    print("\n[2] Every explanation renders in Hindi with the same figures")
    covered = {f["rule_id"] for f in SAMPLES}
    templated = {k.split(".")[0] for k in locale.catalogue("en") if re.match(r"^[RVADN]-", k)}
    check("the samples reach every rule with an explanation template", templated <= covered,
          str(sorted(templated - covered)))
    failures, english_left, figures = [], [], []
    for f in SAMPLES:
        try:
            en, hi = explain.humanize(f, "en"), explain.humanize(f, "hi")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{f['rule_id']}: {exc!r}")
            continue
        for part in ("headline", "plain", "metric", "benchmark"):
            if hi[part] and not DEVANAGARI.search(hi[part]):
                english_left.append(f"{f['rule_id']}.{part}")
        if not all(DEVANAGARI.search(a) for a in hi["actions"]):
            english_left.append(f"{f['rule_id']}.actions")
        if numbers(rendered(en)) != numbers(rendered(hi)):
            figures.append(f"{f['rule_id']}: {sorted(numbers(rendered(en)) ^ numbers(rendered(hi)))}")
        if en["contribution"] != hi["contribution"] or en["severity"] != hi["severity"]:
            figures.append(f"{f['rule_id']}: score changed with language")
    check("every sample renders in both languages", not failures, "; ".join(failures[:3]))
    check("no Hindi explanation falls back to English", not english_left, str(english_left[:5]))
    check("Hindi states exactly the English figures and paragraph numbers", not figures,
          "; ".join(figures[:3]))

    en_brief = explain.build_brief({"findings": SAMPLES[:6]}, "mp")
    hi_brief = explain.build_brief({"findings": SAMPLES[:6]}, "mp", lang="hi")
    check("a Hindi brief keeps the English order, shares and repeats",
          [(s["rule_id"], s["share_pct"], s["occurrences"]) for s in en_brief["signals"]]
          == [(s["rule_id"], s["share_pct"], s["occurrences"]) for s in hi_brief["signals"]])
    text_parts = [hi_brief[k] for k in ("tier_label", "lens", "opening", "primary_risk", "primary_plain",
                                        "corroboration", "closing", "disclaimer")] + hi_brief["actions"]
    check("every part of a Hindi brief is in Hindi", all(DEVANAGARI.search(p) for p in text_parts),
          str([p for p in text_parts if not DEVANAGARI.search(p)][:3]))
    check("an empty case still gets a Hindi brief with an action",
          DEVANAGARI.search(explain.build_brief({"findings": []}, "nonsense", lang="hi")["actions"][0]))
    check("an unknown language reads as English",
          explain.humanize(SAMPLES[0], "xx")["headline"] == explain.humanize(SAMPLES[0])["headline"])

    stored = explain.clause_text(SAMPLES[0]["clause"], "hi") if SAMPLES[0]["clause"] else None
    cite = locale.text("en", "clause.marker.cost_ceiling")
    check("guideline clauses are shown in Hindi, paragraph numbers intact",
          DEVANAGARI.search(explain.clause_text(explain.cite("5.2.11"), "hi") or "")
          and "5.2.11" in explain.clause_text(explain.cite("5.2.11"), "hi")
          and DEVANAGARI.search(explain.clause_text(cite, "hi"))
          and (stored is None or DEVANAGARI.search(stored)))
    check("a clause no translation knows is shown as written",
          explain.clause_text("Some future clause", "hi") == "Some future clause")


def test_stored_english() -> None:
    print("\n[3] Text stored in English is recognised and re-rendered")
    samples = {
        "confidence.reason.parity_differs": {}, "confidence.reason.unchecked": {},
        "confidence.reason.failed": {"count": 3}, "confidence.reason.stale": {"since": "12 Sep 2026 03:00"},
        "confidence.reason.held_back": {}, "confidence.reason.missing": {"count": 1234},
        "title.district_authority": {"name": "Sitapur"}, "title.constituency": {"name": "GODDA"},
        "stood_down.years": {"needed": 3, "years": 2}, "stood_down.no_edits": {},
        "stood_down.history": {"span": 1, "revised": 77, "days": 90, "works": 200},
        "router.ready": {}, "router.skipped": {"missing": ["sanction_date"]},
    }
    wrong = []
    for key, values in samples.items():
        english = locale.text("en", key, **values)
        if locale.rerender("hi", english, [key]) != locale.text("hi", key, **{
                k: (v if isinstance(v, str) else str(v)) for k, v in values.items()}):
            wrong.append(key)
    check("each stored English sentence re-renders as its Hindi template", not wrong, str(wrong))
    check("an unrecognised sentence is kept as written",
          locale.rerender("hi", "Something else entirely", samples) == "Something else entirely")

    note = explain.context_note([SAMPLES[-1]], 1234)
    check("the other-cases count is read back from a stored note",
          explain.parse_agency_other_flags(note) == 1234 and explain.parse_agency_other_flags("") == 0, note)
    hi_note = explain.context_note([SAMPLES[-1]], 1234, "hi")
    check("the Hindi note carries the same count and reasons",
          numbers(hi_note) == numbers(note) and DEVANAGARI.search(hi_note), hi_note)

    health = pd.DataFrame([{"shard_id": "LS:1", "exact": 0, "consecutive_failures": 2,
                            "stale_since": "2026-01-01T00:00:00+00:00", "lifecycle": "QUARANTINED",
                            "awaiting_removal": 5},
                           {"shard_id": "LS:2", "exact": None, "consecutive_failures": 0,
                            "stale_since": None, "lifecycle": "ACTIVE", "awaiting_removal": 0}])
    reasons = [r for _level, rs in data_confidence.area_confidence(health).values() for r in rs]
    unrecognised = [r for r in reasons if explain.confidence_reason(r, "hi") == r]
    check("every data-confidence reason the analysis writes has a Hindi form",
          len(reasons) == 6 and not unrecognised, str(unrecognised))


def test_synthesis() -> None:
    print("\n[4] AI synthesis in Hindi keeps its guardrails")
    case = {"flag_id": "F-TEST", "entity_type": "work", "entity_id": "WS/MP1/2025-2026/1",
            "display_title": "Community hall", "state": "UTTAR PRADESH", "district": "SITAPUR",
            "constituency": "SITAPUR", "era": "post2023", "risk_score": 75.0, "review_status": "pending",
            "findings": [SAMPLES[0], SAMPLES[24], SAMPLES[28]]}
    work = {"description": "Construction of community hall", "category": "Community hall",
            "ia_name": "SITAPUR(DM)", "vendor_name": None, "sanctioned_amount": 216400,
            "total_paid": None, "status": "Sanction", "sanction_date": "2025-01-01", "completion_date": None}

    det = synthesis.deterministic_synthesis(case, "district", work, lang="hi")
    parts = [det["tier_label"], det["risk_level"], det["key_risk_summary"], det["case_explanation"],
             det["authority_specific_summary"], det["plan_rationale"], det["constraint_notice"],
             det["human_review_notice"], *det["limitations_or_missing_evidence"],
             *(a["reason"] for a in det["action_plan"]), *(s["evidence"] for s in det["supporting_signals"])]
    check("the deterministic synthesis is entirely in Hindi", all(DEVANAGARI.search(p) for p in parts),
          str([p for p in parts if not DEVANAGARI.search(p)][:3]))
    en_det = synthesis.deterministic_synthesis(case, "district", work)
    check("its action plan and score match the English one",
          [a["action_id"] for a in det["action_plan"]] == [a["action_id"] for a in en_det["action_plan"]]
          and det["risk_score"] == en_det["risk_score"])

    good_hi = {
        "key_risk_summary": "कार्य एक वर्ष के मानक से देर में है और तुलनीय कार्यों से महँगा है।",
        "case_explanation": "सिस्टम ने पाया कि इस कार्य का सत्यापन आवश्यक है।",
        "supporting_signals": [{"source_agent": "अनुपालन एजेंट", "signal": "विलंब", "evidence": "482 दिन"}],
        "authority_specific_summary": "आगे भुगतान से पहले सत्यापन आवश्यक है।",
        "action_plan": [{"action_id": "verify_documents", "reason": "पहले अभिलेख।"}],
        "plan_rationale": "उच्च स्तर पर भेजने से पहले दस्तावेज़ों की जाँच।",
        "limitations_or_missing_evidence": ["ज़मीन पर भौतिक कार्य का सत्यापन नहीं हो सकता।"],
    }
    prompts: list[str] = []

    def stub(payload):
        def call(system, user, schema, **kw):
            prompts.append(user)
            return LLMResult(True, data=payload, model="stub-model", latency_ms=5)
        return call

    real = synthesis.complete_json
    try:
        synthesis.complete_json = stub(good_hi)
        r = synthesis.synthesise(case, "district", work, lang="hi")
        check("a Hindi model reply is accepted", r["source"] == "groq" and r["key_risk_summary"] == good_hi["key_risk_summary"])
        check("the model is told to write Hindi, with its forbidden words",
              "LANGUAGE:" in prompts[-1] and "धोखाधड़ी" in prompts[-1]
              and locale.text("hi", "synthesis.insufficient") in prompts[-1])
        packet_hi = synthesis.build_evidence_packet(case, "district", work, "hi")
        packet_en = synthesis.build_evidence_packet(case, "district", work)
        check("the model sees the evidence in Hindi, with exactly the English figures",
              json.dumps(packet_hi, indent=2, default=str, ensure_ascii=False) in prompts[-1]
              and numbers(json.dumps(packet_hi, default=str, ensure_ascii=False))
              == numbers(json.dumps(packet_en, default=str, ensure_ascii=False))
              and DEVANAGARI.search(packet_hi["agent_findings"][0]["explanation"]))
        check("the risk level and notices are re-imposed in Hindi",
              r["risk_level"] == locale.text("hi", "risk_band.high")
              and DEVANAGARI.search(r["constraint_notice"]))

        synthesis.synthesise(case, "district", work)
        check("an English request carries no language instruction", "LANGUAGE:" not in prompts[-1])

        synthesis.complete_json = stub(dict(good_hi, key_risk_summary="The work is overdue."))
        r = synthesis.synthesise(case, "district", work, lang="hi")
        check("a reply in English for a Hindi reader falls back to the Hindi brief",
              r["source"] == "deterministic" and r["fallback_reason"] == "language_mismatch"
              and DEVANAGARI.search(r["key_risk_summary"]))

        for lang in ("en", "hi"):
            synthesis.complete_json = stub(dict(good_hi, key_risk_summary="", case_explanation=" "))
            r = synthesis.synthesise(case, "district", work, lang=lang)
            check(f"an empty model reply falls back ({lang})",
                  r["fallback_reason"] == "incomplete_response" and r["key_risk_summary"])

        synthesis.complete_json = stub(dict(
            good_hi,
            action_plan=[{"action_id": "verify_documents", "reason": ""},
                         {"action_id": "cross_check_duplicate", "reason": "Check the other record."}],
            limitations_or_missing_evidence=["Data confidence is reduced.", "ज़मीनी सत्यापन संभव नहीं।"],
            supporting_signals=[{"source_agent": "Compliance Agent", "signal": "Overdue", "evidence": "482 days"}]))
        r = synthesis.synthesise(case, "district", work, lang="hi")
        check("an action the model left unexplained, or explained in English, cites its finding in Hindi",
              r["source"] == "groq" and all(DEVANAGARI.search(a["reason"]) for a in r["action_plan"]),
              str([a["reason"] for a in r["action_plan"]]))
        check("a limitation or signal left in English is not shown to a Hindi reader",
              r["limitations_or_missing_evidence"] == ["ज़मीनी सत्यापन संभव नहीं।"]
              and all(DEVANAGARI.search(x["signal"]) for x in r["supporting_signals"]))

        for phrase in ("इस कार्य में धोखाधड़ी हुई है।", "ठेकेदार दोषी है।", "यह भ्रष्टाचार का मामला है।"):
            synthesis.complete_json = stub(dict(good_hi, case_explanation=phrase))
            r = synthesis.synthesise(case, "district", work, lang="hi")
            if not check(f"accusatory Hindi is refused: {phrase}", r["fallback_reason"] == "language_guardrail"):
                break

        synthesis.complete_json = stub(dict(good_hi, case_explanation="यह कार्य 2014 से लंबित है।"))
        r = synthesis.synthesise(case, "district", work, lang="hi")
        check("an escaped character in the evidence does not make a figure look verified",
              "2014" in r["unverified_numbers"], str(r["unverified_numbers"]))

        synthesis.complete_json = stub(dict(good_hi, case_explanation="लागत ९९९९९९ रुपये है।"))
        r = synthesis.synthesise(case, "district", work, lang="hi")
        check("a figure written in Devanagari numerals is still checked against the evidence",
              "999999" in r["unverified_numbers"], str(r["unverified_numbers"]))

        synthesis._CACHE.clear()
        synthesis.complete_json = stub(good_hi)
        a = synthesis.synthesise_cached(case, "district", work, use_llm=False)
        b = synthesis.synthesise_cached(case, "district", work, use_llm=False, lang="hi")
        c = synthesis.synthesise_cached(case, "district", work, use_llm=False, lang="hi")
        check("English and Hindi are cached separately",
              not a["cached"] and not b["cached"] and c["cached"]
              and a["key_risk_summary"] != b["key_risk_summary"])
    finally:
        synthesis.complete_json = real
        synthesis._CACHE.clear()

    plan = rbac.default_plan("state", 75, case["findings"], explain.build_brief(case, "state", lang="hi")["signals"], "hi")
    check("permitted-action reasons are in Hindi", plan and all(DEVANAGARI.search(a["reason"]) for a in plan))


def _stored_flags(limit: int) -> list[dict]:
    if not DB_PATH.exists():
        return []
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM flags ORDER BY flag_id LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out = []
    for r in rows:
        f = dict(r)
        f["findings"] = json.loads(f.pop("findings_json") or "[]")
        f["tier_briefs"] = json.loads(f.pop("tier_briefs_json") or "{}")
        out.append(f)
    return out


def test_stored_cases() -> None:
    print("\n[5] Stored cases: English unchanged, Hindi faithful")
    flags = _stored_flags(3000)
    if not flags:
        skip("stored cases", f"no flags in {DB_PATH}")
        return
    changed, unfaithful = [], []
    for f in flags:
        for tier in explain.TIERS:
            note = explain.context_note(f["findings"], explain.parse_agency_other_flags(
                (f["tier_briefs"].get("district") or {}).get("context_note")))
            rebuilt = json.loads(json.dumps(explain.build_brief(f, tier, {"note": note}), default=str))
            if rebuilt != f["tier_briefs"].get(tier):
                changed.append(f"{f['flag_id']}|{tier}")
        hi = explain.brief_for(f, "district", "hi")
        en = f["tier_briefs"]["district"]
        for s_en, s_hi in zip(en["signals"], hi["signals"]):
            if numbers(rendered(s_en)) != numbers(rendered(s_hi)):
                unfaithful.append(f"{f['flag_id']}|{s_en['rule_id']}")
        if numbers(en["context_note"]) != numbers(hi["context_note"]):
            unfaithful.append(f"{f['flag_id']}|note")
    check("English briefs are rebuilt exactly as the analysis stored them", not changed,
          f"{len(flags) * 4:,} briefs; differing {changed[:3]}")
    check("Hindi briefs state the stored English figures", not unfaithful,
          f"{len(flags):,} cases; differing {unfaithful[:3]}")


def test_api() -> None:
    print("\n[6] The API serves both languages")
    if not _stored_flags(1):
        skip("API", f"no flags in {DB_PATH}")
        return
    from fastapi.testclient import TestClient
    from astra.api.main import app
    client = TestClient(app)

    listing = client.get("/cases", params={"limit": 5, "lang": "hi"}).json()["cases"]
    fid = listing[0]["flag_id"]
    check("the case list gives each case's primary signal in Hindi",
          all(DEVANAGARI.search(c["primary_signal"] or "") for c in listing))
    plain = client.get(f"/flags/case/{fid}").json()
    check("English is the default and unchanged by the parameter",
          plain == client.get(f"/flags/case/{fid}", params={"lang": "en"}).json()
          and "explained" not in plain["findings"][0])
    hi = client.get(f"/flags/case/{fid}", params={"lang": "hi"}).json()
    check("a Hindi case has its brief and every finding explained in Hindi",
          DEVANAGARI.search(hi["brief"]["primary_plain"])
          and all(DEVANAGARI.search(f["explained"]["headline"]) for f in hi["findings"])
          and hi["risk_score"] == plain["risk_score"])
    check("the audit record itself stays as the agent wrote it",
          [f["summary"] for f in hi["findings"]] == [f["summary"] for f in plain["findings"]])
    check("an unsupported language is refused",
          client.get(f"/flags/case/{fid}", params={"lang": "xx"}).status_code == 422)
    synth = client.get(f"/flags/case/{fid}/synthesis",
                       params={"lang": "hi", "use_llm": False}).json()
    check("the synthesis endpoint answers in Hindi", DEVANAGARI.search(synth["key_risk_summary"])
          and DEVANAGARI.search(synth["constraint_notice"]))
    actions = client.get(f"/flags/case/{fid}/actions", params={"lang": "hi"}).json()
    check("the permitted actions come with a Hindi notice, same actions",
          DEVANAGARI.search(actions["constraint_notice"])
          and [a["action_id"] for a in actions["allowed_actions"]]
          == [a["action_id"] for a in client.get(f"/flags/case/{fid}/actions").json()["allowed_actions"]])
    meta = client.get("/meta/pipeline", params={"lang": "hi"}).json()
    reasons = [t["reason"] for t in meta.get("router_trace") or []] + \
        [r["stood_down"] for r in meta.get("rule_coverage") or [] if r.get("stood_down")]
    check("the pipeline page's reasons are in Hindi", all(DEVANAGARI.search(r) for r in reasons),
          str([r for r in reasons if not DEVANAGARI.search(r)][:2]))


def main() -> int:
    print("=" * 78)
    print("ASTRA case text in Hindi")
    print("=" * 78)
    for test in (test_catalogues, test_explanations, test_stored_english, test_synthesis,
                 test_stored_cases, test_api):
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            check(f"{test.__name__} ran", False, repr(exc))
    print("\n" + "=" * 78)
    failed = [r for r in results if r[0] == FAIL]
    skipped = [r for r in results if r[0] == SKIP]
    print(f"  {len(results) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped")
    for _, name, detail in failed:
        print(f"    FAILED: {name} — {detail[:200]}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
