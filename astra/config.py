"""Central config loader: rules.yaml + paths + era handling."""
from __future__ import annotations

import functools
import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "rules.yaml"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"

#: Overridable so tests never touch the real corpus. `tests/test_system.py`
#: re-ingests from the CSV exports, which DELETEs `works` — pointing that at
#: data/astra.db would destroy anything the live poller has collected.
DB_PATH = Path(os.environ.get("ASTRA_DB_PATH") or (DATA_DIR / "astra.db"))

#: Redirected alongside the database, so a test run cannot leave its own
#: ingest_meta.json / run_meta.json behind for the dashboard to read.
PROCESSED_DIR = Path(os.environ.get("ASTRA_PROCESSED_DIR")
                     or (DB_PATH.parent / "processed" if os.environ.get("ASTRA_DB_PATH")
                         else DATA_DIR / "processed"))

#: Raw per-shard response cache written by the live ingestion path.
SHARD_CACHE_DIR = Path(os.environ.get("ASTRA_SHARD_CACHE") or (RAW_DIR / "shards"))

for _d in (RAW_DIR, PROCESSED_DIR, DB_PATH.parent):
    _d.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def load_rules() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


GUIDELINES_PATH = PROJECT_ROOT / "config" / "guidelines_2023.yaml"


@functools.lru_cache(maxsize=1)
def load_guidelines() -> dict:
    """The index of MPLADS Guidelines 2023 paragraphs the rules cite."""
    with open(GUIDELINES_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def cite(para: str | None, lead: str = "MPLADS Guidelines 2023") -> str | None:
    """"MPLADS Guidelines 2023, para 5.2.11: works of a religious nature..." """
    if not para:
        return None
    entry = (load_guidelines().get("paras") or {}).get(str(para)) or {}
    summary = entry.get("summary")
    return f"{lead}, para {para}: {summary}" if summary else f"{lead}, para {para}"


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
