"""Payment Agent — what the individual payment records show.

Reads every payment record the portal lists (`db.payments`, passed in the
analysis context as `payments`) against its work's sanction and completion
dates. Deterministic checks; each finding is a prompt to look at the payment
orders, never a conclusion about them.

Checks
------
  P-SEQ-01   a payment dated before the work's sanction. Payments are made
             against sanctioned works, so one of the two dates is wrong. None
             on 16 Sep 2026; kept so that a first one is seen.
  P-LATE-01  payments made long after the work was recorded as complete.
             Para 11.2 expects the implementing agency to finalise a completed
             work's accounts quickly. A final bill or a withheld amount paid
             later is an ordinary explanation, so this is low severity.
  P-DUP-01   the same amount paid to the same vendor on the same day more than
             once on one work. The portal gives a payment no id and lists each
             record, so these may be separate equal invoices (a brick supplier
             paid per load) or one payment recorded twice; only the payment
             orders tell which.

What is deliberately NOT a check
--------------------------------
Several vendors on one work (4,241 works on 16 Sep 2026) is how materials and
labour are bought. Payments in the last week of March are shown on the
timeline, but there is no year-end rush to find nationally: funds do not lapse
(para 10.2.3), and the busiest week of payments so far was in June 2026.
"""
from __future__ import annotations

import pandas as pd

from ..config import load_rules
from ..locale import text
from ..schemas import Finding
from .base import BaseAgent

#: the last week of the financial year: 25 to 31 March
YEAR_END = (3, 25)


def mark(payments: pd.DataFrame, works: pd.DataFrame) -> pd.DataFrame:
    """Each payment with what its dates say against its work's record.

    Adds `sanction_date` and `completion_date` from the work, and:
      days_after_completion  days after the recorded completion, if paid after it
      before_sanction        dated before the work's sanction
      year_end_week          dated 25 to 31 March
      repeats                records on the work with the same date, amount and
                             vendor, this one included
    """
    p = payments.copy()
    dates = [c for c in ("sanction_date", "completion_date") if c in works.columns]
    if not works.empty and "work_id" in works.columns:
        p = p.merge(works[["work_id", *dates]].drop_duplicates("work_id"),
                    on="work_id", how="left")
    for col in ("sanction_date", "completion_date"):
        if col not in p.columns:
            p[col] = None
    paid = pd.to_datetime(p["paid_on"], errors="coerce")
    after = (paid - pd.to_datetime(p["completion_date"], errors="coerce")).dt.days
    p["days_after_completion"] = after.where(after > 0)
    p["before_sanction"] = (paid < pd.to_datetime(p["sanction_date"], errors="coerce")).fillna(False)
    p["year_end_week"] = (paid.dt.month == YEAR_END[0]) & (paid.dt.day >= YEAR_END[1])
    if p.empty:
        p["repeats"] = pd.Series(dtype=int)
    else:
        p["repeats"] = (p.groupby(["work_id", "paid_on", "amount", "vendor_id"], dropna=False)
                        ["work_id"].transform("size"))
    return p


def _rs(value) -> str:
    return f"Rs {float(value or 0):,.0f}"


