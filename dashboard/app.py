"""ASTRA — AI-Powered MPLADS Risk Intelligence Platform.

A master-detail investigation workspace, not a record dump:

  header        platform identity, live data mode, freshness, review disclaimer
  authority     MP / District / State Nodal / Ministry — switching reframes the
                SAME case through the orchestrator's per-tier briefs, it does
                not merely hide columns
  overview      executive risk intelligence for the selected authority
  workspace     left: compact filterable case list · right: full investigation

Everything rendered here comes from the pipeline. Where evidence does not exist
for a case (no coordinates, no duplicate match, no vendor), the UI says so
rather than drawing an empty chart.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astra import PLATFORM_NAME, db, rbac, synthesis  # noqa: E402
from astra.config import PROCESSED_DIR  # noqa: E402
from astra.explain import (AGENT_LABEL, AGENT_ROLE, humanize,  # noqa: E402
                           risk_band, rupees)

st.set_page_config(page_title="ASTRA — MPLADS Risk Intelligence",
                   page_icon="🛰️", layout="wide",
                   initial_sidebar_state="expanded")

TIERS = {
    "ministry": ("🏛️", "Ministry (MoSPI)", "National trends and policy signals"),
    "state": ("🗺️", "State Nodal Authority", "Patterns repeating across districts"),
    "district": ("🏢", "District Authority", "Ground-level execution and verification"),
    "mp": ("🧑‍⚖️", "Member of Parliament", "Works recommended in your constituency"),
}
SEV_COLOR = {"critical": "#a93226", "high": "#c0392b",
             "medium": "#d68910", "low": "#5d6d7e"}
STATUS_META = {
    "pending": ("NEW", "#5d6d7e"),
    "under_review": ("UNDER REVIEW", "#2471a3"),
    "confirmed": ("ESCALATED", "#a93226"),
    "false_positive": ("FALSE POSITIVE", "#1e8449"),
}
# Approximate state centroids. Used ONLY to place state-level aggregate bubbles;
# the eSAKSHI exports carry no asset coordinates and none are invented here.
STATE_CENTROIDS = {
    "ANDHRA PRADESH": (15.91, 79.74), "ARUNACHAL PRADESH": (28.21, 94.72),
    "ASSAM": (26.20, 92.94), "BIHAR": (25.10, 85.31), "CHHATTISGARH": (21.28, 81.87),
    "GOA": (15.30, 74.12), "GUJARAT": (22.26, 71.19), "HARYANA": (29.06, 76.09),
    "HIMACHAL PRADESH": (31.10, 77.17), "JHARKHAND": (23.61, 85.28),
    "KARNATAKA": (15.32, 75.71), "KERALA": (10.85, 76.27),
    "MADHYA PRADESH": (22.97, 78.66), "MAHARASHTRA": (19.75, 75.71),
    "MANIPUR": (24.66, 93.91), "MEGHALAYA": (25.47, 91.37), "MIZORAM": (23.16, 92.94),
    "NAGALAND": (26.16, 94.56), "ODISHA": (20.95, 85.10), "PUNJAB": (31.15, 75.34),
    "RAJASTHAN": (27.02, 74.22), "SIKKIM": (27.53, 88.51), "TAMIL NADU": (11.13, 78.66),
    "TELANGANA": (18.11, 79.02), "TRIPURA": (23.94, 91.99),
    "UTTAR PRADESH": (26.85, 80.91), "UTTARAKHAND": (30.07, 79.09),
    "WEST BENGAL": (22.99, 87.85), "DELHI": (28.70, 77.10),
    "JAMMU AND KASHMIR": (33.78, 76.58), "LADAKH": (34.21, 77.62),
    "PUDUCHERRY": (11.94, 79.81), "CHANDIGARH": (30.73, 76.78),
    "ANDAMAN AND NICOBAR ISLANDS": (11.74, 92.66), "LAKSHADWEEP": (10.57, 72.64),
    "DADRA AND NAGAR HAVELI AND DAMAN AND DIU": (20.18, 73.02),
}

CSS = """
<style>
  .block-container {padding-top: 1.2rem; padding-bottom: 1rem; max-width: 1520px;}
  .astra-head {background: linear-gradient(90deg,#0b2d5c 0%,#123f7d 55%,#17539c 100%);
      color:#fff; padding:14px 20px; border-radius:10px; margin-bottom:10px;}
  .astra-head h1 {font-size:1.45rem; margin:0; font-weight:700; letter-spacing:.3px;}
  .astra-head .sub {opacity:.9; font-size:.86rem; margin-top:4px;}
  .astra-chip {display:inline-block; padding:2px 10px; border-radius:11px;
      font-size:.72rem; font-weight:700; letter-spacing:.4px;}
  .astra-note {background:#fff8e1; border-left:4px solid #f0a202; color:#4a3b00;
      padding:7px 12px; border-radius:5px; font-size:.8rem; margin:6px 0 12px 0;}
  .case-card {border:1px solid rgba(140,140,140,.28); border-left-width:5px;
      border-radius:7px; padding:9px 11px; margin-bottom:2px;}
  .case-title {font-weight:640; font-size:.93rem; line-height:1.3;}
  .case-meta {font-size:.76rem; opacity:.72;}
  .case-sig {font-size:.79rem; margin-top:3px;}
  .sig-box {border-left:4px solid #999; border-radius:5px; padding:8px 12px;
      margin:7px 0; background:rgba(128,128,128,.08);}
  .kv {font-size:.79rem; opacity:.85;}
  .pipe {text-align:center; font-size:.85rem; opacity:.7; margin:3px 0;}
  .plan-stage {font-size:.72rem;letter-spacing:.7px;font-weight:700;opacity:.78;
      margin:12px 0 5px 0;}
  .plan-item {border:1px solid rgba(140,140,140,.3);border-left:4px solid #2471a3;
      border-radius:6px;padding:8px 12px;margin-bottom:6px;}
  .plan-why {font-size:.82rem;opacity:.85;margin-top:3px;}
  .ai-badge {display:inline-block;padding:2px 9px;border-radius:10px;
      font-size:.7rem;font-weight:700;letter-spacing:.4px;}
  div[data-testid="stMetricValue"] {font-size:1.45rem;}
  section[data-testid="stSidebar"] {width: 320px !important;}
</style>
"""


# ------------------------------------------------------------------ data

@st.cache_data(ttl=120, show_spinner=False)
def get_facets():
    return db.flag_facets()


@st.cache_data(ttl=120, show_spinner=False)
def get_ingest_meta():
    p = PROCESSED_DIR / "ingest_meta.json"
    meta = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    prov = db.load_provenance()
    return meta, prov


@st.cache_data(ttl=120, show_spinner=False)
def get_run_meta():
    p = PROCESSED_DIR / "run_meta.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


@st.cache_data(ttl=120, show_spinner=False)
def get_corpus_stats():
    with db.connect() as con:
        works = con.execute("SELECT COUNT(*) FROM works").fetchone()[0]
        states = con.execute(
            "SELECT COUNT(DISTINCT state) FROM works WHERE state IS NOT NULL").fetchone()[0]
        districts = con.execute(
            "SELECT COUNT(DISTINCT district) FROM works WHERE district IS NOT NULL").fetchone()[0]
    return {"works": works, "states": states, "districts": districts}


@st.cache_data(ttl=60, show_spinner=False)
def cached_query(**kw):
    return db.query_flags(**kw)


@st.cache_data(ttl=60, show_spinner=False)
def cached_count(**kw):
    return db.count_flags(**kw)


@st.cache_data(ttl=120, show_spinner=False)
def cached_state_summary():
    return db.state_risk_summary()


@st.cache_data(ttl=120, show_spinner=False)
def cached_district_summary(state):
    return db.district_risk_summary(state)


@st.cache_data(ttl=120, show_spinner=False)
def cached_stats(**kw):
    return db.flag_stats(**kw)


@st.cache_data(ttl=120, show_spinner=False)
def cached_rules_scoped(**kw):
    return db.rule_histogram_scoped(**kw)


@st.cache_data(ttl=120, show_spinner=False)
def work_row(work_id: str):
    df = db.read_df("works", "work_id = ?", (work_id,))
    return df.iloc[0].to_dict() if not df.empty else None


@st.cache_data(ttl=120, show_spinner=False)
def agency_works(agency: str, limit: int = 40):
    df = db.read_df("works", "UPPER(ia_name) = ? OR UPPER(vendor_name) = ?",
                    (agency.upper(), agency.upper()))
    return df.head(limit)


@st.cache_data(ttl=900, show_spinner=False)
def cached_synthesis(flag_id: str, tier: str, fingerprint: str, use_llm: bool):
    """Cache on (case, role, evidence fingerprint) so switching authority or
    re-running the pipeline produces a fresh synthesis, but re-selecting the
    same case does not re-bill the API."""
    f = db.get_flag(flag_id)
    if not f:
        return None
    work = None
    if f["entity_type"] == "work":
        df = db.read_df("works", "work_id = ?", (f["entity_id"],))
        work = df.iloc[0].to_dict() if not df.empty else None
    return synthesis.synthesise(f, tier, work, use_llm=use_llm)


@st.cache_data(ttl=300, show_spinner=False)
def llm_status():
    return synthesis.llm_status()


def refresh():
    st.cache_data.clear()


# ------------------------------------------------------------------ ui bits

def chip(text: str, bg: str, fg: str = "#fff") -> str:
    return f"<span class='astra-chip' style='background:{bg};color:{fg}'>{text}</span>"


def status_chip(status: str) -> str:
    label, color = STATUS_META.get(status, ("NEW", "#5d6d7e"))
    return chip(label, color)


def header(meta: dict):
    mode = (meta.get("mode_resolved") or "unknown").upper()
    fresh = meta.get("freshness") or {}
    mode_bg = {"LIVE": "#1e8449", "OFFLINE": "#2471a3"}.get(mode, "#5d6d7e")
    sub = chip(f"{mode} MODE", mode_bg)
    if fresh.get("coverage_pct") is not None:
        sub += (f"&nbsp;&nbsp;<span style='font-size:.82rem'>"
                f"<b>{fresh['coverage_pct']}%</b> current against the live MoSPI "
                f"portal · {fresh.get('tenure', '')}</span>")
    st.markdown(
        f"<div class='astra-head'><h1>🛰️ {PLATFORM_NAME}"
        f"<span style='font-weight:400;font-size:.95rem;opacity:.9'> · AI-Powered "
        f"MPLADS Risk Intelligence Platform</span></h1>"
        f"<div class='sub'>{sub}</div></div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='astra-note'>⚖️ <b>Decision-support system.</b> Every case below "
        "is an AI-generated risk indicator requiring review by the competent "
        "authority. Nothing here is a determination of fraud or wrongdoing.</div>",
        unsafe_allow_html=True)


def case_card_html(f: dict) -> str:
    band, color = risk_band(f["risk_score"])
    title = f.get("display_title") or (f.get("entity_label") or "")[:60]
    loc = " · ".join(x for x in (f.get("district"), f.get("state")) if x)
    score = f"{f['risk_score']:.0f}"
    # the trailing serial distinguishes otherwise-identical duplicate records
    ref = str(f.get("entity_id") or "")
    ref = ref.rsplit("/", 1)[-1] if "/" in ref else ref[:14]
    return (
        f"<div class='case-card' style='border-left-color:{color}'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center'>"
        f"{chip(f'{band.upper()} · {score}', color)}"
        f"{status_chip(f['review_status'])}</div>"
        f"<div class='case-title' style='margin-top:5px'>{title}</div>"
        f"<div class='case-meta'>{loc} &nbsp;·&nbsp; <code>#{ref}</code></div>"
        f"<div class='case-sig'>▸ {f.get('primary_signal') or ''}</div></div>")


# ------------------------------------------------------------------ overview

def overview(tier: str, scope: dict, corpus: dict):
    st.markdown(f"#### {TIERS[tier][0]} {TIERS[tier][1]} — risk overview")
    st.caption(TIERS[tier][2])

    stats = cached_stats(**scope)
    c = st.columns(5)
    c[0].metric("Works analysed", f"{corpus['works']:,}",
                help="Official MPLADS work records in the analysed corpus")
    c[1].metric("Cases in your view", f"{stats['total']:,}")
    c[2].metric("High risk", f"{stats['high']:,}",
                help="Composite risk score of 70 or above")
    c[3].metric("Under review", f"{stats['under_review']:,}")
    c[4].metric("Reviewed / closed", f"{stats['closed']:,}")

    if not stats["total"]:
        st.info("No cases in the current scope.")
        return

    left, right = st.columns([1.15, 1])
    with left:
        if tier == "ministry":
            st.markdown("**States by high-risk cases**")
            summ = cached_state_summary()
            if scope.get("states"):
                summ = summ[summ["state"].isin(scope["states"])]
            chart = summ.head(10).set_index("state")[["high_risk", "flags"]]
            chart.columns = ["High risk", "All cases"]
            st.bar_chart(chart, height=290, horizontal=True)
        elif tier == "state":
            st.markdown("**Districts by high-risk cases**")
            sel = (scope.get("states") or [None])[0]
            summ = cached_district_summary(sel)
            if summ.empty:
                st.caption("No district-level cases in this state.")
            else:
                chart = summ.head(10).set_index("district")[["high_risk", "flags"]]
                chart.columns = ["High risk", "All cases"]
                st.bar_chart(chart, height=290, horizontal=True)
        else:
            st.markdown("**Risk distribution in your view**")
            bands = pd.Series(stats["bands"])
            bands = bands[bands > 0]
            if bands.empty:
                st.caption("No cases in scope.")
            else:
                st.bar_chart(bands, height=290, color="#c0392b")

    with right:
        st.markdown("**What is being detected**")
        hist = cached_rules_scoped(**scope)
        # Fall back to the rule's configured title so a detection never shows as
        # a bare rule id just because it is absent from the sampled cases.
        names: dict[str, str] = {c["rule_id"]: c["title"]
                                 for c in get_run_meta().get("rule_coverage", [])}
        for r in cached_query(limit=120, order="risk", **scope):
            for fd in r["findings"]:
                names[fd["rule_id"]] = humanize(fd)["headline"]
        if hist:
            top = sorted(hist.items(), key=lambda kv: -kv[1])[:8]
            det = pd.DataFrame({
                "Detection": [names.get(k, k)[:52] for k, _ in top],
                "Cases": [v for _, v in top],
            })
            st.dataframe(
                det, hide_index=True, height=290, use_container_width=True,
                column_config={"Cases": st.column_config.ProgressColumn(
                    "Cases", format="%d", min_value=0,
                    max_value=int(det["Cases"].max()))})
        else:
            st.caption("No detections in scope.")

    if tier == "ministry":
        st.markdown("**Geographic concentration** — state-level aggregate")
        summ = cached_state_summary()
        pts = []
        for _, r in summ.iterrows():
            cen = STATE_CENTROIDS.get(str(r["state"]).upper())
            if cen:
                pts.append({"lat": cen[0], "lon": cen[1], "state": r["state"],
                            "high_risk": int(r["high_risk"]), "flags": int(r["flags"]),
                            "radius": 18000 + int(r["high_risk"]) * 320})
        if pts:
            import pydeck as pdk
            mdf = pd.DataFrame(pts)
            st.pydeck_chart(pdk.Deck(
                map_style=None,
                initial_view_state=pdk.ViewState(latitude=22.8, longitude=80, zoom=3.4),
                layers=[pdk.Layer("ScatterplotLayer", data=mdf,
                                  get_position="[lon, lat]", get_radius="radius",
                                  get_fill_color="[192, 57, 43, 150]", pickable=True)],
                tooltip={"text": "{state}\n{high_risk} high-risk of {flags} cases"}))
            st.caption("Bubbles sit at state centroids and are sized by high-risk case "
                       "count. The eSAKSHI exports contain no asset coordinates, so no "
                       "work-level position is shown or implied.")


# ------------------------------------------------------------------ detail tabs

def agent_pipeline_view(brief: dict, score: float):
    st.markdown("##### How the system reached this assessment")
    st.caption("Each agent works independently. The orchestrator combines their "
               "findings into one score and one recommendation.")
    for s in brief["signals"]:
        color = SEV_COLOR.get(s["severity"], "#777")
        badge = chip(f"+{s['contribution']} risk · {s['share_pct']}%", color)
        st.markdown(
            f"<div class='sig-box' style='border-left-color:{color}'>"
            f"<div style='display:flex;justify-content:space-between'>"
            f"<b>{s['agent_label']}</b>{badge}</div>"
            f"<div style='font-size:.78rem;opacity:.7'>{AGENT_ROLE.get(s['agent'], '')}</div>"
            f"<div style='font-weight:600;margin-top:4px'>→ {s['headline']}</div>"
            f"<div class='kv' style='margin-top:3px'>Measured: <b>{s['metric'] or '—'}</b>"
            f" &nbsp;·&nbsp; Compared with: <b>{s['benchmark'] or '—'}</b></div></div>",
            unsafe_allow_html=True)
    band, color = risk_band(score)
    st.markdown(
        f"<div class='pipe'>▼</div>"
        f"<div style='text-align:center;border:1.5px dashed {color};border-radius:8px;"
        f"padding:9px'><b>SYNTHESISER / ORCHESTRATOR</b><br>"
        f"<span style='font-size:.82rem;opacity:.85'>{brief['corroboration']}</span></div>"
        f"<div class='pipe'>▼</div>"
        f"<div style='text-align:center;background:{color};color:#fff;border-radius:8px;"
        f"padding:10px'><b>{band.upper()} · {score:.0f}/100 — HUMAN REVIEW REQUIRED</b></div>",
        unsafe_allow_html=True)




STAGE_COLOR = {"immediate": "#c0392b", "next": "#2471a3",
               "if_unresolved": "#d68910", "escalation": "#7d3c98"}


def render_action_plan(plan: list[dict], tier: str):
    """The constrained plan, grouped into the stages the RBAC engine assigned."""
    if not plan:
        st.info("No actions are available to this authority for this case.")
        return
    by_stage: dict[str, list[dict]] = {}
    for a in plan:
        by_stage.setdefault(a.get("stage", "next"), []).append(a)

    n = 0
    for stage in rbac.STAGE_ORDER:
        items = by_stage.get(stage)
        if not items:
            continue
        color = STAGE_COLOR.get(stage, "#2471a3")
        st.markdown(f"<div class='plan-stage' style='color:{color}'>"
                    f"{rbac.STAGE_LABEL[stage]}</div>", unsafe_allow_html=True)
        for a in items:
            n += 1
            st.markdown(
                f"<div class='plan-item' style='border-left-color:{color}'>"
                f"<b>{n}. {a['label']}</b>"
                f"<div class='plan-why'>{a.get('detail', '')}</div>"
                f"<div class='plan-why' style='opacity:.72'><i>Why: "
                f"{a.get('reason') or 'Recommended for this evidence.'}</i></div>"
                f"</div>", unsafe_allow_html=True)


def tab_ai_synthesis(f: dict, tier: str):
    """LLM synthesis + constrained action plan, with the deterministic fallback."""
    status = llm_status()
    enabled = st.session_state.get("use_llm", True)

    head = st.columns([3, 1])
    with head[0]:
        st.markdown("##### AI synthesis and recommended action plan")
        st.caption(f"Generated for **{rbac.ROLE_LABEL.get(tier, tier)}** — the same "
                   f"evidence is synthesised differently for each authority.")
    with head[1]:
        if not status["configured"]:
            st.markdown(chip("DETERMINISTIC", "#5d6d7e"), unsafe_allow_html=True)
            st.caption("No GROQ_API_KEY set")

    fp = synthesis.evidence_fingerprint(
        synthesis.build_evidence_packet(f, tier), tier)
    with st.spinner("Synthesising…"):
        # When no key is configured the provider returns immediately with
        # reason="no_api_key", so the UI can report the true cause rather than
        # claiming the feature was switched off.
        out = cached_synthesis(f["flag_id"], tier, fp, bool(enabled))
    if not out:
        st.warning("Case not found.")
        return

    if out["source"] == "groq":
        st.markdown(
            chip(f"AI SYNTHESIS · {out.get('model', 'groq')}", "#1e8449") +
            (f"  <span style='font-size:.75rem;opacity:.7'>"
             f"{out.get('latency_ms', 0)} ms</span>" if out.get("latency_ms") else ""),
            unsafe_allow_html=True)
    else:
        reason = {
            "no_api_key": "No API key configured — using the deterministic layer.",
            "llm_disabled": "AI synthesis is switched off in the sidebar.",
            "timeout": "The AI service did not respond in time.",
            "rate_limited": "The AI service rate limit was reached.",
            "unauthorized": "The AI service rejected the configured key.",
            "language_guardrail": "The AI response failed a safety check and was "
                                  "discarded.",
        }.get(out.get("fallback_reason"), "The AI service was unavailable.")
        st.markdown(chip("DETERMINISTIC SYNTHESIS", "#5d6d7e"), unsafe_allow_html=True)
        st.caption(f"{reason} The analysis below is produced by the deterministic "
                   f"layer and is complete — only the wording differs.")

    band, color = risk_band(out["risk_score"])
    st.markdown(
        f"<div class='sig-box' style='border-left-color:{color};"
        f"background:rgba(192,57,43,.09)'>"
        f"<div style='font-size:.72rem;letter-spacing:.5px;opacity:.75'>"
        f"KEY RISK · {band.upper()} · {out['risk_score']:.0f}/100</div>"
        f"<b>{out['key_risk_summary']}</b><br>"
        f"<span style='font-size:.9rem'>{out['case_explanation']}</span></div>",
        unsafe_allow_html=True)

    if out.get("authority_specific_summary"):
        st.markdown(f"**What this means for {rbac.ROLE_LABEL.get(tier, tier)}**")
        st.info(out["authority_specific_summary"])

    if out.get("supporting_signals"):
        with st.expander("Supporting signals used", expanded=False):
            for sg in out["supporting_signals"]:
                st.markdown(f"- **{sg.get('source_agent', '')}** — {sg.get('signal', '')}  \n"
                            f"  <span style='font-size:.82rem;opacity:.8'>"
                            f"{sg.get('evidence', '')}</span>", unsafe_allow_html=True)

    st.markdown("#### AI-generated action plan")
    render_action_plan(out.get("action_plan", []), tier)

    if out.get("plan_rationale"):
        st.markdown("**Why these actions were recommended**")
        st.markdown(out["plan_rationale"])

    if out.get("limitations_or_missing_evidence"):
        with st.expander("Limitations and missing evidence", expanded=False):
            for lim in out["limitations_or_missing_evidence"]:
                st.markdown(f"- {lim}")

    if out.get("rejected_actions"):
        st.caption(f"⛔ {len(out['rejected_actions'])} suggested action(s) were "
                   f"outside this authority's permissions and were removed by the "
                   f"constraint engine before display.")
    if out.get("unverified_numbers"):
        st.caption(f"⚠️ Figures that could not be matched to the underlying "
                   f"evidence: {', '.join(out['unverified_numbers'])}. Treat these "
                   f"as unverified.")

    st.info(f"🔒 {out['constraint_notice']}")
    st.warning(f"⚖️ {out['human_review_notice']}")

    with st.expander("Permitted actions for this authority (deterministic allow-list)"):
        st.caption("Computed from role, risk level and the evidence actually "
                   "found — with no model involvement. The AI may only select "
                   "and sequence from this list.")
        allowed = rbac.allowed_actions(tier, f["risk_score"], f["findings"])
        st.dataframe(
            pd.DataFrame([{"Stage": rbac.STAGE_LABEL[a["stage"]],
                           "Action": a["label"], "id": a["action_id"]}
                          for a in allowed]),
            hide_index=True, use_container_width=True, height=240)


def tab_why(f: dict, brief: dict, tier: str):
    band, color = risk_band(f["risk_score"])
    st.markdown("##### Why this case needs attention")
    st.markdown(
        f"<div class='sig-box' style='border-left-color:{color};"
        f"background:rgba(192,57,43,.10)'>"
        f"<div style='font-size:.72rem;letter-spacing:.5px;opacity:.75'>PRIMARY RISK</div>"
        f"<b>{brief['primary_risk']}</b><br>"
        f"<span style='font-size:.9rem'>{brief['primary_plain']}</span></div>",
        unsafe_allow_html=True)

    if len(brief["signals"]) > 1:
        st.markdown("**Supporting signals**")
        for s in brief["signals"][1:]:
            st.markdown(
                f"<div class='sig-box' style='border-left-color:"
                f"{SEV_COLOR.get(s['severity'], '#777')}'>"
                f"<b>{s['headline']}</b><br>"
                f"<span style='font-size:.88rem'>{s['plain']}</span></div>",
                unsafe_allow_html=True)
    else:
        st.caption("No additional corroborating signals were detected for this case.")

    if brief.get("context_note"):
        st.info(brief["context_note"])

    st.markdown(f"##### Recommended action — {brief['tier_label']}")
    for i, a in enumerate(brief["actions"], 1):
        st.markdown(f"**{i}.** {a}")
    st.caption(brief["closing"])
    st.warning(f"⚖️ {brief['disclaimer']}")


def tab_evidence(f: dict, brief: dict):
    st.markdown("##### Evidence and measurements behind this flag")
    st.caption("Exact values produced by each agent. Nothing on this tab is inferred.")
    for s in brief["signals"]:
        with st.expander(f"{s['rule_id']} · {s['headline']}   ({s['agent_label']})"):
            c1, c2, c3 = st.columns(3)
            c1.metric("Measured", s["metric"] or "—")
            c2.metric("Benchmark", s["benchmark"] or "—")
            c3.metric("Risk contribution", f"+{s['contribution']}")
            if s.get("clause"):
                st.markdown(f"**Scheme provision cited:** {s['clause']}")
            st.markdown(f"**Plain reading:** {s['plain']}")
            raw = next((x for x in f["findings"] if x["rule_id"] == s["rule_id"]), None)
            if raw:
                st.markdown("**Technical detail (audit trail)**")
                st.code(raw["summary"], language=None)
                if raw.get("details"):
                    st.json(raw["details"], expanded=False)


def tab_case(f: dict, w: dict | None):
    st.markdown("##### Work record")
    if w is None:
        st.info(f"This case is a **{f['entity_type']}-level** finding rather than a "
                f"single work record.")
        st.markdown(f"**Entity**  \n{f['entity_label']}")
        st.markdown(f"**Location**  \n{f.get('district') or '—'}, {f.get('state') or '—'}")
        return
    left, right = st.columns(2)
    with left:
        st.markdown(f"**Full description**  \n{w.get('description') or '—'}")
        st.markdown(f"**Work code**  \n`{f['entity_id']}`")
        st.markdown(f"**Work type**  \n{w.get('category') or '—'}")
    with right:
        st.markdown(f"**Location**  \n{w.get('district') or '—'}, {w.get('state') or '—'}")
        st.markdown(f"**Constituency / MP**  \n{w.get('constituency') or '—'} · "
                    f"{w.get('mp_name') or '—'}")
        st.markdown(f"**Implementing agency**  \n{w.get('ia_name') or '—'}")
    st.divider()
    m = st.columns(4)
    m[0].metric("Sanctioned", rupees(w.get("sanctioned_amount"))
                if pd.notna(w.get("sanctioned_amount")) else "Not yet sanctioned")
    m[1].metric("Recommended", rupees(w.get("estimated_cost")))
    m[2].metric("Paid to date", rupees(w.get("total_paid"))
                if pd.notna(w.get("total_paid")) else "None recorded")
    m[3].metric("Workflow stage", str(w.get("status") or "—"))
    d = st.columns(4)
    d[0].markdown(f"**Recommended on**  \n{w.get('recommended_date') or '—'}")
    d[1].markdown(f"**Sanctioned on**  \n{w.get('sanction_date') or '—'}")
    d[2].markdown(f"**Completed on**  \n{w.get('completion_date') or 'Not recorded'}")
    d[3].markdown(f"**Data era**  \n`{f.get('era')}`")
    if pd.notna(w.get("vendor_name")):
        st.markdown(f"**Vendor paid:** {str(w['vendor_name']).title()} · "
                    f"{int(w.get('payment_count') or 0)} payment tranche(s)")


def tab_duplicates(f: dict):
    dups = [x for x in f["findings"] if x["rule_id"].startswith("D-")]
    if not dups:
        st.info("No duplicate-risk evidence was identified for this case.")
        return
    st.markdown("##### Possible repeat records")
    st.caption("Approval checks review one work at a time, so the same work entered "
               "twice is not caught by the normal workflow. These are candidates for "
               "verification, not confirmed duplicates.")
    for d in dups:
        det = d["details"]
        if d["rule_id"] == "D-DUP-02":
            st.markdown(f"**Shared-description cluster** — {det.get('cluster_size')} "
                        f"works in {det.get('district')} totalling "
                        f"{rupees(det.get('total_cost'))}")
            st.code(det.get("normalised_description", ""), language=None)
            st.markdown("Member works: " + ", ".join(
                f"`{w}`" for w in (det.get("member_work_ids") or [])[:8]))
            st.divider()
            continue
        c1, c2 = st.columns(2)
        c1.markdown(f"**This work** · `{f['entity_id']}`")
        c1.info(det.get("this_description", "—"))
        c2.markdown(f"**Matched work** · `{det.get('pair_work_id')}`")
        c2.info(det.get("other_description", "—"))
        s = st.columns(3)
        s[0].metric("Description match", f"{det.get('semantic_sim', 0) * 100:.0f}%")
        s[1].metric("Same sanction amount",
                    "Yes" if det.get("same_sanction_amount") else "No")
        s[2].metric("Evidence strength", str(det.get("evidence_strength", "—")).title())
        st.markdown(f"**Pattern:** {det.get('duplication_mode', '—')}")
        if det.get("geo_km") is not None:
            st.markdown(f"**Distance between assets:** {det['geo_km']} km")
        else:
            st.caption("Proximity is assessed at district level — this eSAKSHI export "
                       "carries no asset coordinates.")
        st.divider()


def tab_network(f: dict, w: dict | None):
    agency = None
    if f["entity_type"] == "agency":
        agency = f["entity_id"].split(":", 1)[-1]
    elif w is not None:
        agency = w.get("ia_name")
    if not agency or pd.isna(agency):
        st.info("No contractor or agency information is available for this case.")
        return

    st.markdown(f"##### Agency / contractor context — {str(agency).title()}")
    net_f = [x for x in f["findings"] if x["agent"] == "network"]
    if net_f:
        d = net_f[0]["details"]
        m = st.columns(4)
        m[0].metric("Works", f"{d.get('works', 0):,}")
        m[1].metric("Districts", d.get("districts", 0))
        m[2].metric("States", d.get("states", 0))
        m[3].metric("Above-benchmark share", f"{d.get('overrun_share', 0) * 100:.0f}%")
        if d.get("likely_national_supplier"):
            st.info("This actor supplies items (vehicles, books, equipment) that are "
                    "commonly procured nationally, so operating across many districts "
                    "is expected — the signal is deliberately down-weighted.")
        if d.get("district_list"):
            st.markdown("**Districts covered:** " + ", ".join(d["district_list"]))
    else:
        st.caption("This agency did not itself trigger a network-pattern signal. "
                   "Its other works are listed below for context.")

    rel = agency_works(str(agency))
    if rel.empty:
        st.caption("No other works by this agency were found in the corpus.")
        return
    st.markdown(f"**Other works by this agency** ({len(rel)} shown)")
    show = rel[["work_id", "district", "category", "sanctioned_amount", "status"]].copy()
    show.columns = ["Work code", "District", "Work type", "Sanctioned (₹)", "Stage"]
    st.dataframe(show, use_container_width=True, height=250, hide_index=True)


def tab_trace():
    run = get_run_meta()
    st.markdown("##### Which agents ran on this batch, and why")
    st.caption("The orchestrator dispatches only agents whose required inputs exist "
               "in the current data, and records that decision.")
    for t in run.get("router_trace", []):
        icon = "🟢" if t["dispatched"] else "⚪"
        detail = (f"produced **{t.get('findings', 0):,} findings**"
                  if t["dispatched"] else t["reason"])
        st.markdown(f"{icon} **{AGENT_LABEL.get(t['agent'], t['agent'])}** — {detail}")

    cov = run.get("rule_coverage", [])
    if not cov:
        return
    st.markdown("##### Rule coverage across the whole batch")
    cdf = pd.DataFrame(cov)[["rule_id", "title", "findings"]]
    cdf.columns = ["Rule", "Check", "Cases found"]
    st.dataframe(cdf, use_container_width=True, height=250, hide_index=True)
    zero = [c["rule_id"] for c in cov if c["findings"] == 0]
    if zero:
        st.caption("Rules reporting zero are shown deliberately: " + ", ".join(zero) +
                   " found no matches — either the corpus is clean on that check, or "
                   "this data source lacks the inputs the rule requires.")


def tab_source():
    meta, prov = get_ingest_meta()
    fresh = meta.get("freshness") or {}
    st.markdown("##### Where this data came from")
    c = st.columns(3)
    c[0].metric("Resolved mode", (meta.get("mode_resolved") or "—").upper())
    c[1].metric("Works ingested", f"{meta.get('works', 0):,}")
    c[2].metric("Fund-flow records", f"{meta.get('fundflows', 0):,}")
    if fresh:
        st.success(
            f"**Live check against mplads.mospi.gov.in** — this batch holds "
            f"{fresh['local_recommended_works']:,} recommended works; the official "
            f"portal reported {fresh['live_recommended_works']:,} when queried at "
            f"{fresh['checked_at'][:16]}Z. That is **{fresh['coverage_pct']}% "
            f"coverage** ({fresh.get('tenure')}).")
    if not prov.empty:
        show = prov[["source", "mode", "rows", "status"]].copy()
        show.columns = ["Source", "Mode", "Rows", "Status"]
        st.dataframe(show, use_container_width=True, hide_index=True, height=230)
    st.caption("Dual-mode ingestion: live official interfaces are attempted under a "
               "strict time budget, and the official CSV exports supply the corpus "
               "when live access is slow, partial or unavailable — so a government "
               "endpoint being down can never block the platform.")


def review_controls(f: dict, tier: str, scope: str = "main"):
    st.markdown("##### Human review decision")
    cur = f["review_status"]
    flow = {"pending": "NEW", "under_review": "UNDER REVIEW",
            "confirmed": "ESCALATED", "false_positive": "CLOSED · FALSE POSITIVE"}
    st.markdown("Workflow: " + "  →  ".join(
        f"**{v}**" if k == cur else f"<span style='opacity:.4'>{v}</span>"
        for k, v in flow.items()), unsafe_allow_html=True)

    note = st.text_input("Reviewer note (optional)",
                         key=f"note_{scope}_{f['flag_id']}",
                         placeholder="Record what you verified…")
    c1, c2, c3 = st.columns(3)
    if c1.button("🔎 Mark under review", key=f"ur_{scope}_{f['flag_id']}",
                 use_container_width=True):
        db.record_feedback(f["flag_id"], "under_review", tier, note)
        refresh(); st.rerun()
    if c2.button("⬆️ Escalate for action", key=f"cf_{scope}_{f['flag_id']}",
                 use_container_width=True, type="primary"):
        db.record_feedback(f["flag_id"], "confirmed", tier, note)
        refresh(); st.rerun()
    if c3.button("✅ Close as false positive", key=f"fp_{scope}_{f['flag_id']}",
                 use_container_width=True):
        db.record_feedback(f["flag_id"], "false_positive", tier,
                           note or "authority review: not a genuine risk")
        refresh(); st.rerun()
    st.caption("The authority decides the outcome — ASTRA only prioritises what to "
               "look at. False-positive decisions are recorded and feed threshold "
               "recalibration. Persistence in this build is local to the demo database.")


def detail_panel(f: dict, tier: str):
    brief = (f.get("tier_briefs") or {}).get(tier) or {}
    if not brief:
        st.warning("No authority brief stored for this case. Re-run "
                   "`python scripts/run_pipeline.py`.")
        return
    band, color = risk_band(f["risk_score"])
    w = work_row(f["entity_id"]) if f["entity_type"] == "work" else None
    score = f"{f['risk_score']:.0f}"

    st.markdown(
        f"<div style='display:flex;justify-content:space-between;align-items:flex-start'>"
        f"<div><div style='font-size:1.1rem;font-weight:680;line-height:1.3'>"
        f"{f.get('display_title') or f['entity_label']}</div>"
        f"<div style='font-size:.79rem;opacity:.72;margin-top:2px'>"
        f"<code>{f['entity_id']}</code> · {f.get('district') or '—'}, "
        f"{f.get('state') or '—'}</div></div>"
        f"<div style='text-align:right;white-space:nowrap'>"
        f"{chip(f'{band.upper()} · {score}/100', color)}<br>"
        f"<span style='display:inline-block;margin-top:4px'>"
        f"{status_chip(f['review_status'])}</span></div></div>",
        unsafe_allow_html=True)
    st.caption(f"Viewing as **{brief.get('tier_label', tier)}** — {brief.get('lens', '')}")

    tabs = st.tabs(["Why flagged", "🤖 AI synthesis & action plan", "Agent trace",
                    "Evidence", "Work record", "Duplicates", "Agency network",
                    "Pipeline", "Data source"])
    with tabs[0]:
        tab_why(f, brief, tier)
        st.divider()
        review_controls(f, tier, scope="why")
    with tabs[1]:
        tab_ai_synthesis(f, tier)
        st.divider()
        review_controls(f, tier, scope="ai")
    with tabs[2]:
        agent_pipeline_view(brief, f["risk_score"])
    with tabs[3]:
        tab_evidence(f, brief)
    with tabs[4]:
        tab_case(f, w)
    with tabs[5]:
        tab_duplicates(f)
    with tabs[6]:
        tab_network(f, w)
    with tabs[7]:
        tab_trace()
    with tabs[8]:
        tab_source()

    with st.expander("🪞 Compare — how the other authorities see this same case"):
        st.caption("Same underlying flag and same evidence, reframed by the "
                   "orchestrator for each audience. This is synthesis, not "
                   "column-hiding.")
        for t, (icon, label, _l) in TIERS.items():
            if t == tier:
                continue
            b = (f.get("tier_briefs") or {}).get(t)
            if not b:
                continue
            st.markdown(f"**{icon} {label}** — *{b['lens']}*")
            st.markdown(f"{b['opening']} {b['primary_plain'][:180]}")
            st.markdown(f"↳ *First action:* {b['actions'][0] if b['actions'] else '—'}")


# ------------------------------------------------------------------ main

def main():
    st.markdown(CSS, unsafe_allow_html=True)
    meta, _prov = get_ingest_meta()
    header(meta)

    facets = get_facets()
    corpus = get_corpus_stats()
    if facets["total"] == 0:
        st.warning("No analysed cases yet. Run `python scripts/fetch_data.py` then "
                   "`python scripts/run_pipeline.py`.")
        return

    sb = st.sidebar
    sb.markdown("### Authority view")
    tier = sb.radio("Signed in as", list(TIERS), index=0,
                    label_visibility="collapsed",
                    format_func=lambda t: f"{TIERS[t][0]}  {TIERS[t][1]}")
    sb.caption(TIERS[tier][2])
    sb.caption("Demo role switcher. Stage-2 roadmap: eSAKSHI SSO with full RBAC "
               "and audit logging.")
    sb.divider()

    scope: dict = {}
    if tier == "mp":
        opts = facets["constituencies"]
        if opts:
            scope["constituencies"] = [sb.selectbox("Your constituency", opts)]
    elif tier == "district":
        opts = facets["districts"]
        if opts:
            scope["districts"] = [sb.selectbox("Your district", opts)]
    elif tier == "state":
        opts = facets["states"]
        if opts:
            scope["states"] = [sb.selectbox("Your state", opts)]
    else:
        sel = sb.multiselect("Filter states (optional)", facets["states"])
        if sel:
            scope["states"] = sel

    sb.markdown("### Filters")
    min_score = sb.slider("Minimum risk score", 0, 100, 0, 5)
    statuses = sb.multiselect("Review status", list(STATUS_META),
                              format_func=lambda s: STATUS_META[s][0])
    run = get_run_meta()
    rule_opts = [c["rule_id"] for c in run.get("rule_coverage", []) if c["findings"] > 0]
    rules = sb.multiselect("Detection type", rule_opts)
    etypes = sb.multiselect(
        "Case level", ["work", "constituency", "agency"],
        format_func=lambda e: {"work": "Individual work",
                               "constituency": "Constituency",
                               "agency": "Agency / vendor"}[e])
    order = sb.selectbox("Sort by", ["risk", "risk_asc", "state"],
                         format_func=lambda o: {"risk": "Highest risk first",
                                                "risk_asc": "Lowest risk first",
                                                "state": "By state"}[o])
    sb.markdown("### AI synthesis")
    status = llm_status()
    st.session_state.setdefault("use_llm", True)
    if status["configured"]:
        st.session_state["use_llm"] = sb.toggle(
            "Use Groq LLM synthesis", value=st.session_state["use_llm"],
            help=f"Model: {status['model']}. Turn off to compare against the "
                 f"deterministic layer.")
        sb.caption(f"🟢 Connected · key {status['key_hint']}")
    else:
        sb.caption("⚪ No GROQ_API_KEY set — the deterministic synthesis layer "
                   "is in use. Add a key to .env to enable AI synthesis.")

    if sb.button("↻ Refresh data", use_container_width=True):
        refresh(); st.rerun()

    filters = dict(scope, min_score=min_score, statuses=statuses or None,
                   rule_ids=rules or None, entity_types=etypes or None)

    with st.expander("📊 Executive overview", expanded=True):
        overview(tier, dict(scope), corpus)

    st.markdown("### 🔍 Case investigation workspace")
    left, right = st.columns([1, 2], gap="medium")

    with left:
        search = st.text_input("Search cases", label_visibility="collapsed",
                               placeholder="Search work code, title or district…")
        rows = cached_query(search=search, limit=250, order=order, **filters)
        total = cached_count(search=search, **filters)
        st.caption(f"**{total:,}** cases match · showing top {len(rows)}")

        if not rows:
            st.info("No cases match these filters. Widen the filters in the sidebar.")
        else:
            ids = [r["flag_id"] for r in rows]
            if st.session_state.get("sel") not in ids:
                st.session_state["sel"] = ids[0]
            with st.container(height=640, border=False):
                for r in rows:
                    st.markdown(case_card_html(r), unsafe_allow_html=True)
                    if st.button("Investigate →", key=f"sel_{r['flag_id']}",
                                 use_container_width=True):
                        st.session_state["sel"] = r["flag_id"]
                        st.rerun()

    with right:
        sel = db.get_flag(st.session_state.get("sel") or "")
        if sel:
            detail_panel(sel, tier)
        else:
            st.info("Select a case from the list to open the investigation workspace.")


main()
