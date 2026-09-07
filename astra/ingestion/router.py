"""Dual-mode Ingestion Router.

                 Real-time official data available?
                              |
                   +----------+----------+
                  YES                    NO / SLOW / PARTIAL
                   |                      |
            Live ingestion          Offline official CSVs
              (live.py)                (offline.py)
                   +----------+----------+
                              |
                    ONE canonical schema
                              |
                  existing agents -> orchestrator
                              |
                          dashboard

Modes
-----
auto     (default) Probe the live official interfaces under a strict time
         budget, then select the most COMPLETE authentic work corpus available:
         the official CSV exports when present (a full national corpus),
         otherwise whatever the live interfaces return. Live sources are always
         used for freshness and for the pre-2023 baseline. The pipeline can
         never be blocked by a slow or down government endpoint.

         Rationale: the live work-level interface is paginated/state-scoped, so
         a real-time pull yields a bounded sample, while the official exports
         are the complete national record. Analysing the complete corpus and
         proving its currency against the live portal beats analysing a
         fraction of it.
live     Live sources only. Fails loudly if they cannot supply a work corpus —
         used to prove the real-time path works.
offline  Official CSV exports only. Deterministic, fast, demo-safe.

The two modes differ ONLY in where rows come from. Both emit the same canonical
`works` / `fundflows` frames, so every downstream component is untouched.

Live enrichment applies in every mode that can reach the network, because it
adds something the CSVs cannot:
  * eSAKSHI live national totals -> freshness check on the offline batch
  * CKAN pre-2023 records        -> the historical-era baseline that makes the
                                    era-separated statistics real
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pandas as pd

from .. import db
from ..config import PROCESSED_DIR
from ..schemas import FundFlow, Work
from . import live as live_mod
from . import offline as offline_mod

MODES = ("auto", "live", "offline")
#: a live work corpus smaller than this is treated as insufficient in auto mode
MIN_LIVE_WORKS = int(os.environ.get("ASTRA_MIN_LIVE_WORKS", "200"))


def _conform(df: pd.DataFrame, model) -> pd.DataFrame:
    """Project any frame onto the canonical column set of a schema model."""
    cols = list(model.model_fields)
    if df.empty:
        return pd.DataFrame(columns=cols)
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = None
    return out[cols]


def ingest(mode: str = "auto", verbose: bool = True,
           enrich: bool = True) -> dict:
    """Resolve a data source per the mode and load the canonical tables."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")

    provenance: list[dict] = []
    works = pd.DataFrame()
    flows = pd.DataFrame()
    tiles: dict = {}
    resolved = None

    def log(msg: str) -> None:
        if verbose:
            print(msg)

    # ------------------------------------------------- live availability probe
    have_offline = offline_mod.datasets_available()
    if mode in ("auto", "live"):
        log("[router] probing live official interfaces...")
        tiles, tprov = live_mod.fetch_esakshi_tiles()
        provenance.append(tprov)
        log(f"[router]   eSAKSHI portal: {tprov['status']}"
            + (f" ({tiles.get('Current Tenure')})" if tiles else ""))

    # ------------------------------------------------------- corpus selection
    # auto prefers the complete official export over a bounded live sample, and
    # falls through to live the moment those exports are missing.
    use_live_corpus = mode == "live" or (mode == "auto" and not have_offline)

    if use_live_corpus:
        log("[router] pulling work records from live interfaces...")
        live_works, wprov = live_mod.fetch_mirror_works()
        provenance.extend(wprov)
        log(f"[router]   live work records: {len(live_works):,}")
        if len(live_works) >= MIN_LIVE_WORKS:
            works = live_works
            resolved = "live"
        elif mode == "live":
            raise RuntimeError(
                f"live mode: only {len(live_works)} work records retrieved "
                f"(need >= {MIN_LIVE_WORKS}). Re-run with mode='auto' to fall "
                f"back to the official CSV exports in datasets/."
            )
        else:
            log(f"[router]   live corpus insufficient ({len(live_works)} < "
                f"{MIN_LIVE_WORKS}) -> falling back to offline official exports")

    if works.empty:
        if not have_offline:
            raise RuntimeError(
                "No data source available: live ingestion did not return a work "
                "corpus and no official CSV exports were found in datasets/."
            )
        log("[router] loading official CSV exports (datasets/)...")
        off = offline_mod.ingest_offline(verbose=verbose)
        works, flows = off["works"], off["fundflows"]
        provenance.extend(off["provenance"])
        resolved = "offline"
        if mode == "auto":
            provenance.append({
                "source": "router decision", "mode": "auto", "table": "works",
                "rows": len(works), "status": "selected",
                "detail": ("official CSV exports selected: complete national corpus; "
                           "the live work-level interface is state-scoped and returns "
                           "only a bounded sample. Live portal still used for "
                           "freshness and the pre-2023 baseline."),
                "fetched_at": datetime.now(timezone.utc).isoformat()})

    # The live mirror carries no fund positions; derive them from the official
    # exports when those are on disk.
    if flows.empty and have_offline:
        flows = offline_mod.build_fundflows(
            works if resolved == "offline" else offline_mod.build_works())

    # -------------------------------------------------------- live enrichment
    freshness = None
    if enrich and mode != "offline":
        hist, hprov = live_mod.fetch_ckan_fundflows()
        provenance.extend(hprov)
        if not hist.empty:
            log(f"[router]   pre-2023 historical fund rows: {len(hist):,} "
                f"(era-separated baseline)")
            parts = [f for f in (_conform(flows, FundFlow), _conform(hist, FundFlow))
                     if not f.empty]
            flows = pd.concat(parts, ignore_index=True) if parts else flows
        freshness = live_mod.freshness_check(works, tiles)
        if freshness:
            log(f"[router]   freshness vs live portal: {freshness['coverage_pct']}% "
                f"({freshness['local_recommended_works']:,} local / "
                f"{freshness['live_recommended_works']:,} live)")

    # ------------------------------------------------------------- persist
    works = _conform(works, Work)
    flows = _conform(flows, FundFlow)
    n_w = db.replace_df("works", works)
    n_f = db.replace_df("fundflows", flows)
    db.record_provenance(provenance)

    meta = {
        "mode_requested": mode,
        "mode_resolved": resolved,
        "works": n_w,
        "fundflows": n_f,
        "eras": works["era"].value_counts().to_dict() if not works.empty else {},
        "flow_eras": flows["era"].value_counts().to_dict() if not flows.empty else {},
        "live_tiles": tiles,
        "freshness": freshness,
        "provenance": provenance,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROCESSED_DIR / "ingest_meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)

    log(f"[router] resolved mode = {resolved.upper()} -> "
        f"works={n_w:,} fundflows={n_f:,}")
    return meta
