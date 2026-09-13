"""Plain-language explanation layer.

The agents emit precise, statistical evidence ("robust z=19.6, n=6509 peers,
era=post2023"). That is the right thing for an audit trail and the wrong thing
to put in front of a District Magistrate. This module turns agent findings into
language a non-technical government officer can act on, WITHOUT inventing
anything: every sentence is derived from values the agents actually produced.

It provides:
  short_title()      a readable case title from a long free-text description
  humanize()         one finding -> headline, plain explanation, metric,
                     benchmark, and its contribution to the risk score
  build_brief()      a whole flag -> structured, authority-specific brief:
                     primary risk, supporting signals, evidence, actions

Framing rules enforced here:
  * nothing is ever described as fraud, corruption or wrongdoing;
  * detections are "indicators requiring verification", never conclusions;
  * every brief ends with the human-review statement.
"""
from __future__ import annotations

import re

from .config import load_rules

# --------------------------------------------------------------- titles

_NOISE = re.compile(
    r"\b(as per (the )?attached( letter| list)?|attached letter|continued? work|"
    r"continue work|balance work|as per estimate|as per (the )?list|"
    r"under mplads?( fund[s]?)?|mplads? fund[s]?|work order no\.?\s*\S*)\b",
    re.IGNORECASE)
_WS = re.compile(r"\s+")


def short_title(description: str | None, category: str | None = None,
                max_len: int = 62) -> str:
    """A compact, readable case title.

    Prefers the free-text description (which names the actual asset and place),
    strips boilerplate, and falls back to the standardised work type. The full
    original text is always kept and shown in the case detail view.
    """
    text = _WS.sub(" ", str(description or "")).strip(" .,-–—")
    text = _WS.sub(" ", _NOISE.sub("", text)).strip(" .,-–—")
    if len(text) < 8:
        text = _WS.sub(" ", str(category or "")).strip()
    if not text:
        return "Untitled work"
    if len(text) <= max_len:
        return text[0].upper() + text[1:]
    cut = text[:max_len]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    return (cut[0].upper() + cut[1:]).rstrip(" .,-–—") + "…"


