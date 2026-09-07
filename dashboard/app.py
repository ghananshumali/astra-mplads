"""ASTRA dashboard — four authority tiers over one flag corpus.

The four views are NOT four pages of filtered SQL: every flag carries four
orchestrator-synthesized framings (tier_views) and each tier renders its own
framing, action language, and aggregation level. Switch roles on the same
flag to see the synthesis differ — that is the demo's core proof point.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astra import HUMAN_REVIEW_DISCLAIMER, PLATFORM_NAME, PLATFORM_TAGLINE  # noqa: E402
from astra import db  # noqa: E402
from astra.config import PROCESSED_DIR  # noqa: E402

st.set_page_config(page_title=f"{PLATFORM_NAME} — MPLADS Risk Analytics",
                   page_icon="🛰️", layout="wide")

# Approximate state centroids for map fallback when works lack lat/long.
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

TIER_LABELS = {
    "mp": "🧑‍⚖️ Member of Parliament",
    "district": "🏛️ District Authority",
    "state": "🗺️ State Nodal Authority",
    "ministry": "🏢 Ministry (MoSPI)",
}
SEV_COLOR = {"critical": "#d62728", "high": "#ff7f0e", "medium": "#e6b800", "low": "#7f7f7f"}


@st.cache_data(ttl=60)
def load_all():
    flags = db.load_flags()
    works = db.read_df("works")
    flows = db.read_df("fundflows")
    return flags, works, flows


@st.cache_data(ttl=60)
def load_ingest_meta():
    """Dual-mode ingestion provenance: which source produced this batch."""
    p = PROCESSED_DIR / "ingest_meta.json"
    meta = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    prov = db.load_provenance()
    return meta, prov


def risk_badge(score: float, alert: bool) -> str:
    color = "#d62728" if score >= 65 else "#ff7f0e" if score >= 40 else "#e6b800"
    tag = " · ALERT" if alert else ""
    return (f"<span style='background:{color};color:white;padding:2px 10px;"
            f"border-radius:12px;font-weight:600'>risk {score:.0f}/100{tag}</span>")


def review_badge(status: str) -> str:
    color = {"pending": "#6c757d", "under_review": "#0d6efd",
             "confirmed": "#d62728", "false_positive": "#198754"}.get(status, "#6c757d")
    label = {"pending": "HUMAN REVIEW REQUIRED", "under_review": "UNDER REVIEW",
             "confirmed": "CONFIRMED BY AUTHORITY", "false_positive": "MARKED FALSE-POSITIVE"}[status]
    return (f"<span style='border:1px solid {color};color:{color};padding:2px 10px;"
            f"border-radius:12px;font-size:0.8em;font-weight:600'>{label}</span>")


def render_flag(f: dict, tier: str):
    with st.container(border=True):
        c1, c2 = st.columns([4, 1])
        with c1:
            st.markdown(f"**{f['entity_label']}**  \n"
                        f"{f['state'] or ''} · {f['district'] or ''} · era: `{f['era']}`")
        with c2:
            st.markdown(risk_badge(f["risk_score"], f["alert"]) + "<br>" +
                        review_badge(f["review_status"]), unsafe_allow_html=True)

        st.markdown(f"**{TIER_LABELS[tier]} briefing** *(orchestrator-synthesized for this tier)*")
        st.info(f["tier_views"].get(tier, f["narrative"]))

        with st.expander("🔗 Causal narrative & evidence chain (all agents)"):
            st.write(f["narrative"])
            for fd in f["findings"]:
                sev = fd["severity"]
                st.markdown(
                    f"<div style='border-left:4px solid {SEV_COLOR[sev]};padding:4px 10px;margin:6px 0'>"
                    f"<b>{fd['rule_id']}</b> — {fd['rule_title']} "
                    f"<span style='color:{SEV_COLOR[sev]}'>[{sev}]</span> · agent: <i>{fd['agent']}</i><br>"
                    f"{fd['summary']}<br>"
                    + (f"<small>📜 {fd['clause']}</small>" if fd.get("clause") else "")
                    + "</div>", unsafe_allow_html=True)

        with st.expander("🪞 Same flag, other authority framings (synthesis demo)"):
            for t, label in TIER_LABELS.items():
                if t != tier:
                    st.markdown(f"**{label}:** {f['tier_views'].get(t, '—')}")

        fc1, fc2, fc3 = st.columns(3)
        if fc1.button("✅ Confirm — escalate", key=f"c_{tier}_{f['flag_id']}"):
            db.record_feedback(f["flag_id"], "confirmed", tier)
            st.cache_data.clear(); st.rerun()
        if fc2.button("🟢 False positive", key=f"fp_{tier}_{f['flag_id']}"):
            db.record_feedback(f["flag_id"], "false_positive", tier,
                               "authority review: not a genuine risk")
            st.cache_data.clear(); st.rerun()
        if fc3.button("🔎 Mark under review", key=f"ur_{tier}_{f['flag_id']}"):
            db.record_feedback(f["flag_id"], "under_review", tier)
            st.cache_data.clear(); st.rerun()


def main():
    st.markdown(
        f"<div style='background:#0b3d91;color:white;padding:14px 20px;border-radius:8px'>"
        f"<span style='font-size:1.6em;font-weight:700'>🛰️ {PLATFORM_NAME}</span> "
        f"<span style='opacity:.85'>· {PLATFORM_TAGLINE}</span><br>"
        f"<small>⚖️ {HUMAN_REVIEW_DISCLAIMER}</small></div>",
        unsafe_allow_html=True)

    meta, prov = load_ingest_meta()
    mode = (meta.get("mode_resolved") or "unknown").upper()
    fresh = meta.get("freshness") or {}
    badge_bg = "#0f7b3f" if mode == "LIVE" else "#1f4e8c"
    fresh_txt = ""
    if fresh.get("coverage_pct") is not None:
        fresh_txt = (f" &nbsp;|&nbsp; <b>{fresh['coverage_pct']}%</b> of the live "
                     f"eSAKSHI portal's {fresh['live_recommended_works']:,} recommended "
                     f"works ({fresh.get('tenure', '')}) — verified live at run time")
    st.markdown(
        f"<div style='background:#eef2f7;color:#12263f;border-left:5px solid {badge_bg};"
        f"padding:8px 14px;margin:8px 0;border-radius:4px;font-size:0.92em'>"
        f"<b>Data source:</b> <span style='background:{badge_bg};color:white;"
        f"padding:1px 8px;border-radius:10px'>{mode} MODE</span>{fresh_txt}</div>",
        unsafe_allow_html=True)

    flags, works, flows = load_all()
    if not flags:
        st.warning("No flags in the database yet. Run: `python scripts/fetch_data.py` "
                   "then `python -m astra.pipeline`.")
        return

    st.sidebar.title("Authority sign-in")
    tier = st.sidebar.radio("View as", list(TIER_LABELS), format_func=lambda t: TIER_LABELS[t])
    st.sidebar.caption("Demo role-switcher. Stage-2 roadmap: full RBAC with "
                       "eSAKSHI SSO + audit logging.")

    df = pd.DataFrame([{k: f[k] for k in
                        ("flag_id", "entity_type", "entity_label", "state", "district",
                         "constituency", "era", "risk_score", "alert", "review_status")}
                       for f in flags])

    # ---- tier scoping ----
    scoped = flags
    if tier == "mp":
        opts = sorted(df["constituency"].dropna().unique().tolist())
        sel = st.sidebar.selectbox("Your constituency", opts) if opts else None
        scoped = [f for f in flags if f["constituency"] == sel] if sel else []
        if not opts:
            st.sidebar.info("No constituency-attributed flags in this batch — "
                            "showing none (MPs see only their own constituency).")
    elif tier == "district":
        opts = sorted(df["district"].dropna().unique().tolist())
        sel = st.sidebar.selectbox("Your district", opts) if opts else None
        scoped = [f for f in flags if f["district"] == sel] if sel else flags
    elif tier == "state":
        opts = sorted(df["state"].dropna().unique().tolist())
        sel = st.sidebar.selectbox("Your state", opts) if opts else None
        scoped = [f for f in flags if f["state"] == sel] if sel else flags

    min_score = st.sidebar.slider("Min risk score", 0, 100, 0, 5)
    only_alerts = st.sidebar.checkbox("Alerts only (crossed review threshold)")
    scoped = [f for f in scoped if f["risk_score"] >= min_score and (f["alert"] or not only_alerts)]

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Works analyzed", f"{len(works):,}")
    m2.metric("Fund-flow rows", f"{len(flows):,}")
    m3.metric("Flags (this view)", len(scoped))
    m4.metric("High-risk alerts", sum(1 for f in scoped if f["alert"]))
    fb = db.feedback_stats()
    m5.metric("Human reviews logged", int(fb["n"].sum()) if not fb.empty else 0)

    tabs = st.tabs(["🚩 Flagged cases", "🗺️ Map & duplicates", "📊 Patterns",
                    "🕸️ Vendor network", "🤖 Orchestration trace",
                    "🔌 Data source", "ℹ️ Method"])

    with tabs[0]:
        if tier == "ministry":
            st.subheader("National exception overview")
            by_state = df.groupby("state", dropna=False)["flag_id"].count().sort_values(ascending=False)
            st.bar_chart(by_state, height=220)
        for f in scoped[:40]:
            render_flag(f, tier)
        if len(scoped) > 40:
            st.caption(f"Showing top 40 of {len(scoped)} by risk score.")
        if not scoped:
            st.success("No flags in scope for this authority view.")

    with tabs[1]:
        st.subheader("Geospatial view — flagged works & duplicate clusters")
        rows = []
        for f in scoped:
            lat = lon = None
            # exact coordinates when the source provides them; state centroid fallback
            w = works[works["work_id"] == f["entity_id"]] if not works.empty else pd.DataFrame()
            if not w.empty and pd.notna(w.iloc[0].get("lat")):
                lat, lon = float(w.iloc[0]["lat"]), float(w.iloc[0]["lon"])
                precise = True
            else:
                cen = STATE_CENTROIDS.get(str(f["state"] or "").upper())
                if cen:
                    import hashlib as _h
                    jseed = int(_h.sha1(f["flag_id"].encode()).hexdigest()[:6], 16)
                    lat = cen[0] + ((jseed % 100) - 50) / 90.0
                    lon = cen[1] + (((jseed // 100) % 100) - 50) / 90.0
                    precise = False
            if lat is not None:
                dup = any(fd["rule_id"].startswith("D-") for fd in f["findings"])
                rows.append({"lat": lat, "lon": lon, "risk": f["risk_score"],
                             "label": f["entity_label"][:60], "dup": dup,
                             "precise": precise})
        if rows:
            mdf = pd.DataFrame(rows)
            mdf["radius"] = 3000 + mdf["risk"] * 400
            mdf["r"] = mdf["dup"].map({True: 214, False: 255})
            mdf["g"] = mdf["dup"].map({True: 39, False: 127})
            mdf["b"] = mdf["dup"].map({True: 40, False: 14})
            import pydeck as pdk
            layer = pdk.Layer(
                "ScatterplotLayer", data=mdf, get_position="[lon, lat]",
                get_radius="radius", get_fill_color="[r, g, b, 170]",
                pickable=True)
            st.pydeck_chart(pdk.Deck(
                map_style=None, layers=[layer],
                initial_view_state=pdk.ViewState(latitude=22.5, longitude=80, zoom=3.6),
                tooltip={"text": "{label}\nrisk {risk}"}))
            if not mdf["precise"].all():
                st.caption("⚠️ Positions without source lat/long are placed near their state "
                           "centroid (jittered) for overview only — eSAKSHI asset geo-tags "
                           "populate exact positions when present.")
            st.markdown("🔴 red = duplicate-detection flag · 🟠 orange = other flags")
        else:
            st.info("No mappable flags in scope.")
        dup_flags = [f for f in scoped
                     if any(fd["rule_id"].startswith("D-") for fd in f["findings"])]
        if dup_flags:
            st.subheader(f"Duplicate / near-duplicate pairs ({len(dup_flags)})")
            for f in dup_flags[:15]:
                fd = next(x for x in f["findings"] if x["rule_id"].startswith("D-"))
                st.markdown(
                    f"- **{f['entity_id']}** ↔ **{fd['details'].get('pair_work_id')}** — "
                    f"sim {fd['details'].get('semantic_sim')} · mode: "
                    f"*{fd['details'].get('duplication_mode')}*  \n"
                    f"  \"{fd['details'].get('this_description','')[:100]}…\" vs "
                    f"\"{fd['details'].get('other_description','')[:100]}…\"")

    with tabs[2]:
        st.subheader("Pattern analytics" + (" — era-aware" if "era" in df else ""))
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Findings by rule**")
            rule_counts: dict[str, int] = {}
            for f in scoped:
                for fd in f["findings"]:
                    rule_counts[f"{fd['rule_id']} {fd['rule_title'][:30]}"] = \
                        rule_counts.get(f"{fd['rule_id']} {fd['rule_title'][:30]}", 0) + 1
            if rule_counts:
                st.bar_chart(pd.Series(rule_counts).sort_values(), height=300)
        with c2:
            st.markdown("**Flags by data era** *(baselines are computed per era — the "
                        "2023 eSAKSHI cutover never contaminates anomaly statistics)*")
            era_counts = df["era"].value_counts()
            st.bar_chart(era_counts, height=300)

    with tabs[3]:
        st.subheader("🕸️ Contractor / implementing-agency network signals")
        st.caption("Same actor recurring across many districts, and actors whose "
                   "works price above peer benchmarks. Screening signals for "
                   "verification — never an allegation against a firm or officer.")
        net = [f for f in scoped if f["entity_type"] == "agency"]
        if not net:
            net = [f for f in flags if f["entity_type"] == "agency"]
            if net:
                st.info("No network signals inside the current filter — showing all "
                        "network signals in the batch.")
        if net:
            rows = []
            for f in net:
                fd = next((x for x in f["findings"] if x["agent"] == "network"), None)
                if not fd:
                    continue
                d = fd["details"]
                rows.append({
                    "actor": str(d.get("actor", ""))[:44].title(),
                    "type": d.get("actor_type"),
                    "works": d.get("works"),
                    "districts": d.get("districts"),
                    "states": d.get("states"),
                    "overrun share": d.get("overrun_share"),
                    "value (Rs)": d.get("total_value"),
                    "national supplier?": d.get("likely_national_supplier"),
                    "severity": fd["severity"],
                    "risk": f["risk_score"],
                })
            ndf = pd.DataFrame(rows).sort_values(
                ["overrun share", "districts"], ascending=False)
            st.dataframe(ndf, use_container_width=True, height=420)
            st.caption("‘national supplier?’ marks actors whose work types (vehicles, "
                       "books, equipment) make wide district coverage legitimate — "
                       "these are deliberately down-weighted to avoid false positives.")
        else:
            st.info("No vendor/agency network signals in this batch.")

    with tabs[4]:
        st.subheader("🤖 Why each agent ran — orchestrator routing trace")
        st.caption("The orchestrator dispatches only agents whose declared inputs exist in "
                   "the current batch (dynamic conditional routing, LangGraph-style), and "
                   "records the decision.")
        meta_p = PROCESSED_DIR / "run_meta.json"
        if meta_p.exists():
            # distinct name: `meta` holds the INGESTION metadata used by later tabs
            run_meta = json.loads(meta_p.read_text(encoding="utf-8"))
            st.write(f"Last pipeline run: `{run_meta['ran_at']}` — "
                     f"{run_meta['flags_produced']} flags")
            for t in run_meta["router_trace"]:
                icon = "🟢" if t["dispatched"] else "⚪"
                extra = f" → **{t.get('findings', 0)} findings**" if t["dispatched"] else ""
                st.markdown(f"{icon} `{t['agent']}` — {t['reason']}{extra}")
        if meta_p.exists():
            cov = json.loads(meta_p.read_text(encoding="utf-8")).get("rule_coverage", [])
            if cov:
                st.subheader("📋 Rule coverage — including rules that matched nothing")
                st.caption("A rule evaluating to zero is a real result: either the "
                           "corpus is clean on that check, or the rule stood down "
                           "because this data source lacks the inputs it requires. "
                           "ASTRA reports that rather than guessing.")
                cdf = pd.DataFrame(cov)[["rule_id", "title", "findings"]]
                st.dataframe(cdf, use_container_width=True, height=300)

        fb = db.feedback_stats()
        st.subheader("🔁 Human feedback loop")
        if fb.empty:
            st.caption("No review actions yet. Confirm / false-positive buttons on each flag "
                       "feed this table; stage-2 uses FP rates to recalibrate rule thresholds.")
        else:
            st.dataframe(fb, use_container_width=True)

    with tabs[5]:
        st.subheader("🔌 Dual-mode ingestion — where this batch came from")
        c1, c2, c3 = st.columns(3)
        c1.metric("Resolved mode", mode)
        c2.metric("Works ingested", f"{meta.get('works', 0):,}")
        c3.metric("Fund-flow rows", f"{meta.get('fundflows', 0):,}")
        if fresh:
            st.success(
                f"**Live freshness check against mplads.mospi.gov.in:** this batch "
                f"holds {fresh['local_recommended_works']:,} recommended works; the "
                f"official portal reported {fresh['live_recommended_works']:,} at "
                f"{fresh['checked_at'][:19]}Z — **{fresh['coverage_pct']}% coverage** "
                f"({fresh.get('tenure')}). The portal was queried live during "
                f"ingestion, so the corpus below is provably current government data.")
        st.markdown("**Source ledger for this batch**")
        if not prov.empty:
            show = prov[["source", "mode", "table_name", "rows", "status", "detail"]]
            st.dataframe(show, use_container_width=True, height=280)
        st.markdown(f"""
