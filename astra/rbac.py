"""Deterministic RBAC / action-constraint engine.

This module decides what each authority is PERMITTED to do about a case. It is
pure Python with no LLM involvement, and it is the security boundary for the
synthesis layer: the language model may only select, order and explain actions
that this module has already authorised. It can never invent one.

Three gates decide whether an action is offered:

  role      the authority viewing the case (an MP cannot order an inspection;
            a District Authority can)
  risk      some actions only become proportionate above a risk threshold
            (escalation is not offered for a low-risk case)
  evidence  some actions only make sense when the corresponding agent actually
            produced a finding (there is nothing to cross-check if the entity
            resolution agent found no duplicate)

Nothing here executes anything. Every action is a RECOMMENDATION for an
authorised human official, which is why the catalogue deliberately contains no
punitive or enforcement actions: no suspension, no penalty, no blacklisting, no
criminal referral. Those are decisions for humans through due process, outside
this system.
"""
from __future__ import annotations

from typing import Any, Literal

Role = Literal["mp", "district", "state", "ministry"]
ROLES: tuple[str, ...] = ("mp", "district", "state", "ministry")

ROLE_LABEL = {
    "mp": "Member of Parliament",
    "district": "District Authority",
    "state": "State Nodal Authority",
    "ministry": "Ministry (MoSPI)",
}

#: Evidence keys an action may require. Derived from agent output, never guessed.
EVIDENCE_KEYS = ("cost_anomaly", "duplicate", "network", "compliance",
                 "delay", "expenditure")

