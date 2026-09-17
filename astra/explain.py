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
  brief_for()        the brief for a stored case in the reader's language

Every sentence is a template in config/locales/<language>.yaml (astra/locale),
so a brief reads the same facts and figures in English and in Hindi. English
is what the analysis stores; another language is rendered on request from the
stored findings.

Framing rules enforced here:
  * nothing is ever described as fraud, corruption or wrongdoing;
  * detections are "indicators requiring verification", never conclusions;
  * every brief ends with the human-review statement.
"""
from __future__ import annotations

import functools
import re

from . import locale
from .config import cite, load_guidelines, load_rules
from .locale import DEFAULT, money, text, text_or, texts

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
    text_ = _WS.sub(" ", str(description or "")).strip(" .,-–—")
    text_ = _WS.sub(" ", _NOISE.sub("", text_)).strip(" .,-–—")
    if len(text_) < 8:
        text_ = _WS.sub(" ", str(category or "")).strip()
    if not text_:
        return "Untitled work"
    if len(text_) <= max_len:
        return text_[0].upper() + text_[1:]
    cut = text_[:max_len]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    return (cut[0].upper() + cut[1:]).rstrip(" .,-–—") + "…"


def rupees(value, lang: str = DEFAULT) -> str:
    """Indian-convention money formatting: lakh / crore."""
    return money(value, lang)


# --------------------------------------------------------------- agents

AGENTS = ("compliance", "anomaly", "entity_resolution", "network", "revisions", "payments")
AGENT_LABEL = {a: text(DEFAULT, f"agent.{a}.label") for a in AGENTS}
AGENT_ROLE = {a: text(DEFAULT, f"agent.{a}.role") for a in AGENTS}
SEVERITY_LABEL = {s: text(DEFAULT, f"severity.{s}") for s in ("critical", "high", "medium", "low")}


def agent_label(agent: str, lang: str = DEFAULT) -> str:
    return text_or(lang, f"agent.{agent}.label", agent)


def risk_band(score: float, lang: str = DEFAULT) -> tuple[str, str]:
    """(label, colour) for a composite risk score."""
    if score >= 70:
        return text(lang, "risk_band.high"), "#c0392b"
    if score >= 40:
        return text(lang, "risk_band.medium"), "#d68910"
    return text(lang, "risk_band.low"), "#5d6d7e"


# --------------------------------------------------------------- findings

def humanize(finding: dict, lang: str = DEFAULT) -> dict:
    """One agent finding -> plain-language, non-accusatory explanation."""
    lang = locale.normalise(lang)
    rid = finding.get("rule_id", "")
    d = finding.get("details") or {}
    sev = finding.get("severity", "medium")
    weights = load_rules()["risk_score"]["weights"]
    agent = finding.get("agent", "")

    def t(key: str, **values) -> str:
        return text(lang, key, **values)

    def ts(key: str, **values) -> list[str]:
        return texts(lang, key, **values)

    def rs(value) -> str:
        return money(value, lang)

    out = {
        "rule_id": rid,
        "agent": agent,
        "agent_label": agent_label(agent, lang),
        "severity": sev,
        "severity_label": text_or(lang, f"severity.{sev}", sev.title()),
        "contribution": 0 if d.get("portal_record_pair") or d.get("held")
                        or d.get("standalone") is False else weights.get(sev, 0),
        # shown for completeness, deliberately outside the score
        "context": bool(d.get("portal_record_pair") or d.get("held")
                        or d.get("standalone") is False),
        "clause": clause_text(finding.get("clause"), lang),
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
        out["headline"] = t("R-TIME-01.headline")
        if days:
            out["plain"] = t("R-TIME-01.plain", days=days, years=years,
                             progress=t("R-TIME-01.incomplete" if d.get("incomplete")
                                        else "R-TIME-01.completed_late"))
        out["metric"] = t("R-TIME-01.metric", days=days) if days else None
        out["benchmark"] = t("R-TIME-01.benchmark", max_days=d.get("max_days", 365))
        out["actions"] = ts("R-TIME-01.actions")

    # ---- compliance: prohibited / non-permissible
    elif rid == "R-PROH-01":
        term = d.get("matched_pattern", "")
        para = d.get("para")
        para_ref = t("R-PROH-01.para_ref", para=para) if para else ""
        kind = "activity" if d.get("match_type") == "activity" else "asset"
        out["headline"] = t(f"R-PROH-01.{kind}.headline")
        out["plain"] = t(f"R-PROH-01.{kind}.plain", term=term, para_ref=para_ref)
        out["actions"] = ts(f"R-PROH-01.{kind}.actions")
        out["metric"] = t("R-PROH-01.metric", term=term)
        out["benchmark"] = t("R-PROH-01.benchmark", para=para or "5.2")

    # ---- compliance: cost floor / ceiling
    elif rid == "R-COST-01":
        cost, floor, ceil = d.get("cost"), d.get("floor"), d.get("ceiling")
        if d.get("district_summary"):
            values = dict(works=d.get("works", 0), out_of=d.get("out_of", 0),
                          share=d.get("share", 0))
            key = "R-COST-01.summary"
        elif floor is not None:
            values = dict(cost=rs(cost), floor=rs(floor))
            key = "R-COST-01.floor"
        else:
            values = dict(cost=rs(cost), ceiling=rs(ceil))
            key = "R-COST-01.ceiling"
        out["headline"] = t(f"{key}.headline")
        out["plain"] = t(f"{key}.plain", **values)
        out["metric"] = t(f"{key}.metric", **values)
        out["benchmark"] = t(f"{key}.benchmark", **values)
        out["actions"] = ts(f"{key}.actions")

    # ---- compliance: idle funds
    elif rid == "R-PILE-01":
        out["headline"] = t("R-PILE-01.headline")
        out["plain"] = t("R-PILE-01.plain", years=d.get("years"), pct=d.get("cum_util_pct", 0),
                         entitlement=rs(d.get("entitlement_total") or d.get("released_total")))
        out["metric"] = t("R-PILE-01.metric", pct=d.get("cum_util_pct", 0))
        out["benchmark"] = t("R-PILE-01.benchmark",
                             threshold=load_rules()["rules"]["unspent_pileup"]["max_cum_util_pct"])
        out["actions"] = ts("R-PILE-01.actions")

    # ---- compliance: spike
    elif rid == "R-SPIKE-01":
        values = dict(spike=d.get("spike_pct", 0), fy=d.get("spike_fy"),
                      dormant=d.get("dormant_avg_pct", 0))
        out["headline"] = t("R-SPIKE-01.headline")
        out["plain"] = t("R-SPIKE-01.plain", **values)
        out["metric"] = t("R-SPIKE-01.metric", **values)
        out["benchmark"] = t("R-SPIKE-01.benchmark", **values)
        out["actions"] = ts("R-SPIKE-01.actions")

    # ---- compliance: SC/ST screening proxy
    elif rid == "R-SCST-02":
        out["headline"] = t("R-SCST-02.headline", kind=d.get("kind", "SC/ST"))
        out["plain"] = t("R-SCST-02.plain", state=d.get("state"), kind=d.get("kind"),
                         pct=d.get("pct", 0), benchmark=d.get("benchmark_pct"))
        out["metric"] = t("R-SCST-02.metric", pct=d.get("pct", 0))
        out["benchmark"] = t("R-SCST-02.benchmark", benchmark=d.get("benchmark_pct"))
        out["actions"] = ts("R-SCST-02.actions")

    # ---- compliance: time to sanction
    elif rid == "R-SANC-01":
        if d.get("district_summary"):
            key, values = "R-SANC-01.summary", dict(works=d.get("works", 0), share=d.get("share", 0))
        else:
            key, values = "R-SANC-01.single", dict(days=d.get("days_waiting") or 0)
        out["headline"] = t(f"{key}.headline")
        out["plain"] = t(f"{key}.plain", **values)
        out["metric"] = t(f"{key}.metric", **values)
        out["benchmark"] = t("R-SANC-01.benchmark")
        out["actions"] = ts(f"{key}.actions")

    elif rid == "R-SANC-02":
        out["headline"] = t("R-SANC-02.headline")
        out["plain"] = t("R-SANC-02.plain", works=d.get("sanctioned_works", 0),
                         median=d.get("median_days", 0), share=d.get("over_limit_share", 0))
        out["metric"] = t("R-SANC-02.metric", median=d.get("median_days", 0))
        out["benchmark"] = t("R-SANC-02.benchmark")
        out["actions"] = ts("R-SANC-02.actions")

    # ---- compliance: repair and renovation cap
    elif rid == "R-REPAIR-01":
        values = dict(fy=d.get("fy"), total=rs(d.get("total")), works=d.get("works"),
                      cap=rs(d.get("cap")))
        out["headline"] = t("R-REPAIR-01.headline")
        if d.get("portal_category_works") is not None:
            out["plain"] = t("R-REPAIR-01.plain_portal", **values,
                             classed=d.get("portal_category_works"),
                             classed_total=rs(d.get("portal_category_total")),
                             described=d.get("described_only_works"))
        else:
            out["plain"] = t("R-REPAIR-01.plain", **values)
        out["metric"] = t("R-REPAIR-01.metric", **values)
        out["benchmark"] = t("R-REPAIR-01.benchmark", **values)
        out["actions"] = ts("R-REPAIR-01.actions")

    # ---- compliance: societies and trusts cap
    elif rid == "R-TRUST-01":
        values = dict(fy=d.get("fy"), total=rs(d.get("total")), works=d.get("works"),
                      cap=rs(d.get("cap")))
        out["headline"] = t("R-TRUST-01.headline")
        out["plain"] = t("R-TRUST-01.plain", **values)
        out["metric"] = t("R-TRUST-01.metric", **values)
        out["benchmark"] = t("R-TRUST-01.benchmark", **values)
        out["actions"] = ts("R-TRUST-01.actions")

    # ---- revisions observed on the portal
    elif rid == "V-REV-01":
        fields = t("list_separator").join(
            c.get("field", "") if lang == DEFAULT else text_or(lang, f"field.{c.get('field', '')}",
                                                                c.get("field", ""))
            for c in d.get("changes", []))
        out["headline"] = t("V-REV-01.headline")
        out["plain"] = t("V-REV-01.plain", fields=fields, seen=str(d.get("observed_at"))[:10])
        out["metric"] = t("V-REV-01.metric", fields=fields)
        out["benchmark"] = t("V-REV-01.benchmark")
        out["actions"] = ts("V-REV-01.actions")

    elif rid == "V-AMT-01":
        out["headline"] = t("V-AMT-01.headline_up" if (d.get("change") or 0) > 0
                            else "V-AMT-01.headline_down")
        out["plain"] = t("V-AMT-01.plain", old=rs(d.get("old_amount")), new=rs(d.get("new_amount")),
                         pct=d.get("pct_change", 0),
                         year_end=t("V-AMT-01.year_end") if d.get("near_year_end") else "")
        out["metric"] = t("V-AMT-01.metric", pct=d.get("pct_change", 0))
        out["benchmark"] = t("V-AMT-01.benchmark")
        out["actions"] = ts("V-AMT-01.actions")

    elif rid == "V-IA-01":
        out["headline"] = t("V-IA-01.headline")
        out["plain"] = t("V-IA-01.plain", old=d.get("old"), new=d.get("new"))
        out["actions"] = ts("V-IA-01.actions")

    elif rid == "V-VEN-01":
        out["headline"] = t("V-VEN-01.headline")
        out["plain"] = t("V-VEN-01.plain", old=d.get("old"), new=d.get("new"),
                         how=t("V-VEN-01.with_payment" if d.get("with_new_payment")
                               else "V-VEN-01.without_payment"))
        out["actions"] = ts("V-VEN-01.actions")

    elif rid == "V-PAY-01":
        out["headline"] = t("V-PAY-01.headline")
        out["plain"] = t("V-PAY-01.plain", old=rs(d.get("old_total_paid")),
                         new=rs(d.get("new_total_paid")))
        out["metric"] = t("V-PAY-01.metric", reduction=rs(d.get("reduction")))
        out["actions"] = ts("V-PAY-01.actions")

    elif rid == "V-LIST-01":
        out["headline"] = t("V-LIST-01.headline")
        out["plain"] = t("V-LIST-01.plain", paid=rs(d.get("total_paid")))
        out["metric"] = t("V-LIST-01.metric", paid=rs(d.get("total_paid")))
        out["actions"] = ts("V-LIST-01.actions")

    elif rid == "V-FREQ-01":
        out["headline"] = t("V-FREQ-01.headline")
        out["plain"] = t("V-FREQ-01.plain", revisions=d.get("revisions"),
                         average=d.get("peer_average"))
        out["actions"] = ts("V-FREQ-01.actions")

    # ---- anomaly: several measures at once
    elif rid == "A-PEER-01":
        measures = d.get("measures", [])
        labels = t("A-PEER-01.separator").join(
            m.get("label", "") if lang == DEFAULT
            else text_or(lang, f"measure.{m.get('measure')}", m.get("label", ""))
            for m in measures)
        out["headline"] = t("A-PEER-01.headline")
        out["plain"] = t("A-PEER-01.plain", peers=d.get("peers", 0), count=len(measures),
                         labels=labels)
        out["metric"] = labels
        out["benchmark"] = t("A-PEER-01.benchmark")
        out["actions"] = ts("A-PEER-01.actions")

    # ---- anomaly: cost vs peers
    elif rid == "A-COST-01":
        cost, bench = rs(d.get("cost")), rs(d.get("benchmark"))
        pct, peers = d.get("pct_vs_benchmark", 0), d.get("peers", 0)
        if d.get("scale_mismatch"):
            out["headline"] = t("A-COST-01.scale.headline")
            out["plain"] = t("A-COST-01.scale.plain", cost=cost, benchmark=bench,
                             ratio=d.get("ratio_vs_benchmark", 0))
            out["actions"] = ts("A-COST-01.scale.actions")
        else:
            out["headline"] = t("A-COST-01.above.headline", pct=pct)
            out["plain"] = t("A-COST-01.above.plain", cost=cost, benchmark=bench, pct=pct,
                             peers=peers, place=d.get("peer_group", "").split("|")[0])
            out["actions"] = ts("A-COST-01.above.actions")
        out["metric"] = t("A-COST-01.metric", cost=cost)
        out["benchmark"] = t("A-COST-01.benchmark", benchmark=bench, peers=peers)

    # ---- anomaly: expenditure pattern
    elif rid == "A-EXP-01":
        values = dict(expenditure=rs(d.get("expenditure")), fy=d.get("fy"))
        out["headline"] = t("A-EXP-01.headline")
        out["plain"] = t("A-EXP-01.plain", **values)
        out["metric"] = t("A-EXP-01.metric", **values)
        out["benchmark"] = t("A-EXP-01.benchmark")
        out["actions"] = ts("A-EXP-01.actions")

    # ---- entity resolution: near-duplicate
    elif rid == "D-DUP-01" and d.get("portal_record_pair"):
        values = dict(pending=d.get("pending_work_id"), sanctioned=d.get("sanctioned_work_id"),
                      stage=d.get("pending_stage") or t("D-DUP-01.pair.stage_unknown"),
                      date=d.get("recommended_date"),
                      similarity=d.get("semantic_sim", 0) * 100)
        out["headline"] = t("D-DUP-01.pair.headline")
        out["plain"] = t("D-DUP-01.pair.plain", **values)
        out["metric"] = t("D-DUP-01.pair.metric", **values)
        out["benchmark"] = t("D-DUP-01.pair.benchmark")
        out["actions"] = ts("D-DUP-01.pair.actions", **values)

    elif rid == "D-DUP-01" and d.get("batch"):
        groups = d.get("same_payee_groups") or []
        values = dict(size=d.get("batch_size"), mp=d.get("batch_mp"), total=rs(d.get("batch_total")),
                      letters=d.get("batch_letters"), payees=d.get("batch_payees"))
        out["headline"] = t("D-DUP-01.batch.headline", **values)
        out["plain"] = t("D-DUP-01.batch.plain", **values)
        out["metric"] = t("D-DUP-01.batch.metric", **values)
        out["benchmark"] = t("D-DUP-01.batch.benchmark")
        out["actions"] = ts("D-DUP-01.batch.actions", **values)
        for group in groups[:3]:
            out["actions"].append(t("D-DUP-01.batch.same_payee", payee=group.get("payee"),
                                    works=", ".join(group.get("work_ids") or [])))

    elif rid == "D-DUP-01" and d.get("photo_match"):
        cluster = d.get("photo_cluster") or []
        values = dict(pair=d.get("pair_work_id"), works=max(len(cluster), 2),
                      similarity=d.get("semantic_sim", 0) * 100)
        out["headline"] = t("D-DUP-01.photo.headline")
        out["plain"] = t("D-DUP-01.photo.plain", **values,
                         others=t("D-DUP-01.photo.others", count=len(cluster) - 2)
                         if len(cluster) > 2 else "")
        out["metric"] = t("D-DUP-01.photo.metric", **values)
        out["benchmark"] = t("D-DUP-01.photo.benchmark")
        out["actions"] = ts("D-DUP-01.photo.actions", **values)

    elif rid == "D-DUP-01" and d.get("held"):
        values = dict(similarity=d.get("semantic_sim", 0) * 100, pair=d.get("pair_work_id"))
        photo = d.get("photo_check")
        out["headline"] = t("D-DUP-01.held.headline")
        out["plain"] = t("D-DUP-01.held.plain", **values,
                         same_amount=t("D-DUP-01.match.same_amount", cost=rs(d.get("this_cost")))
                         if d.get("same_sanction_amount") else "",
                         same_payee=t("D-DUP-01.held.same_payee", payee=d.get("shared_payee"))
                         if d.get("shared_payee") else "",
                         photo=t(f"D-DUP-01.held.photo_{photo}")
                         if photo in ("look_alike", "different_photos", "no_photo", "not_completed",
                                      "failed") else "")
        out["metric"] = t("D-DUP-01.held.metric", **values)
        out["benchmark"] = t("D-DUP-01.held.benchmark")
        out["actions"] = ts("D-DUP-01.held.actions", **values)

    elif rid == "D-DUP-01":
        strong = d.get("evidence_strength") == "strong"
        values = dict(similarity=d.get("semantic_sim", 0) * 100, pair=d.get("pair_work_id"))
        out["headline"] = t("D-DUP-01.match.headline_strong" if strong
                            else "D-DUP-01.match.headline_similar")
        out["plain"] = t("D-DUP-01.match.plain", **values,
                         same_amount=t("D-DUP-01.match.same_amount", cost=rs(d.get("this_cost")))
                         if d.get("same_sanction_amount") else "")
        out["metric"] = t("D-DUP-01.match.metric", **values)
        out["benchmark"] = t("D-DUP-01.match.benchmark",
                             threshold=load_rules()["duplicates"]["semantic_threshold"] * 100)
        out["actions"] = ts("D-DUP-01.match.actions", **values)

    # ---- entity resolution: generic cluster
    elif rid == "D-DUP-02":
        values = dict(size=d.get("cluster_size"), district=d.get("district"),
                      description=d.get("normalised_description", "")[:50],
                      total=rs(d.get("total_cost")))
        out["headline"] = t("D-DUP-02.headline")
        out["plain"] = t("D-DUP-02.plain", **values)
        out["metric"] = t("D-DUP-02.metric", **values)
        out["benchmark"] = t("D-DUP-02.benchmark")
        out["actions"] = ts("D-DUP-02.actions")

    # ---- network
    elif rid == "N-NET-01":
        raw_kind = d.get("actor_type", "agency")
        kind = text_or(lang, f"N-NET-01.kind.{raw_kind}", str(raw_kind))
        bits = []
        if d.get("districts", 0) > 1:
            bits.append(t("N-NET-01.spread", works=d.get("works"), districts=d.get("districts")))
        if d.get("overrun_share", 0) > 0:
            bits.append(t("N-NET-01.overrun", share=d.get("overrun_share", 0) * 100))
        out["headline"] = t("N-NET-01.headline", kind=kind)
        out["plain"] = t("N-NET-01.plain", kind=kind, bits=t("N-NET-01.join").join(bits),
                         same_name=(t("N-NET-01.same_name", count=d["same_name_vendor_ids"])
                                    if d.get("same_name_vendor_ids") else ""),
                         national=t("N-NET-01.national") if d.get("likely_national_supplier") else "")
        out["metric"] = t("N-NET-01.metric", works=d.get("works"), districts=d.get("districts"))
        out["benchmark"] = t("N-NET-01.benchmark",
                             threshold=load_rules()["network"]["district_spread_threshold"])
        out["actions"] = ts("N-NET-01.actions")

    # ---- individual payment records
    elif rid == "P-SEQ-01":
        values = dict(count=d.get("payments"), amount=rs(d.get("amount")),
                      sanction=d.get("sanction_date"), earliest=d.get("earliest"))
        out["headline"] = t("P-SEQ-01.headline")
        out["plain"] = t("P-SEQ-01.plain", **values)
        out["metric"] = t("P-SEQ-01.metric", **values)
        out["benchmark"] = t("P-SEQ-01.benchmark")
        out["actions"] = ts("P-SEQ-01.actions")

    elif rid == "P-LATE-01":
        values = dict(count=d.get("late_payments"), amount=rs(d.get("late_amount")),
                      completed=d.get("completion_date"), days=d.get("max_days"),
                      min_days=d.get("min_days"), share=d.get("share_of_paid") or 0)
        out["headline"] = t("P-LATE-01.headline")
        out["plain"] = t("P-LATE-01.plain", **values)
        out["metric"] = t("P-LATE-01.metric", **values)
        out["benchmark"] = t("P-LATE-01.benchmark", **values)
        out["actions"] = ts("P-LATE-01.actions")

    elif rid == "P-DUP-01":
        top = d.get("largest") or {}
        values = dict(sets=d.get("sets"), extra=d.get("extra_records"),
                      amount=rs(d.get("repeated_amount")), share=d.get("share_of_paid") or 0,
                      example=rs(top.get("amount")), vendor=top.get("vendor_name") or "",
                      day=top.get("paid_on") or "", times=top.get("times") or 0)
        out["headline"] = t("P-DUP-01.headline")
        out["plain"] = t("P-DUP-01.plain", **values)
        out["metric"] = t("P-DUP-01.metric", **values)
        out["benchmark"] = t("P-DUP-01.benchmark")
        out["actions"] = ts("P-DUP-01.actions")

    elif lang != DEFAULT and locale.has(lang, f"rule.{rid}"):
        # a rule with no explanation template: at least name it in the reader's language
        out["headline"] = t(f"rule.{rid}")

    if d.get("data_confidence") == "reduced":
        reasons = t("confidence.separator").join(confidence_reason(r, lang)
                                           for r in d.get("data_confidence_reasons") or [])
        out["plain"] = t("confidence.plain", plain=out["plain"], reasons=reasons).strip()
        out["actions"] = [t("confidence.action")] + out["actions"]
    if not out["actions"]:
        out["actions"] = [t("finding.default_action")]
    return out


# ------------------------------------------------ text stored in English

CONFIDENCE_REASONS = tuple(f"confidence.reason.{k}" for k in
                           ("parity_differs", "unchecked", "failed", "stale", "held_back", "missing"))


def confidence_reason(reason: str, lang: str = DEFAULT) -> str:
    """A stored data-confidence reason (see data_confidence) in `lang`."""
    return locale.rerender(lang, reason, CONFIDENCE_REASONS)


@functools.lru_cache(maxsize=None)
def _clauses(lang: str) -> dict[str, str]:
    """{English clause as stored on a finding: the same clause in `lang`}."""
    out: dict[str, str] = {}
    lead = text(lang, "clause.lead")

    def walk(node):
        if isinstance(node, dict):
            if node.get("id") and node.get("clause") and locale.has(lang, f"clause.rule.{node['id']}"):
                out[str(node["clause"])] = text(lang, f"clause.rule.{node['id']}")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(load_rules())
    for para in (load_guidelines().get("paras") or {}):
        if locale.has(lang, f"clause.para.{para}"):
            out[str(cite(para))] = text(lang, "clause.cite", lead=lead, para=para,
                                        summary=text(lang, f"clause.para.{para}"))
    out[text(DEFAULT, "clause.marker.cost_ceiling")] = text(lang, "clause.marker.cost_ceiling")
    return out


def clause_text(clause: str | None, lang: str = DEFAULT) -> str | None:
    """A finding's guideline clause in `lang`; as stored if no translation exists."""
    if clause is None or locale.normalise(lang) == DEFAULT:
        return clause
    return _clauses(locale.normalise(lang)).get(clause, clause)