class PaymentAgent(BaseAgent):
    name = "payments"
    needs_works = {"work_id"}

    def __init__(self) -> None:
        self.context: dict = {}
        self.notes: dict[str, str] = {}

    def applicable(self, works: pd.DataFrame, flows: pd.DataFrame) -> bool:
        return not works.empty

    def run(self, works: pd.DataFrame, flows: pd.DataFrame) -> list[Finding]:
        cfg = load_rules().get("payments") or {}
        self.notes = {}
        payments = self.context.get("payments")
        if not isinstance(payments, pd.DataFrame) or payments.empty:
            for rule in cfg.values():
                if isinstance(rule, dict) and rule.get("id"):
                    self.notes[rule["id"]] = text("en", "stood_down.no_payments")
            return []
        marked = mark(payments[payments["work_id"].isin(works["work_id"])], works)
        paid = marked.groupby("work_id")["amount"].sum()
        out: list[Finding] = []
        out += self._before_sanction(marked, cfg.get("before_sanction"))
        out += self._after_completion(marked, paid, cfg.get("after_completion"))
        out += self._repeated(marked, paid, cfg.get("repeated"))
        return out

    @staticmethod
    def _finding(rule: dict, work_id: str, summary: str, details: dict,
                 severity: str | None = None) -> Finding:
        return Finding(agent="payments", rule_id=rule["id"], rule_title=rule["title"],
                       clause=rule.get("clause"), severity=severity or rule["severity"],
                       entity_type="work", entity_id=str(work_id), summary=summary,
                       details={"basis": rule.get("basis"), "para": rule.get("para"), **details})

    @staticmethod
    def _records(grp: pd.DataFrame, limit: int = 10) -> list[dict]:
        cols = ["paid_on", "amount", "vendor_name", "vendor_id", "status",
                "days_after_completion"]
        rows = grp.sort_values(["paid_on", "amount"])[cols].head(limit)
        return [{k: (None if pd.isna(v) else v) for k, v in r.items()}
                for r in rows.to_dict("records")]

    # ------------------------------------------------------------------ rules
    def _before_sanction(self, marked: pd.DataFrame, rule: dict | None) -> list[Finding]:
        if not rule:
            return []
        out = []
        for work_id, grp in marked[marked["before_sanction"]].groupby("work_id"):
            amount = float(grp["amount"].sum())
            sanction = grp["sanction_date"].iloc[0]
            earliest = grp["paid_on"].min()
            out.append(self._finding(
                rule, work_id,
                f"{len(grp)} payment(s) worth {_rs(amount)} are dated before the work was "
                f"sanctioned on {sanction}, the earliest on {earliest}. Payments are made "
                f"against sanctioned works, so one of these dates is wrong on the portal.",
                {"sanction_date": sanction, "payments": int(len(grp)), "amount": amount,
                 "earliest": earliest, "records": self._records(grp)}))
        return out

    def _after_completion(self, marked: pd.DataFrame, paid: pd.Series,
                          rule: dict | None) -> list[Finding]:
        if not rule:
            return []
        late = marked[marked["days_after_completion"] > rule["min_days"]]
        out = []
        for work_id, grp in late.groupby("work_id"):
            amount = float(grp["amount"].sum())
            total = float(paid.get(work_id) or 0.0)
            share = amount / total if total else None
            latest = int(grp["days_after_completion"].max())
            completed = grp["completion_date"].iloc[0]
            out.append(self._finding(
                rule, work_id,
                f"{len(grp)} payment(s) worth {_rs(amount)} were made more than "
                f"{rule['min_days']} days after the work was recorded as completed on "
                f"{completed}, the latest {latest} days after"
                + (f" ({share:.0%} of what was paid on the work)" if share is not None else "")
                + ". Para 11.2 expects a completed work's accounts to be finalised quickly; a "
                  "final bill or a withheld amount paid later is one ordinary explanation.",
                {"completion_date": completed, "late_payments": int(len(grp)),
                 "late_amount": amount, "paid_total": total,
                 "share_of_paid": round(share, 3) if share is not None else None,
                 "max_days": latest, "min_days": rule["min_days"],
                 "records": self._records(grp)}))
        return out

    def _repeated(self, marked: pd.DataFrame, paid: pd.Series,
                  rule: dict | None) -> list[Finding]:
        if not rule:
            return []
        out = []
        for work_id, grp in marked[marked["repeats"] > 1].groupby("work_id"):
            sets = (grp.groupby(["paid_on", "amount", "vendor_id", "vendor_name"], dropna=False)
                    .size().reset_index(name="times"))
            extra = int((sets["times"] - 1).sum())
            repeated = float(((sets["times"] - 1) * sets["amount"]).sum())
            total = float(paid.get(work_id) or 0.0)
            share = repeated / total if total else None
            top = sets.sort_values(["amount", "times"], ascending=False).iloc[0]
            severity = (rule.get("material_severity", rule["severity"])
                        if repeated >= rule.get("material_amount", float("inf"))
                        else rule["severity"])
            out.append(self._finding(
                rule, work_id,
                f"{len(sets)} payment(s) on this work are recorded more than once with the same "
                f"date, amount and vendor: {extra} extra record(s) worth {_rs(repeated)}"
                + (f" ({share:.0%} of what was paid on the work)" if share is not None else "")
                + f", for example {_rs(top['amount'])} to {top['vendor_name']} on {top['paid_on']}, "
                  f"recorded {int(top['times'])} times. Equal invoices on one day are possible, so "
                  f"the payment orders decide whether any is a duplicate.",
                {"sets": int(len(sets)), "extra_records": extra, "repeated_amount": repeated,
                 "paid_total": total,
                 "share_of_paid": round(share, 3) if share is not None else None,
                 "largest": {"paid_on": top["paid_on"], "amount": float(top["amount"]),
                             "vendor_name": top["vendor_name"],
                             "vendor_id": None if pd.isna(top["vendor_id"]) else top["vendor_id"],
                             "times": int(top["times"])},
                 "records": self._records(grp)},
                severity=severity))
        return out