#: The complete catalogue of permissible recommendations.
#:
#: `roles`      which authorities may be offered this action
#: `min_risk`   composite risk score at or above which it becomes proportionate
#: `requires`   evidence that must be present for the action to be meaningful
#: `stage`      immediate | next | if_unresolved | escalation
ACTION_CATALOGUE: dict[str, dict[str, Any]] = {
    # ---------------------------------------------------------- MP
    "request_status_update": {
        "label": "Request a project status update",
        "detail": "Ask the implementing agency, through the district authority, "
                  "for the current physical and financial status of this work.",
        "roles": ("mp",), "min_risk": 0, "requires": (), "stage": "immediate",
    },
    "seek_clarification": {
        "label": "Seek clarification on the flagged concern",
        "detail": "Ask the district authority for a written explanation of the "
                  "specific issue identified before the next instalment is "
                  "recommended.",
        "roles": ("mp",), "min_risk": 0, "requires": (), "stage": "immediate",
    },
    "monitor_progress": {
        "label": "Monitor implementation progress",
        "detail": "Track this work in the constituency review until the "
                  "concern is closed.",
        "roles": ("mp",), "min_risk": 0, "requires": (), "stage": "next",
    },
    "raise_with_authority": {
        "label": "Raise the matter with the implementing authority",
        "detail": "Formally record the concern with the district authority so "
                  "it enters their verification queue.",
        "roles": ("mp",), "min_risk": 40, "requires": (), "stage": "if_unresolved",
    },

    # ---------------------------------------------------- District Authority
    "verify_documents": {
        "label": "Verify sanction and utilisation documents",
        "detail": "Check the sanction order, detailed estimate and utilisation "
                  "certificate on record against what the system observed.",
        "roles": ("district",), "min_risk": 0, "requires": (), "stage": "immediate",
    },
    "review_expenditure": {
        "label": "Review expenditure records",
        "detail": "Reconcile released and disbursed amounts against the "
                  "measurement book and payment vouchers.",
        "roles": ("district",), "min_risk": 0,
        "requires": ("cost_anomaly", "expenditure"), "stage": "immediate",
    },
    "compare_with_benchmark": {
        "label": "Compare the estimate against approved rates",
        "detail": "Compare the sanctioned cost with the state Schedule of "
                  "Rates for this work type and quantity.",
        "roles": ("district",), "min_risk": 0,
        "requires": ("cost_anomaly",), "stage": "next",
    },
    "cross_check_duplicate": {
        "label": "Cross-check the potentially repeated record",
        "detail": "Compare this work's file against the matched work to "
                  "confirm whether they are separate assets at separate "
                  "locations.",
        "roles": ("district",), "min_risk": 0,
        "requires": ("duplicate",), "stage": "immediate",
    },
    "request_progress_report": {
        "label": "Request a dated progress report from the agency",
        "detail": "Obtain the current physical progress with a revised "
                  "completion date and the recorded reason for delay.",
        "roles": ("district",), "min_risk": 0,
        "requires": ("delay",), "stage": "immediate",
    },
    "conduct_physical_inspection": {
        "label": "Conduct a physical site inspection",
        "detail": "Verify on site that the asset exists, matches the "
                  "sanctioned scope, and is at the recorded location.",
        "roles": ("district",), "min_risk": 40, "requires": (),
        "stage": "if_unresolved",
    },
    "mark_under_review": {
        "label": "Mark the case under review",
        "detail": "Record in eSAKSHI that verification is in progress so the "
                  "state and Ministry views reflect it.",
        "roles": ("district",), "min_risk": 0, "requires": (), "stage": "next",
    },
    "escalate_case": {
        "label": "Escalate the case for higher-level review",
        "detail": "Refer the case to the State Nodal Authority with the "
                  "verification findings attached.",
        "roles": ("district",), "min_risk": 60, "requires": (),
        "stage": "escalation",
    },

    # ------------------------------------------------ State Nodal Authority
    "request_district_review": {
        "label": "Request a district-level review",
        "detail": "Ask the concerned district authority to verify this case "
                  "and report back within the monitoring cycle.",
        "roles": ("state",), "min_risk": 0, "requires": (), "stage": "immediate",
    },
    "compare_across_districts": {
        "label": "Compare the pattern across districts",
        "detail": "Check whether the same work type, agency or anomaly recurs "
                  "in other districts of the state.",
        "roles": ("state",), "min_risk": 0, "requires": (), "stage": "immediate",
    },
    "review_agency_portfolio": {
        "label": "Review this agency's wider portfolio",
        "detail": "Examine other works handled by the same implementing "
                  "agency or vendor for a common cause.",
        "roles": ("state",), "min_risk": 0,
        "requires": ("network",), "stage": "next",
    },
    "prioritise_for_monitoring": {
        "label": "Prioritise the case for monitoring",
        "detail": "Add the case to the state monitoring list for the next "
                  "review cycle.",
        "roles": ("state",), "min_risk": 0, "requires": (), "stage": "next",
    },
    "seek_district_report": {
        "label": "Seek a consolidated report from district authorities",
        "detail": "Request a written report covering this case and comparable "
                  "cases in the district.",
        "roles": ("state",), "min_risk": 40, "requires": (),
        "stage": "if_unresolved",
    },
    "escalate_systemic_pattern": {
        "label": "Refer the systemic pattern upward",
        "detail": "Where the pattern spans districts, refer it to the Ministry "
                  "with the supporting comparison.",
        "roles": ("state",), "min_risk": 60, "requires": (),
        "stage": "escalation",
    },

    # ------------------------------------------------------------ Ministry
    "monitor_national_pattern": {
        "label": "Monitor the national and state-level pattern",
        "detail": "Track how often this detection type occurs across states "
                  "to distinguish a local issue from a systemic one.",
        "roles": ("ministry",), "min_risk": 0, "requires": (), "stage": "immediate",
    },
    "review_rule_threshold": {
        "label": "Review the detection threshold",
        "detail": "Assess this rule's false-positive rate from reviewer "
                  "feedback before changing its threshold.",
        "roles": ("ministry",), "min_risk": 0, "requires": (), "stage": "immediate",
    },
    "request_state_review": {
        "label": "Request a review from the State Nodal Authority",
        "detail": "Ask the concerned state to verify this case and report on "
                  "comparable cases.",
        "roles": ("ministry",), "min_risk": 40, "requires": (), "stage": "next",
    },
    "prioritise_systemic_issue": {
        "label": "Prioritise the issue for scheme-level attention",
        "detail": "Where the same category recurs across states, place it on "
                  "the scheme monitoring agenda.",
        "roles": ("ministry",), "min_risk": 0, "requires": (), "stage": "next",
    },
    "seek_additional_information": {
        "label": "Seek additional information from the relevant authorities",
        "detail": "Request supporting records needed to determine whether the "
                  "pattern is systemic.",
        "roles": ("ministry",), "min_risk": 40, "requires": (),
        "stage": "if_unresolved",
    },
    "consider_guidance_update": {
        "label": "Consider a scheme-level monitoring intervention",
        "detail": "Where a pattern is confirmed across states, consider "
                  "guidance or reporting changes for the scheme.",
        "roles": ("ministry",), "min_risk": 60, "requires": (),
        "stage": "escalation",
    },
}

STAGE_ORDER = ("immediate", "next", "if_unresolved", "escalation")
STAGE_LABEL = {
    "immediate": "IMMEDIATE ACTIONS",
    "next": "NEXT STEPS",
    "if_unresolved": "IF CONCERNS PERSIST",
    "escalation": "ESCALATION / FURTHER REVIEW",
}


