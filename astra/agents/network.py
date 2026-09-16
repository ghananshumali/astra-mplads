"""Network Agent — vendor, implementing-agency and district-authority networks.

Three kinds of actor are analysed over the same bipartite structure
(actor -> districts / works / cost outcomes):

  VENDOR              the vendor paid the most on a work. eSAKSHI payment
                      records carry the vendor's name and portal id, and the
                      actor is the id: a name is not a vendor. On 16 Sep 2026,
                      1,424 names belonged to more than one vendor id, and
                      grouping by name had merged 13 vendors called "Ajay
                      Kumar", 12 of them working in one state each, into one
                      vendor "across 7 states". Without ids (the CSV path) the
                      name is the only key there is.
  AGENCY              the implementing agency that executes the work, chosen by
                      the district authority (para 3.2.10). Payment records
                      carry it, so only paid works have one. Names are grouped
                      exactly, as typed on the portal.
  DISTRICT AUTHORITY  the Implementing District Authority (`ia_name`). Its case
                      is the authority's own case, the one the compliance
                      module's district-level rules open, so the entity id is
                      the authority's name without a prefix.

Signals
-------
  * geographic spread     - one actor across many districts/states, which is
                            unusual for local MPLADS works. Not a signal for an
                            implementing agency: state bodies (rural
                            engineering departments, state construction
                            corporations) execute works across their state by
                            design, and a district authority covers one district.
  * overrun concentration - a disproportionate share of that actor's works
                            priced as outliers against peer benchmarks.

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

#: (actor type, grouping column, name column), in the order they are analysed
ACTORS = (
    ("vendor", "vendor_id", "vendor_name"),
    ("agency", "implementing_agency", "implementing_agency"),
    ("district_authority", "ia_name", "ia_name"),
)

_LABELS = {"vendor": "Vendor", "agency": "Implementing agency",
           "district_authority": "District authority"}


def entity_id(actor_type: str, key: str) -> str:
    """The case an actor's findings belong to."""
    return key if actor_type == "district_authority" else f"{actor_type}:{key}"


def _present(df: pd.DataFrame, col: str) -> bool:
    return col in df.columns and df[col].notna().any()


class NetworkAgent(BaseAgent):
    name = "network"
    needs_works = {"work_id"}
    needs_flows = set()

    def applicable(self, works: pd.DataFrame, flows: pd.DataFrame) -> bool:
        """Needs at least one actor column to build a network from."""
        if works.empty:
            return False
        return any(_present(works, c) for c in
                   ("vendor_id", "vendor_name", "implementing_agency", "ia_name"))

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
        # materiality floor as the anomaly agent so an actor's "overrun share"
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
        for actor_type, key_col, name_col in ACTORS:
            if actor_type == "vendor" and not _present(df, key_col):
                key_col = name_col               # no vendor ids: the name is all there is
            if _present(df, key_col):
                out += self._actor_signals(df, actor_type, key_col, name_col, cfg)
        return out

    def _actor_signals(self, df: pd.DataFrame, actor_type: str, key_col: str,
                       name_col: str, cfg: dict) -> list[Finding]:
        work = df.copy()
        by_id = key_col != name_col
        if by_id:
            work["_key"] = work[key_col].map(lambda v: None if pd.isna(v) else str(v).strip())
            work = work[work["_key"].fillna("").str.len() > 0]
        else:
            key = work[key_col].fillna("").astype(str).str.strip()
            # A district authority keeps its name as written: its case is keyed on it.
            work["_key"] = key if actor_type == "district_authority" else key.str.upper()
            work = work[work["_key"].str.len() > 3]
        if work.empty:
            return []
        names = (work[name_col].fillna("").astype(str).str.strip().str.upper()
                 if name_col in work.columns else work["_key"])
        work["_name"] = names
        ids_per_name = (work.groupby("_name")["_key"].nunique() if by_id
                        else pd.Series(dtype=int))

        spread_actors = cfg.get("spread_actors", ["vendor", "district_authority"])
        out: list[Finding] = []
        for key, grp in work.groupby("_key"):
            n = len(grp)
            if n < cfg["min_works"]:
                continue
            districts = grp["district"].dropna().nunique() if "district" in grp.columns else 0
            states = grp["state"].dropna().nunique()
            overrun_n = int(grp["overrun"].sum())
            overrun_share = float(grp["overrun"].mean())
            total_value = float(pd.to_numeric(grp["cost"], errors="coerce").sum(skipna=True) or 0)
            actor = grp["_name"].mode().iloc[0] if grp["_name"].str.len().gt(0).any() else str(key)

            # is this actor plausibly a national supplier rather than a local contractor?
            types = " ".join(grp["category"].astype(str).str.lower().unique()[:20])
            national_supply = any(h in types for h in _NATIONAL_SUPPLY_HINTS)

            spread = (actor_type in spread_actors
                      and districts > cfg["district_spread_threshold"])
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

            same_name = int(ids_per_name.get(actor, 1)) - 1 if by_id else 0
            who = f"{_LABELS[actor_type]} '{actor.title()}'"
            if by_id:
                who += f" (portal vendor id {key})"
            details = {
                "actor_type": actor_type, "actor": actor, "works": n,
                "districts": int(districts), "states": int(states),
                "overrun_works": overrun_n,
                "overrun_share": round(overrun_share, 2),
                "total_value": total_value,
                "likely_national_supplier": bool(national_supply),
                "state_list": sorted(grp["state"].dropna().unique().tolist())[:6],
                "district_list": (sorted(grp["district"].dropna().unique().tolist())[:8]
                                  if "district" in grp.columns else []),
                "sample_work_ids": grp["work_id"].astype(str).tolist()[:6],
            }
            if actor_type == "vendor":
                details["grouped_by"] = "vendor_id" if by_id else "vendor_name"
                if by_id:
                    details["vendor_id"] = str(key)
                    details["same_name_vendor_ids"] = same_name
            out.append(Finding(
                agent="network",
                rule_id=cfg["id"],
                rule_title=cfg["title"],
                severity=severity,
                entity_type="agency",
                entity_id=entity_id(actor_type, str(key)),
                summary=(f"{who} " + "; and ".join(reasons)
                         + (f". {same_name} other vendor id(s) share this name and are "
                            f"counted separately" if same_name else "")
                         + ". Network signal for verification only — it does not "
                           "imply wrongdoing by the firm or the authority."),
                details=details,
            ))
        return out
