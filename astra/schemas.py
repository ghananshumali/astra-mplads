"""Canonical data schemas.

Two core entities regardless of source (data.gov.in, eSAKSHI, dataful):

- Work: one recommended/sanctioned work item (most granular; not every
  source provides every field — agents route on field availability).
- FundFlow: constituency x financial-year fund release/expenditure row
  (drives utilization-spike and pile-up rules).

- Finding: one agent's single piece of evidence about an entity.
- Flag: orchestrator-composed case = entity + findings + risk score +
  causal narrative + per-tier framings. Always carries review_status.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high", "critical"]
EntityType = Literal["work", "constituency", "agency"]
ReviewStatus = Literal["pending", "under_review", "confirmed", "false_positive"]


class Work(BaseModel):
    work_id: str
    source: str = "unknown"
    era: str = "unknown"                     # pre2023 | post2023 | unknown
    state: Optional[str] = None
    district: Optional[str] = None
    constituency: Optional[str] = None
    mp_name: Optional[str] = None
    house: Optional[str] = None              # LS / RS
    category: Optional[str] = None
    description: Optional[str] = None
    recommended_date: Optional[str] = None
    sanction_date: Optional[str] = None
    completion_date: Optional[str] = None
    status: Optional[str] = None
    estimated_cost: Optional[float] = None
    sanctioned_amount: Optional[float] = None
    expenditure: Optional[float] = None
    ia_name: Optional[str] = None            # implementing agency (IDA)
    vendor_name: Optional[str] = None        # contractor/vendor paid for the work
    work_type: Optional[str] = None          # standardized MPLADS work-type string
    total_paid: Optional[float] = None       # sum of disbursements to date
    payment_count: Optional[int] = None      # number of disbursement tranches
    last_payment_date: Optional[str] = None
    payment_status: Optional[str] = None
    is_sc_constituency: Optional[int] = None # constituency reserved for SC
    is_st_constituency: Optional[int] = None # constituency reserved for ST
    lat: Optional[float] = None
    lon: Optional[float] = None
    fy: Optional[str] = None


class FundFlow(BaseModel):
    row_id: str
    source: str = "unknown"
    era: str = "unknown"
    state: Optional[str] = None
    constituency: Optional[str] = None
    mp_name: Optional[str] = None
    house: Optional[str] = None
    fy: Optional[str] = None
    entitlement: Optional[float] = None
    released: Optional[float] = None
    expenditure: Optional[float] = None
    utilization_pct: Optional[float] = None
    sc_expenditure: Optional[float] = None   # spend in SC areas (for R-SCST-01)
    st_expenditure: Optional[float] = None   # spend in ST areas
    recommended: Optional[float] = None      # amount recommended by the MP
    sanctioned: Optional[float] = None       # amount sanctioned by the DA
    works_count: Optional[int] = None


class Finding(BaseModel):
    """A single piece of evidence emitted by one agent."""
    agent: str                               # compliance | anomaly | entity_resolution | network
    rule_id: str                             # e.g. R-SCST-01, A-COST-01
    rule_title: str
    clause: Optional[str] = None             # guideline clause cited (compliance agent)
    severity: Severity
    entity_type: EntityType
    entity_id: str
    summary: str                             # one-sentence plain-language evidence
    details: dict[str, Any] = Field(default_factory=dict)


class Flag(BaseModel):
    """Orchestrator output: one flagged case for human review."""
    flag_id: str
    entity_type: EntityType
    entity_id: str
    entity_label: str
    state: Optional[str] = None
    district: Optional[str] = None
    constituency: Optional[str] = None
    era: str = "unknown"
    risk_score: float                        # 0-100 composite
    alert: bool                              # crossed review threshold
    findings: list[Finding]
    narrative: str                           # causal chain, plain language
    tier_views: dict[str, str] = Field(default_factory=dict)  # mp/district/state/ministry
    # presentation layer (see astra/explain.py) - additive, never replaces the above
    display_title: Optional[str] = None      # short readable case title
    primary_signal: Optional[str] = None     # one-line reason, for list views
    tier_briefs: dict[str, Any] = Field(default_factory=dict)  # structured per tier
    review_status: ReviewStatus = "pending"
    reviewer_note: Optional[str] = None
    created_at: Optional[str] = None
