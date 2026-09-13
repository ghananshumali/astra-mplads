"""Entity-Resolution Agent — duplicate / near-duplicate work detection.

Why this is NOT redundant with the multi-authority approval workflow:
approval verifies ONE work's papers; nobody cross-checks that work against
the whole state/national corpus of other sanctioned works. Duplication in
practice comes from (a) constituency-boundary overlaps between two MPs,
(b) re-entry across the pre-2023 physical -> post-2023 eSAKSHI migration,
(c) cross-scheme re-badging of an existing asset, and (d) plain double-entry
of the same recommendation.

Method
------
MPLADS work descriptions are heavily templated ("Construction of CC road from
X to Y in Z village"), so naive all-pairs-over-threshold matching drowns in
false positives. Instead:

  1. block on (state, district) — duplicates are local by construction;
  2. TF-IDF char_wb 3-5 grams, kept SPARSE;
  3. top-k nearest neighbours per work (cosine) — bounded O(n*k) candidates
     instead of O(n^2), so the whole national corpus is tractable;
  4. every candidate must clear BOTH a semantic and a rapidfuzz token-set
     threshold — the token check kills template-only matches that share
     boilerplate but name different places;
  5. geospatial gate (haversine) when eSAKSHI asset coordinates exist;
  6. cross-era pairs get a small bonus (the migration double-entry prior).

Findings are capped per work so one templated cluster cannot dominate the
review queue.

Records the portal keeps for one work at two stages
---------------------------------------------------
When a work is sanctioned, the eSAKSHI portal lists the sanctioned record under
a new id and work code, and keeps the original recommendation — still at stage
"NA" — in its recommended report. Both are genuine portal records, and ASTRA
stores both so its figures equal the portal's. As text they are identical, so
they clear every gate above; measured on 13 Sep 2026 there were 595 such pairs,
591 with the same recommendation date and 568 with the same amount.

Calling those "identical recommendation double-entry" would overstate the
evidence. A pair of a pending recommendation at stage "NA" and a sanctioned
record the portal lists only from the sanctioned report onward, from the same
member on the same recommendation date, is therefore reported as a portal
record pair: kept visible, at low severity, and excluded from the risk score
(see `portal_record_pair` and `Orchestrator._aggregate`). Two separate
recommendations of one work, one sanctioned and one still pending, remain an
ordinary duplicate finding, as does every other near-duplicate.
"""
from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np
import pandas as pd

from ..config import load_rules
from ..schemas import Finding
from .base import BaseAgent


def _tokens(text: str) -> set[str]:
    """Word tokens of >=3 letters/digits, lowercased."""
    return set(re.findall(r"[a-z0-9]{3,}", str(text).lower()))


def _locators(text: str) -> set[str]:
    """Numeric locators: chainage (KM 0/400), ward, survey and premises numbers.

    In MPLADS descriptions numbers almost always identify WHERE the work is
    ("KM 0 400", "ward no 15", "Pry No 817/3"). Two otherwise-identical
    descriptions carrying DIFFERENT locators are evidence of two distinct
    works on the same road or in the same ward - not of duplication.
    """
    return set(re.findall(r"\d+", str(text).lower()))


#: How a portal record pair is described wherever a duplication mode is shown.
PORTAL_RECORD_PAIR = ("the portal lists both the original recommendation and a "
                      "sanctioned record")


def _same_text(a, b) -> bool:
    """Equal after case and spacing are normalised; a missing value never matches."""
    def norm(v):
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return ""
        return " ".join(str(v).upper().split())
    return bool(norm(a)) and norm(a) == norm(b)