def context_note(findings: list[dict], agency_other_flags: int = 0, lang: str = DEFAULT) -> str:
    """What a case's own findings cannot say: other cases at the same authority,
    and reduced confidence in the data behind it."""
    note = (text(lang, "note.same_authority", count=agency_other_flags)
            if agency_other_flags > 0 else "")
    reduced = sorted({r for f in findings or [] if (f.get("details") or {}).get("data_confidence") == "reduced"
                      for r in (f.get("details") or {}).get("data_confidence_reasons", [])})
    if reduced:
        reasons = text(lang, "confidence.separator").join(confidence_reason(r, lang) for r in reduced)
        note = (text(lang, "note.reduced", reasons=reasons) + " " + note).strip()
    return note


def parse_agency_other_flags(note: str | None) -> int:
    """The other-cases count a stored English context note carries (0 if none)."""
    pattern = locale.pattern("note.same_authority")
    for sentence in re.split(r"(?<=\.) ", note or ""):
        match = pattern.match(sentence) if pattern else None
        if match:
            return int(match.group("count").replace(",", ""))
    return 0


# --------------------------------------------------------------- briefs

TIERS = ("mp", "district", "state", "ministry")


def tier_stance(tier: str, lang: str = DEFAULT) -> dict:
    tier = tier if tier in TIERS else "district"
    return {part: text(lang, f"tier.{tier}.{part}") for part in ("label", "lens", "opening", "closing")}