def evidence_flags(findings: list[dict]) -> set[str]:
    """Which kinds of evidence the deterministic agents actually produced.

    Drives the `requires` gate so an action is never suggested for evidence
    the pipeline did not find.
    """
    flags: set[str] = set()
    for f in findings or []:
        rid = str(f.get("rule_id", ""))
        agent = str(f.get("agent", ""))
        if agent == "compliance":
            flags.add("compliance")
        if agent == "network":
            flags.add("network")
        if rid.startswith("D-"):
            flags.add("duplicate")
        if rid == "A-COST-01":
            flags.add("cost_anomaly")
        if rid == "A-EXP-01":
            flags.add("expenditure")
        if rid == "R-TIME-01":
            flags.add("delay")
        if rid in ("R-COST-01", "R-PILE-01"):
            flags.add("expenditure")
    return flags


def allowed_actions(role: str, risk_score: float,
                    findings: list[dict] | None = None) -> list[dict]:
    """The actions this authority may be offered for this case, in stage order.

    This is the authoritative allow-list. The synthesis layer passes it to the
    model and validates the model's output back against it.
    """
    if role not in ROLES:
        role = "district"
    have = evidence_flags(findings or [])
    out: list[dict] = []
    for action_id, spec in ACTION_CATALOGUE.items():
        if role not in spec["roles"]:
            continue
        if risk_score < spec["min_risk"]:
            continue
        req = spec["requires"]
        if req and not (set(req) & have):
            continue
        out.append({
            "action_id": action_id,
            "label": spec["label"],
            "detail": spec["detail"],
            "stage": spec["stage"],
        })
    out.sort(key=lambda a: STAGE_ORDER.index(a["stage"]))
    return out


def allowed_ids(role: str, risk_score: float,
                findings: list[dict] | None = None) -> set[str]:
    return {a["action_id"] for a in allowed_actions(role, risk_score, findings)}


def validate_plan(plan_actions: list[dict], role: str, risk_score: float,
                  findings: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    """Filter a proposed action plan down to what this role may actually do.

    Returns (accepted, rejected_ids). Anything the model invented, or that is
    not permitted for this role/risk/evidence combination, is dropped here
    rather than shown to an officer. This function is the reason the LLM cannot
    widen its own authority.
    """
    permitted = {a["action_id"]: a for a in allowed_actions(role, risk_score, findings)}
    accepted: list[dict] = []
    rejected: list[str] = []
    seen: set[str] = set()

    for item in plan_actions or []:
        aid = str(item.get("action_id", "")).strip()
        if aid not in permitted:
            rejected.append(aid or "<unnamed>")
            continue
        if aid in seen:
            continue
        seen.add(aid)
        spec = permitted[aid]
        accepted.append({
            "action_id": aid,
            "label": spec["label"],
            "detail": spec["detail"],
            "stage": spec["stage"],
            # the model may explain WHY, but never what the action is
            "reason": str(item.get("reason") or "").strip()[:400],
        })
    accepted.sort(key=lambda a: STAGE_ORDER.index(a["stage"]))
    return accepted, rejected


def default_plan(role: str, risk_score: float,
                 findings: list[dict] | None = None,
                 signals: list[dict] | None = None) -> list[dict]:
    """Deterministic action plan used when the LLM is unavailable.

    Takes the permitted actions in stage order and attaches the plain-language
    reason from the strongest matching signal, so the fallback is still
    evidence-grounded rather than generic boilerplate.
    """
    acts = allowed_actions(role, risk_score, findings)
    signals = signals or []
    primary = signals[0] if signals else {}

    # map each evidence type to the signal that produced it, so an action cites
    # the finding that actually motivates it rather than a generic headline
    by_evidence: dict[str, dict] = {}
    for sig in signals:
        rid = str(sig.get("rule_id", ""))
        agent = str(sig.get("agent", ""))
        keys = []
        if rid.startswith("D-"):
            keys.append("duplicate")
        if rid == "A-COST-01":
            keys.append("cost_anomaly")
        if rid in ("A-EXP-01", "R-COST-01", "R-PILE-01"):
            keys.append("expenditure")
        if rid == "R-TIME-01":
            keys.append("delay")
        if agent == "network":
            keys.append("network")
        if agent == "compliance":
            keys.append("compliance")
        for k in keys:
            by_evidence.setdefault(k, sig)

    out = []
    for a in acts:
        a = dict(a)
        req = ACTION_CATALOGUE[a["action_id"]]["requires"]
        motive = next((by_evidence[k] for k in req if k in by_evidence), primary)
        headline = motive.get("headline") or "the flagged concern"
        if a["stage"] in ("immediate", "next"):
            a["reason"] = f"The system detected: {headline}."
        elif a["stage"] == "if_unresolved":
            a["reason"] = ("Only if the checks above do not resolve the concern.")
        else:
            a["reason"] = ("Only if material concerns remain after verification.")
        out.append(a)
    return out


CONSTRAINT_NOTICE = (
    "Recommendations are generated within the actions permitted for this "
    "authority level. Final decisions and actions remain with authorised "
    "human officials."
)