def portal_record_pair(ra, rb) -> tuple | None:
    """(pending, sanctioned) if two matched records look like one work at two
    stages on the portal, else None.

    All of these must hold, because together they are the pattern measured on
    the portal and nothing weaker is:

    * one record is a pending recommendation (an `ES-` id, which the portal
      gives only before sanction) at stage "NA";
    * the other is a sanctioned record (a `WS/` work code) that the portal
      does NOT list in its recommended report (`in_recommended` == 0, joined
      from `work_listing` by the pipeline) — so the pending row is the only
      recommendation it has;
    * both name the same member and the same recommendation date.

    A pending record at "Pending for Sanction" beside a sanctioned record that
    is itself in the recommended report is two separate recommendations — 180
    such pairs on 13 Sep 2026, usually with consecutive portal ids — and stays
    an ordinary duplicate finding. Without listing data, nothing is relabelled.
    Amounts are not required to agree: a sanction can differ from the estimate.
    """
    a_id, b_id = str(ra["work_id"]), str(rb["work_id"])
    if a_id.startswith("ES-") and b_id.startswith("WS/"):
        pending, sanctioned = ra, rb
    elif b_id.startswith("ES-") and a_id.startswith("WS/"):
        pending, sanctioned = rb, ra
    else:
        return None
    if not _same_text(pending.get("status"), "NA"):
        return None
    listed = sanctioned.get("in_recommended")
    if listed is None or pd.isna(listed) or int(listed) != 0:
        return None
    if not _same_text(pending.get("mp_name"), sanctioned.get("mp_name")):
        return None
    if not _same_text(pending.get("recommended_date"), sanctioned.get("recommended_date")):
        return None
    return pending, sanctioned


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class EntityResolutionAgent(BaseAgent):
    name = "entity_resolution"
    needs_works = {"work_id", "description"}
    needs_flows = set()

    def run(self, works: pd.DataFrame, flows: pd.DataFrame) -> list[Finding]:
        cfg = load_rules()["duplicates"]
        df = works.copy()
        if "description" not in df.columns:
            return []
        df["description"] = df["description"].fillna("").astype(str).str.strip()
        df = df[df["description"].str.len() >= 15]      # too-short texts create noise
        if len(df) < 2:
            return []
        for col in ("state", "district", "era"):
            if col not in df.columns:
                df[col] = "NA"
            df[col] = df[col].fillna("NA")

        from rapidfuzz import fuzz
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.neighbors import NearestNeighbors

        # Corpus-wide document frequency -> boilerplate vocabulary. Tokens that
        # occur in more than boilerplate_df_pct of all works ("construction",
        # "road", "village") carry no identifying evidence.
        dfreq: Counter = Counter()
        all_tokens = [_tokens(t) for t in df["description"]]
        for t in all_tokens:
            dfreq.update(t)
        n_docs = max(len(all_tokens), 1)
        boilerplate = {w for w, c in dfreq.items()
                       if 100.0 * c / n_docs > cfg["boilerplate_df_pct"]}

        out: list[Finding] = []
        seen_pairs: set[tuple[str, str]] = set()
        per_work: dict[str, int] = {}
        block_keys = [k for k in cfg["same_scope_keys"] if k in df.columns]
        groups = df.groupby(block_keys) if block_keys else [((), df)]

        for _, grp in groups:
            n = len(grp)
            if n < 2:
                continue
            texts = grp["description"].str.lower().tolist()
            tok_specific = [_tokens(t) - boilerplate for t in texts]
            locators = [_locators(t) for t in texts]
            try:
                vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=1)
                mat = vec.fit_transform(texts)
            except ValueError:
                continue

            k = min(int(cfg["top_k_neighbours"]) + 1, n)          # +1: first neighbour is the work itself
            nn = NearestNeighbors(n_neighbors=k, metric="cosine").fit(mat)
            dist, idx = nn.kneighbors(mat)
            sim_all = 1.0 - dist
            idxs = grp.index.to_list()

            for i in range(n):
                for j_pos in range(1, k):
                    j = int(idx[i, j_pos])
                    if j == i:
                        continue
                    sem = float(sim_all[i, j_pos])
                    if sem < cfg["semantic_threshold"] - cfg["cross_era_bonus"]:
                        continue          # cheap reject before the fuzzy check

                    ia, ib = idxs[i], idxs[j]
                    ra, rb = grp.loc[ia], grp.loc[ib]
                    cross_era = (ra["era"] != rb["era"]
                                 and "unknown" not in (ra["era"], rb["era"]))
                    score = sem + (cfg["cross_era_bonus"] if cross_era else 0.0)
                    if score < cfg["semantic_threshold"]:
                        continue

                    fz = fuzz.token_set_ratio(texts[i], texts[j])
                    if fz < cfg["fuzzy_threshold"]:
                        continue          # shared template, different places

                    # EVIDENCE GATE: the pair must share specific identifiers
                    # (place names, ward numbers, landmarks), not merely the
                    # work-type boilerplate that every MPLADS record carries.
                    ta, tb = tok_specific[i], tok_specific[j]
                    shared = ta & tb
                    if (len(ta) < cfg["min_specific_tokens"]
                            or len(tb) < cfg["min_specific_tokens"]
                            or len(shared) < cfg["min_shared_specific"]):
                        continue

                    # DISCRIMINATOR: differing numeric locators mean different
                    # positions (e.g. culverts at KM 0+400 vs KM 1+200 on one
                    # road). Conservative by design - this tool must not
                    # allege duplication between demonstrably distinct works.
                    la, lb = locators[i], locators[j]
                    if (la or lb) and la != lb:
                        continue

                    geo_km = None
                    if all(pd.notna(ra.get(c)) and pd.notna(rb.get(c))
                           for c in ("lat", "lon")):
                        geo_km = _haversine_km(ra["lat"], ra["lon"], rb["lat"], rb["lon"])
                        if geo_km > cfg["geo_radius_km"]:
                            continue      # geographically distinct assets

                    a_id, b_id = str(ra["work_id"]), str(rb["work_id"])
                    pair = tuple(sorted((a_id, b_id)))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)

                    exact = sem >= 0.995 and fz >= 99
                    same_cost = (pd.notna(this_cost := ra.get("sanctioned_amount"))
                                 and pd.notna(other_cost := rb.get("sanctioned_amount"))
                                 and float(this_cost) == float(other_cost))
                    # Grade the evidence so the review queue self-prioritises:
                    # only a near-verbatim match on several specific identifiers
                    # (or identical sanction amounts) is a strong signal.
                    strong = (sem >= 0.97 and len(shared) >= 4) or (exact and same_cost)
                    severity = cfg["severity"] if strong else "medium"
                    same_mp = str(ra.get("mp_name")) == str(rb.get("mp_name"))
                    record_pair = portal_record_pair(ra, rb)
                    if record_pair is not None:
                        severity = "low"
                        mode = PORTAL_RECORD_PAIR
                    elif cross_era:
                        mode = "cross-era re-entry (physical -> eSAKSHI migration)"
                    elif not same_mp:
                        mode = "cross-MP overlap (constituency-boundary duplication)"
                    elif exact:
                        mode = "identical recommendation double-entry"
                    else:
                        mode = "same-recommender near-duplicate"

                    for this, other in ((ra, rb), (rb, ra)):
                        wid = str(this["work_id"])
                        if per_work.get(wid, 0) >= cfg["max_findings_per_work"]:
                            continue
                        per_work[wid] = per_work.get(wid, 0) + 1
                        if record_pair is not None:
                            pending, sanctioned = record_pair
                            out.append(Finding(
                                agent="entity_resolution",
                                rule_id=cfg["id"],
                                rule_title=cfg["title"],
                                severity=severity,
                                entity_type="work",
                                entity_id=wid,
                                summary=(
                                    f"Probably the same work as {other['work_id']}: the "
                                    f"portal lists the original recommendation "
                                    f"{pending['work_id']} (stage "
                                    f"{pending.get('status') or 'not recorded'}) and the "
                                    f"sanctioned record {sanctioned['work_id']} separately, "
                                    f"both recommended by {pending.get('mp_name')} on "
                                    f"{pending.get('recommended_date')}. Shown for "
                                    f"completeness; it adds nothing to the risk score."
                                ),
                                details={
                                    "pair_work_id": str(other["work_id"]),
                                    "portal_record_pair": True,
                                    "pending_work_id": str(pending["work_id"]),
                                    "sanctioned_work_id": str(sanctioned["work_id"]),
                                    "pending_stage": pending.get("status"),
                                    "recommended_date": pending.get("recommended_date"),
                                    "semantic_sim": round(min(score, 1.0), 3),
                                    "fuzzy_score": float(fz),
                                    "duplication_mode": mode,
                                    "evidence_strength": "portal record pair",
                                    "same_amount": bool(
                                        pd.notna(pending.get("estimated_cost"))
                                        and pd.notna(sanctioned.get("sanctioned_amount"))
                                        and float(pending["estimated_cost"])
                                        == float(sanctioned["sanctioned_amount"])),
                                    "other_description": str(other["description"])[:180],
                                    "this_description": str(this["description"])[:180],
                                    "other_mp": other.get("mp_name"),
                                },
                            ))
                            continue
                        out.append(Finding(
                            agent="entity_resolution",
                            rule_id=cfg["id"],
                            rule_title=cfg["title"],
                            severity=severity,
                            entity_type="work",
                            entity_id=wid,
                            summary=(
                                f"Near-duplicate of work {other['work_id']} in "
                                f"{this.get('district') or this.get('state')} — text "
                                f"similarity {min(score, 1.0):.0%}, token match "
                                f"{fz:.0f}/100, sharing {len(shared)} specific "
                                f"identifiers ({', '.join(sorted(shared)[:4])})"
                                + (f", assets {geo_km:.1f} km apart"
                                   if geo_km is not None else "")
                                + (f"; both sanctioned at Rs {float(this['sanctioned_amount']):,.0f}"
                                   if (pd.notna(this.get("sanctioned_amount"))
                                       and pd.notna(other.get("sanctioned_amount"))
                                       and float(this["sanctioned_amount"])
                                       == float(other["sanctioned_amount"])) else "")
                                + f"; likely mode: {mode}"
                            ),
                            details={
                                "pair_work_id": str(other["work_id"]),
                                "semantic_sim": round(min(score, 1.0), 3),
                                "fuzzy_score": float(fz),
                                "geo_km": round(geo_km, 2) if geo_km is not None else None,
                                "duplication_mode": mode,
                                "shared_identifiers": sorted(shared)[:8],
                                "evidence_strength": "strong" if strong else "review",
                                "same_sanction_amount": bool(same_cost),
                                "other_description": str(other["description"])[:180],
                                "this_description": str(this["description"])[:180],
                                "other_mp": other.get("mp_name"),
                                "this_cost": (float(this.get("sanctioned_amount"))
                                              if pd.notna(this.get("sanctioned_amount"))
                                              else None),
                                "other_cost": (float(other.get("sanctioned_amount"))
                                               if pd.notna(other.get("sanctioned_amount"))
                                               else None),
                            },
                        ))

        out += self._generic_clusters(df, boilerplate, cfg)
        return out

    def _generic_clusters(self, df: pd.DataFrame, boilerplate: set,
                          cfg: dict) -> list[Finding]:
        """Repeated LOW-DETAIL descriptions in one district -> ONE cluster finding.

        e.g. 40 works described only as "Purchase of Street Lights". These are
        usually distinct assets, so flagging them pairwise as duplicates would
        be wrong and would swamp the review queue. Reported once, as a
        transparency signal: the descriptions are too thin for a district
        authority to verify that the works are distinct.
        """
        gcfg = cfg["generic_cluster"]
        work = df.copy()
        work["_norm"] = (work["description"].str.lower()
                         .str.replace(r"[^a-z0-9 ]", " ", regex=True)
                         .str.replace(r"\s+", " ", regex=True).str.strip())
        work["_spec"] = [len(_tokens(t) - boilerplate) for t in work["description"]]
        generic = work[work["_spec"] < cfg["min_specific_tokens"]]
        if generic.empty:
            return []

        out: list[Finding] = []
        for (state, district, norm), grp in generic.groupby(
                ["state", "district", "_norm"], dropna=False):
            if len(grp) < gcfg["min_cluster_size"] or not norm:
                continue
            cost = pd.to_numeric(grp.get("sanctioned_amount"), errors="coerce")
            if cost.isna().all():
                cost = pd.to_numeric(grp.get("estimated_cost"), errors="coerce")
            total = float(cost.sum(skipna=True) or 0.0)
            out.append(Finding(
                agent="entity_resolution",
                rule_id=gcfg["id"],
                rule_title=gcfg["title"],
                severity=gcfg["severity"],
                entity_type="work",
                entity_id=str(grp.iloc[0]["work_id"]),
                summary=(
                    f"{len(grp)} works in {district or state} share one low-detail "
                    f"description ('{norm[:60]}') totalling Rs {total:,.0f}. The "
                    f"descriptions are too thin to confirm these are distinct "
                    f"assets - separate locations should be verified. Not "
                    f"presumed duplicates."
                ),
                details={
                    "cluster_size": int(len(grp)),
                    "normalised_description": norm[:120],
                    "total_cost": total,
                    "district": district, "state": state,
                    "member_work_ids": grp["work_id"].astype(str).tolist()[:12],
                    "mps": sorted({str(m) for m in grp["mp_name"].dropna()})[:5],
                },
            ))
        return out
