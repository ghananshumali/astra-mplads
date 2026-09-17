"""Synthesizer / Orchestrator Agent — the meta-policy layer.

Responsibilities:
(a) DYNAMIC ROUTING — inspects the data batch and dispatches only the
    sub-agents whose declared inputs are present (mirrors a LangGraph-style
    conditional graph; the router trace is persisted so the demo can show
    WHY each agent did or didn't run).
(b) AGGREGATION — folds multi-agent findings per entity into one composite
    risk score (severity-weighted, capped at 100).
(c) CAUSAL NARRATIVE — chains the findings into one plain-language
    explanation, enriched with cross-case context the individual agents
    cannot see (same agency in other flagged cases, constituency-level
    rule breaches co-occurring with work-level anomalies).
(d) TIER SYNTHESIS — reframes the SAME flag for four authorities: MP,
    District, State Nodal, Ministry. Not field-filtering: each tier gets a
    different framing, action ask, and aggregation level.

If a local Ollama server is running (OLLAMA_HOST env var or localhost:11434)
narratives are optionally polished by a local open-source LLM; otherwise the
deterministic composer output ships as-is — the demo never depends on an LLM.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd

from .. import HUMAN_REVIEW_DISCLAIMER
from .. import data_confidence
from .. import data_contract as contract
from ..config import PROCESSED_DIR, load_rules
from ..explain import brief_to_text, build_brief, context_note, humanize, short_title
from ..locale import text
from ..schemas import Finding, Flag
from .anomaly import AnomalyAgent
from .base import BaseAgent
from .compliance import ComplianceAgent
from .entity_resolution import EntityResolutionAgent
from .network import NetworkAgent
from .payments import PaymentAgent
from .revisions import RevisionAgent

TIERS = ("mp", "district", "state", "ministry")


class Orchestrator:
    def __init__(self) -> None:
        self.agents: list[BaseAgent] = [
            ComplianceAgent(), AnomalyAgent(), EntityResolutionAgent(), NetworkAgent(),
            RevisionAgent(), PaymentAgent(),
        ]
        self.route_trace: list[dict] = []
        self.rule_coverage: list[dict] = []
        self.context: dict = {}
        self.confidence: dict[str, int] = {}

    # ---------------- (a) routing ----------------

    def route(self, works: pd.DataFrame, flows: pd.DataFrame) -> list[BaseAgent]:
        selected = []
        self.route_trace = []
        for agent in self.agents:
            ok = agent.applicable(works, flows)
            missing = sorted(
                (agent.needs_works - set(works.columns if not works.empty else [])) |
                {c for c in agent.needs_works
                 if not works.empty and c in works.columns and works[c].isna().all()}
            )
            self.route_trace.append({
                "agent": agent.name,
                "dispatched": ok,
                "reason": (text("en", "router.ready") if ok else
                           text("en", "router.skipped", missing=missing or sorted(agent.needs_flows))),
            })
            if ok:
                selected.append(agent)
        return selected

    # ---------------- main entry ----------------

    def run(self, works: pd.DataFrame, flows: pd.DataFrame,
            context: dict | None = None) -> list[Flag]:
        """Run every applicable module and fold the findings into cases.

        `context` carries what is not a works or fund-flow row: the observed
        edits (`versions`), works the portal stopped listing (`retired`) and
        each area's health (`area_health`) and every payment record
        (`payments`). `pipeline.run_pipeline` builds it; without it the
        revision and payment modules and data confidence stand down.
        """
        self.context = context or {}
        agents = self.route(works, flows)
        all_findings: list[Finding] = []
        for agent in agents:
            if hasattr(agent, "context"):
                agent.context = self.context
            found = agent.run(works, flows)
            for t in self.route_trace:
                if t["agent"] == agent.name:
                    t["findings"] = len(found)
                    if getattr(agent, "last_stats", None):
                        t["stats"] = agent.last_stats
            all_findings.extend(found)

        self.confidence = data_confidence.annotate(
            all_findings, works, self.context.get("area_health"))
        self.rule_coverage = self._rule_coverage(all_findings)
        flags = self._aggregate(all_findings, works)
        self._persist_trace(len(flags))
        return flags

    @staticmethod
    def _known_rules() -> dict[str, str]:
        """Every rule id/title declared in config, whether or not it fired."""
        cfg = load_rules()
        found: dict[str, str] = {}

        def walk(node):
            if isinstance(node, dict):
                if "id" in node and "title" in node:
                    found[str(node["id"])] = str(node["title"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        for section in ("rules", "anomaly", "duplicates", "network", "revisions", "payments"):
            walk(cfg.get(section))
        return found

    def _rule_coverage(self, findings: list[Finding]) -> list[dict]:
        """Per-rule match counts, INCLUDING rules that produced nothing.

        A rule that evaluates to zero is a real result and is reported as such:
        it means the corpus is clean on that check, or the rule stood down for
        want of the inputs it needs (e.g. R-SCST-01 requires SC/ST area
        attribution that the published extracts do not carry).
        """
        counts: dict[str, int] = defaultdict(int)
        for f in findings:
            counts[f.rule_id] += 1
        notes: dict[str, str] = {}
        for agent in self.agents:
            notes.update(getattr(agent, "notes", {}) or {})
        cfg = load_rules()
        basis: dict[str, str | None] = {}

        def walk(node):
            if isinstance(node, dict):
                if "id" in node:
                    basis[str(node["id"])] = node.get("basis")
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(cfg)
        coverage = []
        for rid, title in sorted(self._known_rules().items()):
            entry = {"rule_id": rid, "title": title, "findings": counts.get(rid, 0),
                     "evaluated": rid not in notes, "basis": basis.get(rid)}
            if rid in notes:
                entry["stood_down"] = notes[rid]
            coverage.append(entry)
        return coverage

    # ---------------- (b) aggregation ----------------

    def _aggregate(self, findings: list[Finding], works: pd.DataFrame) -> list[Flag]:
        cfg = load_rules()["risk_score"]
        weights, threshold = cfg["weights"], cfg["review_threshold"]

        by_entity: dict[tuple[str, str], list[Finding]] = defaultdict(list)
        for f in findings:
            by_entity[(f.entity_type, f.entity_id)].append(f)
        # A finding marked `standalone: False` (a common deviation the guideline
        # allows with recorded reasons) joins a case that exists for another
        # reason, and never opens one by itself; its district authority's
        # summary case carries it instead.
        by_entity = {key: fs for key, fs in by_entity.items()
                     if any(f.details.get("standalone", True) for f in fs)}

        # cross-case context maps (what no single agent can see)
        agency_flagged: dict[str, int] = defaultdict(int)
        state_flag_count: dict[str, int] = defaultdict(int)
        widx = works.set_index("work_id") if not works.empty else pd.DataFrame()
        for (etype, eid), fs in by_entity.items():
            meta = self._entity_meta(etype, eid, widx)
            if meta.get("ia_name"):
                agency_flagged[meta["ia_name"]] += 1
            if meta.get("state"):
                state_flag_count[meta["state"]] += 1

        flags: list[Flag] = []
        for (etype, eid), fs in sorted(by_entity.items()):
            fs.sort(key=lambda f: ["critical", "high", "medium", "low"].index(f.severity))
            # A portal record pair (one work's recommendation and sanctioned
            # record, both listed by the portal) stays visible but carries no
            # weight: it is how the portal records a work, not evidence of risk.
            # Context findings (`standalone: False`) are shown on the case but
            # carry no weight either: a deviation the guideline allows with
            # recorded reasons must not tip a case into an alert. Nor do held
            # findings (`held`): a text match nothing in the record separates,
            # or a batch of identical works, waits for evidence before it counts.
            score = min(100.0, sum(weights[f.severity] for f in fs
                                   if not f.details.get("portal_record_pair")
                                   and not f.details.get("held")
                                   and f.details.get("standalone", True)))
            meta = self._entity_meta(etype, eid, widx)
            if meta.get("actor_type"):
                # a vendor or implementing agency: its name is on the finding,
                # since a vendor's entity id is its portal id
                found = next((f.details for f in fs if f.details.get("actor")), {})
                meta["actor"] = found.get("actor") or eid.partition(":")[2]
                meta["vendor_id"] = found.get("vendor_id")
            context = {
                "agency_other_flags": max(0, agency_flagged.get(meta.get("ia_name") or "", 1) - 1),
                "state_total_flags": state_flag_count.get(meta.get("state") or "", 0),
            }
            narrative = self._narrative(etype, eid, fs, meta, context, score)
            flag_id = "F-" + hashlib.sha1(f"{etype}|{eid}".encode()).hexdigest()[:10].upper()

            # ---- presentation layer: structured, authority-specific briefs
            raw = {"findings": [f.model_dump() for f in fs]}
            note = context_note(raw["findings"],
                                context["agency_other_flags"] if meta.get("ia_name") else 0)
            briefs = {t: build_brief(raw, t, {"note": note}) for t in TIERS}
            tier_views = {t: brief_to_text(b) for t, b in briefs.items()}
            primary = briefs["district"]["primary_risk"]
            title = self._display_title(etype, eid, meta)

            flags.append(Flag(
                flag_id=flag_id,
                entity_type=etype,
                entity_id=eid,
                entity_label=self._label(etype, eid, meta),
                display_title=title,
                primary_signal=primary,
                tier_briefs=briefs,
                state=meta.get("state"),
                district=meta.get("district"),
                constituency=meta.get("constituency"),
                era=meta.get("era", "unknown"),
                risk_score=round(score, 1),
                alert=score >= threshold,
                findings=fs,
                narrative=narrative,
                tier_views=tier_views,
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
        flags.sort(key=lambda f: -f.risk_score)
        return flags

    def _entity_meta(self, etype: str, eid: str, widx: pd.DataFrame) -> dict:
        if etype == "work" and not widx.empty and eid in widx.index:
            row = widx.loc[eid]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            return {k: (None if not isinstance(v, (list, dict)) and pd.isna(v) else v)
                    for k, v in row.to_dict().items()}
        if etype == "work":
            # a work the portal no longer lists: describe it from its last record
            retired = self.context.get("retired") if self.context else None
            if isinstance(retired, pd.DataFrame) and not retired.empty:
                match = retired[retired["work_id"].astype(str) == str(eid)]
                if not match.empty:
                    try:
                        return json.loads(match.iloc[0].get("record_json") or "{}")
                    except ValueError:
                        return {}
        if etype == "constituency" and "|" in eid:
            name, state = eid.split("|", 1)
            return {"constituency": name, "state": state}
        if etype == "agency":
            kind, sep, _ = eid.partition(":")
            if sep and kind in ("vendor", "agency"):
                return {"actor_type": kind}
            return {"ia_name": eid}
        return {}

    @staticmethod
    def _display_title(etype: str, eid: str, meta: dict) -> str:
        """Short, readable case title for list views (full text kept on the flag)."""
        if etype == "work":
            return short_title(meta.get("description"), meta.get("category"))
        if etype == "constituency":
            return text("en", "title.constituency", name=meta.get("constituency", eid))
        if meta.get("actor_type"):
            return str(meta.get("actor") or eid.partition(":")[2]).title()[:62]
        actor = str(meta.get("ia_name", eid))
        if contract.is_district_authority(actor):
            return text("en", "title.district_authority", name=actor.split("(")[0].strip().title())
        return actor.title()[:62]

    @staticmethod
    def _label(etype: str, eid: str, meta: dict) -> str:
        if etype == "work":
            desc = (meta.get("description") or "")[:70]
            return f"Work {eid}" + (f" — {desc}" if desc else "")
        if etype == "constituency":
            return f"{meta.get('constituency', eid)} ({meta.get('state', '')})".strip()
        name = str(meta.get("actor") or eid.partition(":")[2]).title()
        if meta.get("actor_type") == "vendor":
            return f"Vendor: {name}" + (f" (portal vendor id {meta['vendor_id']})"
                                        if meta.get("vendor_id") else "")
        if meta.get("actor_type") == "agency":
            return f"Implementing agency: {name}"
        actor = str(meta.get("ia_name", eid))
        if contract.is_district_authority(actor):
            return f"District authority: {actor.split('(')[0].strip().title()}"
        return f"District authority: {actor.title()}"

    # ---------------- (c) causal narrative ----------------

    def _narrative(self, etype: str, eid: str, fs: list[Finding],
                   meta: dict, ctx: dict, score: float) -> str:
        parts = [f.summary for f in fs]
        chain = "Flagged because: " + "; furthermore, ".join(parts[:4]) + "."
        extras = []
        if ctx["agency_other_flags"] > 0 and meta.get("ia_name"):
            # `ia_name` is the district authority, not an implementing agency
            # (see data_contract), so this is a count for the whole district.
            extras.append(
                f"The same district authority ({str(meta['ia_name']).split('(')[0].strip().title()}) "
                f"has {ctx['agency_other_flags']} other flagged case(s) in this dataset."
            )
        if len(fs) > 1:
            agents_involved = sorted({f.agent for f in fs})
            extras.append(
                f"Independent signals from {len(agents_involved)} agent(s) "
                f"({', '.join(agents_involved)}) reinforce each other, raising the "
                f"composite risk score to {score:.0f}/100."
            )
        rules = ", ".join(sorted({f.rule_id for f in fs}))
        cite = f"Rules/thresholds triggered: {rules}."
        return " ".join([chain] + extras + [cite, HUMAN_REVIEW_DISCLAIMER])

    def _polish(self, text: str) -> str:
        """Optional local-LLM polish via Ollama; deterministic text otherwise."""
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        model = os.environ.get("ASTRA_OLLAMA_MODEL")
        if not model:
            return text
        try:
            import requests
            r = requests.post(f"{host}/api/generate", json={
                "model": model, "stream": False,
                "prompt": ("Rewrite this audit risk note in crisp plain English, keeping every "
                           "number and rule ID exactly, max 80 words:\n" + text),
            }, timeout=20)
            return r.json().get("response", text).strip() or text
        except Exception:
            return text

    # ---------------- (d) tier synthesis ----------------

    def _tier_views(self, etype: str, eid: str, fs: list[Finding],
                    meta: dict, ctx: dict, score: float) -> dict[str, str]:
        top = fs[0]
        where = meta.get("constituency") or meta.get("district") or meta.get("state") or "your jurisdiction"
        n_signals = len(fs)
        views = {
            "mp": (
                f"In {where}: one of your recommended works needs attention. {top.summary} "
                f"Suggested step: ask the implementing agency for a written justification "
                f"and revised estimate before the next installment is recommended. "
                f"This is a review prompt, not an accusation — your office closes it "
                f"by responding through the district authority."
            ),
            "district": (
                f"Execution check required ({n_signals} signal(s), risk {score:.0f}/100). {top.summary} "
                f"Action for DA: verify measurement book, utilization certificate and "
                f"physical progress photos for this case; record findings in eSAKSHI. "
                + (f"Note: the same implementing agency appears in "
                   f"{ctx['agency_other_flags']} other flagged case(s) — consider a joint "
                   f"inspection." if ctx["agency_other_flags"] else "")
            ),
            "state": (
                f"Cross-district pattern input: this case contributes to "
                f"{ctx['state_total_flags']} open flags in {meta.get('state') or 'this state'}. "
                f"{top.summary} SNA lens: check whether the same work category or agency "
                f"recurs across districts before the next tranche recommendation cycle."
            ),
            "ministry": (
                f"National monitoring signal: {top.rule_id} ({top.rule_title}) triggered; "
                f"severity {top.severity}, composite risk {score:.0f}/100, era "
                f"{meta.get('era', 'unknown')}. Feeds the state-wise exception rate for "
                f"{meta.get('state') or 'N/A'}; policy question is whether this rule's "
                f"threshold needs recalibration given observed false-positive feedback."
            ),
        }
        if etype == "constituency":
            views["mp"] = (
                f"Fund-utilization advisory for {where}: {top.summary} "
                f"Suggested step: prioritize recommendations against unspent balance; "
                f"your nodal district can share the pending-works list. Review prompt only."
            )
        if etype == "agency":
            views["mp"] = (
                f"An implementing agency active on works you recommended shows an unusual "
                f"pattern: {top.summary} No action needed from your office yet; the "
                f"district authority has been prompted to verify."
            )
        return {k: self._polish(v) for k, v in views.items()}

    # ---------------- trace persistence ----------------

    def _persist_trace(self, n_flags: int) -> None:
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        with open(PROCESSED_DIR / "run_meta.json", "w", encoding="utf-8") as fh:
            json.dump({
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "router_trace": self.route_trace,
                "rule_coverage": getattr(self, "rule_coverage", []),
                "flags_produced": n_flags,
                "data_confidence": self.confidence,
                "unchecked_provisions": contract.UNCHECKABLE,
            }, fh, indent=2)
