"""Compliance Agent — deterministic MPLADS guideline rule engine (no ML).

Every finding cites the specific guideline clause it enforces and the exact
threshold crossed. Rules and thresholds live in config/rules.yaml.

Work-level rules   : one-year completion norm, prohibited categories,
                     per-work cost floor/ceiling.
Constituency rules : SC/ST allocation minima, non-lapsable-fund utilization
                     spike, chronic under-utilization pile-up.
"""
from __future__ import annotations

import re

import pandas as pd

from ..config import load_rules
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
       details: dict, severity: str | None = None) -> Finding:
    return Finding(
        agent="compliance",
        rule_id=rule["id"],
        rule_title=rule["title"],
        clause=rule.get("clause"),
        severity=severity or rule["severity"],
        entity_type=entity_type,
        entity_id=str(entity_id),
        summary=summary,
        details=details,
    )


class ComplianceAgent(BaseAgent):
    name = "compliance"
    needs_works = {"work_id"}
    needs_flows = {"fy"}

    def run(self, works: pd.DataFrame, flows: pd.DataFrame) -> list[Finding]:
        cfg = load_rules()["rules"]
        out: list[Finding] = []
        if not works.empty:
            out += self._completion_norm(works, cfg["completion_norm"])
            out += self._prohibited(works, cfg["prohibited_works"])
            out += self._cost_ceiling(works, cfg["cost_ceiling"])
        if not flows.empty:
            out += self._scst(flows, cfg["scst_allocation"])
            out += self._utilization_spike(flows, cfg["utilization_spike"])
            out += self._pileup(flows, cfg["unspent_pileup"])
        if not works.empty and "is_sc_constituency" in works.columns:
            out += self._scst_reserved_proxy(works, cfg["scst_reserved_proxy"])
        return out

    def _scst_reserved_proxy(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        """State-level share of MPLADS spend reaching SC/ST-reserved seats.

        Screening indicator only: the guideline obligation in para 2.8 is about
        SC/ST AREAS within a constituency, which the published extracts do not
        expose. A low share here tells the State Nodal Authority where to pull
        the district ledgers, and nothing more.
        """
        df = works.copy()
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
                    f"para 2.8 governs SC/ST areas, which these extracts do not "
                    f"itemise — pull district-level allocation to assess compliance.",
                    {"state": state, "kind": kind, "pct": round(pct, 2),
                     "benchmark_pct": minimum, "reserved_seats": int(seats),
                     "state_expenditure": total,
                     "proxy": "reserved-constituency share, not DA-level area spend"},
                ))
        return out

    # ---------------- work-level rules ----------------

    def _completion_norm(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        if "sanction_date" not in works.columns:
            return []
        df = works.dropna(subset=["sanction_date"]).copy()
        if df.empty:
            return []
        sanction = pd.to_datetime(df["sanction_date"], errors="coerce")
        completion = pd.to_datetime(df.get("completion_date"), errors="coerce")
        today = pd.Timestamp.today()
        end = completion.fillna(today)
        days = (end - sanction).dt.days
        incomplete = completion.isna() & df.get("status", pd.Series(index=df.index, dtype=object)) \
            .astype(str).str.lower().ne("completed")
        out = []
        for idx in df.index[(days > rule["max_days"]) & sanction.notna()]:
            d = int(days.loc[idx])
            over_outer = d > rule["outer_limit_days"]
            still_open = bool(incomplete.loc[idx]) if idx in incomplete.index else False
            state = "still incomplete" if still_open else "completed late"
            out.append(_f(
                rule, "work", df.loc[idx, "work_id"],
                f"Work {state}: {d} days since sanction vs the 1-year completion norm"
                + (" (beyond the 18-month outer limit)" if over_outer else ""),
                {"days_elapsed": d, "max_days": rule["max_days"],
                 "outer_limit_days": rule["outer_limit_days"], "incomplete": still_open},
                severity=rule["overdue_severity"] if over_outer else rule["severity"],
            ))
        return out

    def _prohibited(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        """Negative-list check that asks WHAT IS BEING BUILT.

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

        asset_pats = rule.get("asset_patterns", [])
        activity_pats = rule.get("activity_patterns", [])
        permissible = {a.lower() for a in rule.get("permissible_assets", [])}
        asset_re = re.compile("|".join(re.escape(a) for a in asset_pats), re.IGNORECASE) \
            if asset_pats else None
        activity_re = re.compile("|".join(re.escape(a) for a in activity_pats), re.IGNORECASE) \
            if activity_pats else None

        out: list[Finding] = []
        for idx, raw in desc.items():
            text = raw.strip()
            if not text:
                continue

            # --- activity patterns: about the nature of the expenditure
            if activity_re:
                am = activity_re.search(text)
                if am:
                    out.append(_f(
                        rule, "work", works.loc[idx, "work_id"],
                        f"Expenditure appears to be of a non-permissible nature: "
                        f"'{am.group(0)}'. Operation and maintenance is the state's "
                        f"responsibility and is not an admissible MPLADS charge - "
                        f"confirm the scope before release.",
                        {"matched_pattern": am.group(0), "match_type": "activity",
                         "text": text[:200]},
                        severity=rule.get("activity_severity", "medium"),
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

            out.append(_f(
                rule, "work", works.loc[idx, "work_id"],
                f"The asset being created appears to be a negative-list item: "
                f"'{hm.group(0)}' heads the work description "
                f"(\"{head[:60]}\"). Non-permissible category under the scheme - "
                f"confirm the asset's nature before sanction or release.",
                {"matched_pattern": hm.group(0), "match_type": "asset",
                 "head_asset_phrase": head[:120], "text": text[:200]},
            ))
        return out

    def _cost_ceiling(self, works: pd.DataFrame, rule: dict) -> list[Finding]:
        cost = pd.Series(float("nan"), index=works.index, dtype="float64")
        for col in ("sanctioned_amount", "estimated_cost"):
            if col in works.columns:
                cost = cost.fillna(pd.to_numeric(works[col], errors="coerce"))
        if cost.isna().all():
            return []
        out = []
        for idx in works.index[cost.notna() & (cost > 0)]:
            c = float(cost.loc[idx])
            if c < rule["min_work_cost"]:
                out.append(_f(rule, "work", works.loc[idx, "work_id"],
                              f"Sanctioned cost Rs {c:,.0f} is below the "
                              f"Rs {rule['min_work_cost']:,.0f} per-work minimum permitted "
                              f"under the scheme",
                              {"cost": c, "floor": rule["min_work_cost"]}))
            elif c > rule["max_work_cost"]:
                out.append(_f(rule, "work", works.loc[idx, "work_id"],
                              f"Sanctioned cost Rs {c:,.0f} exceeds the "
                              f"Rs {rule['max_work_cost']:,.0f} single-work review ceiling",
                              {"cost": c, "ceiling": rule["max_work_cost"]}))
        return out

    # ---------------- constituency-level rules ----------------

    @staticmethod
    def _ckey(r) -> str:
        return f"{r.get('constituency') or r.get('mp_name') or 'NA'}|{r.get('state') or 'NA'}"

    def _scst(self, flows: pd.DataFrame, rule: dict) -> list[Finding]:
        """SC/ST AREA allocation minima.

        Requires genuine SC/ST area attribution per constituency-year. A source
        that does not itemise area-wise spend leaves these columns null, and the
        rule stands down rather than reading "no data" as "zero spend" - which
        would flag every general constituency in the country.
        """
        need = {"sc_expenditure", "st_expenditure"}
        if not need.issubset(flows.columns):
            return []
        df = flows.dropna(subset=["expenditure"]).copy()
        df = df[df["expenditure"] > 0]
        df = df.dropna(subset=["sc_expenditure", "st_expenditure"], how="all")
        if df.empty:
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
        df = flows.copy()
        if "utilization_pct" not in df.columns or df["utilization_pct"].isna().all():
            rel = pd.to_numeric(df.get("released"), errors="coerce")
            exp = pd.to_numeric(df.get("expenditure"), errors="coerce")
            df["utilization_pct"] = 100 * exp / rel.where(rel > 0)
        df = df.dropna(subset=["utilization_pct", "fy"])
        # negative utilisation is a data artefact of derived figures, not a signal
        df = df[df["utilization_pct"] >= 0]
        return df[df["fy"].map(self._is_annual_fy)]

    def _utilization_spike(self, flows: pd.DataFrame, rule: dict) -> list[Finding]:
        """MPLADS funds are NON-LAPSABLE: the risk signal is a sudden terminal
        spike after dormant years, not high spending per se."""
        df = self._utilization_series(flows)
        out = []
        for key, grp in df.groupby(df.apply(self._ckey, axis=1)):
            grp = grp.sort_values("fy")
            u = grp["utilization_pct"].astype(float).tolist()
            fys = grp["fy"].tolist()
            if len(u) < rule["min_dormant_years"] + 1:
                continue
            for i in range(rule["min_dormant_years"], len(u)):
                dormant = u[i - rule["min_dormant_years"]:i]
                if all(x < rule["low_util_pct"] for x in dormant) and \
                   u[i] >= rule["spike_util_pct"] and \
                   u[i] >= rule["spike_ratio"] * (sum(dormant) / len(dormant) or 1):
                    out.append(_f(
                        rule, "constituency", key,
                        f"Utilization spiked to {u[i]:.0f}% in {fys[i]} after "
                        f"{rule['min_dormant_years']}+ dormant years averaging "
                        f"{sum(dormant)/len(dormant):.0f}% — a classic end-loading "
                        f"pattern under a non-lapsable fund regime",
                        {"spike_fy": fys[i], "spike_pct": round(u[i], 1),
                         "dormant_years": fys[i - rule["min_dormant_years"]:i],
                         "dormant_avg_pct": round(sum(dormant) / len(dormant), 1)},
                    ))
                    break
        return out

    def _pileup(self, flows: pd.DataFrame, rule: dict) -> list[Finding]:
        df = self._utilization_series(flows)
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
                        f"Cumulative utilization only {cum:.0f}% of Rs {rel:,.0f} released "
                        f"across {len(grp)} years — funds idling while entitlements accrue",
                        {"cum_util_pct": round(cum, 1), "released_total": float(rel),
                         "spent_total": float(exp), "years": len(grp)},
                    ))
        return out
