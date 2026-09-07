"""Central config loader: rules.yaml + paths + era handling."""
from __future__ import annotations

import functools
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "rules.yaml"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
DB_PATH = DATA_DIR / "astra.db"

for _d in (RAW_DIR, PROCESSED_DIR):
    _d.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def load_rules() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def era_of(date_str: str | None) -> str:
    """Tag a record with its data regime relative to the eSAKSHI cutover.

    Pre-2023 physical-mode data and post-2023 real-time data have different
    completeness profiles; blending them into one anomaly baseline would make
    the regime change itself register as an anomaly spike. Every record
    carries this tag and peer groups never straddle it.
    """
    cfg = load_rules()["era"]
    if not date_str:
        return "unknown"
    return "post2023" if str(date_str)[:10] >= cfg["cutoff_date"] else "pre2023"
