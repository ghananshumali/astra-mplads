"""Ingest real MPLADS data into data/astra.db via the dual-mode router.

  python scripts/fetch_data.py                 # auto  (default)
  python scripts/fetch_data.py --mode live     # live official/open interfaces only
  python scripts/fetch_data.py --mode offline  # official CSV exports in datasets/
  python scripts/fetch_data.py --no-enrich     # skip live enrichment entirely

auto never blocks: it probes the live official interfaces, uses the most
complete authentic corpus available, and falls back to the official CSV
exports whenever live ingestion is slow, partial, or unavailable.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astra.ingestion.router import ingest


def main() -> None:
    ap = argparse.ArgumentParser(description="ASTRA dual-mode MPLADS ingestion")
    ap.add_argument("--mode", choices=("auto", "live", "offline"), default="auto")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip live freshness check and pre-2023 baseline pull")
    args = ap.parse_args()

    meta = ingest(mode=args.mode, enrich=not args.no_enrich)

    print("\n[ASTRA] ingestion summary")
    print(f"  mode requested : {meta['mode_requested']}")
    print(f"  mode resolved  : {meta['mode_resolved']}")
    print(f"  works          : {meta['works']:,}   eras={meta['eras']}")
    print(f"  fundflows      : {meta['fundflows']:,}   eras={meta['flow_eras']}")
    if meta.get("freshness"):
        f = meta["freshness"]
        print(f"  freshness      : {f['coverage_pct']}% of the live eSAKSHI portal's "
              f"{f['live_recommended_works']:,} recommended works ({f['tenure']})")
    print("  sources:")
    for p in meta["provenance"]:
        print(f"    [{p['status']:<12}] {p['source'][:60]:<60} rows={p['rows']:,}")


if __name__ == "__main__":
    main()