#: what each authority is being asked to do about a case (English)
TIER_STANCE = {t: tier_stance(t) for t in TIERS}

HUMAN_REVIEW_NOTE = text(DEFAULT, "brief.disclaimer")


def build_brief(flag: dict, tier: str, context: dict | None = None,
                lang: str = DEFAULT) -> dict:
    """Structured, authority-specific explanation of one flagged case.

    Everything here is derived from the agents' own output — no invented facts.
    """
    lang = locale.normalise(lang)
    context = context or {}
    findings = flag.get("findings") or []
    signals = [humanize(f, lang) for f in findings]
    # strongest first: severity weight, then a stable order
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    # context shown outside the score never leads a brief
    signals.sort(key=lambda s: (s.get("context", False), order.get(s["severity"], 9)))

    stance = tier_stance(tier, lang)
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
            s["headline"] = text(lang, "brief.matches", headline=s["headline"],
                                 count=s["occurrences"])

    # deduplicate actions while preserving order
    actions: list[str] = []
    for s in signals:
        for a in s["actions"]:
            if a not in actions:
                actions.append(a)
    if tier in ("mp", "state", "ministry"):
        actions = [text(lang, f"brief.tier_action.{tier}")] + actions[:2]
    else:
        actions = actions[:4]
    if not actions:
        # a case can reach here with no findings (or an unrecognised tier); the
        # reviewer must still be told what to do rather than shown an empty list
        actions = [text(lang, "brief.no_actions")]

    agents_involved = sorted({s["agent_label"] for s in signals if not s.get("context")})
    corroboration = (
        text(lang, "brief.corroboration_many", count=len(agents_involved))
        if len(agents_involved) > 1 else
        text(lang, "brief.corroboration_one", agent=agents_involved[0]) if agents_involved else "")

    return {
        "tier": tier,
        "tier_label": stance["label"],
        "lens": stance["lens"],
        "opening": stance["opening"],
        "primary_risk": primary["headline"] if primary else text(lang, "brief.review_required"),
        "primary_plain": primary["plain"] if primary else "",
        "signals": signals,
        "corroboration": corroboration,
        "context_note": context.get("note", ""),
        "actions": actions,
        "closing": stance["closing"],
        "disclaimer": text(lang, "brief.disclaimer"),
    }