def rupees(value) -> str:
    """Indian-convention money formatting: lakh / crore."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1e7:
        return f"₹{v / 1e7:,.2f} crore"
    if abs(v) >= 1e5:
        return f"₹{v / 1e5:,.2f} lakh"
    return f"₹{v:,.0f}"


# --------------------------------------------------------------- agents

AGENT_LABEL = {
    "compliance": "Compliance Agent",
    "anomaly": "Statistical Anomaly Agent",
    "entity_resolution": "Entity Resolution Agent",
    "network": "Network Analysis Agent",
}
AGENT_ROLE = {
    "compliance": "Checks each work against MPLADS scheme rules",
    "anomaly": "Compares cost against similar works in the same state",
    "entity_resolution": "Looks for the same work recorded more than once",
    "network": "Looks at contractor and agency patterns across districts",
}

SEVERITY_LABEL = {"critical": "Critical", "high": "High",
                  "medium": "Medium", "low": "Low"}


def risk_band(score: float) -> tuple[str, str]:
    """(label, colour) for a composite risk score."""
    if score >= 70:
        return "High risk", "#c0392b"
    if score >= 40:
        return "Medium risk", "#d68910"
    return "Low risk", "#5d6d7e"


# --------------------------------------------------------------- findings

def humanize(finding: dict) -> dict:
    """One agent finding -> plain-language, non-accusatory explanation."""
    rid = finding.get("rule_id", "")
    d = finding.get("details") or {}
    sev = finding.get("severity", "medium")
    weights = load_rules()["risk_score"]["weights"]

    out = {
        "rule_id": rid,
        "agent": finding.get("agent", ""),
        "agent_label": AGENT_LABEL.get(finding.get("agent", ""), finding.get("agent", "")),
        "severity": sev,
        "severity_label": SEVERITY_LABEL.get(sev, sev.title()),
        "contribution": 0 if d.get("portal_record_pair") else weights.get(sev, 0),
        "clause": finding.get("clause"),
        "headline": finding.get("rule_title", rid),
        "plain": finding.get("summary", ""),
        "metric": None,
        "benchmark": None,
        "technical": finding.get("summary", ""),
        "actions": [],
    }

    # ---- compliance: completion norm
    if rid == "R-TIME-01":
        days = d.get("days_elapsed")
        years = (days or 0) / 365.0
        out["headline"] = "Work is overdue against the one-year completion norm"
        out["plain"] = (
            f"This work was sanctioned {days:,} days ago "
            f"({years:.1f} years) and is {'still not complete' if d.get('incomplete') else 'recorded as completed late'}. "
            f"The scheme expects works to finish within one year of sanction."
            if days else out["plain"])
        out["metric"] = f"{days:,} days since sanction" if days else None
        out["benchmark"] = f"{d.get('max_days', 365)} days (scheme norm)"
        out["actions"] = [
            "Obtain the current physical progress report from the implementing agency.",
            "Ask the agency to record the reason for delay and a revised completion date.",
        ]

    # ---- compliance: prohibited / non-permissible
    elif rid == "R-PROH-01":
        term = d.get("matched_pattern", "")
        if d.get("match_type") == "activity":
            out["headline"] = "Expenditure may not be an admissible MPLADS charge"
            out["plain"] = (
                f"The description indicates '{term}'. Routine operation and maintenance "
                f"is the state government's responsibility and is not normally payable "
                f"from MPLADS funds. The scope needs confirming before any release.")
            out["actions"] = ["Confirm from the estimate whether this is new asset creation or routine upkeep."]
        else:
            out["headline"] = "Asset may fall in the scheme's non-permissible list"
            out["plain"] = (
                f"The work description suggests the asset being created is a "
                f"'{term}', which is not normally permissible under the scheme. "
                f"This needs confirming — descriptions often name nearby landmarks, "
                f"so the actual asset may well be permissible.")
            out["actions"] = ["Confirm from the sanction file what asset is actually being built."]
        out["metric"] = f"matched term: '{term}'"
        out["benchmark"] = "MPLADS non-permissible works list"

    # ---- compliance: cost floor / ceiling
    elif rid == "R-COST-01":
        cost, floor, ceil = d.get("cost"), d.get("floor"), d.get("ceiling")
        if floor is not None:
            out["headline"] = "Sanctioned below the scheme's minimum work value"
            out["plain"] = (
                f"This work is sanctioned at {rupees(cost)}, below the "
                f"{rupees(floor)} minimum permitted per work. Very small works can "
                f"also indicate a larger work split into pieces, so the sanction "
                f"basis is worth checking.")
            out["metric"] = f"{rupees(cost)} sanctioned"
            out["benchmark"] = f"{rupees(floor)} minimum per work"
            out["actions"] = ["Check whether this is part of a larger work split into smaller sanctions."]
        else:
            out["headline"] = "Sanctioned above the single-work review ceiling"
            out["plain"] = (f"This work is sanctioned at {rupees(cost)}, above the "
                            f"{rupees(ceil)} value at which a single work warrants "
                            f"closer scrutiny.")
            out["metric"] = f"{rupees(cost)} sanctioned"
            out["benchmark"] = f"{rupees(ceil)} review ceiling"
            out["actions"] = ["Verify the detailed estimate and technical sanction for a work of this size."]

    # ---- compliance: idle funds
    elif rid == "R-PILE-01":
        out["headline"] = "Funds are lying largely unspent"
        out["plain"] = (
            f"Only {d.get('cum_util_pct', 0):.0f}% of the "
            f"{rupees(d.get('released_total'))} released has been spent across "
            f"{d.get('years')} years. MPLADS funds do not lapse, so unspent money "
            f"accumulates rather than being returned — this is an efficiency "
            f"concern, not a rule breach.")
        out["metric"] = f"{d.get('cum_util_pct', 0):.0f}% of funds utilised"
        out["benchmark"] = f"{load_rules()['rules']['unspent_pileup']['max_cum_util_pct']:.0f}% threshold"
        out["actions"] = ["Review the pending works list and identify what is blocking execution."]

    # ---- compliance: spike
    elif rid == "R-SPIKE-01":
        out["headline"] = "Sudden surge in spending after dormant years"
        out["plain"] = (
            f"Utilisation jumped to {d.get('spike_pct', 0):.0f}% in {d.get('spike_fy')} "
            f"after years averaging {d.get('dormant_avg_pct', 0):.0f}%. Because MPLADS "
            f"funds never lapse, a late surge can indicate rushed, year-end spending.")
        out["metric"] = f"{d.get('spike_pct', 0):.0f}% in {d.get('spike_fy')}"
        out["benchmark"] = f"{d.get('dormant_avg_pct', 0):.0f}% in prior years"
        out["actions"] = ["Check whether works sanctioned in the surge year followed normal appraisal."]

    # ---- compliance: SC/ST screening proxy
    elif rid == "R-SCST-02":
        out["headline"] = f"Low share of spending in {d.get('kind', 'SC/ST')}-reserved constituencies"
        out["plain"] = (
            f"In {d.get('state')}, {d.get('kind')}-reserved constituencies received "
            f"{d.get('pct', 0):.1f}% of MPLADS spending, against a {d.get('benchmark_pct')}% "
            f"scheme benchmark. This is a screening indicator only — the scheme's "
            f"requirement concerns SC/ST areas, which this dataset does not itemise.")
        out["metric"] = f"{d.get('pct', 0):.1f}% of state spending"
        out["benchmark"] = f"{d.get('benchmark_pct')}% scheme benchmark"
        out["actions"] = ["Pull district-level allocation data to assess actual area-wise compliance."]

    # ---- anomaly: cost vs peers
    elif rid == "A-COST-01":
        cost, bench = d.get("cost"), d.get("benchmark")
        pct, peers = d.get("pct_vs_benchmark", 0), d.get("peers", 0)
        if d.get("scale_mismatch"):
            out["headline"] = "Cost far outside the range for this work type"
            out["plain"] = (
                f"This work costs {rupees(cost)} against a typical {rupees(bench)} "
                f"for the same type of work in this state — about "
                f"{d.get('ratio_vs_benchmark', 0):,.0f} times higher. A gap this wide "
                f"usually means the work is a different scale to its peers (for example "
                f"a whole-ward installation recorded under a single-unit category), "
                f"so the classification should be checked before reading it as an overrun.")
            out["actions"] = ["Confirm the work's scope and whether it is categorised correctly."]
        else:
            out["headline"] = f"Cost is {pct:.0f}% above comparable works"
            out["plain"] = (
                f"This work costs {rupees(cost)}. Comparable works of the same type in "
                f"{d.get('peer_group', '').split('|')[0]} typically cost {rupees(bench)} — "
                f"this is {pct:.0f}% higher. The comparison uses {peers:,} similar works "
                f"from the same period.")
            out["actions"] = [
                "Compare the estimate against the state Schedule of Rates for this work type.",
                "Verify measurements and quantities recorded in the measurement book.",
            ]
        out["metric"] = f"{rupees(cost)} actual"
        out["benchmark"] = f"{rupees(bench)} typical ({peers:,} comparable works)"

    # ---- anomaly: expenditure pattern
    elif rid == "A-EXP-01":
        out["headline"] = "Annual spending is unusual compared with other constituencies"
        out["plain"] = (
            f"Spending of {rupees(d.get('expenditure'))} in {d.get('fy')} sits far "
            f"outside the normal range for that year across all constituencies.")
        out["metric"] = f"{rupees(d.get('expenditure'))} in {d.get('fy')}"
        out["benchmark"] = "national distribution for the same year"
        out["actions"] = ["Review the year's sanction and release records for this constituency."]

    # ---- entity resolution: near-duplicate
    elif rid == "D-DUP-01" and d.get("portal_record_pair"):
        out["headline"] = "Listed twice on the portal: recommendation and sanctioned record"
        out["plain"] = (
            f"The portal still lists the original recommendation {d.get('pending_work_id')}"
            f" (stage \"{d.get('pending_stage') or 'not recorded'}\") alongside the "
            f"sanctioned record {d.get('sanctioned_work_id')}, for the same member and "
            f"the same recommendation date ({d.get('recommended_date')}). This is how "
            f"the portal keeps a work that has moved from recommendation to sanction, "
            f"so it is probably one work rather than a double entry. It is shown for "
            f"completeness and does not add to the risk score.")
        out["metric"] = (f"{d.get('semantic_sim', 0) * 100:.0f}% description match, "
                         f"same member and date")
        out["benchmark"] = "one record per work expected"
        out["actions"] = [
            f"If needed, confirm on the portal that {d.get('pending_work_id')} is the "
            f"recommendation that became {d.get('sanctioned_work_id')}.",
        ]

    elif rid == "D-DUP-01":
        same_cost = d.get("same_sanction_amount")
        strong = d.get("evidence_strength") == "strong"
        out["headline"] = ("Very likely the same work recorded twice" if strong
                           else "Closely similar to another work nearby")
        out["plain"] = (
            f"The description is {d.get('semantic_sim', 0) * 100:.0f}% identical to work "
            f"{d.get('pair_work_id')} in the same district"
            + (f", and both are sanctioned for the same amount "
               f"({rupees(d.get('this_cost'))})" if same_cost else "")
            + ". Approval checks look at one work at a time, so a repeat entry like "
              "this is not caught by the normal workflow. The two records should be "
              "checked to confirm whether they are separate assets.")
        out["metric"] = f"{d.get('semantic_sim', 0) * 100:.0f}% description match"
        out["benchmark"] = f"{load_rules()['duplicates']['semantic_threshold'] * 100:.0f}% similarity threshold"
        out["actions"] = [
            f"Compare this work's file against work {d.get('pair_work_id')}.",
            "Confirm the two works are at different locations before further release.",
        ]

    # ---- entity resolution: generic cluster
    elif rid == "D-DUP-02":
        out["headline"] = "Several works share one vague description"
        out["plain"] = (
            f"{d.get('cluster_size')} works in {d.get('district')} are all described "
            f"only as \"{d.get('normalised_description', '')[:50]}\", together worth "
            f"{rupees(d.get('total_cost'))}. They are probably distinct assets, but the "
            f"descriptions are too thin for anyone to verify that from the record.")
        out["metric"] = f"{d.get('cluster_size')} works, {rupees(d.get('total_cost'))}"
        out["benchmark"] = "distinct locations expected per work"
        out["actions"] = ["Ask the agency to record specific locations for each of these works."]

    # ---- network
    elif rid == "N-NET-01":
        kind = d.get("actor_type", "agency")
        out["headline"] = f"Unusual pattern for this {kind}"
        bits = []
        if d.get("districts", 0) > 1:
            bits.append(f"appears on {d.get('works')} works across "
                        f"{d.get('districts')} districts")
        if d.get("overrun_share", 0) > 0:
            bits.append(f"{d.get('overrun_share', 0) * 100:.0f}% of its works are priced "
                        f"above comparable works")
        out["plain"] = (
            f"This {kind} " + " and ".join(bits) + ". "
            + ("Its work types (vehicles, books, equipment) are commonly supplied "
               "nationally, so wide coverage may be entirely normal. "
               if d.get("likely_national_supplier") else "")
            + "This is a pattern worth understanding, not an allegation against the firm.")
        out["metric"] = f"{d.get('works')} works, {d.get('districts')} districts"
        out["benchmark"] = f"{load_rules()['network']['district_spread_threshold']} districts typical"
        out["actions"] = ["Review other flagged works involving this agency for a common cause."]

    if not out["actions"]:
        out["actions"] = ["Verify the supporting records for this work."]
    return out


# --------------------------------------------------------------- briefs

#: what each authority is being asked to do about a case
TIER_STANCE = {
    "mp": {
        "label": "Member of Parliament",
        "lens": "Works you have recommended in your constituency",
        "opening": "One of the works recommended from your office needs attention.",
        "closing": ("Your office can close this by asking the district authority for "
                    "a written explanation. This is a review prompt, not an "
                    "allegation against you or the agency."),
    },
    "district": {
        "label": "District Authority",
        "lens": "Ground-level execution and verification",
        "opening": "This work requires verification before further release.",
        "closing": ("Record your verification in eSAKSHI so the state and Ministry "
                    "views update automatically."),
    },
    "state": {
        "label": "State Nodal Authority",
        "lens": "Patterns repeating across districts",
        "opening": "This case contributes to a pattern worth reviewing across districts.",
        "closing": ("Check whether the same work type or agency recurs elsewhere in "
                    "the state before the next release cycle."),
    },
    "ministry": {
        "label": "Ministry (MoSPI)",
        "lens": "National trends and policy signals",
        "opening": "This case feeds the national exception statistics.",
        "closing": ("The policy question is whether this rule's threshold needs "
                    "recalibrating, based on how often reviewers mark it a false positive."),
    },
}

HUMAN_REVIEW_NOTE = ("This is an AI-generated risk indicator for human review. "
                     "It is not a determination of fraud or wrongdoing.")


def build_brief(flag: dict, tier: str, context: dict | None = None) -> dict:
    """Structured, authority-specific explanation of one flagged case.

    Everything here is derived from the agents' own output — no invented facts.
    """
    context = context or {}
    findings = flag.get("findings") or []
    signals = [humanize(f) for f in findings]
    # strongest first: severity weight, then a stable order
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    signals.sort(key=lambda s: order.get(s["severity"], 9))

    stance = TIER_STANCE.get(tier, TIER_STANCE["district"])
    primary = signals[0] if signals else None
    total = sum(s["contribution"] for s in signals) or 1

    # Collapse repeats of the same rule for display (e.g. a work matching two
    # separate near-duplicates). Contributions are summed so the displayed
    # shares still reconcile with the composite score.
    grouped: list[dict] = []
    seen: dict[str, dict] = {}
    for s in signals:
        key = s["rule_id"]
        if key in seen:
            seen[key]["contribution"] += s["contribution"]
            seen[key]["occurrences"] += 1
            seen[key]["repeats"].append(s)
        else:
            s = dict(s, occurrences=1, repeats=[])
            seen[key] = s
            grouped.append(s)
    signals = grouped
    for s in signals:
        s["share_pct"] = round(100 * s["contribution"] / total)
        if s["occurrences"] > 1:
            s["headline"] = f"{s['headline']} ({s['occurrences']} matches)"

    # deduplicate actions while preserving order
    actions: list[str] = []
    for s in signals:
        for a in s["actions"]:
            if a not in actions:
                actions.append(a)
    if tier == "mp":
        actions = ["Ask the district authority for a written explanation before "
                   "recommending the next instalment."] + actions[:2]
    elif tier == "state":
        actions = ["Check whether this work type or agency recurs across other "
                   "districts in the state."] + actions[:2]
    elif tier == "ministry":
        actions = ["Track this rule's false-positive rate before changing its "
                   "threshold."] + actions[:2]
    else:
        actions = actions[:4]
    if not actions:
        # a case can reach here with no findings (or an unrecognised tier); the
        # reviewer must still be told what to do rather than shown an empty list
        actions = ["Verify the supporting records for this work with the "
                   "implementing agency."]

    agents_involved = sorted({s["agent_label"] for s in signals})
    corroboration = (
        f"{len(agents_involved)} independent agents each raised a separate issue with "
        f"this case. Signals that reinforce one another carry more weight than any "
        f"single check, which is why this case scores highly."
        if len(agents_involved) > 1 else
        f"Raised by one agent ({agents_involved[0]})." if agents_involved else "")

    return {
        "tier": tier,
        "tier_label": stance["label"],
        "lens": stance["lens"],
        "opening": stance["opening"],
        "primary_risk": primary["headline"] if primary else "Review required",
        "primary_plain": primary["plain"] if primary else "",
        "signals": signals,
        "corroboration": corroboration,
        "context_note": context.get("note", ""),
        "actions": actions,
        "closing": stance["closing"],
        "disclaimer": HUMAN_REVIEW_NOTE,
    }


def brief_to_text(brief: dict) -> str:
    """Flat text rendering, used for the API's backward-compatible tier_views."""
    parts = [brief["opening"], brief["primary_plain"]]
    for s in brief["signals"][1:3]:
        parts.append(s["plain"])
    if brief.get("context_note"):
        parts.append(brief["context_note"])
    if brief["actions"]:
        parts.append("Suggested next step: " + brief["actions"][0])
    parts.append(brief["closing"])
    parts.append(HUMAN_REVIEW_NOTE)
    return " ".join(p for p in parts if p)
