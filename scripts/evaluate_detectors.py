"""Planted-case evaluation of ASTRA's detectors, with a projection to 1M records.

    python scripts/evaluate_detectors.py                       # reads data/astra.db
    python scripts/evaluate_detectors.py --db path/to/copy.db  # any corpus
    python scripts/evaluate_detectors.py --per-pattern 100 --no-scaling

What this measures, and what it cannot
--------------------------------------
There are no confirmed labels for MPLADS irregularities, so real-world
precision and recall cannot be measured. Instead, known patterns are planted
into copies of REAL works, the full analysis runs on the real corpus with those
works in it, and each detector's hits and misses on the planted cases are
counted:

  * detection rate (recall on planted cases), with a 95% Wilson interval;
  * how detection falls as a pattern gets subtler (cost at 2x, 3x, 5x, 10x);
  * how often each rule fires on the untouched corpus (a volume, NOT a false
    positive rate: nobody knows how many of those are genuine);
  * run time and peak memory at several corpus sizes, fitted to project 1M.

Planted cases are cleaner than real irregularities, so these rates are an upper
bound for the patterns as planted. Precision needs reviewers' verdicts on a
sample of real findings: see scripts/review_sample.py.

Reads the database read-only and never writes to it. Results go to
<processed>/evaluation/: results.json and detection_1m.png.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np          # noqa: E402
import pandas as pd         # noqa: E402

from astra import data_contract as contract   # noqa: E402
from astra.agents.orchestrator import Orchestrator   # noqa: E402
from astra.config import DB_PATH, PROCESSED_DIR, load_rules   # noqa: E402

RNG = np.random.default_rng(2026)
TODAY = date.today()


# ------------------------------------------------------------------ loading
def load(db_path: Path) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        def table(name):
            return pd.read_sql_query(f"SELECT * FROM {name}", con)
        data = {name: table(name) for name in ("works", "fundflows", "work_listing",
                                               "work_versions", "retired_works")}
        data["area_health"] = pd.read_sql_query(
            "SELECT s.shard_id, p.exact, w.lifecycle, w.consecutive_failures, w.stale_since, "
            "w.fetched_at, (SELECT COUNT(*) FROM missing_works m WHERE m.shard_id = s.shard_id) "
            "AS awaiting_removal FROM shards s LEFT JOIN shard_parity p ON p.shard_id = s.shard_id "
            "LEFT JOIN shard_watermarks w ON w.shard_id = s.shard_id", con)
    finally:
        con.close()
    return data


def as_stored(frame: pd.DataFrame) -> pd.DataFrame:
    """What a round trip through SQLite gives: missing text is None, not NaN."""
    out = frame.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].where(out[col].notna(), None)
    return out


# ------------------------------------------------------------------ running
class Run:
    """One full analysis over in-memory data, keeping every finding."""

    def __init__(self, works, flows, versions, retired, health):
        self.inputs = (works, flows, versions, retired, health)
        self.findings = []
        self.cases = 0
        self.alerts = 0
        self.seconds = 0.0
        self.peak_gb = None

    def go(self) -> "Run":
        works, flows, versions, retired, health = self.inputs
        orch = Orchestrator()
        orch._persist_trace = lambda n: None     # never overwrite the real run_meta.json
        captured = {}
        original = orch._aggregate

        def capture(findings, frame):
            captured["findings"] = list(findings)
            return original(findings, frame)

        orch._aggregate = capture
        peak = {"rss": 0}
        stop = threading.Event()

        def watch():
            try:
                import psutil
                proc = psutil.Process()
                while not stop.is_set():
                    peak["rss"] = max(peak["rss"], proc.memory_info().rss)
                    time.sleep(0.25)
            except ImportError:
                peak["rss"] = 0

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        started = time.monotonic()
        flags = orch.run(works, flows, {"versions": versions, "retired": retired, "area_health": health})
        self.seconds = time.monotonic() - started
        self.cases = len(flags)
        self.alerts = sum(1 for f in flags if f.alert)
        stop.set()
        thread.join()
        self.peak_gb = peak["rss"] / 2**30 if peak["rss"] else None
        self.findings = captured.get("findings", [])
        return self

    def hits(self, rule_id: str) -> set[str]:
        return {str(f.entity_id) for f in self.findings if f.rule_id == rule_id}

    def by_rule(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.rule_id] = counts.get(f.rule_id, 0) + 1
        return counts


def flows_for(works: pd.DataFrame, flows: pd.DataFrame) -> pd.DataFrame:
    from astra.pipeline import LIVE_SOURCE, live_fundflows
    rebuilt = live_fundflows(works)
    if rebuilt is None:
        return as_stored(flows)
    kept = flows[flows["source"] != LIVE_SOURCE] if not flows.empty else flows
    return as_stored(pd.concat([rebuilt, kept], ignore_index=True))


# ------------------------------------------------------------------ planting
def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def pick(pool: pd.DataFrame, n: int, taken: set[str]) -> pd.DataFrame:
    pool = pool[~pool["work_id"].isin(taken)]
    if pool.empty:
        return pool
    chosen = pool.sample(n=min(n, len(pool)), random_state=int(RNG.integers(1 << 31)))
    taken.update(chosen["work_id"])
    return chosen


def plant(works: pd.DataFrame, versions: pd.DataFrame, baseline: Run, per: int):
    """Plant each pattern into real works. Returns (works, versions, cases).

    `cases` is a list of {pattern, variant, entity, rule}. Works chosen for a
    pattern were not already found by that pattern's rule in the baseline, so
    every hit counted below is a detection the planting caused.
    """
    w = works.copy()
    new_rows: list[dict] = []
    new_versions: list[dict] = []
    cases: list[dict] = []
    taken: set[str] = set()
    today = pd.Timestamp(TODAY)
    now = datetime.now(timezone.utc).isoformat()
    sanctioned = w[w["sanction_date"].notna() & (w["status"] != contract.RECORD_PAIR_STAGE)]
    index = {wid: i for i, wid in enumerate(w["work_id"])}

    def set_fields(work_id, **fields):
        row = index[work_id]
        for key, value in fields.items():
            w.iat[row, w.columns.get_loc(key)] = value

    # 1. works not permitted: the asset itself, in several real-world phrasings
    variants = {
        "english": "Construction of temple at {p}",
        "named deity": "Construction of Shiv Mandir in {p}",
        "mosque": "Construction of masjid at {p}",
        "hindi, with a wall": "Mandir ki chardiwari nirman {p}",
        "temple hall": "Construction of temple hall at {p}",
    }
    clean = sanctioned[~sanctioned["work_id"].isin(baseline.hits("R-PROH-01"))]
    for variant, template in variants.items():
        for row in pick(clean, per // len(variants), taken).itertuples():
            place = str(row.district or "the village").title()
            set_fields(row.work_id, description=template.format(p=place))
            cases.append({"pattern": "Work not permitted (para 5.2.11)", "variant": variant,
                          "entity": row.work_id, "rule": "R-PROH-01"})

    # 2. cost far above comparable works, at several strengths
    groups = sanctioned.groupby(["state", "category", "era"])["sanctioned_amount"]
    size = groups.transform("size")
    median = groups.transform("median")
    eligible = sanctioned[(size >= 30) & (median >= 100000)]
    eligible = eligible[~eligible["work_id"].isin(baseline.hits("A-COST-01"))]
    medians = median.loc[eligible.index]
    for factor in (2, 3, 5, 10):
        for row in pick(eligible, per // 4, taken).itertuples():
            cost = float(medians.loc[row.Index]) * factor
            set_fields(row.work_id, sanctioned_amount=cost, estimated_cost=cost)
            cases.append({"pattern": "Cost above comparable works", "variant": f"{factor}x the peer median",
                          "entity": row.work_id, "rule": "A-COST-01"})

    # 3. the same work entered twice, with a small wording change
    descr = sanctioned["description"].fillna("")
    rich = sanctioned[descr.str.split().str.len() >= 8]
    rich = rich[~rich["work_id"].isin(baseline.hits("D-DUP-01"))]
    for i, row in enumerate(pick(rich, per, taken).itertuples()):
        clone = w.loc[w["work_id"] == row.work_id].iloc[0].to_dict()
        clone["work_id"] = f"EVAL-DUP-{i}"
        clone["description"] = str(row.description).replace(" of ", " of the ", 1)
        new_rows.append(clone)
        cases.append({"pattern": "Same work entered twice", "variant": "clone, one word changed",
                      "entity": row.work_id, "rule": "D-DUP-01", "pair": clone["work_id"]})

    # 4. overdue against the one-year norm
    open_works = sanctioned[~sanctioned["is_completed"].astype(bool)]
    open_works = open_works[~open_works["work_id"].isin(baseline.hits("R-TIME-01"))]
    for row in pick(open_works, per, taken).itertuples():
        set_fields(row.work_id, sanction_date=str((today - pd.Timedelta(days=500)).date()),
                   recommended_date=str((today - pd.Timedelta(days=540)).date()))
        cases.append({"pattern": "Overdue against the one-year norm (para 3.2.12)",
                      "variant": "500 days, not complete", "entity": row.work_id, "rule": "R-TIME-01"})

    # 5. recommendation waiting beyond 45 days
    pending = w[(w["status"] == contract.PENDING_STAGE) & w["sanction_date"].isna()]
    pending = pending[~pending["work_id"].isin(baseline.hits("R-SANC-01"))]
    for row in pick(pending, per, taken).itertuples():
        set_fields(row.work_id, recommended_date=str((today - pd.Timedelta(days=120)).date()))
        cases.append({"pattern": "Awaiting sanction beyond 45 days (para 3.2.4)",
                      "variant": "120 days", "entity": row.work_id, "rule": "R-SANC-01"})

    # 6. sanctioned below Rs 2.5 lakh
    normal = sanctioned[sanctioned["sanctioned_amount"] >= 300000]
    for row in pick(normal, per, taken).itertuples():
        set_fields(row.work_id, sanctioned_amount=150000.0, estimated_cost=150000.0)
        cases.append({"pattern": "Below the normal minimum (para 3.2.9)", "variant": "Rs 1.5 lakh",
                      "entity": row.work_id, "rule": "R-COST-01"})

    # 7-9. edits observed after sanction
    old_enough = sanctioned[pd.to_datetime(sanctioned["sanction_date"], errors="coerce")
                            < today - pd.Timedelta(days=30)]
    edits = (
        ("Work changed after sanction (para 3.2.15)", "description edited", "V-REV-01",
         lambda r: [("description", r.description, f"{r.description} (revised site)")]),
        ("Amount revised after sanction", "+30%", "V-AMT-01",
         lambda r: [("sanctioned_amount", r.sanctioned_amount, r.sanctioned_amount * 1.3),
                    ("estimated_cost", r.sanctioned_amount, r.sanctioned_amount * 1.3)]),
        ("Payments reversed", "total paid down by Rs 50,000", "V-PAY-01",
         lambda r: [("total_paid", 200000.0, 150000.0)]),
    )
    for pattern, variant, rule, changes in edits:
        pool = old_enough[old_enough["sanctioned_amount"] >= 400000] if rule == "V-AMT-01" else old_enough
        for row in pick(pool, per, taken).itertuples():
            for field, old, new in changes(row):
                new_versions.append({"work_id": row.work_id, "observed_at": now, "field": field,
                                     "old_value": str(old), "new_value": str(new),
                                     "shard_id": getattr(row, "shard_id", None)})
            cases.append({"pattern": pattern, "variant": variant, "entity": row.work_id, "rule": rule})

    # 10. unusual on two measures at once, as multiples of the peer group's
    #     own typical time to sanction and to complete
    done = sanctioned[sanctioned["is_completed"].astype(bool) & sanctioned["completion_date"].notna()].copy()
    done["eval_to_sanction"] = (pd.to_datetime(done["sanction_date"], errors="coerce")
                            - pd.to_datetime(done["recommended_date"], errors="coerce")).dt.days
    done["eval_to_complete"] = (pd.to_datetime(done["completion_date"], errors="coerce")
                            - pd.to_datetime(done["sanction_date"], errors="coerce")).dt.days
    done = done[(done["eval_to_sanction"] >= 0) & (done["eval_to_complete"] >= 0)]
    grouped = done.groupby(["state", "category"])
    done["eval_peers"] = grouped["work_id"].transform("size")
    done["eval_typ_sanction"] = grouped["eval_to_sanction"].transform("median").clip(lower=7)
    done["eval_typ_complete"] = grouped["eval_to_complete"].transform("median").clip(lower=7)
    done = done[(done["eval_peers"] >= 60) & ~done["work_id"].isin(baseline.hits("A-PEER-01"))]
    for strength in (3, 10, 30):
        for row in pick(done, per // 3, taken).itertuples():
            sanction = pd.Timestamp(row.sanction_date)
            set_fields(row.work_id,
                       recommended_date=str((sanction - pd.Timedelta(days=int(row.eval_typ_sanction * strength))).date()),
                       completion_date=str((sanction + pd.Timedelta(days=int(row.eval_typ_complete * strength))).date()))
            cases.append({"pattern": "Unusual time to sanction and to complete",
                          "variant": f"{strength}x the peer group's typical times", "entity": row.work_id,
                          "rule": "A-PEER-01"})

    # 11. repair and renovation above Rs 50 lakh in a year, for one MP
    texts = w["category"].fillna("") + " " + w["description"].fillna("")
    has_repair = texts.str.contains("repair|renovat|marammat", case=False, regex=True)
    ls = w[(w["house"] == "LS") & w["constituency"].notna() & (w["fy"] == "2025-26")]
    clean_mps = ls[~ls["constituency"].isin(w.loc[has_repair, "constituency"])]
    mps = clean_mps.drop_duplicates("constituency")
    for i, row in enumerate(mps.sample(n=min(per // 4, len(mps)), random_state=7).itertuples()):
        template = w.loc[w["work_id"] == row.work_id].iloc[0].to_dict()
        for j in range(6):
            clone = dict(template, work_id=f"EVAL-REPAIR-{i}-{j}",
                         description=f"Repair of government school building number {j} in {row.constituency}",
                         estimated_cost=1000000.0, sanctioned_amount=1000000.0)
            new_rows.append(clone)
        cases.append({"pattern": "Repair and renovation above Rs 50 lakh (para 5.1.9)",
                      "variant": "6 works of Rs 10 lakh", "entity": contract.mp_key(template),
                      "rule": "R-REPAIR-01"})

    if new_rows:
        w = pd.concat([w, pd.DataFrame(new_rows)], ignore_index=True)
    v = pd.concat([versions, pd.DataFrame(new_versions)], ignore_index=True) if new_versions else versions
    return as_stored(w), v, cases


# ------------------------------------------------------------------ report
def score(cases: list[dict], run: Run) -> tuple[list[dict], list[dict]]:
    hits_by_rule: dict[str, set[str]] = {}
    pairs: set[tuple[str, str]] = set()
    for f in run.findings:
        hits_by_rule.setdefault(f.rule_id, set()).add(str(f.entity_id))
        if f.rule_id == "D-DUP-01" and f.details.get("pair_work_id"):
            pairs.add(tuple(sorted((str(f.entity_id), str(f.details["pair_work_id"])))))
    for case in cases:
        if case["rule"] == "D-DUP-01":
            case["detected"] = tuple(sorted((case["entity"], case["pair"]))) in pairs
        else:
            case["detected"] = case["entity"] in hits_by_rule.get(case["rule"], set())
    table = pd.DataFrame(cases)
    rows, variants = [], []
    for (pattern, rule), grp in table.groupby(["pattern", "rule"], sort=False):
        tp, n = int(grp["detected"].sum()), len(grp)
        low, high = wilson(tp, n)
        rows.append({"pattern": pattern, "rule": rule, "planted": n, "detected": tp,
                     "missed": n - tp, "rate": tp / n, "ci95": [round(low, 3), round(high, 3)]})
        for variant, sub in grp.groupby("variant", sort=False):
            k = int(sub["detected"].sum())
            variants.append({"pattern": pattern, "variant": variant, "planted": len(sub),
                             "detected": k, "rate": k / len(sub)})
    return rows, variants


def measure_scale(db_path: Path, share: float) -> dict:
    """Time and peak memory of one full analysis on a sample, in its own process.

    A process's peak memory never falls, so sizes measured one after another
    in the same process would all report the largest run's peak.
    """
    import subprocess
    code = (
        "import json,sys,time;sys.path.insert(0,r'%s');"
        "import scripts.evaluate_detectors as e;"
        "from astra import data_contract as c;"
        "d=e.load(__import__('pathlib').Path(r'%s'));"
        "w=e.as_stored(c.enrich(d['works'],d['work_listing']));"
        "w=w.sample(frac=%r,random_state=11) if %r<1 else w;"
        "r=e.Run(w,e.flows_for(w,d['fundflows']),d['work_versions'],d['retired_works'],d['area_health']).go();"
        "print(json.dumps({'works':len(w),'seconds':r.seconds,'peak_gb':r.peak_gb,'cases':r.cases,'alerts':r.alerts}))"
    ) % (ROOT, db_path, share, share)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[-2000:])
    return json.loads(out.stdout.strip().splitlines()[-1])


def chart(rows, variants, volume, scaling, path: Path, cases: float = 0, alerts: float = 0) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(19, 7.2), gridspec_kw={"width_ratios": [1.25, 1, 1]})
    fig.suptitle("ASTRA detectors on planted cases, projected to 1 million records",
                 fontsize=15, fontweight="bold", x=0.01, ha="left")

    ax = axes[0]
    labels = [f"{r['pattern']}\n{r['rule']}" for r in rows][::-1]
    caught = [1000 * r["rate"] for r in rows][::-1]
    missed = [1000 - c for c in caught]
    y = np.arange(len(rows))
    ax.barh(y, caught, color="#2e7d6b", label="caught (true positives)")
    ax.barh(y, missed, left=caught, color="#c96a4a", label="missed (false negatives)")
    for yi, c, r in zip(y, caught, rows[::-1]):
        ax.text(min(c, 1000) - 8, yi, f"{c:.0f}", va="center", ha="right", color="white", fontsize=8.5,
                fontweight="bold")
        ax.text(1008, yi, f"{r['ci95'][0]:.0%}-{r['ci95'][1]:.0%}", va="center", fontsize=7.5, color="#555")
    ax.set_yticks(y, labels, fontsize=8)
    ax.set_xlim(0, 1130)
    ax.set_xlabel("per 1,000 genuine cases of the pattern (95% interval on the right)")
    ax.set_title("True positives and false negatives\n(independent of corpus size)", fontsize=11)
    ax.legend(loc="upper center", bbox_to_anchor=(0.45, -0.09), ncol=2, fontsize=8, frameon=False)

    ax = axes[1]
    graded = [v for v in variants if v["pattern"] in ("Cost above comparable works",
                                                      "Work not permitted (para 5.2.11)",
                                                      "Unusual time to sanction and to complete")]
    names = [f"{v['variant']}" for v in graded][::-1]
    rates = [100 * v["rate"] for v in graded][::-1]
    colours = ["#1a437f" if "Cost" in v["pattern"] else ("#8a9a5b" if "permitted" in v["pattern"] else "#9c6b8e")
               for v in graded][::-1]
    ax.barh(np.arange(len(graded)), rates, color=colours)
    ax.set_yticks(np.arange(len(graded)), names, fontsize=8)
    ax.set_xlim(0, 105)
    ax.set_xlabel("% of planted cases detected")
    ax.set_title("Where detection falls off\n(blue: cost; green: not permitted; purple: two measures)", fontsize=11)
    for yi, r in enumerate(rates):
        ax.text(r + 1, yi, f"{r:.0f}%", va="center", fontsize=8)

    ax = axes[2]
    top = sorted(volume.items(), key=lambda kv: -kv[1])[:12][::-1]
    ax.barh([k for k, _ in top], [v for _, v in top], color="#5d6d7e")
    ax.set_xscale("log")
    ax.set_xlabel("findings per 1,000,000 records (log scale)")
    ax.set_title(f"Expected findings at 1M records (same mix as today)\n"
                 f"about {cases:,.0f} cases for review, {alerts:,.0f} alerts", fontsize=11)
    for yi, (_, v) in enumerate(top):
        ax.text(v * 1.08, yi, f"{v:,.0f}", va="center", fontsize=8)
    note = (f"At 1M records the analysis alone would take about {scaling['projected_minutes_1m']:.0f}-"
            f"{scaling['linear_minutes_1m']:.0f} min (fitted vs straight-line scaling from "
            f"{scaling['sizes'][0]:,}-{scaling['sizes'][-1]:,} works) and about "
            f"{scaling['projected_gb_1m']:.0f} GB of memory in one process." if scaling else "")
    import textwrap
    footer = ("Planted cases are cleaner than real irregularities, so detection rates are upper bounds "
              "for the patterns as planted. Finding volume is not a false-positive rate: precision needs "
              "reviewers' verdicts on a sample (scripts/review_sample.py). Volumes include findings shown "
              "only as context on another case. " + note)
    fig.text(0.01, 0.012, "\n".join(textwrap.wrap(footer, 230)), fontsize=9, color="#333", va="bottom")
    fig.tight_layout(rect=(0, 0.09, 1, 0.94))
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ------------------------------------------------------------------ main
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--per-pattern", type=int, default=200)
    parser.add_argument("--no-scaling", action="store_true")
    parser.add_argument("--out", type=Path, default=PROCESSED_DIR / "evaluation")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"[evaluate] reading {args.db} (read-only)")
    data = load(args.db)
    works = as_stored(contract.enrich(data["works"], data["work_listing"]))
    versions, retired, health = data["work_versions"], data["retired_works"], data["area_health"]
    flows = flows_for(works, data["fundflows"])
    print(f"[evaluate] {len(works):,} works; baseline run")
    baseline = Run(works, flows, versions, retired, health).go()
    print(f"[evaluate] baseline {baseline.seconds:.0f}s, {len(baseline.findings):,} findings")

    planted, planted_versions, cases = plant(works, versions, baseline, args.per_pattern)
    print(f"[evaluate] planted {len(cases):,} cases; evaluation run")
    evaluation = Run(planted, flows_for(planted, data["fundflows"]), planted_versions, retired, health).go()
    rows, variants = score(cases, evaluation)

    per_million = 1_000_000 / len(works)
    volume = {rid: n * per_million for rid, n in baseline.by_rule().items()}

    scaling = None
    if not args.no_scaling:
        points = []
        for share in (0.25, 0.5, 1.0):
            point = measure_scale(args.db, share)
            points.append(point)
            print(f"[evaluate] {point['works']:,} works in a separate process: "
                  f"{point['seconds']:.0f}s, peak {point['peak_gb']:.2f} GB")
        sizes = [pt["works"] for pt in points]
        seconds = [pt["seconds"] for pt in points]
        peaks = [pt["peak_gb"] for pt in points]
        exponent, intercept = np.polyfit(np.log(sizes), np.log(seconds), 1)
        mem_slope, mem_intercept = np.polyfit(sizes, peaks, 1)
        scaling = {"sizes": sizes, "seconds": [round(x, 1) for x in seconds],
                   "peak_gb": [round(x, 2) for x in peaks], "exponent": float(exponent),
                   "projected_minutes_1m": float(math.exp(intercept) * 1_000_000 ** exponent / 60),
                   "projected_gb_1m": float(mem_intercept + mem_slope * 1_000_000),
                   "linear_minutes_1m": float(seconds[-1] / sizes[-1] * 1_000_000 / 60),
                   "note": "analysis only (orchestrator); loading and saving add to both"}

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus_works": len(works), "per_pattern": args.per_pattern,
        "rules_threshold_note": "thresholds as in config/rules.yaml at run time",
        "detection": rows, "by_variant": variants,
        "findings_per_million_records": {k: round(v) for k, v in sorted(volume.items())},
        "cases_per_million_records": round(baseline.cases * per_million),
        "alerts_per_million_records": round(baseline.alerts * per_million),
        "baseline": {"cases": baseline.cases, "alerts": baseline.alerts,
                     "findings": len(baseline.findings)},
        "scaling": scaling,
        "caveats": [
            "Detection rates are on planted cases, which are cleaner than real irregularities: upper bounds.",
            "Finding volume is not a false-positive rate; precision needs reviewed samples.",
            "Projection to 1M assumes the same mix of work types, states and stages as the corpus measured.",
        ],
    }
    (args.out / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    chart(rows, variants, volume, scaling, args.out / "detection_1m.png",
          cases=baseline.cases * per_million, alerts=baseline.alerts * per_million)
    print(json.dumps({"detection": rows, "scaling": scaling}, indent=2, default=str))
    print(f"[evaluate] wrote {args.out / 'results.json'} and {args.out / 'detection_1m.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
