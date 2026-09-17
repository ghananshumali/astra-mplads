"""End-to-end pipeline: load canonical tables -> orchestrate agents -> save flags.

Each run is recorded in `analysis_runs`: when it ran, which stored changes it
includes, the hash of the rules it used, which rules found something or stood
down and why, and what `data_contract.check()` measured about the corpus.
"""
from __future__ import annotations

import hashlib
import subprocess
import time
import uuid
from datetime import datetime, timezone

import pandas as pd

from . import data_contract as contract
from . import db
from .agents.orchestrator import Orchestrator
from .config import CONFIG_PATH, PROJECT_ROOT, load_guidelines

#: `source` of the works and fund positions that come from the eSAKSHI portal.
LIVE_SOURCE = "esakshi_api"


def live_fundflows(works: pd.DataFrame) -> pd.DataFrame | None:
    """Constituency x FY fund positions derived from the live works.

    None for a corpus not read from the portal: the CSV path builds its fund
    positions from the official exports instead, and they are left alone.
    Positions are sums over works, so they go stale the moment a work changes;
    the analysis rebuilds them before every run rather than trusting the ones
    built at switch-over.
    """
    from .ingestion import offline
    from .ingestion.router import _conform
    from .schemas import FundFlow

    if works.empty or "source" not in works:
        return None
    live = works[works["source"] == LIVE_SOURCE]
    if live.empty:
        return None
    # A column with no values at all reads back from SQLite as objects, which
    # the fund-position sums cannot round; make the amounts numeric first.
    live = live.assign(**{c: pd.to_numeric(live[c], errors="coerce")
                          for c in ("estimated_cost", "sanctioned_amount", "expenditure")
                          if c in live.columns})
    flows = offline.build_fundflows(live)
    if not flows.empty:
        houses = (live.assign(_c=live["constituency"].fillna(""))
                  .drop_duplicates(["state", "_c", "mp_name"])
                  .set_index(["state", "_c", "mp_name"])["house"])
        flows["house"] = [houses.get((s, c or "", m))
                          for s, c, m in zip(flows["state"],
                                             flows["constituency"].fillna(""),
                                             flows["mp_name"])]
        flows["source"] = LIVE_SOURCE
    return _conform(flows, FundFlow)


def _code_version() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
                             capture_output=True, text=True, timeout=5)
        head = out.stdout.strip() or None
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "astra", "config"],
                               cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5)
        return f"{head}+uncommitted" if head and dirty.stdout.strip() else head
    except (OSError, subprocess.SubprocessError):
        return None


def _context() -> dict:
    """What the analysis reads besides works and fund flows."""
    return {
        "versions": db.read_df("work_versions"),
        "payments": db.read_df("payments"),
        "retired": db.read_df("retired_works"),
        "area_health": db.area_health(),
        "photos": db.photo_evidence(),
    }


def run_pipeline(verbose: bool = True, *, refresh_flows: bool = True) -> dict:
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    # Taken before the corpus is read: a change stored after this point is
    # newer than the flags written here, so the poller still sees it as due.
    changes_seen = db.get_state("data_changed_at") or ""
    works = db.read_df("works")
    rebuilt = live_fundflows(works) if refresh_flows else None
    if rebuilt is not None:
        # Stored, then read back: the analysis must see the positions exactly
        # as the database holds them. Built in memory, a Rajya Sabha member's
        # missing constituency is NaN, which is truthy, and every member of a
        # state collapsed into one "nan" constituency; stored, it is NULL.
        db.replace_rows("fundflows", rebuilt, where="source = ?", params=(LIVE_SOURCE,))
    flows = db.read_df("fundflows")
    # Which portal report lists each work. Analysis-only: the duplicate check
    # uses it to tell one work's two portal records from a real double entry.
    # Empty for a CSV-built corpus, in which case nothing depends on it.
    # Which portal reports list each work, and its slice. The duplicate check
    # uses `in_recommended` to tell one work's two portal records from a real
    # double entry; completion is read from `in_completed` (see data_contract).
    # Empty for a CSV-built corpus, which falls back to the dates alone.
    listing = db.read_df("work_listing")
    works = contract.enrich(works, listing)
    context = _context()
    facts = contract.check(works, context["payments"])
    orch = Orchestrator()
    flags = orch.run(works, flows, context)
    n = db.save_flags(flags)
    # The duplicate matches held this run: what the photo check looks at next.
    resolver = next((a for a in orch.agents if a.name == "entity_resolution"), None)
    if resolver is not None:
        db.replace_duplicate_groups(getattr(resolver, "last_groups", []))
    finished_at = datetime.now(timezone.utc).isoformat()
    db.set_state("last_analysis_at", finished_at)
    db.set_state("analysis_covers_changes_at", changes_seen)
    summary = {
        "works": len(works),
        "fundflows": len(flows),
        "flags": n,
        "alerts": sum(1 for f in flags if f.alert),
        "seconds": round(time.monotonic() - started, 1),
        "router_trace": orch.route_trace,
    }
    guidelines = load_guidelines().get("source") or {}
    db.record_analysis_run({
        "run_id": uuid.uuid4().hex[:12], "started_at": started_at, "finished_at": finished_at,
        "seconds": summary["seconds"], "works": summary["works"],
        "fundflows": summary["fundflows"], "flags": n, "alerts": summary["alerts"],
        "covers_changes_at": changes_seen or None,
        "rules_sha256": hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest(),
        "guidelines": f"{guidelines.get('title', '')} ({guidelines.get('effective', '')})",
        "code_version": _code_version(),
        "rule_coverage": orch.rule_coverage,
        "contract": facts,
        "summary": {"router_trace": orch.route_trace, "data_confidence": orch.confidence,
                    "unchecked_provisions": contract.UNCHECKABLE},
    })
    if verbose:
        print(f"[ASTRA] works={summary['works']} fundflows={summary['fundflows']} "
              f"-> flags={n} (alerts={summary['alerts']})")
        for t in orch.route_trace:
            mark = "->" if t["dispatched"] else "xx"
            print(f"  {mark} {t['agent']}: {t.get('findings', 0) if t['dispatched'] else t['reason']}")
    return summary


if __name__ == "__main__":
    run_pipeline()
