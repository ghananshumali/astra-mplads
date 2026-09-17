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
recommendations of one work, one sanctioned and one still pending, go through
the record checks below like every other near-duplicate.

The text finds candidates; the record decides
---------------------------------------------
Identical text is not evidence that one work was entered twice. Measured on the
live corpus on 16 Sep 2026, 4,459 of the 4,522 alerts this rule raised came from
a member recommending three or more works with exactly one description
(benches, high-mast lights, water tankers, borewells for different villages),
and 63 from a plain pair. So every text match is read against the record:

  1. A **batch**: the same member recommended `batch_min_works` or more works
     with exactly this description. Identical items for different sites are
     normal, so each work carries one held batch finding, not pairwise
     duplicates. Works of a batch paid to the same gram panchayat are singled
     out as held pairs: a village is the only site the record names.
  2. A **separating detail** clears a pair, and no finding is made: different
     numbers in the descriptions (quantities, wards, chainage; the locator
     gate), place names that only one of them carries, amounts at least
     `amount_differs_pct` apart, or payment to different panchayats or
     municipalities.
  3. A pair nothing separates is **held**: shown with its reason, outside the
     risk score, until evidence such as the works' photos shows whether they
     are one work or two. Ids, dates, letter numbers, stages, vendors and small
     amount differences never clear a pair: they differ between two copies of
     one work as easily as between two works.
  4. **Photos** (`astra.ingestion.photos` fingerprints the portal's photos of
     held works; the analysis context carries them as `photos`): one image file,
     byte for byte, recorded for two works raises them at
     `photo_match_severity`, and for three or more works of a batch at
     `photo_shared_severity`. Photos that only look alike (fingerprints within
     `photo_max_bits`, different files) stay held and say so, for a person to
     compare: many uploads are phone photos of a printed site photo under the
     same camera stamp, and two different sites photographed that way came
     within one bit on one fingerprint in the first real sample. Different
     photos leave a pair held too: a second photo can always be taken. Every
     run records the pairs and batches it held (`last_groups`, stored as
     `duplicate_groups`) for the next check.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

from ..config import load_rules
from ..photo_hash import bits_apart, same_photo
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
#: How a batch of identical works is described wherever a duplication mode is shown.
BATCH = "batch of identical works"

#: A payee or implementing agency that is a local government body. Measured on
#: payment records (16 Sep 2026): gram panchayats and "GP ...", janpad and zila
#: parishads, block development and panchayat officers, panchayati raj divisions,
#: nagar palikas, panchayats and nigams, municipalities, in their common spellings.
LOCAL_BODY = re.compile(
    r"(^|[^a-z])(g\.?\s?p\.?|gram\s*panch\w*|grampanch\w*|village\s*panch\w*|"
    r"panch(a?y?a?t|yat|ayt)h?\w*|nagar\s*(palika?|palik|panch\w*|nigam|nigan|parishad|parisad)|"
    r"municipal\w*|janpad|(zil+a|jila)\s*(parishad|panch\w*)|mandal\s*parishad|sarpanch)"
    r"([^a-z]|$)", re.I)
#: The village level of that: the only body whose name places a work.
VILLAGE_BODY = re.compile(
    r"(^|[^a-z])(g\.?\s?p\.?|gram\s*panch\w*|grampanch\w*|village\s*panch\w*|sarpanch)([^a-z]|$)",
    re.I)


