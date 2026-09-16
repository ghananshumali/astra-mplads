"""Compliance Agent — deterministic MPLADS guideline rule engine (no ML).

Every finding cites the paragraph of the MPLADS Guidelines (1 April 2023) it
rests on, and the exact threshold crossed. Rules and thresholds live in
config/rules.yaml; the paragraphs are indexed in config/guidelines_2023.yaml.
What each field means, and why the rules read it the way they do, is in
`astra/data_contract.py`.

Work-level rules   : one-year completion norm (3.2.12), works not permissible
                     (5.2), minimum work value (3.2.9), time to sanction (3.2.4).
MP-level rules     : repair and renovation cap (5.1.9), societies and trusts
                     cap (6.2.6.2), SC/ST allocation (5.4.1), and two
                     fund-series heuristics under the non-lapsable regime
                     (10.4.4).
District authority : typical time to sanction (3.2.4, screening).

A rule that cannot be evaluated on the data says why in `self.notes`, which
the orchestrator records with the run.
"""
from __future__ import annotations

import re
from datetime import date

import pandas as pd

from .. import data_contract as contract
from ..config import cite, load_rules
from ..locale import text
from ..schemas import Finding
from .base import BaseAgent


#: compounds where a religious word forms part of a secular institution's name
_SECULAR_COMPOUND = re.compile(
    r"\b(shishu|vidya|vidhya|saraswati|bal|balak|gyan|shiksha|sanskar|"
    r"samaj|samaaj|rang|rangam|natya|kala|krida|yuvak)\s*"
    r"(mandir|mandira|mandiram)\b", re.IGNORECASE)

#: Indic postpositions that mark the preceding noun as a LOCATION
_INDIC_LOCATIVE = re.compile(
    r"^\s*(?:n[iaeou]|k[aeiou]|no|nu)?\s*"
    r"(?:ke\s+(?:pas|paas|paass|nikat|samip|samne|samne|nazdik|bagal|piche|"
    r"aas[\s-]?pas|ass[\s-]?pass)|ki\s+taraf|se\s|se$|tak\b|pase\b|paas\b|"
    r"pas\b|par\b|thi\b|sudhi\b|najik\b|javal\b|shejari\b|wali\b|wale\b)",
    re.IGNORECASE)


def _f(rule: dict, entity_type: str, entity_id: str, summary: str,
       details: dict, severity: str | None = None, clause: str | None = None) -> Finding:
    details = dict(details)
    details.setdefault("basis", rule.get("basis"))
    if rule.get("para"):
        details.setdefault("para", rule["para"])
    if entity_type == "work" and rule.get("standalone") is False:
        # joins a case only when the work has another finding (orchestrator)
        details.setdefault("standalone", False)
    return Finding(
        agent="compliance",
        rule_id=rule["id"],
        rule_title=rule["title"],
        clause=clause or rule.get("clause"),
        severity=severity or rule["severity"],
        entity_type=entity_type,
        entity_id=str(entity_id),
        summary=summary,
        details=details,
    )


def _pattern_map(patterns) -> dict[str, str | None]:
    """Patterns as {term: paragraph}; a plain list maps to no paragraph."""
    if isinstance(patterns, dict):
        return {str(k): (str(v) if v is not None else None) for k, v in patterns.items()}
    return {str(p): None for p in patterns or []}


def _word_regex(terms) -> re.Pattern | None:
    terms = [t for t in terms if t]
    if not terms:
        return None
    alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![a-z]){alternation}(?![a-z])", re.IGNORECASE)


