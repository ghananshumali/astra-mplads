"""Calibration: reviewers' verdicts on a sample of real findings -> precision per rule.

    python scripts/review_sample.py export                 # writes a CSV to fill in
    python scripts/review_sample.py export --per-rule 40
    python scripts/review_sample.py score review.csv       # precision per rule

Why
---
Planted cases (scripts/evaluate_detectors.py) show whether a detector catches a
pattern. They cannot show how many of its findings on real data are genuine:
only people who check the records can. This draws a sample for them and turns
their verdicts into a measured precision per rule, with an interval, which is
what any threshold change should rest on.

The sample, per rule
--------------------
Half the highest-scoring cases and half drawn at random, so the precision
estimate covers the whole range and not only the obvious cases. Each row
carries the finding, its evidence and the guideline paragraph; the reviewer
fills `verdict` with genuine, not_genuine or cannot_tell, and a note.

Scoring does not change any threshold. It reports precision per rule and names
the rules whose lower bound falls below --target, for a person to decide on.
Reads the database read-only.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astra.config import DB_PATH, PROCESSED_DIR   # noqa: E402

VERDICTS = ("genuine", "not_genuine", "cannot_tell")
FIELDS = ("sample_id", "rule_id", "rule_title", "basis", "para", "severity", "flag_id",
          "entity_type", "entity_id", "case_title", "state", "district", "risk_score",
          "summary", "evidence", "verdict", "reviewer", "note")


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def export(db_path: Path, per_rule: int, out: Path, seed: int) -> Path:
    import random
    rng = random.Random(seed)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    by_rule: dict[str, list[dict]] = {}
    for row in con.execute("SELECT flag_id, entity_type, entity_id, display_title, state, district, "
                           "risk_score, findings_json FROM flags"):
        for finding in json.loads(row["findings_json"] or "[]"):
            details = finding.get("details") or {}
            if details.get("portal_record_pair"):
                continue
            by_rule.setdefault(finding["rule_id"], []).append({
                "rule_id": finding["rule_id"], "rule_title": finding.get("rule_title"),
                "basis": details.get("basis"), "para": details.get("para"),
                "severity": finding.get("severity"), "flag_id": row["flag_id"],
                "entity_type": row["entity_type"], "entity_id": row["entity_id"],
                "case_title": row["display_title"], "state": row["state"],
                "district": row["district"], "risk_score": row["risk_score"],
                "summary": finding.get("summary"),
                "evidence": json.dumps({k: v for k, v in details.items()
                                        if k not in ("history_note", "text")}, default=str)[:900],
            })
    con.close()
    rows = []
    for rule_id in sorted(by_rule):
        items = by_rule[rule_id]
        top = sorted(items, key=lambda r: -(r["risk_score"] or 0))[: per_rule // 2]
        rest = [r for r in items if r not in top]
        chosen = top + rng.sample(rest, min(len(rest), per_rule - len(top)))
        for i, item in enumerate(chosen, 1):
            rows.append({**item, "sample_id": f"{rule_id}-{i:03d}", "verdict": "", "reviewer": "", "note": ""})
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[review] {len(rows):,} findings across {len(by_rule)} rules -> {out}")
    print(f"[review] fill 'verdict' with one of {', '.join(VERDICTS)}")
    return out


def score(path: Path, target: float, out: Path) -> dict:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    unknown = {r["verdict"] for r in rows if r["verdict"] and r["verdict"] not in VERDICTS}
    if unknown:
        raise SystemExit(f"unrecognised verdicts: {sorted(unknown)}; use {', '.join(VERDICTS)}")
    report = {"scored_at": datetime.now(timezone.utc).isoformat(), "source": str(path),
              "target_precision": target, "rules": []}
    for rule_id in sorted({r["rule_id"] for r in rows}):
        mine = [r for r in rows if r["rule_id"] == rule_id]
        genuine = sum(r["verdict"] == "genuine" for r in mine)
        not_genuine = sum(r["verdict"] == "not_genuine" for r in mine)
        decided = genuine + not_genuine
        low, high = wilson(genuine, decided)
        report["rules"].append({
            "rule_id": rule_id, "sampled": len(mine), "reviewed": decided,
            "cannot_tell": sum(r["verdict"] == "cannot_tell" for r in mine),
            "genuine": genuine, "not_genuine": not_genuine,
            "precision": round(genuine / decided, 3) if decided else None,
            "ci95": [round(low, 3), round(high, 3)] if decided else None,
            "needs_review": bool(decided) and low < target,
        })
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{'rule':<12}{'reviewed':>9}{'genuine':>9}{'precision':>11}   95% interval")
    for r in report["rules"]:
        if r["precision"] is None:
            print(f"{r['rule_id']:<12}{0:>9}{'':>9}{'—':>11}")
            continue
        flag = "  <- below target" if r["needs_review"] else ""
        print(f"{r['rule_id']:<12}{r['reviewed']:>9}{r['genuine']:>9}{r['precision']:>11.0%}"
              f"   {r['ci95'][0]:.0%}-{r['ci95'][1]:.0%}{flag}")
    print(f"[review] wrote {out}")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export", help="draw a sample of findings to review")
    ex.add_argument("--db", type=Path, default=DB_PATH)
    ex.add_argument("--per-rule", type=int, default=30)
    ex.add_argument("--seed", type=int, default=2026)
    ex.add_argument("--out", type=Path,
                    default=PROCESSED_DIR / "calibration" / f"review_{datetime.now():%Y%m%d}.csv")
    sc = sub.add_parser("score", help="precision per rule from a filled-in sample")
    sc.add_argument("csv", type=Path)
    sc.add_argument("--target", type=float, default=0.5)
    sc.add_argument("--out", type=Path, default=PROCESSED_DIR / "calibration" / "precision.json")
    args = parser.parse_args(argv)
    if args.command == "export":
        export(args.db, args.per_rule, args.out, args.seed)
    else:
        score(args.csv, args.target, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