def _norm_text(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _amount(row) -> float | None:
    for col in ("sanctioned_amount", "estimated_cost"):
        value = row.get(col)
        if value is not None and pd.notna(value) and float(value) > 0:
            return float(value)
    return None


def payees_by_work(works: pd.DataFrame, payments: pd.DataFrame | None,
                   cols: tuple[str, ...] = ("vendor_name", "implementing_agency")) -> dict[str, list[str]]:
    """work id -> the names on its payments (by default vendors and implementing
    agencies), from every payment record and the work's own fields."""
    names: dict[str, set[str]] = defaultdict(set)
    for frame in (payments, works):
        if frame is None or frame.empty or "work_id" not in frame.columns:
            continue
        for col in cols:
            if col not in frame.columns:
                continue
            sub = frame[["work_id", col]].dropna()
            for wid, name in zip(sub["work_id"].astype(str), sub[col].astype(str)):
                name = " ".join(name.split())
                if name:
                    names[wid].add(name)
    return {wid: sorted(v) for wid, v in names.items()}


def _same_text(a, b) -> bool:
    """Equal after case and spacing are normalised; a missing value never matches."""
    def norm(v):
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return ""
        return " ".join(str(v).upper().split())
    return bool(norm(a)) and norm(a) == norm(b)


def _same_body(a: str, b: str, threshold: float, fuzz) -> bool:
    """Two payee names for one body ("GRAM PANCHAYAT BAMHANI" / "gram panchyat bamhani")."""
    return fuzz.token_set_ratio(a.lower(), b.lower()) >= threshold


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
    an ordinary duplicate candidate. Without listing data, nothing is relabelled.
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


def _text(row, col):
    value = row.get(col)
    return None if value is None or (not isinstance(value, str) and pd.isna(value)) else value


def _group(kind: str, work_ids: list[str]) -> dict:
    ids = sorted(dict.fromkeys(work_ids))
    digest = hashlib.sha1("|".join(ids).encode()).hexdigest()[:12]
    return {"group_id": f"{kind}:{digest}", "kind": kind, "work_ids": ids}


def _photo_verdict(a_id: str, b_id: str, photos: dict, cfg: dict) -> dict:
    """What the photo checks of two works say about them.

    `same_file` when a photo of one is byte for byte a photo of the other (one
    upload recorded twice); `look_alike` when a photo of each is within
    `photo_max_bits` on both fingerprints but no file is shared, which only a
    person can settle; `different_photos` when both have photos and none
    match; otherwise why there is nothing to compare: `not_run`,
    `not_completed`, `no_photo` (including works with only documents),
    `failed`. Documents are never compared: one order or certificate
    legitimately covers several works.
    """
    pa, pb = photos.get(a_id), photos.get(b_id)
    if not pa or not pb:
        return {"result": "not_run"}
    candidates = [(p, q) for p in pa.get("photos", []) for q in pb.get("photos", [])]
    for p, q in candidates:
        if p.get("sha256") and p["sha256"] == q.get("sha256"):
            return {"result": "same_file", "files": {a_id: p["file_name"], b_id: q["file_name"]}}
    for p, q in candidates:
        if same_photo(p, q, cfg["photo_max_bits"]):
            return {"result": "look_alike", "files": {a_id: p["file_name"], b_id: q["file_name"]},
                    "bits": max(bits_apart(p["dhash"], q["dhash"]), bits_apart(p["vhash"], q["vhash"]))}
    if pa.get("photos") and pb.get("photos"):
        return {"result": "different_photos"}
    for status in ("failed", "not_completed"):
        if status in (pa["status"], pb["status"]):
            return {"result": status}
    return {"result": "no_photo"}


#: Photo-check results that raise a pair: only one file shared. Photos that
#: look alike stay held, because different sites photographed the same way
#: fingerprint alike.
SHARED_EVIDENCE = ("same_file",)


class EntityResolutionAgent(BaseAgent):
    name = "entity_resolution"
    needs_works = {"work_id", "description"}
    needs_flows = set()

    def __init__(self) -> None:
        #: The analysis context; its `payments` name who was paid for each work,
        #: and its `photos` the fingerprints a photo check recorded.
        self.context: dict = {}
        #: How the last run's text matches were decided (recorded in the run's trace).
        self.last_stats: dict = {}
        #: The pairs and batches the last run held.
        self.last_groups: list[dict] = []

    def run(self, works: pd.DataFrame, flows: pd.DataFrame) -> list[Finding]:
        cfg = load_rules()["duplicates"]
        self.last_stats = {}
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

        # Names on each work's payments: vendors and implementing agencies both
        # can be the panchayat or municipality that places a work; vendors alone
        # are who was paid.
        payments = self.context.get("payments")
        payees = payees_by_work(works, payments)
        vendors = payees_by_work(works, payments, ("vendor_name",))
        # A batch: one member, one exact description, several works.
        mps = df["mp_name"] if "mp_name" in df.columns else pd.Series(None, index=df.index)
        keys = [(" ".join(str(m).upper().split()), _norm_text(d))
                if m is not None and pd.notna(m) and str(m).strip() else None
                for m, d in zip(mps, df["description"])]
        sizes = Counter(k for k in keys if k)
        batch_of = {str(wid): k for wid, k in zip(df["work_id"], keys)
                    if k and sizes[k] >= cfg["batch_min_works"]}

        candidates: list[dict] = []
        seen_pairs: set[tuple[str, str]] = set()
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

                    # DISCRIMINATOR: differing numbers mean different works
                    # (culverts at KM 0+400 vs KM 1+200 on one road, ward 4 vs
                    # ward 7, "2 units" vs "3 units"). Conservative by design -
                    # this tool must not allege duplication between
                    # demonstrably distinct works.
                    la, lb = locators[i], locators[j]
                    if (la or lb) and la != lb:
                        continue

                    geo_km = None
                    if all(pd.notna(ra.get(c)) and pd.notna(rb.get(c))
                           for c in ("lat", "lon")):
                        geo_km = _haversine_km(ra["lat"], ra["lon"], rb["lat"], rb["lon"])
                        if geo_km > cfg["geo_radius_km"]:
                            continue      # geographically distinct assets

                    pair = tuple(sorted((str(ra["work_id"]), str(rb["work_id"]))))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    candidates.append({
                        "ra": ra, "rb": rb, "sem": sem, "score": score, "fz": float(fz),
                        "shared": shared, "geo_km": geo_km, "cross_era": cross_era,
                        "separated_by": self._separating(ra, rb, ta, tb, payees, cfg, fuzz),
                    })

        stats: Counter = Counter()
        out: list[Finding] = []
        per_work: dict[str, int] = {}
        held: list[dict] = []
        batched: dict[tuple, list[dict]] = defaultdict(list)
        for c in candidates:
            ra, rb = c["ra"], c["rb"]
            stats["text_matches"] += 1
            record_pair = portal_record_pair(ra, rb)
            if record_pair is not None:
                stats["portal_record_pairs"] += 1
                out += self._record_pair_findings(c, record_pair, per_work, cfg)
                continue
            key = batch_of.get(str(ra["work_id"]))
            if key is not None and key == batch_of.get(str(rb["work_id"])):
                stats["matches_within_batches"] += 1
                batched[key].append(c)
                continue
            if c["separated_by"]:
                stats["cleared"] += 1
                for reason in c["separated_by"]:
                    stats[f"cleared_by_{reason}"] += 1
                continue
            held.append(c)

        # Photo fingerprints a photo check recorded for held works, if any.
        photos = self.context.get("photos") or {}
        groups: list[dict] = []
        members_of: dict[tuple, list] = defaultdict(list)
        for label, key in zip(df.index, keys):
            if key in batched:
                members_of[key].append(label)
        for key, pairs in batched.items():
            rows = df.loc[members_of[key]]
            groups.append(_group("batch", rows["work_id"].astype(str).tolist()))
            out += self._batch_findings(rows, pairs, payees, vendors, cfg, fuzz, stats, held,
                                        photos)

        matches: Counter = Counter()
        for c in held:
            matches[str(c["ra"]["work_id"])] += 1
            matches[str(c["rb"]["work_id"])] += 1
        for c in held:
            groups.append(_group("pair", [str(c["ra"]["work_id"]), str(c["rb"]["work_id"])]))
            verdict = _photo_verdict(str(c["ra"]["work_id"]), str(c["rb"]["work_id"]), photos, cfg)
            stats[f"held_pairs_photo_{verdict['result']}"] += 1
            if verdict["result"] in SHARED_EVIDENCE:
                stats[f"pairs_raised_{verdict['result']}"] += 1
                out += self._photo_match_findings(c, verdict, per_work, matches, vendors, cfg,
                                                  cfg["photo_match_severity"])
                continue
            stats["held_pairs"] += 1
            out += self._held_findings(c, per_work, matches, vendors, cfg, verdict)

        out += self._generic_clusters(df, boilerplate, cfg)
        #: The held groups, for the photo check to look at (`duplicate_groups`).
        self.last_groups = groups
        self.last_stats = dict(sorted(stats.items()))
        return out

    # ------------------------------------------------------------ deciding
    @staticmethod
    def _separating(ra, rb, ta: set, tb: set, payees: dict, cfg: dict, fuzz) -> list[str]:
        """The recorded details that show two text-matched works are two works.

        Only details that place or size a work count. Ids, dates, letter
        numbers, stages and vendors differ between two copies of one work as
        easily as between two works, so they never clear a pair.
        """
        reasons = []
        typo = cfg["typo_similarity"]
        only_a = {t for t in ta - tb if not any(fuzz.ratio(t, u) >= typo for u in tb)}
        only_b = {t for t in tb - ta if not any(fuzz.ratio(t, u) >= typo for u in ta)}
        if only_a and only_b:
            reasons.append("description")     # each names something the other does not
        ca, cb = _amount(ra), _amount(rb)
        if ca and cb and abs(ca - cb) / max(ca, cb) * 100 >= cfg["amount_differs_pct"]:
            reasons.append("amount")
        la = [p for p in payees.get(str(ra["work_id"]), []) if LOCAL_BODY.search(p)]
        lb = [p for p in payees.get(str(rb["work_id"]), []) if LOCAL_BODY.search(p)]
        if la and lb and not any(_same_body(x, y, cfg["same_body_similarity"], fuzz)
                                 for x in la for y in lb):
            reasons.append("local_body")
        return reasons

    @staticmethod
    def _mode(c: dict) -> str:
        ra, rb = c["ra"], c["rb"]
        if c["cross_era"]:
            return "cross-era re-entry (physical -> eSAKSHI migration)"
        if str(ra.get("mp_name")) != str(rb.get("mp_name")):
            return "cross-MP overlap (constituency-boundary duplication)"
        if c["sem"] >= 0.995 and c["fz"] >= 99:
            return "identical recommendation double-entry"
        return "same-recommender near-duplicate"

    # ------------------------------------------------------------ findings
    @staticmethod
    def _record_pair_findings(c: dict, record_pair: tuple, per_work: dict, cfg: dict) -> list[Finding]:
        out = []
        pending, sanctioned = record_pair
        for this, other in ((c["ra"], c["rb"]), (c["rb"], c["ra"])):
            wid = str(this["work_id"])
            if per_work.get(wid, 0) >= cfg["max_findings_per_work"]:
                continue
            per_work[wid] = per_work.get(wid, 0) + 1
            out.append(Finding(
                agent="entity_resolution",
                rule_id=cfg["id"],
                rule_title=cfg["title"],
                severity="low",
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
                    "semantic_sim": round(min(c["score"], 1.0), 3),
                    "fuzzy_score": c["fz"],
                    "duplication_mode": PORTAL_RECORD_PAIR,
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
        return out

    @staticmethod
    def _pair_details(c: dict, this, other, matches: Counter, payees: dict) -> dict:
        """What a finding about two matched works records about both."""
        wid, oid = str(this["work_id"]), str(other["work_id"])
        this_cost, other_cost = _amount(this), _amount(other)
        return {
            "pair_work_id": oid,
            "semantic_sim": round(min(c["score"], 1.0), 3),
            "fuzzy_score": c["fz"],
            "geo_km": round(c["geo_km"], 2) if c["geo_km"] is not None else None,
            "duplication_mode": EntityResolutionAgent._mode(c),
            "shared_identifiers": sorted(c["shared"])[:8],
            "same_sanction_amount": bool(this_cost is not None and this_cost == other_cost),
            "this_cost": this_cost,
            "other_cost": other_cost,
            "this_description": str(this["description"])[:180],
            "other_description": str(other["description"])[:180],
            "other_mp": other.get("mp_name"),
            "this_payees": payees.get(wid, [])[:4],
            "other_payees": payees.get(oid, [])[:4],
            "this_status": _text(this, "status"),
            "other_status": _text(other, "status"),
            "this_letter": _text(this, "letter_no"),
            "other_letter": _text(other, "letter_no"),
            "total_matches": int(matches.get(wid, 0)),
            "in_batch": bool(c.get("in_batch")),
            "shared_payee": c.get("shared_payee"),
        }

    @staticmethod
    def _held_findings(c: dict, per_work: dict, matches: Counter, payees: dict,
                       cfg: dict, verdict: dict | None = None) -> list[Finding]:
        out = []
        shared = c["shared"]
        verdict = verdict or {"result": "not_run"}
        photo_check = verdict["result"]
        photo_note = {
            "look_alike": " Their photos on the portal look alike but are not the same file: "
                          "compare them by eye, since different sites photographed the same "
                          "way can look alike.",
            "different_photos": " Their photos on the portal are different pictures, which "
                                "suggests two sites without proving it.",
            "no_photo": " The portal has no photo for at least one of them yet.",
            "not_completed": " At least one of them is not completed, so it has no photo yet.",
            "failed": " The portal did not return their photos at the last check.",
        }.get(photo_check, "")
        for this, other in ((c["ra"], c["rb"]), (c["rb"], c["ra"])):
            wid = str(this["work_id"])
            if per_work.get(wid, 0) >= cfg["max_findings_per_work"]:
                continue
            per_work[wid] = per_work.get(wid, 0) + 1
            details = EntityResolutionAgent._pair_details(c, this, other, matches, payees)
            this_cost = details["this_cost"]
            out.append(Finding(
                agent="entity_resolution",
                rule_id=cfg["id"],
                rule_title=cfg["title"],
                severity="low",
                entity_type="work",
                entity_id=wid,
                summary=(
                    f"Matches work {details['pair_work_id']} in "
                    f"{this.get('district') or this.get('state')}: "
                    f"descriptions {min(c['score'], 1.0):.0%} alike, sharing "
                    f"{len(shared)} specific identifiers ({', '.join(sorted(shared)[:4])})"
                    + (f", both sanctioned at Rs {this_cost:,.0f}"
                       if details["same_sanction_amount"] else "")
                    + (f", both paid to {c['shared_payee']}" if c.get("shared_payee") else "")
                    + ". No recorded detail separates them (numbers or place names in the "
                    "descriptions, amount, panchayat or municipality paid), so the pair is "
                    "held outside the risk score until evidence such as the works' photos "
                    f"shows whether they are one work or two; likely mode: "
                    f"{details['duplication_mode']}.{photo_note}"
                ),
                details={**details, "held": True, "evidence_strength": "held",
                         "photo_check": photo_check,
                         **({"this_file": verdict["files"].get(wid),
                             "other_file": verdict["files"].get(details["pair_work_id"]),
                             "photo_bits": verdict.get("bits")}
                            if photo_check == "look_alike" else {})},
            ))
        return out

    @staticmethod
    def _photo_match_findings(c: dict, verdict: dict, per_work: dict, matches: Counter,
                              payees: dict, cfg: dict, severity: str,
                              cluster: list[str] | None = None) -> list[Finding]:
        """Two works with one photo file on the portal: no longer held, and
        scored at `severity`."""
        out = []
        for this, other in ((c["ra"], c["rb"]), (c["rb"], c["ra"])):
            wid, oid = str(this["work_id"]), str(other["work_id"])
            if per_work.get(wid, 0) >= cfg["max_findings_per_work"]:
                continue
            per_work[wid] = per_work.get(wid, 0) + 1
            details = EntityResolutionAgent._pair_details(c, this, other, matches, payees)
            others = [w for w in (cluster or []) if w not in (wid, oid)]
            out.append(Finding(
                agent="entity_resolution",
                rule_id=cfg["id"],
                rule_title=cfg["title"],
                severity=severity,
                entity_type="work",
                entity_id=wid,
                summary=(
                    f"The photo recorded on the portal for this work ({verdict['files'].get(wid)}) "
                    f"is the same image file, byte for byte, as the one recorded for work {oid} "
                    f"({verdict['files'].get(oid)})"
                    + (f" and {len(others)} other work(s) ({', '.join(others[:3])})" if others else "")
                    + f"; their descriptions are {min(c['score'], 1.0):.0%} alike. Works "
                    "recorded as separate assets should not share a site photo: one work may be "
                    "recorded twice, or a wrong photo uploaded. For review; likely mode: "
                    f"{details['duplication_mode']}."
                ),
                details={**details, "held": False, "photo_match": True, "match_kind": "photo",
                         "evidence_strength": "same photo file", "photo_check": verdict["result"],
                         "this_file": verdict["files"].get(wid),
                         "other_file": verdict["files"].get(oid),
                         "photo_cluster": cluster or [wid, oid]},
            ))
        return out

    @staticmethod
    def _batch_findings(rows: pd.DataFrame, pairs: list[dict], payees: dict, vendors: dict,
                        cfg: dict, fuzz, stats: Counter, held: list[dict],
                        photos: dict | None = None) -> list[Finding]:
        """One held finding per work of a batch; findings for works of the batch
        sharing one photo file; and held pairs for works of the batch paid to the
        same gram panchayat. Works whose photos only look alike are listed on the
        batch for a person to compare, not raised."""
        photos = photos or {}
        rows = rows.sort_values("work_id")
        records = rows.to_dict("records")
        ids = [str(r["work_id"]) for r in records]
        amounts = [_amount(r) for r in records]
        total = float(sum(a for a in amounts if a))
        letters = {str(r.get("letter_no")).strip() for r in records
                   if _text(r, "letter_no") and str(r.get("letter_no")).strip()}
        paid_to = {p for wid in ids for p in vendors.get(wid, [])}
        shared = sorted(pairs[0]["shared"])[:8]
        out: list[Finding] = []

        # works of the batch sharing one photo file, grouped
        checked = [pos for pos, wid in enumerate(ids) if wid in photos]
        evidenced = [pos for pos in checked if photos[ids[pos]].get("photos")]
        parent = {pos: pos for pos in evidenced}
        found_pair: dict[tuple[int, int], dict] = {}
        look_alike: list[tuple[int, int]] = []

        def root(p):
            while parent[p] != p:
                parent[p] = parent[parent[p]]
                p = parent[p]
            return p

        for x, a in enumerate(evidenced):
            for b in evidenced[x + 1:]:
                verdict = _photo_verdict(ids[a], ids[b], photos, cfg)
                if verdict["result"] in SHARED_EVIDENCE:
                    found_pair[(a, b)] = verdict
                    parent[root(b)] = root(a)
                elif verdict["result"] == "look_alike":
                    look_alike.append((a, b))
        clusters: dict[int, list[int]] = defaultdict(list)
        for pos in evidenced:
            clusters[root(pos)].append(pos)
        clusters = {k: v for k, v in clusters.items() if len(v) >= 2}
        cluster_of = {pos: k for k, members_ in clusters.items() for pos in members_}
        # a look-alike inside a group already raised adds nothing
        look_alike = [(a, b) for a, b in look_alike
                      if not (a in cluster_of and cluster_of.get(a) == cluster_of.get(b))]
        if look_alike:
            stats["look_alike_pairs_within_batches"] += len(look_alike)
        matched = Counter()
        for members_ in clusters.values():
            cluster_ids = [ids[p] for p in members_]
            severity = (cfg["photo_match_severity"] if len(members_) == 2
                        else cfg["photo_shared_severity"])
            stats["shared_photo_groups_within_batches"] += 1
            for a in members_:
                # a partner it matched directly (every member of a group has one)
                b, verdict = next((q, found_pair[(min(a, q), max(a, q))]) for q in members_
                                  if q != a and (min(a, q), max(a, q)) in found_pair)
                c = {"ra": pd.Series(records[a]), "rb": pd.Series(records[b]), "sem": 1.0,
                     "score": 1.0, "fz": 100.0, "shared": set(shared), "geo_km": None,
                     "cross_era": False, "in_batch": True}
                # one finding on `a` naming the group; `b` gets its own in its turn
                out += [f for f in EntityResolutionAgent._photo_match_findings(
                    c, verdict, {ids[b]: cfg["max_findings_per_work"]}, matched, vendors, cfg,
                    severity, cluster_ids) if f.entity_id == ids[a]]

        # works paid to the same gram panchayat: the one place the record names
        villages: list[tuple[str, list[int]]] = []
        for pos, wid in enumerate(ids):
            for name in payees.get(wid, []):
                if not VILLAGE_BODY.search(name):
                    continue
                group = next((g for g in villages
                              if _same_body(g[0], name, cfg["same_body_similarity"], fuzz)), None)
                if group is None:
                    villages.append((name, [pos]))
                elif pos not in group[1]:
                    group[1].append(pos)
        same_payee = [(name, members) for name, members in villages if len(members) >= 2]
        for name, members in same_payee:
            for x, a in enumerate(members):
                for b in members[x + 1:]:
                    ca, cb = amounts[a], amounts[b]
                    if ca and cb and abs(ca - cb) / max(ca, cb) * 100 >= cfg["amount_differs_pct"]:
                        continue
                    if a in cluster_of and cluster_of.get(a) == cluster_of.get(b):
                        continue          # already raised: one photo file for both
                    stats["held_pairs_within_batches"] += 1
                    held.append({"ra": pd.Series(records[a]), "rb": pd.Series(records[b]),
                                 "sem": 1.0, "score": 1.0, "fz": 100.0, "shared": set(shared),
                                 "geo_km": None, "cross_era": False, "separated_by": [],
                                 "in_batch": True, "shared_payee": name})

        members = [{"work_id": wid, "amount": amounts[pos],
                    "payees": vendors.get(wid, [])[:3],
                    "status": _text(records[pos], "status"),
                    "letter_no": _text(records[pos], "letter_no"),
                    "recommended_date": _text(records[pos], "recommended_date")}
                   for pos, wid in enumerate(ids[:cfg["batch_members_shown"]])]
        groups = [{"payee": name, "work_ids": [ids[p] for p in members_]}
                  for name, members_ in same_payee]
        first = records[0]
        mp = first.get("mp_name")
        stats["batches"] += 1
        stats["works_in_batches"] += len(ids)
        photo_summary = {"checked": len(checked),
                         "with_photos": sum(1 for p in checked if photos[ids[p]].get("photos")),
                         "with_documents": sum(1 for p in checked if photos[ids[p]].get("documents")),
                         "shared_groups": len(clusters),
                         "look_alike_pairs": len(look_alike),
                         "look_alike": [[ids[a], ids[b]] for a, b in look_alike[:20]]}
        for pos, wid in enumerate(ids):
            this = records[pos]
            mine = next((g for g in groups if wid in g["work_ids"]), None)
            out.append(Finding(
                agent="entity_resolution",
                rule_id=cfg["id"],
                rule_title=cfg["title"],
                severity="low",
                entity_type="work",
                entity_id=wid,
                summary=(
                    f"One of {len(ids)} works {mp} recommended with exactly this description, "
                    f"together Rs {total:,.0f}, under {len(letters)} recommendation letter(s) "
                    f"and paid to {len(paid_to)} payee(s) so far. Identical items for "
                    f"different sites are normal, so these works are not treated as "
                    f"duplicates of each other: the batch is held outside the risk score "
                    f"until the sites are confirmed"
                    + (f"; this work and {len(mine['work_ids']) - 1} other(s) of the batch "
                       f"were paid to {mine['payee']} and are checked as held pairs"
                       if mine else "")
                    + "."
                ),
                details={
                    "batch": True,
                    "held": True,
                    # the batch opens one case, on its first work; its other
                    # works show it on cases that exist for another reason
                    "standalone": pos == 0,
                    "batch_size": len(ids),
                    "batch_total": total,
                    "batch_mp": mp,
                    "batch_letters": len(letters),
                    "batch_payees": len(paid_to),
                    "members": members,
                    "members_shown": len(members),
                    "same_payee_groups": groups,
                    "shared_identifiers": shared,
                    "semantic_sim": 1.0,
                    "duplication_mode": BATCH,
                    "evidence_strength": "batch",
                    "this_description": str(this["description"])[:180],
                    "this_cost": amounts[pos],
                    "photo_check": "checked" if checked else "not_run",
                    "photo_summary": photo_summary,
                },
            ))
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
