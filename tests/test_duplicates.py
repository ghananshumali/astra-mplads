"""Duplicate detection: the text finds candidates, the record decides.

    python tests/test_duplicates.py

The portal keeps a work's original recommendation (stage "NA") listed after the
work is sanctioned under a new id and code, and lists that sanctioned record
only from the sanctioned report onward, so one work appears as two records.
That pair is reported as a portal record pair — visible, low severity, no
weight in the risk score.

Every other text match is read against the record: a batch of identical works
from one member is reported once per work and held; a detail that separates
two works (different numbers or place names in the descriptions, amounts far
apart, different panchayats paid) clears the pair; a pair nothing separates is
held — visible, low severity, outside the risk score — until evidence decides.
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
    BATCH, LOCAL_BODY, PORTAL_RECORD_PAIR, VILLAGE_BODY, EntityResolutionAgent, portal_record_pair)
from astra.agents.orchestrator import Orchestrator               # noqa: E402
from astra.explain import build_brief                            # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def work(work_id, description, *, mp="SHRI A", date="2025-01-10", status="Sanction",
         estimate=800000.0, sanctioned=800000.0, in_recommended=1, letter="LN/MP100/2024-2025/1"):
    return {"work_id": work_id, "description": description, "state": "UTTAR PRADESH",
            "in_recommended": in_recommended,
            "district": "SITAPUR", "constituency": "SITAPUR", "era": "post2023",
            "mp_name": mp, "recommended_date": date, "status": status,
            "estimated_cost": estimate, "sanctioned_amount": sanctioned, "letter_no": letter,
            "house": "LS", "category": "Community hall", "work_type": "Community hall",
            "ia_name": "SITAPUR IDA", "vendor_name": None, "lat": None, "lon": None}


BATCH_TEXT = "Solar high mast light installation at public places Sidhauli Kamlapur area"


def corpus() -> tuple[pd.DataFrame, pd.DataFrame]:
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
    shelter = "Bus shelter construction near Tilhar crossing Mishrikh tehsil market"
    tanker = "Drinking water tanker purchase for Laharpur tehsil villages supply scheme"
    cooler = "Supply of {} units water cooler Sitapur district hospital general ward"
    pump = "Hand pump installation at {} primary school Machhrehta block campus"
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
        # two sanctioned records of one work: nothing in the record separates them
        work("WS/MP100/2024-2025/9101", borewell, sanctioned=300000.0, estimate=300000.0),
        work("WS/MP100/2024-2025/9102", borewell, sanctioned=300000.0, estimate=300000.0,
             letter="LN/MP100/2024-2025/2"),
        # pending + sanctioned, but recommended on different dates
        work("ES-LS-5002", tank, status="NA", sanctioned=None, date="2025-02-01"),
        work("WS/MP100/2024-2025/9201", tank, date="2025-06-15", in_recommended=0),
        # pending + sanctioned, but by different members
        work("ES-LS-5003", gate, status="NA", sanctioned=None, mp="SHRI B"),
        work("WS/MP101/2024-2025/9301", gate, mp="SHRI C", in_recommended=0),
        # a batch: one member, one exact description, four works
        *[work(f"WS/MP102/2025-2026/96{i:02d}", BATCH_TEXT, mp="SHRI D", sanctioned=450000.0,
               estimate=450000.0, letter="LN/MP102/2025-2026/7") for i in range(1, 5)],
        # separated by amount: Rs 4 lakh and Rs 6 lakh are two works
        work("WS/MP103/2025-2026/9701", shelter, mp="SHRI E", sanctioned=400000.0, estimate=400000.0),
        work("WS/MP103/2025-2026/9702", shelter, mp="SHRI E", sanctioned=600000.0, estimate=600000.0),
        # separated by the panchayats paid
        work("WS/MP104/2025-2026/9801", tanker, mp="SHRI F", sanctioned=350000.0, estimate=350000.0),
        work("WS/MP104/2025-2026/9802", tanker, mp="SHRI F", sanctioned=350000.0, estimate=350000.0),
        # separated by numbers in the descriptions
        work("WS/MP105/2025-2026/9901", cooler.format(2), mp="SHRI G"),
        work("WS/MP105/2025-2026/9902", cooler.format(3), mp="SHRI G"),
        # separated by place names only one of them carries
        work("WS/MP106/2025-2026/9951", pump.format("Barauli"), mp="SHRI H"),
        work("WS/MP106/2025-2026/9952", pump.format("Kasmanda"), mp="SHRI H"),
    ]
    payments = pd.DataFrame([
        {"work_id": "WS/MP102/2025-2026/9601", "vendor_name": "GRAM PANCHAYAT RAMPUR KALAN", "implementing_agency": None},
        {"work_id": "WS/MP102/2025-2026/9602", "vendor_name": "Gram Panchyat Rampur Kalan", "implementing_agency": None},
        {"work_id": "WS/MP102/2025-2026/9603", "vendor_name": "GP Kheri", "implementing_agency": None},
        {"work_id": "WS/MP102/2025-2026/9604", "vendor_name": "M/S Sun Light Traders", "implementing_agency": None},
        {"work_id": "WS/MP104/2025-2026/9801", "vendor_name": "GRAM PANCHAYAT BHADEWARA", "implementing_agency": None},
        {"work_id": "WS/MP104/2025-2026/9802", "vendor_name": "GRAM PANCHAYAT TAMBOUR", "implementing_agency": None},
    ])
    return pd.DataFrame(rows), payments


def pair_findings(findings, a, b):
    return [f for f in findings if f.rule_id == "D-DUP-01"
            and {f.entity_id, f.details.get("pair_work_id")} == {a, b}]


def on(findings, work_id):
    return [f for f in findings if f.rule_id == "D-DUP-01" and f.entity_id == work_id]


def main() -> int:
    print("=" * 74)
    print("ASTRA duplicate detection — the text finds candidates, the record decides")
    print("=" * 74)
    try:
        works, payments = corpus()
        agent = EntityResolutionAgent()
        agent.context = {"payments": payments}
        findings = agent.run(works, pd.DataFrame())

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

        print("\n[2] a pair nothing in the record separates is held, not scored")
        real = pair_findings(findings, "WS/MP100/2024-2025/9101", "WS/MP100/2024-2025/9102")
        check("two identical sanctioned records are held on both works, at low severity",
              len(real) == 2 and all(f.severity == "low" and f.details.get("held")
                                     and not f.details.get("portal_record_pair")
                                     and f.details["duplication_mode"]
                                     == "identical recommendation double-entry" for f in real),
              str([(f.severity, f.details.get("held"), f.details.get("duplication_mode")) for f in real]))
        check("a different letter number does not separate them",
              real and real[0].details.get("this_letter") != real[0].details.get("other_letter"))
        check("and the photos are recorded as not yet compared",
              all(f.details.get("photo_check") == "not_run" for f in real))
        dated = pair_findings(findings, "ES-LS-5002", "WS/MP100/2024-2025/9201")
        check("different recommendation dates do not separate a pair either",
              dated and all(f.details.get("held") for f in dated)
              and not any(f.details.get("portal_record_pair") for f in dated),
              str([f.details.get("held") for f in dated]))
        members = pair_findings(findings, "ES-LS-5003", "WS/MP101/2024-2025/9301")
        check("a pair whose records name different members is held as a cross-member overlap",
              members and all(f.details.get("held") and "cross-MP" in f.details["duplication_mode"]
                              for f in members),
              str([f.details.get("duplication_mode") for f in members]))
        double = pair_findings(findings, "ES-LS-5004", "WS/MP100/2025-2026/9401")
        check("two separate recommendations (one pending for sanction) are held, not relabelled",
              double and all(f.details.get("held") and not f.details.get("portal_record_pair")
                             for f in double))
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

        print("\n[3] a detail that separates two works clears the pair")
        check("amounts 10% or more apart", not pair_findings(
            findings, "WS/MP103/2025-2026/9701", "WS/MP103/2025-2026/9702"))
        check("different gram panchayats paid", not pair_findings(
            findings, "WS/MP104/2025-2026/9801", "WS/MP104/2025-2026/9802"))
        check("different numbers in the descriptions (2 units, 3 units)", not pair_findings(
            findings, "WS/MP105/2025-2026/9901", "WS/MP105/2025-2026/9902"))
        check("place names only one of them carries (Barauli, Kasmanda)", not pair_findings(
            findings, "WS/MP106/2025-2026/9951", "WS/MP106/2025-2026/9952"))
        stats = agent.last_stats
        check("each clearing is counted by its reason",
              stats.get("cleared_by_amount", 0) >= 1 and stats.get("cleared_by_local_body", 0) >= 1
              and stats.get("cleared_by_description", 0) >= 1, str(stats))
        check("payee names are read as local bodies in their common spellings",
              all(LOCAL_BODY.search(n) for n in ("Gram Panchyat Rampur", "GP Kheri", "Janpad Panchayat Sitamau",
                                                 "Block Development and Panchayat Officer Mansa",
                                                 "NAGAR PALIK NIGAM JAGDALPUR", "Zila Parishad, Ranchi"))
              and not any(LOCAL_BODY.search(n) for n in ("Panch Agro Industries", "M/S Sun Light Traders"))
              and VILLAGE_BODY.search("GP Kheri") and not VILLAGE_BODY.search("Zila Parishad, Ranchi"))

        print("\n[4] a batch of identical works is one held item per work")
        batch_ids = [f"WS/MP102/2025-2026/96{i:02d}" for i in range(1, 5)]
        batch = [f for w in batch_ids for f in on(findings, w) if f.details.get("batch")]
        check("each of the four works carries one batch finding", len(batch) == 4
              and all(f.details["batch_size"] == 4 and f.details["duplication_mode"] == BATCH
                      and f.severity == "low" and f.details.get("held") for f in batch),
              str([(f.entity_id, f.details.get("batch_size")) for f in batch]))
        check("no pairwise duplicate is reported inside the batch",
              not any(pair_findings(findings, a, b) for a in batch_ids[2:] for b in batch_ids
                      if a != b))
        check("only the batch's first work opens a case for it",
              [f.details.get("standalone") for f in batch] == [True, False, False, False])
        groups = batch[0].details.get("same_payee_groups") or []
        check("works of the batch paid to one gram panchayat are named, spelling aside",
              len(groups) == 1 and set(groups[0]["work_ids"]) == set(batch_ids[:2]), str(groups))
        same_village = pair_findings(findings, batch_ids[0], batch_ids[1])
        check("and held as a pair to check first",
              len(same_village) == 2 and all(f.details.get("in_batch") and f.details.get("shared_payee")
                                             for f in same_village),
              str([f.details.get("shared_payee") for f in same_village]))

        print("\n[5] risk score and plain-language brief")
        flags = {f.entity_id: f for f in Orchestrator()._aggregate(findings, works)}
        pending_flag = flags.get("ES-LS-5001")
        check("a work whose only finding is a portal record pair scores zero",
              pending_flag is not None and pending_flag.risk_score == 0
              and not pending_flag.alert,
              pending_flag and f"score {pending_flag.risk_score}")
        held_flag = flags.get("WS/MP100/2024-2025/9101")
        check("a held pair stays visible as a case but scores zero and raises no alert",
              held_flag is not None and held_flag.risk_score == 0 and not held_flag.alert,
              held_flag and f"score {held_flag.risk_score}")
        check("a batch opens one case, on its first work",
              batch_ids[0] in flags and not any(w in flags for w in batch_ids[2:]),
              str([w for w in batch_ids if w in flags]))
        brief = build_brief({"findings": [f.model_dump() for f in twin[:1]]}, "district", {})
        signal = brief["signals"][0] if brief.get("signals") else {}
        check("the brief explains a record pair as the portal's own record-keeping",
              signal.get("headline", "").startswith("Listed twice on the portal")
              and "probably one work" in signal.get("plain", ""),
              signal.get("headline"))
        check("and shows no contribution to the score", signal.get("contribution") == 0,
              str(signal.get("contribution")))
        held_brief = build_brief({"findings": [real[0].model_dump()]}, "district", {})
        held_signal = held_brief["signals"][0] if held_brief.get("signals") else {}
        check("a held pair is explained as held, with nothing separating the two",
              held_signal.get("headline", "").startswith("Held:")
              and "held outside the risk score" in held_signal.get("plain", "")
              and held_signal.get("contribution") == 0, held_signal.get("headline"))
        batch_brief = build_brief({"findings": [batch[0].model_dump()]}, "district", {})
        batch_signal = batch_brief["signals"][0] if batch_brief.get("signals") else {}
        check("a batch is explained as identical items, with the shared panchayat to check first",
              batch_signal.get("headline", "").startswith("One of 4 identical works")
              and any("GRAM PANCHAYAT RAMPUR KALAN" in a or "Rampur Kalan" in a
                      for a in batch_signal.get("actions", []))
              and batch_signal.get("contribution") == 0,
              str(batch_signal.get("actions")))
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
