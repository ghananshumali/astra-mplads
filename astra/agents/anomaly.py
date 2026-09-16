"""Statistical Anomaly Agent — cost-overrun & expenditure-pattern outliers.

Benchmarking design:
- Official state Schedule-of-Rates (SoR) tables are not published as open
  data, so the benchmark is an EMPIRICAL SoR PROXY: the median cost of peer
  works within the same (state x category x era) group. A loader hook exists
  to swap in official SoR CSVs when MoSPI provides them (drop files into
  data/sor/, same peer-key format).
- Robust z-score = (x - median) / (1.4826 * MAD): resistant to the very
  outliers we hunt. IsolationForest cross-checks multivariate structure.
- Peer groups NEVER straddle the eSAKSHI era boundary — the 2023 regime
  change would otherwise register as a fake anomaly wave.

Peer profile (A-PEER-01)
------------------------
Cost alone is A-COST-01's question. A-PEER-01 asks whether a work is extreme on
SEVERAL measures at once compared with works of the same type in the same
state: time from recommendation to sanction, time from sanction to completion,
the share of the sanctioned amount paid on a completed work, and the number of
payments. Each measure gets a robust z-score in its peer group; a work is
reported only when two or more are extreme in the risky direction, and the
finding names them with the typical value. Isolation Forest is run on the same
measures only to corroborate (`iforest_agrees`): it never raises a finding by
itself, because it cannot say why a work looks unusual.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import load_rules
from ..schemas import Finding
from .base import BaseAgent

_SOR_DIR_NOTE = "empirical peer median (SoR proxy; official SoR loadable via data/sor/)"


def _robust_z(x: pd.Series) -> pd.Series:
    med = x.median()
    mad = (x - med).abs().median()
    if mad == 0 or np.isnan(mad):
        std = x.std(ddof=0)
        if not std or np.isnan(std):
            return pd.Series(0.0, index=x.index)
        return (x - x.mean()) / std
    return (x - med) / (1.4826 * mad)


#: A-PEER-01 measures: (label, risky direction, log-scale, plain unit)
_PEER_MEASURES = {
    "days_to_sanction": ("time from recommendation to sanction", "high", True, "days"),
    "days_to_complete": ("time from sanction to completion", "high", True, "days"),
    "paid_share": ("share of the sanctioned amount paid on a completed work", "low", False, "share"),
    "payment_count": ("number of payments", "high", True, "payments"),
}


def peer_measures(works: pd.DataFrame) -> pd.DataFrame:
    """The A-PEER-01 measures for each work; NaN where a measure does not apply."""
    def day(col):
        return pd.to_datetime(works[col], errors="coerce") if col in works else pd.Series(pd.NaT, index=works.index)

    recommended, sanctioned, completed = day("recommended_date"), day("sanction_date"), day("completion_date")
    out = pd.DataFrame(index=works.index)
    out["days_to_sanction"] = (sanctioned - recommended).dt.days.where(lambda d: d >= 0)
    done = works["is_completed"].astype(bool) if "is_completed" in works else completed.notna()
    out["days_to_complete"] = (completed - sanctioned).dt.days.where(lambda d: d >= 0).where(done)
    amount = pd.to_numeric(works.get("sanctioned_amount"), errors="coerce")
    paid = pd.to_numeric(works.get("total_paid"), errors="coerce")
    out["paid_share"] = (paid / amount.where(amount > 0)).where(done & paid.notna())
    count = pd.to_numeric(works.get("payment_count"), errors="coerce")
    out["payment_count"] = count.where(count > 0)
    return out


class AnomalyAgent(BaseAgent):
    name = "anomaly"
    needs_works = {"work_id"}
    needs_flows = set()

    def run(self, works: pd.DataFrame, flows: pd.DataFrame) -> list[Finding]:
        cfg = load_rules()["anomaly"]
        out: list[Finding] = []
        if not works.empty:
            out += self._cost_outliers(works, cfg["cost_overrun"])
            if "peer_profile" in cfg:
                out += self._peer_profile(works, cfg["peer_profile"])
        if not flows.empty:
            out += self._expenditure_outliers(flows, cfg["expenditure_pattern"])
        return out

    def _cost_outliers(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        df = works.copy()
        # coalesce per row: sanctioned amount is authoritative, estimated cost
        # is the fallback for works that have not reached sanction yet
        df["cost"] = np.nan
        for col in ("sanctioned_amount", "estimated_cost"):
            if col in df.columns:
                df["cost"] = df["cost"].fillna(pd.to_numeric(df[col], errors="coerce"))
        if df["cost"].isna().all():
            return []
        df = df[df["cost"] > 0].dropna(subset=["cost"])
        if df.empty:
            return []

        for col, default in (("state", "NA"), ("category", "uncat"), ("era", "unknown")):
            if col not in df.columns:
                df[col] = default
            df[col] = df[col].fillna(default)
        out: list[Finding] = []

        for (state, cat, era), grp in df.groupby(["state", "category", "era"]):
            if len(grp) < rule["min_peer_group"]:
                continue
            logs = np.log1p(grp["cost"])
            median_cost = float(grp["cost"].median())

            # Degenerate peer group: costs concentrated on a single value, so
            # median absolute deviation is 0 and a z-score carries no meaning.
            # Fall back to a high-percentile cut on the raw cost.
            mad = float((logs - logs.median()).abs().median())
            degenerate = mad == 0
            if degenerate:
                cut = float(np.percentile(grp["cost"], rule["degenerate_percentile"]))
                candidates = grp.index[grp["cost"] > cut]
                z = pd.Series(np.nan, index=grp.index)
                method = f"percentile>{rule['degenerate_percentile']} (degenerate peer group)"
            else:
                z = _robust_z(logs)
                candidates = grp.index[z > rule["z_threshold"]]
                method = "robust z-score (median/MAD)"

            iso_flag = pd.Series(False, index=grp.index)
            try:
                from sklearn.ensemble import IsolationForest
                feats = np.log1p(grp[["cost"]].to_numpy())
                iso = IsolationForest(
                    contamination=rule["iforest_contamination"], random_state=42
                ).fit(feats)
                iso_flag = pd.Series(iso.predict(feats) == -1, index=grp.index)
            except Exception:
                pass  # z-score alone still works

            for idx in candidates:
                c = float(grp.loc[idx, "cost"])
                pct_over = 100 * (c - median_cost) / median_cost if median_cost else 0
                # materiality gate: extreme AND meaningfully above the benchmark
                if (pct_over < rule["min_pct_above_benchmark"]
                        or (c - median_cost) < rule["min_abs_excess"]):
                    continue
                zval = float(z.loc[idx]) if not degenerate else float("nan")
                ratio = c / median_cost if median_cost else float("inf")
                # Beyond scale_mismatch_ratio the peer group is mixing
                # unit-priced and bulk works: report it as a classification
                # question, not as a cost overrun allegation.
                scale_mismatch = ratio >= rule["scale_mismatch_ratio"]
                if scale_mismatch:
                    magnitude = (f"{ratio:,.0f}x the {state} / "
                                 f"'{str(cat)[:40]}' peer benchmark of "
                                 f"Rs {median_cost:,.0f}")
                    summary = (
                        f"Cost Rs {c:,.0f} is {magnitude} (n={len(grp)} peers, "
                        f"era={era}). A gap this large usually means the peer "
                        f"group mixes unit-priced and bulk works rather than a "
                        f"true overrun — recommend verifying the work's "
                        f"classification and scope before any cost inference."
                    )
                else:
                    summary = (
                        f"Cost Rs {c:,.0f} is {pct_over:+.0f}% vs the {state} / "
                        f"'{str(cat)[:40]}' peer benchmark of Rs {median_cost:,.0f} "
                        + (f"(robust z={zval:.1f}, " if not degenerate
                           else "(top-percentile in a single-value peer group, ")
                        + f"n={len(grp)} peers, era={era})"
                    )
                out.append(Finding(
                    agent="anomaly",
                    rule_id=rule["id"],
                    rule_title=rule["title"],
                    clause=None,
                    severity="medium" if scale_mismatch else rule["severity"],
                    entity_type="work",
                    entity_id=str(grp.loc[idx, "work_id"]),
                    summary=summary,
                    details={
                        "basis": rule.get("basis"),
                        "cost": c, "benchmark": median_cost,
                        "pct_vs_benchmark": round(pct_over, 1),
                        "z": None if degenerate else round(zval, 2),
                        "method": method,
                        "ratio_vs_benchmark": round(ratio, 2),
                        "scale_mismatch": bool(scale_mismatch),
                        "peer_group": f"{state}|{cat}|{era}", "peers": len(grp),
                        "iforest_agrees": bool(iso_flag.loc[idx]),
                        "benchmark_source": _SOR_DIR_NOTE,
                    },
                ))
        return out

    def _peer_profile(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        measures = peer_measures(works)
        if measures.notna().sum().sum() == 0:
            return []
        keys = works[[c for c in ("state", "category", "era") if c in works.columns]].fillna("NA")
        frame = pd.concat([works[["work_id"]], keys, measures], axis=1)
        group_cols = list(keys.columns)
        out: list[Finding] = []
        for peer, grp in frame.groupby(group_cols):
            if len(grp) < rule["min_peer_group"]:
                continue
            extreme: dict[str, pd.Series] = {}
            zs: dict[str, pd.Series] = {}
            for col, (_label, direction, log, _unit) in _PEER_MEASURES.items():
                values = grp[col].dropna()
                if len(values) < rule["min_peer_group"]:
                    continue
                scaled = np.log1p(values) if log else values
                mad = float((scaled - scaled.median()).abs().median())
                if mad == 0:
                    continue          # most peers share one value: no spread to measure
                z = (scaled - scaled.median()) / (1.4826 * mad)
                zs[col] = z
                hit = z > rule["z_threshold"] if direction == "high" else z < -rule["z_threshold"]
                extreme[col] = hit.reindex(grp.index, fill_value=False)
            if len(extreme) < rule["min_features"]:
                continue
            hits = pd.DataFrame(extreme)
            candidates = hits.index[hits.sum(axis=1) >= rule["min_features"]]
            if len(candidates) == 0:
                continue

            iso_agrees = pd.Series(False, index=grp.index)
            try:
                from sklearn.ensemble import IsolationForest
                cols = list(extreme)
                matrix = grp[cols].apply(lambda c: c.fillna(c.median())).to_numpy(dtype=float)
                iso = IsolationForest(n_estimators=50, max_samples=min(256, len(grp)),
                                      contamination=rule["iforest_contamination"],
                                      random_state=42).fit(matrix)
                iso_agrees = pd.Series(iso.predict(matrix) == -1, index=grp.index)
            except Exception:
                pass

            peer_label = " / ".join(str(p) for p in (peer if isinstance(peer, tuple) else (peer,))[:2])
            for idx in candidates:
                named = []
                for col in hits.columns[hits.loc[idx]]:
                    label, _direction, _log, unit = _PEER_MEASURES[col]
                    value = float(grp.loc[idx, col])
                    typical = float(grp[col].median())
                    named.append({"measure": col, "label": label, "value": round(value, 3),
                                  "peer_median": round(typical, 3),
                                  "z": round(float(zs[col].loc[idx]), 2), "unit": unit})
                parts = []
                for m in named:
                    if m["unit"] == "share":
                        parts.append(f"{m['label']} {m['value']:.0%} (typical {m['peer_median']:.0%})")
                    else:
                        parts.append(f"{m['label']} {m['value']:,.0f} {m['unit']} "
                                     f"(typical {m['peer_median']:,.0f})")
                out.append(Finding(
                    agent="anomaly", rule_id=rule["id"], rule_title=rule["title"],
                    clause=rule.get("clause"), severity=rule["severity"],
                    entity_type="work", entity_id=str(grp.loc[idx, "work_id"]),
                    summary=(f"Unusual on {len(named)} measures compared with {len(grp):,} works "
                             f"of the same type in {peer_label}: " + "; ".join(parts) + "."),
                    details={"basis": rule.get("basis"), "measures": named,
                             "peer_group": "|".join(str(p) for p in (peer if isinstance(peer, tuple) else (peer,))),
                             "peers": int(len(grp)),
                             "iforest_agrees": bool(iso_agrees.loc[idx]),
                             "method": "robust z-score per measure within the peer group; "
                                       "Isolation Forest as corroboration only"},
                ))
        return out

    def _expenditure_outliers(self, flows: pd.DataFrame, rule: dict) -> list[Finding]:
        df = flows.copy()
        df["exp"] = pd.to_numeric(df.get("expenditure"), errors="coerce")
        df = df.dropna(subset=["exp", "fy"])
        # Some historical rows derive expenditure as (release - unspent), which
        # can go non-positive; those cannot enter a log-scale comparison.
        df = df[df["exp"] > 0]
        if df.empty or "state" not in df.columns:
            return []
        df["era"] = df.get("era", "unknown")
        out = []
        for (fy, era), grp in df.groupby(["fy", "era"]):
            if len(grp) < 8:
                continue
            z = _robust_z(np.log1p(grp["exp"]))
            for idx in grp.index[z.abs() > rule["z_threshold"]]:
                r = grp.loc[idx]
                key = f"{r.get('constituency') or r.get('mp_name') or 'NA'}|{r.get('state') or 'NA'}"
                direction = "above" if z.loc[idx] > 0 else "below"
                out.append(Finding(
                    agent="anomaly",
                    rule_id=rule["id"],
                    rule_title=rule["title"],
                    severity=rule["severity"],
                    entity_type="constituency",
                    entity_id=key,
                    summary=(
                        f"FY {fy} expenditure Rs {float(r['exp']):,.0f} is an extreme outlier "
                        f"{direction} the national distribution for that year "
                        f"(robust z={float(z.loc[idx]):.1f}, era={era})"
                    ),
                    details={"fy": fy, "expenditure": float(r["exp"]),
                             "z": round(float(z.loc[idx]), 2), "era": era},
                ))
        return out
