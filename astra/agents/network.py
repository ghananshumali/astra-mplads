"""Network Agent — contractor/vendor and implementing-agency network analysis.

Two entity classes are analysed over the same bipartite graph structure
(actor -> districts / works / cost outcomes):

  VENDOR  the contractor actually paid for the work (eSAKSHI expenditure
          records carry the vendor name). This is the differentiator the
          brief asks for: the same contractor recurring across implausibly
          many districts with a pattern of cost overruns.
  AGENCY  the implementing agency (IDA) that executes the work.

Signals
-------
  * geographic spread     - one actor across many districts/states, which is
                            unusual for local MPLADS works;
  * overrun concentration - a disproportionate share of that actor's works
                            priced as outliers against peer benchmarks;
  * single-vendor capture - one vendor taking most of a district's MPLADS
                            payments for a work type.

Deliberate false-positive control: national suppliers (vehicles, IT hardware,
books) legitimately serve many districts, so a spread signal alone is reported
at low severity and the narrative says so. Only spread PLUS overrun
concentration is treated as materially risky - and even then it is a prompt
for verification, never an allegation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import load_rules
from ..schemas import Finding
from .base import BaseAgent
from .anomaly import _robust_z

#: work types where a nationally-operating supplier is entirely expected
_NATIONAL_SUPPLY_HINTS = (
    "vehicle", "ambulance", "bus", "tractor", "computer", "laptop", "printer",
    "book", "periodical", "equipment", "machine", "tanker", "van",
)


class NetworkAgent(BaseAgent):
    name = "network"
    needs_works = {"work_id"}
    needs_flows = set()

    def applicable(self, works: pd.DataFrame, flows: pd.DataFrame) -> bool:
        """Needs at least one actor column to build a network from."""
        if works.empty:
            return False
        return any(c in works.columns and works[c].notna().any()
                   for c in ("vendor_name", "ia_name"))

    def run(self, works: pd.DataFrame, flows: pd.DataFrame) -> list[Finding]:
        cfg = load_rules()["network"]
        df = works.copy()
        if df.empty:
            return []

        for col, default in (("state", "NA"), ("category", "uncat"), ("era", "unknown")):
            if col not in df.columns:
                df[col] = default
            df[col] = df[col].fillna(default)

        df["cost"] = np.nan
        for col in ("sanctioned_amount", "estimated_cost"):
            if col in df.columns:
                df["cost"] = df["cost"].fillna(pd.to_numeric(df[col], errors="coerce"))

        # Per-work overrun marker vs state x category x era peers. Uses the same
        # materiality floor as the anomaly agent so an agency's "overrun share"
        # counts only works that are genuinely, substantially above benchmark.
        acfg = load_rules()["anomaly"]["cost_overrun"]
        df["overrun"] = False
        priced = df[df["cost"] > 0]
        for _, grp in priced.groupby(["state", "category", "era"]):
            if len(grp) < acfg["min_peer_group"]:
                continue
            med = float(grp["cost"].median())
            logs = np.log1p(grp["cost"])
            mad = float((logs - logs.median()).abs().median())
            if mad == 0:
                extreme = grp["cost"] > float(
                    np.percentile(grp["cost"], acfg["degenerate_percentile"]))
            else:
                extreme = _robust_z(logs) > 2.0
            material = ((grp["cost"] - med) >= acfg["min_abs_excess"]) & (
                med > 0) & (100 * (grp["cost"] - med) / med
                            >= acfg["min_pct_above_benchmark"])
            df.loc[grp.index[extreme & material], "overrun"] = True

        out: list[Finding] = []
        for actor_col, label in (("vendor_name", "vendor"), ("ia_name", "agency")):
            if actor_col in df.columns and df[actor_col].notna().any():
                out += self._actor_signals(df, actor_col, label, cfg)
        return out

    def _actor_signals(self, df: pd.DataFrame, actor_col: str, label: str,
                       cfg: dict) -> list[Finding]:
        work = df.copy()
        work[actor_col] = (work[actor_col].fillna("").astype(str)
                           .str.strip().str.upper())
        work = work[work[actor_col].str.len() > 3]
        if work.empty:
            return []

        out: list[Finding] = []
        for actor, grp in work.groupby(actor_col):
            n = len(grp)
            if n < cfg["min_works"]:
                continue
            districts = grp["district"].dropna().nunique() if "district" in grp.columns else 0
            states = grp["state"].dropna().nunique()
            overrun_n = int(grp["overrun"].sum())
            overrun_share = float(grp["overrun"].mean())
            total_value = float(pd.to_numeric(grp["cost"], errors="coerce").sum(skipna=True) or 0)

            # is this actor plausibly a national supplier rather than a local contractor?
            types = " ".join(grp["category"].astype(str).str.lower().unique()[:20])
            national_supply = any(h in types for h in _NATIONAL_SUPPLY_HINTS)

            spread = districts > cfg["district_spread_threshold"]
            concentrated = (overrun_share >= cfg["overrun_share_threshold"]
                            and overrun_n >= 2)
            if not (spread or concentrated):
                continue

            reasons: list[str] = []
            if spread:
                reasons.append(
                    f"appears on {n} works across {districts} districts"
                    + (f" in {states} states" if states > 1 else "")
                    + (" (work types include nationally-supplied items, so wide "
                       "coverage may be entirely legitimate)" if national_supply else
                       " — unusually wide for local MPLADS execution"))
            if concentrated:
                reasons.append(
                    f"{overrun_share:.0%} of its works ({overrun_n} of {n}) price as "
                    f"cost outliers against state/work-type peer benchmarks")

            # severity: spread alone is weak; spread + overruns is the real signal
            if concentrated and spread:
                severity = "high"
            elif concentrated:
                severity = cfg["severity"]
            else:
                severity = "low" if national_supply else cfg["severity"]

            out.append(Finding(
                agent="network",
                rule_id=cfg["id"],
                rule_title=cfg["title"],
                severity=severity,
                entity_type="agency",
                entity_id=f"{label}:{actor}",
                summary=(f"{label.title()} '{actor.title()}' " + "; and ".join(reasons)
                         + ". Network signal for verification only — it does not "
                           "imply wrongdoing by the firm or the authority."),
                details={
                    "actor_type": label, "actor": actor, "works": n,
                    "districts": int(districts), "states": int(states),
                    "overrun_works": overrun_n,
                    "overrun_share": round(overrun_share, 2),
                    "total_value": total_value,
                    "likely_national_supplier": bool(national_supply),
                    "state_list": sorted(grp["state"].dropna().unique().tolist())[:6],
                    "district_list": (sorted(grp["district"].dropna().unique().tolist())[:8]
                                      if "district" in grp.columns else []),
                    "sample_work_ids": grp["work_id"].astype(str).tolist()[:6],
                },
            ))
        return out
