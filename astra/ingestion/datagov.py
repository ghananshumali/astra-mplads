"""data.gov.in Open Government Data API client.

API shape: https://api.data.gov.in/resource/<RESOURCE_ID>
           ?api-key=<KEY>&format=json&limit=<n>&offset=<n>

Uses the public sample key by default (override with DATAGOV_API_KEY env var
— free registration at data.gov.in issues personal keys). Responses are
cached under data/raw/ so the demo can run offline after one fetch.
"""
from __future__ import annotations

import json
import os
import time

import pandas as pd
import requests

from ..config import RAW_DIR

BASE = "https://api.data.gov.in/resource/{rid}"
PUBLIC_SAMPLE_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"
PAGE = 1000


def api_key() -> str:
    return os.environ.get("DATAGOV_API_KEY", PUBLIC_SAMPLE_KEY)


def fetch_resource(resource_id: str, max_records: int = 50000,
                   force: bool = False) -> pd.DataFrame:
    """Fetch a full resource with pagination; cache as JSON lines in data/raw."""
    cache = RAW_DIR / f"datagov_{resource_id}.json"
    if cache.exists() and not force:
        with open(cache, "r", encoding="utf-8") as fh:
            return pd.DataFrame(json.load(fh))

    records: list[dict] = []
    offset = 0
    while offset < max_records:
        r = requests.get(
            BASE.format(rid=resource_id),
            params={"api-key": api_key(), "format": "json",
                    "limit": min(PAGE, max_records - offset), "offset": offset},
            timeout=60,
        )
        r.raise_for_status()
        payload = r.json()
        batch = payload.get("records", [])
        records.extend(batch)
        total = int(payload.get("total", 0) or 0)
        offset += len(batch)
        if not batch or (total and offset >= total):
            break
        time.sleep(0.4)  # be polite to the public API

    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(records, fh)
    return pd.DataFrame(records)