class ComplianceAgent(BaseAgent):
    name = "compliance"
    needs_works = {"work_id"}
    needs_flows = {"fy"}

    def __init__(self, today: date | None = None) -> None:
        self.today = today
        self.notes: dict[str, str] = {}

    @property
    def _today(self) -> date:
        return self.today or date.today()

    def run(self, works: pd.DataFrame, flows: pd.DataFrame) -> list[Finding]:
        cfg = load_rules()["rules"]
        self.notes = {}
        out: list[Finding] = []
        if not works.empty:
            if "is_completed" not in works.columns:
                works = contract.enrich(works)
            out += self._completion_norm(works, cfg["completion_norm"])
            out += self._prohibited(works, cfg["prohibited_works"])
            out += self._cost_ceiling(works, cfg["cost_ceiling"])
            if "sanction_timeline" in cfg:
                out += self._sanction_timeline(works, cfg["sanction_timeline"])
            if "repair_cap" in cfg:
                out += self._repair_cap(works, cfg["repair_cap"])
            if "trust_cap" in cfg:
                out += self._trust_cap(works, cfg["trust_cap"])
        if not flows.empty:
            out += self._scst(flows, cfg["scst_allocation"])
            out += self._utilization_spike(flows, cfg["utilization_spike"])
            out += self._pileup(flows, cfg["unspent_pileup"])
        if not works.empty and "is_sc_constituency" in works.columns:
            out += self._scst_reserved_proxy(works, cfg["scst_reserved_proxy"])
        return out

    def _scst_reserved_proxy(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        """State-level share of MPLADS spend reaching SC/ST-reserved seats.

        Screening indicator only: the guideline obligation in para 5.4.1 is
        about SC/ST AREAS within a constituency, which the published extracts
        do not expose. A low share here tells the State Nodal Authority where
        to pull the district ledgers, and nothing more.
        """
        df = works.copy()
        # Reserved seats are Lok Sabha constituencies. Rajya Sabha members hold
        # no seat, so their spend has no place in this share — included, it
        # only inflates each state's total and pushes reserved-seat shares
        # down. Measured on the live corpus: two false findings (Andhra
        # Pradesh ST, Bihar SC) that exist only because RS spend was mixed in.
        # The CSV corpus carries no `house`, so NULL is treated as Lok Sabha
        # and that path is unchanged.
        if "house" in df.columns:
            df = df[df["house"].fillna("LS") != "RS"]
        if df.empty:
            return []
        spend = pd.to_numeric(df.get("expenditure"), errors="coerce")
        if spend is None or spend.isna().all():
            spend = pd.to_numeric(df.get("sanctioned_amount"), errors="coerce")
        df["_spend"] = spend.fillna(0.0)
        df["is_sc_constituency"] = pd.to_numeric(
            df["is_sc_constituency"], errors="coerce").fillna(0)
        df["is_st_constituency"] = pd.to_numeric(
            df.get("is_st_constituency"), errors="coerce").fillna(0)

        out: list[Finding] = []
        for state, grp in df.groupby("state", dropna=True):
            total = float(grp["_spend"].sum())
            if total < rule["min_state_expenditure"]:
                continue
            sc_seats = grp.loc[grp["is_sc_constituency"] == 1, "constituency"].nunique()
            st_seats = grp.loc[grp["is_st_constituency"] == 1, "constituency"].nunique()
            sc_pct = 100 * float(grp.loc[grp["is_sc_constituency"] == 1, "_spend"].sum()) / total
            st_pct = 100 * float(grp.loc[grp["is_st_constituency"] == 1, "_spend"].sum()) / total

            for kind, pct, seats, minimum in (
                ("SC", sc_pct, sc_seats, rule["sc_min_pct"]),
                ("ST", st_pct, st_seats, rule["st_min_pct"]),
            ):
                if seats < rule["min_reserved_seats"] or pct >= minimum:
                    continue
                out.append(_f(
                    rule, "constituency", f"STATE:{state}",
                    f"{kind}-reserved constituencies in {state} received {pct:.1f}% of "
                    f"Rs {total:,.0f} MPLADS spend across {seats} reserved seat(s), "
                    f"below the {minimum}% scheme benchmark. Screening indicator: "
                    f"para 5.4.1 governs SC/ST areas, which these extracts do not "
                    f"itemise — pull district-level allocation to assess compliance.",
                    {"state": state, "kind": kind, "pct": round(pct, 2),
                     "benchmark_pct": minimum, "reserved_seats": int(seats),
                     "state_expenditure": total,
                     "proxy": "reserved-constituency share, not DA-level area spend"},
                ))
        return out

    # ---------------- work-level rules ----------------

    def _completion_norm(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        """One-year completion norm, para 3.2.12.

        Completion is read from the portal's Works Completed report, not the
        stage text (see data_contract): most completed works read "Physical
        Inspection". An earlier version tested the stage for the literal
        "completed", which no portal stage is, so no work counted as complete.
        """
        if "sanction_date" not in works.columns:
            return []
        df = works.dropna(subset=["sanction_date"]).copy()
        if df.empty:
            return []
        sanction = pd.to_datetime(df["sanction_date"], errors="coerce")
        completion = pd.to_datetime(df.get("completion_date"), errors="coerce")
        completed = df["is_completed"].astype(bool) if "is_completed" in df else completion.notna()
        today = pd.Timestamp(self._today)
        end = completion.where(completed & completion.notna(), today)
        days = (end - sanction).dt.days
        out = []
        for idx in df.index[(days > rule["max_days"]) & sanction.notna()]:
            d = int(days.loc[idx])
            over_outer = d > rule["outer_limit_days"]
            still_open = not bool(completed.loc[idx])
            state = "still incomplete" if still_open else "completed late"
            out.append(_f(
                rule, "work", df.loc[idx, "work_id"],
                f"Work {state}: {d} days since sanction against the one-year norm "
                f"(para 3.2.12, which allows longer periods justified in the sanction letter)"
                + (" — beyond 18 months" if over_outer else ""),
                {"days_elapsed": d, "max_days": rule["max_days"],
                 "outer_limit_days": rule["outer_limit_days"], "incomplete": still_open,
                 "outer_limit_basis": rule.get("outer_limit_basis")},
                severity=rule["overdue_severity"] if over_outer else rule["severity"],
            ))
        return out

    def _prohibited(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        """Negative-list check (para 5.2) that asks WHAT IS BEING BUILT.

        MPLADS descriptions follow the shape "<verb> of <ASSET> at/near/from
        <LOCATION>", and the location is very often a temple, mosque or church
        used purely as a landmark. On the real corpus, naive keyword matching
        produced 5,498 hits of which ~90% were landmarks - "Construction of
        Community Hall near Mallikarjun Temple" is a community hall, not a
        religious work. Reporting those as prohibited religious works would be
        a false accusation on the most politically sensitive rule in the scheme.

        So the asset test runs only against the HEAD ASSET PHRASE - the words
        between the construction verb and the first locational preposition -
        and stands down if that phrase names a permissible asset. Activity
        patterns (O&M, land acquisition, recurring expenditure) describe the
        nature of the spend rather than an asset and are matched directly.
        Each term carries the paragraph of the negative list it indicates.
        """
        desc = works.get("description", pd.Series(dtype=object)).fillna("").astype(str)

        verb = re.compile(
            r"\b(construction|constructing|construct|const\.?|renovation|renovate|"
            r"repair(?:ing)?|building|build|purchase|procurement|installation|"
            r"installing|providing|provision|setting\s+up|erection|upgradation|"
            r"improvement|development|laying)\b\s*(?:of\s+|for\s+)?", re.IGNORECASE)
        stop = re.compile(
            r"\b(at|near|nearby|opposite|opp|from|to|towards|toward|beside|behind|"
            r"adjacent|adjoining|in|into|on|under|within|around|infront|in\s+front|"
            r"ward|village|gram|panchayat|block|mandal|tehsil|taluk|district|gp|"
            r"colony|nagar|basti|road\s+no|ref)\b", re.IGNORECASE)

        assets = _pattern_map(rule.get("asset_patterns"))
        activities = _pattern_map(rule.get("activity_patterns"))
        permissible = {a.lower() for a in rule.get("permissible_assets", [])}
        asset_re = _word_regex(assets)
        activity_re = _word_regex(activities)

        def para_of(mapping: dict, matched: str) -> str | None:
            return mapping.get(matched.lower()) or next(
                (v for k, v in mapping.items() if k.lower() == matched.lower()), None)

        out: list[Finding] = []
        for idx, raw in desc.items():
            text = raw.strip()
            if not text:
                continue

            # --- activity patterns: about the nature of the expenditure
            if activity_re:
                am = activity_re.search(text)
                if am:
                    para = para_of(activities, am.group(0))
                    out.append(_f(
                        rule, "work", works.loc[idx, "work_id"],
                        f"The description indicates '{am.group(0)}', which the "
                        f"negative list excludes"
                        + (f" (para {para})" if para else "")
                        + ". Confirm the scope before release: repair and renovation "
                          "of an asset is permitted (para 5.1.9), routine upkeep is not.",
                        {"matched_pattern": am.group(0), "match_type": "activity",
                         "text": text[:200], "para": para},
                        severity=rule.get("activity_severity", "medium"),
                        clause=cite(para) or rule.get("clause"),
                    ))
                    continue

            # --- asset patterns: only against the head asset phrase
            if not asset_re:
                continue
            vm = verb.search(text)
            head = text[vm.end():] if vm else text
            sm = stop.search(head)
            head = head[:sm.start()] if sm else head
            head = head.strip(" ,.-:;()")
            if not head:
                continue

            hm = asset_re.search(head)
            if not hm:
                continue
            # "Mandir" is not always a temple. Saraswati Shishu Mandir and Vidya
            # Mandir are school names, Samaj Mandir is a community hall and
            # Rang Mandir a theatre - all permissible. Domain disambiguation
            # matters more than raw keyword matching on this rule.
            if _SECULAR_COMPOUND.search(text):
                continue
            # Transliterated Indic postpositions. Hindi/Gujarati place the
            # locational marker AFTER the noun ("Hanuman Mandir ke pas" = near
            # Hanuman temple, "mandir se ... tak" = from the temple to ...), so
            # a term trailed by one of these is a landmark, not the asset.
            if _INDIC_LOCATIVE.match(head[hm.end():]):
                continue
            # the head phrase names a permissible asset -> the religious term is
            # qualifying it ("temple road", "mandir hall"), not the asset itself
            head_words = {w.strip(" ,.-:;()").lower() for w in head.split()}
            if head_words & permissible:
                continue
            # Global veto. A large share of real descriptions are Hindi or
            # Gujarati written in Latin script ("mandir se ... tak pcc nirman
            # karya" = a road FROM a temple), which the English clause parsing
            # above cannot segment. If ANY permissible asset noun appears
            # anywhere in the text, the work is about that asset and the
            # religious term is a landmark.
            #
            # This deliberately trades recall for precision: a genuine "temple
            # hall" is let through unflagged. On a rule that would otherwise
            # accuse an MP of funding religious construction, a missed flag is
            # far cheaper than a false one - and the duplicate, cost and
            # completion agents still see the work.
            all_words = {w.strip(" ,.-:;()").lower() for w in text.split()}
            if all_words & permissible:
                continue

            para = para_of(assets, hm.group(0))
            out.append(_f(
                rule, "work", works.loc[idx, "work_id"],
                f"The asset being created appears to be a negative-list item: "
                f"'{hm.group(0)}' heads the work description "
                f"(\"{head[:60]}\")"
                + (f", which para {para} excludes" if para else "")
                + ". Confirm the asset's nature before sanction or release.",
                {"matched_pattern": hm.group(0), "match_type": "asset",
                 "head_asset_phrase": head[:120], "text": text[:200], "para": para},
                clause=cite(para) or rule.get("clause"),
            ))
        return out

    def _cost_ceiling(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        """Minimum work value, para 3.2.9, and ASTRA's large-work marker.

        The minimum concerns the sanctioned amount, so works awaiting sanction
        are left out. The guideline itself allows a smaller work when the
        district authority records its reasons, so a finding asks for that
        record. The upper marker is not a guideline limit (there is none).
        """
        df = works
        if rule.get("sanctioned_only") and "sanction_date" in works.columns:
            df = works[works["sanction_date"].notna()]
        cost = pd.Series(float("nan"), index=df.index, dtype="float64")
        for col in ("sanctioned_amount", "estimated_cost"):
            if col in df.columns:
                cost = cost.fillna(pd.to_numeric(df[col], errors="coerce"))
        if cost.isna().all():
            return []
        out = []
        below = df.index[cost.notna() & (cost > 0) & (cost < rule["min_work_cost"])]
        out += self._district_summary(
            rule, df.loc[below], cost.loc[below], df,
            lambda n, share, grp: (
                f"{n:,} of this district authority's {len(grp):,} sanctioned works "
                f"({share:.0%}) are below the Rs {rule['min_work_cost']:,.0f} a work should "
                f"normally reach (para 3.2.9). Smaller works are allowed when the sanction "
                f"letter records the reasons — check that they do."),
            population=df[df.get("ia_name").notna()] if "ia_name" in df else df)
        for idx in df.index[cost.notna() & (cost > 0)]:
            c = float(cost.loc[idx])
            if c < rule["min_work_cost"]:
                out.append(_f(rule, "work", df.loc[idx, "work_id"],
                              f"Sanctioned at Rs {c:,.0f}, below the Rs "
                              f"{rule['min_work_cost']:,.0f} a work should normally "
                              f"reach (para 3.2.9). Allowed when the sanction letter "
                              f"records the reasons — check that it does.",
                              {"cost": c, "floor": rule["min_work_cost"]}))
            elif c > rule["max_work_cost"]:
                out.append(_f(rule, "work", df.loc[idx, "work_id"],
                              f"Sanctioned at Rs {c:,.0f}, above ASTRA's Rs "
                              f"{rule['max_work_cost']:,.0f} marker for checking the "
                              f"detailed estimate. The guidelines set no maximum.",
                              {"cost": c, "ceiling": rule["max_work_cost"],
                               "basis": rule.get("max_basis", "heuristic"), "para": None},
                              severity=rule.get("max_severity", rule["severity"]),
                              clause=text("en", "clause.marker.cost_ceiling")))
        return out

    def _sanction_timeline(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        """Sanction or rejection within 45 days of recommendation, para 3.2.4.

        Work level: recommendations still awaiting sanction beyond the limit.
        District authority level: where sanctions typically take longer. Both
        count from the recommendation date and cannot exclude model code of
        conduct periods (para 3.2.6), so the days are an upper bound.
        """
        if not {"recommended_date", "status"} <= set(works.columns):
            return []
        today = pd.Timestamp(self._today)
        recommended = pd.to_datetime(works["recommended_date"], errors="coerce")
        out: list[Finding] = []

        pending = works["status"].eq(contract.PENDING_STAGE) & works["sanction_date"].isna() \
            if "sanction_date" in works else works["status"].eq(contract.PENDING_STAGE)
        waited = (today - recommended).dt.days
        overdue = works.index[pending & (waited > rule["max_days"])]
        out += self._district_summary(
            rule, works.loc[overdue], waited.loc[overdue], works,
            lambda n, share, grp: (
                f"{n:,} recommendations to this district authority have waited more than "
                f"45 days for sanction or rejection (para 3.2.4), {share:.0%} of those "
                f"pending. Counted from the recommendation date without excluding model "
                f"code of conduct periods, so some delays may be shorter."),
            population=works[pending])
        for idx in overdue:
            d = int(waited.loc[idx])
            out.append(_f(
                rule, "work", works.loc[idx, "work_id"],
                f"Recommended {d} days ago and still awaiting sanction or rejection, "
                f"against 45 days (para 3.2.4). Counted from the recommendation date "
                f"without excluding model code of conduct periods, so the delay may "
                f"be shorter.",
                {"days_waiting": d, "max_days": rule["max_days"],
                 "recommended_date": works.loc[idx, "recommended_date"],
                 "upper_bound": True}))

        pattern = rule.get("district_pattern")
        if pattern and {"sanction_date", "ia_name"} <= set(works.columns):
            done = works[works["sanction_date"].notna() & recommended.notna()].copy()
            done["_days"] = (pd.to_datetime(done["sanction_date"], errors="coerce")
                             - recommended.loc[done.index]).dt.days
            done = done[done["_days"] >= 0]
            for authority, grp in done.groupby("ia_name", dropna=True):
                if len(grp) < pattern["min_sanctioned_works"]:
                    continue
                median = float(grp["_days"].median())
                if median <= pattern["median_days_over"]:
                    continue
                share = float((grp["_days"] > rule["max_days"]).mean())
                out.append(_f(
                    pattern, "agency", authority,
                    f"Half of this district authority's {len(grp):,} sanctions took "
                    f"more than {median:.0f} days from recommendation, and "
                    f"{share:.0%} took longer than the 45 days in para 3.2.4 "
                    f"(model code of conduct periods not excluded).",
                    {"median_days": round(median, 1), "over_limit_share": round(share, 3),
                     "sanctioned_works": int(len(grp)), "max_days": rule["max_days"],
                     "upper_bound": True}))
        return out

    @staticmethod
    def _district_summary(rule: dict, hits: pd.DataFrame, values: pd.Series,
                          works: pd.DataFrame, describe, population: pd.DataFrame) -> list[Finding]:
        """One case per district authority for a common, low-severity deviation.

        `hits` are the works that deviate, `values` the measure behind each,
        `population` the works the share is taken over.
        """
        if "ia_name" not in works.columns or hits.empty:
            return []
        minimum = int(rule.get("district_summary_min_works", 1))
        totals = population.groupby("ia_name").size() if not population.empty else pd.Series(dtype=int)
        out = []
        for authority, grp in hits.groupby("ia_name", dropna=True):
            if len(grp) < minimum:
                continue
            total = int(totals.get(authority, len(grp)))
            share = len(grp) / total if total else 1.0
            sample = values.loc[grp.index].sort_values(ascending=rule["id"] == "R-COST-01")
            out.append(_f(
                rule, "agency", authority, describe(len(grp), share, population[population["ia_name"] == authority]),
                {"district_summary": True, "works": int(len(grp)), "out_of": total,
                 "share": round(share, 3),
                 "example_work_ids": grp.loc[sample.index[:20], "work_id"].astype(str).tolist()}))
        return out

    @staticmethod
    def _recommended(works: pd.DataFrame) -> pd.Series:
        """Each work's recommended amount, for the per-MP yearly limits."""
        return pd.to_numeric(works.get("estimated_cost"), errors="coerce").fillna(
            pd.to_numeric(works.get("sanctioned_amount"), errors="coerce"))

    @staticmethod
    def _in_category(works: pd.DataFrame, category: str) -> pd.Series | None:
        """Works the portal itself classes as `category`; None if the corpus
        carries no portal category at all (the CSV path)."""
        if "work_category" not in works.columns or works["work_category"].isna().all():
            return None
        return (works["work_category"].fillna("").astype(str).str.strip().str.casefold()
                == category.casefold())

    def _repair_cap(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        """Repair and renovation up to Rs 50 lakh a year per MP, para 5.1.9.

        Adds the recommended amounts of repair and renovation works per MP and
        financial year of recommendation (the paragraph limits what an MP
        recommends in a year; `fy` is the sanction year, see data_contract),
        leaving out the pending "NA" record the portal keeps beside a sanctioned
        one. A work counts if the portal classes it as
        "Repair and Renovation", or if its activity type or description says
        repair or renovation: the two disagree often (see data_contract), and
        a repair recorded under another category is still a repair. A year
        over the cap only once works of the second kind are added is reported
        at `described_only_severity`, and every finding says how many works
        each way counted.
        """
        if "mp_name" not in works.columns or not {"fy", "recommended_date"} & set(works.columns):
            return []
        terms = _word_regex(rule.get("patterns", []))
        text = (works.get("category", pd.Series("", index=works.index)).fillna("").astype(str)
                + " " + works.get("description", pd.Series("", index=works.index)).fillna("").astype(str))
        described = (text.str.contains(terms) if terms is not None
                     else pd.Series(False, index=works.index))
        portal = self._in_category(works, rule.get("category", contract.REPAIR_CATEGORY))
        classed = portal if portal is not None else pd.Series(False, index=works.index)
        amount = self._recommended(works)
        mask = (classed | described) & contract.counts_toward_totals(works) & amount.notna()
        repairs = works[mask].assign(_amount=amount[mask], _classed=classed[mask])
        if repairs.empty:
            return []
        repairs = repairs.assign(_key=repairs.apply(contract.mp_key, axis=1),
                                 _fy=contract.recommendation_fy(repairs))
        out = []
        for (key, fy), grp in repairs.groupby(["_key", "_fy"], dropna=True):
            total = float(grp["_amount"].sum())
            if total <= rule["yearly_cap"]:
                continue
            details = {"fy": fy, "total": total, "cap": rule["yearly_cap"],
                       "works": int(len(grp)),
                       "work_ids": grp["work_id"].astype(str).tolist()[:20]}
            if portal is None:
                how = "Works were identified from their category or description."
                severity = rule["severity"]
            else:
                in_category = grp[grp["_classed"]]
                category_total = float(in_category["_amount"].sum())
                details.update(portal_category_works=int(len(in_category)),
                               portal_category_total=category_total,
                               described_only_works=int(len(grp) - len(in_category)),
                               described_only_total=total - category_total)
                how = (f"{len(in_category)} of them, worth Rs {category_total:,.0f}, are "
                       f"recorded on the portal as repair and renovation; the other "
                       f"{len(grp) - len(in_category)} are described as repair or "
                       f"renovation under another category.")
                severity = (rule["severity"] if category_total > rule["yearly_cap"]
                            else rule.get("described_only_severity", rule["severity"]))
            out.append(_f(
                rule, "constituency", key,
                f"Repair and renovation works recommended in {fy} add up to "
                f"Rs {total:,.0f} across {len(grp)} works, above the Rs "
                f"{rule['yearly_cap']:,.0f} a year para 5.1.9 allows. {how}",
                details, severity=severity))
        return out

    def _trust_cap(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        """Societies and trusts up to Rs 50 lakh a year per MP, para 6.2.6.2.

        Adds the recommended amounts of the works the portal classes as "Trust
        and Society", per MP and financial year of recommendation (not `fy`,
        which is the sanction year), leaving out
        the pending "NA" record the portal keeps beside a sanctioned one. The
        other limit in the same paragraph, Rs 1 crore a term for any one
        society or trust, needs the society or trust named, which works do not
        do (`data_contract.UNCHECKABLE`).
        """
        in_category = self._in_category(works, rule.get("category", contract.TRUST_CATEGORY))
        if in_category is None:
            self.notes[rule["id"]] = text("en", "stood_down.no_category")
            return []
        if "mp_name" not in works.columns or not {"fy", "recommended_date"} & set(works.columns):
            return []
        amount = self._recommended(works)
        mask = in_category & contract.counts_toward_totals(works) & amount.notna()
        grants = works[mask].assign(_amount=amount[mask])
        if grants.empty:
            return []
        grants = grants.assign(_key=grants.apply(contract.mp_key, axis=1),
                               _fy=contract.recommendation_fy(grants))
        out = []
        for (key, fy), grp in grants.groupby(["_key", "_fy"], dropna=True):
            total = float(grp["_amount"].sum())
            if total <= rule["yearly_cap"]:
                continue
            out.append(_f(
                rule, "constituency", key,
                f"Works for societies and trusts recommended in {fy} add up to "
                f"Rs {total:,.0f} across {len(grp)} works, above the Rs "
                f"{rule['yearly_cap']:,.0f} a year para 6.2.6.2 allows for all "
                f"societies and trusts together. Works were identified from the "
                f"portal's own category.",
                {"fy": fy, "total": total, "cap": rule["yearly_cap"], "works": int(len(grp)),
                 "work_ids": grp["work_id"].astype(str).tolist()[:20]}))
        return out

    # ---------------- constituency-level rules ----------------

    @staticmethod
    def _ckey(r) -> str:
        return contract.mp_key(r)

    def _scst(self, flows: pd.DataFrame, rule: dict) -> list[Finding]:
        """SC/ST AREA allocation minima, para 5.4.1.

        Requires genuine SC/ST area attribution per constituency-year. A source
        that does not itemise area-wise spend leaves these columns null, and the
        rule stands down rather than reading "no data" as "zero spend" - which
        would flag every general constituency in the country.
        """
        need = {"sc_expenditure", "st_expenditure"}
        if not need.issubset(flows.columns) or flows[list(need)].isna().all().all():
            self.notes[rule["id"]] = contract.UNCHECKABLE["5.4.1"]
            return []
        df = flows.dropna(subset=["expenditure"]).copy()
        df = df[df["expenditure"] > 0]
        df = df.dropna(subset=["sc_expenditure", "st_expenditure"], how="all")
        if df.empty:
            self.notes[rule["id"]] = contract.UNCHECKABLE["5.4.1"]
            return []
        out = []
        for (_, r) in df.iterrows():
            exp = float(r["expenditure"])
            sc_raw, st_raw = r.get("sc_expenditure"), r.get("st_expenditure")
            entity = self._ckey(r)
            sc_pct = 100 * float(sc_raw) / exp if pd.notna(sc_raw) else None
            st_pct = 100 * float(st_raw) / exp if pd.notna(st_raw) else None
            if sc_pct is not None and sc_pct < rule["sc_min_pct"]:
                out.append(_f(rule, "constituency", entity,
                              f"SC-area spend {sc_pct:.1f}% of expenditure in {r.get('fy')} — below the 15% minimum",
                              {"fy": r.get("fy"), "sc_pct": round(sc_pct, 1), "min": rule["sc_min_pct"]}))
            if st_pct is not None and st_pct < rule["st_min_pct"]:
                out.append(_f(rule, "constituency", entity,
                              f"ST-area spend {st_pct:.1f}% of expenditure in {r.get('fy')} — below the 7.5% minimum",
                              {"fy": r.get("fy"), "st_pct": round(st_pct, 1), "min": rule["st_min_pct"]}))
        return out

    @staticmethod
    def _is_annual_fy(fy) -> bool:
        """True for an annual financial year ('2024-25'), false for a multi-year
        term aggregate ('2019-24').

        Historical pre-2023 rows are published per Lok Sabha TERM, not per year.
        Mixing a 5-year aggregate into a year-on-year series would invent
        dormancy and spikes that never happened, so time-series rules run only
        on genuine annual rows.
        """
        m = re.match(r"^(\d{4})-(\d{2,4})$", str(fy).strip())
        if not m:
            return False
        start = int(m.group(1))
        tail = m.group(2)
        end = int(tail) if len(tail) == 4 else int(str(start)[:2] + tail)
        return end - start == 1

    def _utilization_series(self, flows: pd.DataFrame) -> pd.DataFrame:
        """Annual utilisation rows fit for a year-on-year comparison.

        Only financial years that have ended, and that carry an entitlement:
        the rows measure spending on works recommended in the year, so a year
        still in progress always looks under-spent (see the note in rules.yaml).
        """
        df = flows.copy()
        entitlement = pd.to_numeric(df.get("released"), errors="coerce")
        if "utilization_pct" not in df.columns or df["utilization_pct"].isna().all():
            exp = pd.to_numeric(df.get("expenditure"), errors="coerce")
            df["utilization_pct"] = 100 * exp / entitlement.where(entitlement > 0)
        df = df.dropna(subset=["utilization_pct", "fy"])
        # negative utilisation is a data artefact of derived figures, not a signal
        df = df[df["utilization_pct"] >= 0]
        if df.empty:
            return df
        # An empty boolean Series would index columns, not rows: use .loc with arrays.
        df = df.loc[df["fy"].map(self._is_annual_fy).astype(bool).to_numpy()]
        if df.empty:
            return df
        return df.loc[df["fy"].map(lambda fy: contract.fy_has_ended(fy, self._today)).astype(bool).to_numpy()]

    def _utilization_spike(self, flows: pd.DataFrame, rule: dict) -> list[Finding]:
        """MPLADS funds are NON-LAPSABLE: the risk signal is a sudden terminal
        spike after dormant years, not high spending per se."""
        df = self._utilization_series(flows)
        needed = rule["min_dormant_years"] + 1
        years = df["fy"].nunique() if not df.empty else 0
        if years < needed:
            self.notes[rule["id"]] = text("en", "stood_down.years", needed=needed, years=years)
            return []
        out = []
        for key, grp in df.groupby(df.apply(self._ckey, axis=1)):
            grp = grp.sort_values("fy")
            u = grp["utilization_pct"].astype(float).tolist()
            fys = grp["fy"].tolist()
            if len(u) < needed:
                continue
            for i in range(rule["min_dormant_years"], len(u)):
                dormant = u[i - rule["min_dormant_years"]:i]
                if all(x < rule["low_util_pct"] for x in dormant) and \
                   u[i] >= rule["spike_util_pct"] and \
                   u[i] >= rule["spike_ratio"] * (sum(dormant) / len(dormant) or 1):
                    out.append(_f(
                        rule, "constituency", key,
                        f"Utilisation of the entitlement rose to {u[i]:.0f}% for works "
                        f"recommended in {fys[i]}, after {rule['min_dormant_years']}+ years "
                        f"averaging {sum(dormant)/len(dormant):.0f}% — an end-loading "
                        f"pattern under a non-lapsable fund regime",
                        {"spike_fy": fys[i], "spike_pct": round(u[i], 1),
                         "dormant_years": fys[i - rule["min_dormant_years"]:i],
                         "dormant_avg_pct": round(sum(dormant) / len(dormant), 1)},
                    ))
                    break
        return out

    def _pileup(self, flows: pd.DataFrame, rule: dict) -> list[Finding]:
        df = self._utilization_series(flows)
        years = df["fy"].nunique() if not df.empty else 0
        if years < rule["min_years"]:
            self.notes[rule["id"]] = text("en", "stood_down.years", needed=rule["min_years"],
                                          years=years)
            return []
        out = []
        for key, grp in df.groupby(df.apply(self._ckey, axis=1)):
            if len(grp) < rule["min_years"]:
                continue
            rel = pd.to_numeric(grp.get("released"), errors="coerce").sum()
            exp = pd.to_numeric(grp.get("expenditure"), errors="coerce").sum()
            if rel and rel > 0:
                cum = 100 * exp / rel
                if cum < rule["max_cum_util_pct"]:
                    out.append(_f(
                        rule, "constituency", key,
                        f"Spending on works recommended over {len(grp)} completed years is "
                        f"only {cum:.0f}% of the Rs {rel:,.0f} entitlement for those years — "
                        f"funds carried forward rather than used",
                        {"cum_util_pct": round(cum, 1), "released_total": float(rel),
                         "entitlement_total": float(rel),
                         "spent_total": float(exp), "years": len(grp)},
                    ))
        return out
