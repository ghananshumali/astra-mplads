"""Duplicate detection: portal record pairs are labelled, real duplicates are not.

    python tests/test_duplicates.py

The portal keeps a work's original recommendation (stage "NA") listed after the
work is sanctioned under a new id and code, and lists that sanctioned record
only from the sanctioned report onward, so one work appears as two records.
These checks pin down that exactly that pair is reported as a portal record
pair — visible, low severity, no weight in the risk score — while two separate
recommendations of one work, and every other near-duplicate, keep full severity.
Offline, on a synthetic corpus; nothing is read from or written to the real
database.
"""
from __future__ import annotations

import os
import random
import shutil
import string
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="astra-dup-"))
os.environ["ASTRA_DB_PATH"] = str(_TMP / "scratch.db")
os.environ["ASTRA_PROCESSED_DIR"] = str(_TMP / "processed")

import pandas as pd                                              # noqa: E402

from astra.agents.entity_resolution import (                     # noqa: E402
    PORTAL_RECORD_PAIR, EntityResolutionAgent, portal_record_pair)
from astra.agents.orchestrator import Orchestrator               # noqa: E402
from astra.explain import build_brief                            # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def work(work_id, description, *, mp="SHRI A", date="2025-01-10", status="Sanction",
         estimate=800000.0, sanctioned=800000.0, in_recommended=1):
    return {"work_id": work_id, "description": description, "state": "UTTAR PRADESH",
            "in_recommended": in_recommended,
            "district": "SITAPUR", "constituency": "SITAPUR", "era": "post2023",
            "mp_name": mp, "recommended_date": date, "status": status,
            "estimated_cost": estimate, "sanctioned_amount": sanctioned,
            "house": "LS", "category": "Community hall", "work_type": "Community hall",
            "ia_name": "SITAPUR IDA", "vendor_name": None, "lat": None, "lon": None}


def corpus() -> pd.DataFrame:
    rng = random.Random(7)

    def word():
        return "".join(rng.choice(string.ascii_lowercase) for _ in range(8))

    rows = [work(f"WS/MP900/2024-2025/{i}",
                 f"Repair of {word()} {word()} {word()} lane near {word()}")
            for i in range(1, 240)]
    hall = "Construction of community hall near Hanuman temple Rampura village Sitapur block"
    borewell = "Borewell installation at Kishanpur primary school Hardoi tehsil campus"
    tank = "Water tank construction beside Ganeshganj panchayat bhawan Laharpur road"
    gate = "Entrance gate for Sarvodaya inter college Mahmudabad kasba compound"
    library = "Library building at Stariya block Dungarpur tehsil headquarters"
    wall = "Boundary wall for Kshetra panchayat Ghazipur Saidpur cremation ground"
    rows += [
        # a portal record pair: pending recommendation + its sanctioned record,
        # which the portal lists only from the sanctioned report onward
        work("ES-LS-5001", hall, status="NA", sanctioned=None),
        work("WS/MP100/2024-2025/9001", hall, in_recommended=0),
        # two separate recommendations of one work: one sanctioned, one still
        # pending, both in the recommended report (180 such pairs, 13 Sep 2026)
        work("ES-LS-5004", library, status="Pending for Sanction"),
        work("WS/MP100/2025-2026/9401", library),
        # a pending "NA" row beside a sanctioned record that IS recommended
        work("ES-LS-5005", wall, status="NA", sanctioned=None),
        work("WS/MP100/2025-2026/9501", wall),
        # a genuine double entry: two sanctioned records of one work
        work("WS/MP100/2024-2025/9101", borewell, sanctioned=300000.0, estimate=300000.0),
        work("WS/MP100/2024-2025/9102", borewell, sanctioned=300000.0, estimate=300000.0),
        # pending + sanctioned, but recommended on different dates
        work("ES-LS-5002", tank, status="NA", sanctioned=None, date="2025-02-01"),
        work("WS/MP100/2024-2025/9201", tank, date="2025-06-15", in_recommended=0),
        # pending + sanctioned, but by different members
        work("ES-LS-5003", gate, status="NA", sanctioned=None, mp="SHRI B"),
        work("WS/MP101/2024-2025/9301", gate, mp="SHRI C", in_recommended=0),
    ]
    return pd.DataFrame(rows)


def pair_findings(findings, a, b):
    return [f for f in findings if f.rule_id == "D-DUP-01"
            and {f.entity_id, f.details.get("pair_work_id")} == {a, b}]


