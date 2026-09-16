"""Revision Agent — what changed on a work after it was sanctioned.

Reads the edits the poller observed on the portal (`work_versions`) and the
works the portal stopped listing (`retired_works`). Deterministic rules, plus
one frequency indicator that stands down until there is enough history.

Why a separate module
---------------------
The other modules judge a work as it stands. Some risk only shows as a change:
a work's description or site edited after sanction (not allowed, para 3.2.15),
an amount revised, the vendor on existing payments swapped, payments reversed,
or a paid work that disappears from the portal's lists.

What is NOT a revision
----------------------
Works move forward: stages advance, payments arrive, a completion date is
filled in, a first vendor appears. Those fields (`data_contract.PROGRESS_FIELDS`)
are never reported, and a field going from empty to a value is never a
revision. Whitespace or letter-case edits are ignored.

Limits, stated in every finding
-------------------------------
The portal publishes no history, so only edits made since polling began are
visible; `observed_at` is when the poller saw an edit, up to about a day after
it was made; and `vendor_name` is the vendor paid the most, so a second vendor
overtaking the first also reads as a change (reported separately from a change
with no new payment).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .. import data_contract as contract
from ..config import load_rules
from ..locale import text
from ..schemas import Finding
from .base import BaseAgent

_SPACES = re.compile(r"\s+")


def _norm(value) -> str | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    text = _SPACES.sub(" ", str(value)).strip()
    return text.casefold() or None


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _day(value) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


class RevisionAgent(BaseAgent):
    name = "revisions"
    needs_works = {"work_id"}

    def __init__(self, today: date | None = None) -> None:
        self.today = today
        self.context: dict = {}
        self.notes: dict[str, str] = {}

    def applicable(self, works: pd.DataFrame, flows: pd.DataFrame) -> bool:
        return not works.empty

    # ------------------------------------------------------------------ run
    def run(self, works: pd.DataFrame, flows: pd.DataFrame) -> list[Finding]:
        cfg = load_rules().get("revisions") or {}
        self.notes = {}
        versions = self.context.get("versions")
        retired = self.context.get("retired")
        versions = versions if isinstance(versions, pd.DataFrame) else pd.DataFrame()
        retired = retired if isinstance(retired, pd.DataFrame) else pd.DataFrame()
        if versions.empty and retired.empty:
            reason = text("en", "stood_down.no_edits")
            for rule in cfg.values():
                if isinstance(rule, dict) and rule.get("id"):
                    self.notes[rule["id"]] = reason
            return []

        records = self._work_records(works, retired)
        out: list[Finding] = []
        if not versions.empty:
            v = versions.copy()
            v["work_id"] = v["work_id"].astype(str)
            v = v[v["field"] != "listing"]
            out += self._place_changes(v, records, cfg.get("post_sanction_change"))
            out += self._amount_changes(v, records, cfg.get("amount_revision"))
            out += self._agency_changes(v, records, cfg.get("agency_change"))
            out += self._vendor_changes(v, records, cfg.get("vendor_switch"))
            out += self._payment_reversals(v, records, cfg.get("payment_reversal"))
            out += self._frequency(v, records, cfg.get("revision_frequency"))
        out += self._removed_after_payment(retired, cfg.get("removed_after_payment"))
        return out

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _work_records(works: pd.DataFrame, retired: pd.DataFrame) -> dict[str, dict]:
        cols = [c for c in ("work_id", "sanction_date", "category", "state", "status",
                            "sanctioned_amount", "total_paid", "description")
                if c in works.columns]
        records = {str(r["work_id"]): r for r in works[cols].to_dict("records")} \
            if not works.empty else {}
        if not retired.empty and "record_json" in retired.columns:
            for row in retired.to_dict("records"):
                try:
                    record = json.loads(row.get("record_json") or "{}")
                except ValueError:
                    record = {}
                records.setdefault(str(row["work_id"]), record)
        return records

    @staticmethod
    def _finding(rule: dict, work_id: str, summary: str, details: dict,
                 severity: str | None = None) -> Finding:
        details = {"basis": rule.get("basis"), "para": rule.get("para"), **details,
                   "history_note": "Only edits observed since polling began are visible; "
                                   "observed times can lag the portal edit by up to a day."}
        return Finding(agent="revisions", rule_id=rule["id"], rule_title=rule["title"],
                       clause=rule.get("clause"), severity=severity or rule["severity"],
                       entity_type="work", entity_id=str(work_id), summary=summary,
                       details=details)

    @staticmethod
    def _sanctioned_before(record: dict | None, observed_at: str,
                           same_observation: pd.DataFrame) -> bool:
        """Was the work already sanctioned when this edit happened?

        Not if the sanction date itself was set in the same observation: the
        edit then coincides with sanction, which para 3.2.15 does not bar.
        """
        sanctioned = _day((record or {}).get("sanction_date"))
        seen = _day(observed_at)
        if sanctioned is None or seen is None or sanctioned >= seen:
            return False
        set_now = same_observation[(same_observation["field"] == "sanction_date")
                                   & same_observation["old_value"].isna()]
        return set_now.empty

    @staticmethod
    def _changed(frame: pd.DataFrame) -> pd.DataFrame:
        """Edits of a value to a different value: not empty-to-value, not cosmetic."""
        old = frame["old_value"].map(_norm)
        new = frame["new_value"].map(_norm)
        return frame[old.notna() & new.notna() & (old != new)]

    # --------------------------------------------------------------- rules
    def _place_changes(self, v: pd.DataFrame, records: dict, rule: dict | None) -> list[Finding]:
        if not rule:
            return []
        edits = self._changed(v[v["field"].isin(rule.get("fields", contract.PLACE_FIELDS))])
        if edits.empty:
            return []
        # A category relabelled on many works at once is the portal renaming a
        # work type, not a change to any one work.
        cat = edits[edits["field"].isin(["category", "work_type"])]
        bulk = cat.groupby(["old_value", "new_value"])["work_id"].nunique()
        renamed = {pair for pair, n in bulk.items() if n >= 50}
        out = []
        for (work_id, observed_at), grp in edits.groupby(["work_id", "observed_at"]):
            grp = grp[~grp.apply(lambda r: (r["field"] in ("category", "work_type"))
                                 and (r["old_value"], r["new_value"]) in renamed, axis=1)]
            if grp.empty:
                continue
            same = v[(v["work_id"] == work_id) & (v["observed_at"] == observed_at)]
            record = records.get(work_id)
            if not self._sanctioned_before(record, observed_at, same):
                continue
            changes = [{"field": r["field"], "old": str(r["old_value"])[:160],
                        "new": str(r["new_value"])[:160]} for r in grp.to_dict("records")]
            fields = ", ".join(c["field"] for c in changes)
            out.append(self._finding(
                rule, work_id,
                f"The work's {fields} changed on the portal after sanction "
                f"(sanctioned {record.get('sanction_date')}, change seen {observed_at[:10]}). "
                f"Para 3.2.15 allows no change to a sanctioned work or its site.",
                {"changes": changes, "observed_at": observed_at,
                 "sanction_date": record.get("sanction_date")}))
        return out

    def _amount_changes(self, v: pd.DataFrame, records: dict, rule: dict | None) -> list[Finding]:
        if not rule:
            return []
        edits = v[v["field"].isin(contract.AMOUNT_FIELDS)]
        out = []
        for (work_id, observed_at), grp in edits.groupby(["work_id", "observed_at"]):
            row = grp[grp["field"] == "sanctioned_amount"]
            row = row if not row.empty else grp
            r = row.iloc[0]
            old, new = _number(r["old_value"]), _number(r["new_value"])
            if not old or new is None or old <= 0:
                continue
            same = v[(v["work_id"] == work_id) & (v["observed_at"] == observed_at)]
            record = records.get(work_id)
            if not self._sanctioned_before(record, observed_at, same):
                continue
            delta = new - old
            pct = 100 * delta / old
            if abs(pct) < rule["min_pct"] or abs(delta) < rule["min_abs"]:
                continue
            seen = _day(observed_at)
            year_end = date(seen.year if seen.month <= 3 else seen.year + 1, 3, 31) if seen else None
            near_year_end = bool(seen and year_end and 0 <= (year_end - seen).days <= rule.get("year_end_days", 15))
            upward = delta > 0
            out.append(self._finding(
                rule, work_id,
                f"The work's amount was revised {'up' if upward else 'down'} by "
                f"Rs {abs(delta):,.0f} ({pct:+.0f}%), from Rs {old:,.0f} to Rs {new:,.0f}, "
                f"after sanction (change seen {observed_at[:10]})"
                + (", in the last days of the financial year" if near_year_end else "")
                + ". Check the revised estimate and, for an increase, the MP's consent (para 3.2.3).",
                {"old_amount": old, "new_amount": new, "change": delta,
                 "pct_change": round(pct, 1), "observed_at": observed_at,
                 "near_year_end": near_year_end, "sanction_date": (record or {}).get("sanction_date")},
                severity=rule["severity"] if upward else rule.get("downward_severity", "low")))
        return out

    def _agency_changes(self, v: pd.DataFrame, records: dict, rule: dict | None) -> list[Finding]:
        if not rule:
            return []
        out = []
        for r in self._changed(v[v["field"] == "ia_name"]).to_dict("records"):
            same = v[(v["work_id"] == r["work_id"]) & (v["observed_at"] == r["observed_at"])]
            record = records.get(r["work_id"])
            if not self._sanctioned_before(record, r["observed_at"], same):
                continue
            out.append(self._finding(
                rule, r["work_id"],
                f"The district authority executing this work changed after sanction, from "
                f"{r['old_value']} to {r['new_value']} (seen {r['observed_at'][:10]}).",
                {"old": r["old_value"], "new": r["new_value"], "observed_at": r["observed_at"]}))
        return out

    def _vendor_changes(self, v: pd.DataFrame, records: dict, rule: dict | None) -> list[Finding]:
        if not rule:
            return []
        out = []
        for r in self._changed(v[v["field"] == "vendor_name"]).to_dict("records"):
            same = v[(v["work_id"] == r["work_id"]) & (v["observed_at"] == r["observed_at"])]
            counts = same[same["field"] == "payment_count"]
            new_payment = any((_number(c["new_value"]) or 0) > (_number(c["old_value"]) or 0)
                              for c in counts.to_dict("records"))
            if new_payment:
                summary = (f"A newly paid vendor, {r['new_value']}, became the largest payee on "
                           f"this work in place of {r['old_value']} (seen {r['observed_at'][:10]}).")
                severity = rule["severity"]
            else:
                summary = (f"The vendor recorded against this work's existing payments changed from "
                           f"{r['old_value']} to {r['new_value']} with no new payment "
                           f"(seen {r['observed_at'][:10]}).")
                severity = rule.get("relabel_severity", rule["severity"])
            out.append(self._finding(
                rule, r["work_id"], summary,
                {"old": r["old_value"], "new": r["new_value"], "observed_at": r["observed_at"],
                 "with_new_payment": new_payment},
                severity=severity))
        return out

    def _payment_reversals(self, v: pd.DataFrame, records: dict, rule: dict | None) -> list[Finding]:
        if not rule:
            return []
        out = []
        for r in v[v["field"] == "total_paid"].to_dict("records"):
            old, new = _number(r["old_value"]), _number(r["new_value"])
            if old is None or new is None or old - new < rule.get("min_abs", 0) or new >= old:
                continue
            out.append(self._finding(
                rule, r["work_id"],
                f"The total paid on this work fell from Rs {old:,.0f} to Rs {new:,.0f} "
                f"(seen {r['observed_at'][:10]}). Payments go directly to vendors (para 10.7.1), "
                f"so a reduction means a payment record was reversed or removed.",
                {"old_total_paid": old, "new_total_paid": new, "reduction": old - new,
                 "observed_at": r["observed_at"]}))
        return out

    def _removed_after_payment(self, retired: pd.DataFrame, rule: dict | None) -> list[Finding]:
        if not rule or retired.empty:
            return []
        out = []
        for row in retired.to_dict("records"):
            try:
                record = json.loads(row.get("record_json") or "{}")
            except ValueError:
                continue
            paid = _number(record.get("total_paid")) or 0.0
            if paid <= 0:
                continue
            out.append(self._finding(
                rule, row["work_id"],
                f"This work had Rs {paid:,.0f} paid against it and is no longer listed on "
                f"the portal (confirmed {str(row.get('retired_at'))[:10]}). A stopped or "
                f"abandoned work should be referred to the State Nodal Authority (para 3.2.19).",
                {"total_paid": paid, "retired_at": row.get("retired_at"),
                 "first_missing_at": row.get("first_missing_at"),
                 "last_record": {k: record.get(k) for k in
                                 ("state", "district", "constituency", "mp_name", "description",
                                  "status", "sanctioned_amount")}}))
        return out

    def _frequency(self, v: pd.DataFrame, records: dict, rule: dict | None) -> list[Finding]:
        if not rule:
            return []
        revisions = v[~v["field"].isin(contract.PROGRESS_FIELDS)]
        revisions = self._changed(revisions)
        observed = pd.to_datetime(v["observed_at"], errors="coerce", utc=True)
        span = (observed.max() - observed.min()).days if observed.notna().any() else 0
        revised = revisions["work_id"].nunique()
        if span < rule["min_history_days"] or revised < rule["min_revised_works"]:
            self.notes[rule["id"]] = text("en", "stood_down.history", span=span, revised=revised,
                                          days=rule["min_history_days"],
                                          works=rule["min_revised_works"])
            return []
        per_work = revisions.groupby("work_id")["observed_at"].nunique()
        frame = pd.DataFrame({"work_id": per_work.index, "revisions": per_work.values})
        frame["peer"] = frame["work_id"].map(
            lambda w: f"{(records.get(w) or {}).get('state')}|{(records.get(w) or {}).get('category')}")
        out = []
        for peer, grp in frame.groupby("peer"):
            typical = float(grp["revisions"].mean())
            for r in grp.to_dict("records"):
                if r["revisions"] < rule["min_revisions"] or r["revisions"] < rule["z_threshold"] * typical:
                    continue
                out.append(self._finding(
                    rule, r["work_id"],
                    f"This work was revised on {r['revisions']} separate occasions; revised "
                    f"works of the same type in the state average {typical:.1f}.",
                    {"revisions": int(r["revisions"]), "peer_average": round(typical, 2),
                     "peer_group": peer, "history_days": span}))
        return out
