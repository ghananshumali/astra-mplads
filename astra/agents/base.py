"""Agent contract for ASTRA's multi-agent pipeline.

Each agent:
  - declares `needs` — the dataframe columns it requires to be useful, so the
    orchestrator can ROUTE around agents whose inputs aren't present in the
    current data batch (dynamic routing, not a fixed pipeline);
  - consumes pandas DataFrames (works / fundflows);
  - emits a list of Finding objects (evidence), never verdicts.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from ..schemas import Finding


class BaseAgent(ABC):
    name: str = "base"
    #: columns required on works / fundflows for this agent to be applicable
    needs_works: set[str] = set()
    needs_flows: set[str] = set()

    def applicable(self, works: pd.DataFrame, flows: pd.DataFrame) -> bool:
        """Routing predicate: can this agent do useful work on this batch?"""
        def has(df: pd.DataFrame, cols: set[str]) -> bool:
            return (not cols) or (
                not df.empty and all(c in df.columns and df[c].notna().any() for c in cols)
            )
        w_ok = has(works, self.needs_works)
        f_ok = has(flows, self.needs_flows)
        if self.needs_works and self.needs_flows:
            return w_ok or f_ok
        return (w_ok if self.needs_works else True) and (f_ok if self.needs_flows else True)

    @abstractmethod
    def run(self, works: pd.DataFrame, flows: pd.DataFrame) -> list[Finding]:
        ...