def main() -> int:
    print("=" * 74)
    print("ASTRA duplicate detection — portal record pairs")
    print("=" * 74)
    try:
        works = corpus()
        findings = EntityResolutionAgent().run(works, pd.DataFrame())

        print("\n[1] one work, two portal records")
        twin = pair_findings(findings, "ES-LS-5001", "WS/MP100/2024-2025/9001")
        check("the pair is still found and shown on both works", len(twin) == 2,
              f"{len(twin)} findings")
        check("it is labelled a portal record pair, not a double entry",
              all(f.details.get("portal_record_pair")
                  and f.details.get("duplication_mode") == PORTAL_RECORD_PAIR for f in twin),
              twin and twin[0].details.get("duplication_mode"))
        check("at low severity", all(f.severity == "low" for f in twin),
              str({f.severity for f in twin}))
        check("naming which record is the recommendation and which the sanction",
              all(f.details.get("pending_work_id") == "ES-LS-5001"
                  and f.details.get("sanctioned_work_id") == "WS/MP100/2024-2025/9001"
                  for f in twin))
        check("and saying so in the summary",
              all("original recommendation ES-LS-5001" in f.summary
                  and "adds nothing to the risk score" in f.summary for f in twin),
              twin and twin[0].summary[:120])

        print("\n[2] everything else is unchanged")
        real = pair_findings(findings, "WS/MP100/2024-2025/9101", "WS/MP100/2024-2025/9102")
        check("a genuine double entry keeps high severity and its mode",
              len(real) == 2 and all(f.severity == "high"
                                     and not f.details.get("portal_record_pair")
                                     and f.details["duplication_mode"]
                                     == "identical recommendation double-entry" for f in real),
              str([(f.severity, f.details.get("duplication_mode")) for f in real]))
        dated = pair_findings(findings, "ES-LS-5002", "WS/MP100/2024-2025/9201")
        check("a pending and a sanctioned record recommended on different dates is not relabelled",
              dated and not any(f.details.get("portal_record_pair") for f in dated),
              str([f.severity for f in dated]))
        members = pair_findings(findings, "ES-LS-5003", "WS/MP101/2024-2025/9301")
        check("nor one whose records name different members",
              members and not any(f.details.get("portal_record_pair") for f in members)
              and all("cross-MP" in f.details["duplication_mode"] for f in members),
              str([f.details.get("duplication_mode") for f in members]))
        double = pair_findings(findings, "ES-LS-5004", "WS/MP100/2025-2026/9401")
        check("two separate recommendations (one pending for sanction) stay a real duplicate",
              double and all(f.severity == "high" and not f.details.get("portal_record_pair")
                             for f in double),
              str([(f.severity, f.details.get("duplication_mode")) for f in double]))
        recommended = pair_findings(findings, "ES-LS-5005", "WS/MP100/2025-2026/9501")
        check("an NA row beside a sanctioned record that is itself recommended is not relabelled",
              recommended and not any(f.details.get("portal_record_pair") for f in recommended))
        blind = EntityResolutionAgent().run(works.drop(columns=["in_recommended"]), pd.DataFrame())
        check("without listing data nothing is relabelled",
              not any(f.details.get("portal_record_pair") for f in blind)
              and pair_findings(blind, "ES-LS-5001", "WS/MP100/2024-2025/9001"))
        rows = works.set_index("work_id")
        check("the pair test itself refuses two pending or two sanctioned records",
              portal_record_pair(rows.loc["WS/MP100/2024-2025/9101"].to_frame().T.reset_index()
                                 .rename(columns={"index": "work_id"}).iloc[0],
                                 rows.loc["WS/MP100/2024-2025/9102"].to_frame().T.reset_index()
                                 .rename(columns={"index": "work_id"}).iloc[0]) is None)

        print("\n[3] risk score and plain-language brief")
        flags = {f.entity_id: f for f in Orchestrator()._aggregate(findings, works)}
        pending_flag = flags.get("ES-LS-5001")
        check("a work whose only finding is a portal record pair scores zero",
              pending_flag is not None and pending_flag.risk_score == 0
              and not pending_flag.alert,
              pending_flag and f"score {pending_flag.risk_score}")
        dup_flag = flags.get("WS/MP100/2024-2025/9101")
        check("a genuine duplicate still scores its weight",
              dup_flag is not None and dup_flag.risk_score == 25,
              dup_flag and f"score {dup_flag.risk_score}")
        brief = build_brief({"findings": [f.model_dump() for f in twin[:1]]}, "district", {})
        signal = brief["signals"][0] if brief.get("signals") else {}
        check("the brief explains it as the portal's own record-keeping",
              signal.get("headline", "").startswith("Listed twice on the portal")
              and "probably one work" in signal.get("plain", ""),
              signal.get("headline"))
        check("and shows no contribution to the score", signal.get("contribution") == 0,
              str(signal.get("contribution")))
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print("\n" + "=" * 74)
    failed = [r for r in results if r[0] == FAIL]
    print(f"  {len(results) - len(failed)} passed, {len(failed)} failed")
    for _, name, detail in failed:
        print(f"    FAILED: {name} — {detail}")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
