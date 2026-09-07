"""End-to-end pipeline: load canonical tables -> orchestrate agents -> save flags."""
from __future__ import annotations

import pandas as pd

from . import db
from .agents.orchestrator import Orchestrator


def run_pipeline(verbose: bool = True) -> dict:
    works = db.read_df("works")
    flows = db.read_df("fundflows")
    orch = Orchestrator()
    flags = orch.run(works, flows)
    n = db.save_flags(flags)
    summary = {
        "works": len(works),
        "fundflows": len(flows),
        "flags": n,
        "alerts": sum(1 for f in flags if f.alert),
        "router_trace": orch.route_trace,
    }
    if verbose:
        print(f"[ASTRA] works={summary['works']} fundflows={summary['fundflows']} "
              f"-> flags={n} (alerts={summary['alerts']})")
        for t in orch.route_trace:
            mark = "->" if t["dispatched"] else "xx"
            print(f"  {mark} {t['agent']}: {t.get('findings', 0) if t['dispatched'] else t['reason']}")
    return summary


if __name__ == "__main__":
    run_pipeline()