**How the router decides.** `auto` probes the live official interfaces under a
strict time budget, then analyses the most complete authentic corpus available.
The official CSV exports in `datasets/` are the full national record, so they
supply the work corpus, while the live portal supplies the freshness check and
the pre-2023 historical baseline. If those exports are absent, the router pulls
work records live instead. A slow or unavailable government endpoint can never
block the pipeline.

- `--mode live`    — live interfaces only (proves the real-time path works)
- `--mode offline` — official CSV exports only (deterministic, demo-safe)
- `--mode auto`    — default; resilient, never blocked

**Work eras in this batch:** {meta.get('eras', {})}
**Fund-flow eras:** {meta.get('flow_eras', {})} — pre-2023 rows come from the
live open-data interfaces, which is what makes the era-separated baselines real
rather than hypothetical.
""")

    with tabs[6]:
        st.markdown(f"""
**Data sources (live, free/open):** data.gov.in OGD API (MPLADS releases,
expenditure, works — historical), MPLADS eSAKSHI public reports where
available. Raw pulls cached under `data/raw/`.

**Era handling:** every record is tagged `pre2023` / `post2023` around the
eSAKSHI 2023-04 cutover; anomaly peer groups and expenditure baselines never
straddle the boundary.

**Benchmarks:** cost anomalies score against the median of (state × category
× era) peers — an *empirical Schedule-of-Rates proxy*. Official SoR tables
drop into `data/sor/` when made available.

**Agents:** Ingestion · Compliance (deterministic, clause-cited) · Statistical
Anomaly (robust-z + IsolationForest) · Entity-Resolution (TF-IDF + fuzzy +
geo) · Network (agency concentration) · **Orchestrator** (routing, scoring,
causal narrative, tier synthesis).

**Roadmap (stage 2):** predictive delay forecasting, NL query interface, full
RBAC + audit logging, OCR for scanned certificates, official SoR integration,
threshold auto-recalibration from reviewer feedback.

⚖️ *{HUMAN_REVIEW_DISCLAIMER}*
""")


main()