def brief_for(flag: dict, tier: str, lang: str = DEFAULT) -> dict:
    """The brief for a stored case, in `lang`.

    English is the brief the analysis stored. Another language is rebuilt from
    the stored findings, with the context note recovered from the stored one,
    so both state the same facts.
    """
    stored = (flag.get("tier_briefs") or {}).get(tier)
    if locale.normalise(lang) == DEFAULT:
        return stored or build_brief(flag, tier)
    stored_note = ((flag.get("tier_briefs") or {}).get("district") or stored or {}).get("context_note")
    note = context_note(flag.get("findings") or [], parse_agency_other_flags(stored_note), lang)
    return build_brief(flag, tier, {"note": note}, lang)


def display_title(flag: dict, lang: str = DEFAULT) -> str | None:
    """A case's list title in `lang`: a work keeps its own description."""
    title = flag.get("display_title") or flag.get("entity_label")
    if flag.get("entity_type") == "work":
        return title
    return locale.rerender(lang, title, ("title.district_authority", "title.constituency"))


def brief_to_text(brief: dict) -> str:
    """Flat text rendering, used for the API's backward-compatible tier_views."""
    parts = [brief["opening"], brief["primary_plain"]]
    for s in brief["signals"][1:3]:
        parts.append(s["plain"])
    if brief.get("context_note"):
        parts.append(brief["context_note"])
    if brief["actions"]:
        parts.append(text(DEFAULT, "brief.next_step", action=brief["actions"][0]))
    parts.append(brief["closing"])
    parts.append(HUMAN_REVIEW_NOTE)
    return " ".join(p for p in parts if p)
