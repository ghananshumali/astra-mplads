"""Photo evidence for held duplicate matches.

    python tests/test_photos.py

Offline: images are drawn here, the portal is a fake, and the database, the raw
response cache and the writer lock are scratch files. Checks that a photo
re-saved or resized still reads as the same picture and a different one does
not; that the portal's internal work numbers come from the cached completed
report; that a photo check records what it found and stops when the portal
stops answering; that only held works are due, pairs first; and that the
analysis raises two works sharing one photo file, keeps photos that only look
alike held for a person to compare, and keeps the rest held.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="astra-photos-"))
os.environ["ASTRA_DB_PATH"] = str(_TMP / "scratch.db")
os.environ["ASTRA_PROCESSED_DIR"] = str(_TMP / "processed")
os.environ["ASTRA_SHARD_CACHE"] = str(_TMP / "shards")

import pandas as pd                                                   # noqa: E402
from PIL import Image, ImageDraw                                      # noqa: E402

from astra import db                                                  # noqa: E402
from astra.agents.entity_resolution import EntityResolutionAgent      # noqa: E402
from astra.agents.orchestrator import Orchestrator                    # noqa: E402
from astra.explain import humanize                                    # noqa: E402
from astra.ingestion import instance_lock, photos                     # noqa: E402
from astra.ingestion.esakshi_api import CircuitOpen, SourceError      # noqa: E402
from astra.photo_hash import bits_apart, fingerprint, same_photo      # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def scene(seed: int, size=(720, 1280)) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", size, (rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    draw = ImageDraw.Draw(img)
    for _ in range(40):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        draw.rectangle([x, y, x + rng.randrange(40, 300), y + rng.randrange(40, 300)],
                       fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    return img


def jpeg(img: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# ------------------------------------------------------------------ fingerprints
def test_fingerprints() -> dict:
    print("\n[1] the same picture, and a different one")
    original = scene(1)
    base = fingerprint(jpeg(original))
    resaved = fingerprint(jpeg(original, quality=45))
    resized = fingerprint(jpeg(original.resize((360, 640))))
    other = fingerprint(jpeg(scene(2)))
    check("a photo re-saved at lower quality is the same picture",
          same_photo(base, resaved, 6), f"{bits_apart(base['dhash'], resaved['dhash'])} bits")
    check("a photo resized to half is the same picture",
          same_photo(base, resized, 6), f"{bits_apart(base['dhash'], resized['dhash'])} bits")
    check("a different photo is not", not same_photo(base, other, 6),
          f"{bits_apart(base['dhash'], other['dhash'])} bits")
    check("the fingerprint records the image size", base["width"] == 720 and base["height"] == 1280)
    return {"same": jpeg(original), "same_resaved": jpeg(original, quality=45), "other": jpeg(scene(2)),
            "third": jpeg(scene(3)), "third_resaved": jpeg(scene(3), quality=45)}


# ------------------------------------------------------------------ portal ids
def write_cache(rows: list[dict]) -> None:
    path = _TMP / "shards" / "2" / "7" / "85"
    path.mkdir(parents=True, exist_ok=True)
    (path / "completed.json.gz").write_bytes(gzip.compress(json.dumps(rows).encode()))


def test_portal_ids() -> None:
    print("\n[2] the portal's internal work number comes from the cached completed report")
    write_cache([
        {"ACTIVITY_NAME": "WS/MP1/2024-2025/11-Installing tube-wells and borewells", "WORK_ID": 7001},
        {"ACTIVITY_NAME": "WS/MP1/2024-2025/12-Installing tube-wells and borewells", "WORK_ID": 7002},
        {"ACTIVITY_NAME": "WS/MP1/2024-2025/13-Installing tube-wells and borewells", "WORK_ID": None},
    ])
    ids = photos.portal_work_ids()
    check("every completed work with a number is mapped, by work code",
          ids == {"WS/MP1/2024-2025/11": "7001", "WS/MP1/2024-2025/12": "7002"}, str(ids))


# ------------------------------------------------------------------ photo job
class FakePortal:
    def __init__(self, images: dict, attachments: dict, fail: set = frozenset(), down_after: int | None = None):
        self.images, self.listed, self.fail, self.down_after = images, attachments, fail, down_after
        self.calls = 0

    def _count(self):
        self.calls += 1
        if self.down_after is not None and self.calls > self.down_after:
            raise CircuitOpen("portal paused")

    def attachments(self, internal):
        self._count()
        if internal in self.fail:
            raise SourceError("HTTP 500", path="/getAttachIdsbyFlag")
        return self.listed.get(internal, [])

    def attachment(self, attach_id):
        self._count()
        return self.images[attach_id]


def test_check_works(images: dict) -> None:
    print("\n[3] a photo check records what it found")
    ids = {"W1": "1", "W2": "2", "W3": "3", "W4": "4", "W6": "6"}
    portal = FakePortal(
        images={"a1": images["same"], "a2": images["same_resaved"], "d1": b"%PDF payorder",
                "d3": b"%PDF completion certificate"},
        attachments={"1": [("a1", "site.jpg"), ("d1", "payorder.pdf"), ("x1", "notes.docx")],
                     "2": [("a2", "IMG_2.JPEG")], "3": [("d3", "completion.pdf")], "6": []},
        fail={"4"})
    tally = photos.check_works(["W1", "W2", "W3", "W4", "W5", "W6"], client=portal, ids=ids, pause=0,
                               log=lambda m: None)
    evidence = db.photo_evidence()
    check("photos are fingerprinted; documents are recorded by name, never fetched",
          evidence["W1"]["status"] == "photos" and len(evidence["W1"]["photos"]) == 1
          and [d["file_name"] for d in evidence["W1"]["documents"]] == ["payorder.pdf"]
          and evidence["W2"]["status"] == "photos",
          str({k: v["status"] for k, v in evidence.items()}))
    check("a completed work with only a PDF is recorded as documents, with nothing to compare",
          evidence["W3"]["status"] == "documents" and not evidence["W3"]["documents"][0]["sha256"])
    check("a completed work with nothing attached has no photo", evidence["W6"]["status"] == "no_photo")
    check("a portal error is recorded as failed, and the run goes on",
          evidence["W4"]["status"] == "failed", str(dict(tally)))
    check("a work with no internal number is not completed yet", evidence["W5"]["status"] == "not_completed")
    check("the portal was asked only for attachment lists and photos",
          portal.calls == 5 + 2, f"{portal.calls} calls")
    down = FakePortal(images={"a1": images["same"]}, attachments={"1": [("a1", "x.jpg")], "2": [("a1", "y.jpg")]},
                      down_after=2)
    tally = photos.check_works(["W1", "W2"], client=down, ids=ids, pause=0, log=lambda m: None)
    check("a run stops when the portal stops answering, keeping what it recorded",
          tally.get("stopped_early") == 1 and tally.get("photos") == 1, str(dict(tally)))
    rechecked = FakePortal(images={"a3": images["third"]}, attachments={"3": [("a3", "new.jpg")]})
    photos.check_works(["W3"], client=rechecked, ids=ids, pause=0, log=lambda m: None)
    check("a check replaces what the last one found for that work",
          db.photo_evidence()["W3"]["status"] == "photos")


def test_due() -> None:
    print("\n[4] only held works are due, pairs first")
    db.replace_duplicate_groups([
        {"group_id": "batch:b1", "kind": "batch", "work_ids": ["B1", "B2", "B3"]},
        {"group_id": "pair:p1", "kind": "pair", "work_ids": ["P1", "P2"]},
    ])
    later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    due = db.due_photo_checks(recheck_before=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat())
    check("works never checked are due, a pair's before a batch's", due == ["P1", "P2", "B1", "B2", "B3"], str(due))
    db.save_photo_check("P1", portal_work_id="9", status="photos", found=[
        {"attach_id": "x", "file_name": "x.jpg", "kind": "photo", "dhash": "0" * 16, "vhash": "0" * 16}])
    db.save_photo_check("P2", portal_work_id=None, status="not_completed")
    due = db.due_photo_checks(recheck_before=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat())
    check("a checked work is not due again straight away", "P1" not in due and "P2" not in due, str(due))
    due = db.due_photo_checks(recheck_before=later)
    check("a work not yet completed is due again once its recheck time passes",
          "P2" in due and "P1" not in due, str(due))
    db.replace_duplicate_groups([{"group_id": "pair:p1", "kind": "pair", "work_ids": ["P1", "P2"]}])
    check("a work the analysis no longer holds is not due", "B1" not in db.due_photo_checks(recheck_before=later))
    summary = db.photo_check_summary()
    check("the summary counts held works and what their checks found",
          summary["held_works"] == 2 and summary["by_status"].get("photos") == 1, str(summary))
    lock = instance_lock.WriterLock(instance_lock.lock_path(db.DB_PATH))
    lock.acquire(role="poller")
    try:
        try:
            photos.run(1, log=lambda m: None)
            refused = False
        except RuntimeError:
            refused = True
        check("a photo check refuses while a poller holds the database", refused)
    finally:
        lock.release()


# ------------------------------------------------------------------ analysis
def work(work_id, description, *, mp="SHRI A", amount=300000.0, status="Physical Inspection"):
    return {"work_id": work_id, "description": description, "state": "UTTAR PRADESH", "district": "SITAPUR",
            "constituency": "SITAPUR", "era": "post2023", "mp_name": mp, "recommended_date": "2025-01-10",
            "status": status, "estimated_cost": amount, "sanctioned_amount": amount, "letter_no": "LN/1",
            "house": "LS", "category": "Borewell", "work_type": "Borewell", "ia_name": "SITAPUR IDA",
            "vendor_name": None, "lat": None, "lon": None, "in_recommended": 1}


def fp(raw: bytes, name: str) -> dict:
    f = fingerprint(raw)
    return {"attach_id": name, "file_name": name, "dhash": f["dhash"], "vhash": f["vhash"],
            "sha256": __import__("hashlib").sha256(raw).hexdigest()}


def doc(content: bytes, name: str) -> dict:
    return {"attach_id": name, "file_name": name, "sha256": __import__("hashlib").sha256(content).hexdigest()}


def seen(status="photos", photos_=(), documents=()) -> dict:
    return {"status": status, "photos": list(photos_), "documents": list(documents)}


def test_analysis(images: dict) -> None:
    print("\n[5] the analysis reads the photos")
    rng = random.Random(5)

    def word():
        return "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(8))

    rows = [work(f"WS/MP9/2024-2025/{i}", f"Repair of {word()} {word()} {word()} lane near {word()}")
            for i in range(1, 240)]
    twin = "Borewell installation at Kishanpur primary school Hardoi tehsil campus"
    other_twin = "Community hall construction near Ganeshganj panchayat bhawan Laharpur road"
    papers = "Boundary wall for Kshetra panchayat Ghazipur Saidpur cremation ground"
    other_papers = "Entrance gate for Sarvodaya inter college Mahmudabad kasba compound"
    batch = "Solar high mast light installation at public places Sidhauli Kamlapur area"
    alike = "Hand pump installation near Rampur Bamanpura primary school ground"
    rows += [work("P-1", twin), work("P-2", twin),                           # one photo file
             work("L-1", alike), work("L-2", alike),                         # photos look alike
             work("Q-1", other_twin), work("Q-2", other_twin),               # different photos
             work("D-1", papers), work("D-2", papers),                       # same certificate
             work("E-1", other_papers), work("E-2", other_papers),           # different certificates
             *[work(f"B-{i}", batch, mp="SHRI D", amount=450000.0) for i in range(1, 6)]]
    works = pd.DataFrame(rows)
    evidence = {
        "P-1": seen(photos_=[fp(images["same"], "p1.jpg")]),
        "P-2": seen(photos_=[fp(images["same"], "p2.jpg")]),
        "L-1": seen(photos_=[fp(images["same"], "l1.jpg")]),
        "L-2": seen(photos_=[fp(images["same_resaved"], "l2.jpg")]),
        "Q-1": seen(photos_=[fp(images["same"], "q1.jpg")]),
        "Q-2": seen(photos_=[fp(images["other"], "q2.jpg")]),
        "D-1": seen("documents", documents=[doc(b"%PDF cc 7173133", "cc 7173133.pdf")]),
        "D-2": seen("documents", documents=[doc(b"%PDF cc 7173133", "cc 7173133 (1).pdf")]),
        "E-1": seen("documents", documents=[doc(b"%PDF certificate one", "60004 Completion.pdf")]),
        "E-2": seen("documents", documents=[doc(b"%PDF certificate two", "60015 completion.pdf")]),
        "B-1": seen(photos_=[fp(images["third"], "b1.jpg")]),
        "B-2": seen(photos_=[fp(images["third"], "b2.jpg")]),
        "B-3": seen(photos_=[fp(images["other"], "b3.jpg")]),
        "B-4": seen("not_completed"),
        "B-5": seen(photos_=[fp(images["third_resaved"], "b5.jpg")]),
    }
    agent = EntityResolutionAgent()
    agent.context = {"photos": evidence}
    found = agent.run(works, pd.DataFrame())

    def on(wid):
        return [f for f in found if f.rule_id == "D-DUP-01" and f.entity_id == wid]

    p = [f for f in on("P-1") if f.details.get("pair_work_id") == "P-2"]
    check("two works sharing one photo file are raised, critical, no longer held",
          len(p) == 1 and p[0].severity == "critical" and p[0].details.get("photo_match")
          and not p[0].details.get("held") and p[0].details.get("photo_check") == "same_file",
          str([(f.severity, f.details.get("photo_check")) for f in p]))
    lk = [f for f in on("L-1") if f.details.get("pair_work_id") == "L-2"]
    check("two works whose photos only look alike stay held, low, naming both files",
          len(lk) == 1 and lk[0].details.get("held") and lk[0].severity == "low"
          and not lk[0].details.get("photo_match") and lk[0].details.get("photo_check") == "look_alike"
          and lk[0].details.get("this_file") == "l1.jpg" and lk[0].details.get("other_file") == "l2.jpg"
          and "compare them by eye" in lk[0].summary,
          str([(f.severity, f.details.get("photo_check")) for f in lk]))
    q = [f for f in on("Q-1") if f.details.get("pair_work_id") == "Q-2"]
    check("two works with different photos stay held, saying so",
          len(q) == 1 and q[0].details.get("held") and q[0].details.get("photo_check") == "different_photos"
          and "different pictures" in q[0].summary)
    dd = [f for f in on("D-1") if f.details.get("pair_work_id") == "D-2"]
    check("two works carrying the same document stay held: one order often covers several works",
          len(dd) == 1 and dd[0].details.get("held") and not dd[0].details.get("photo_match")
          and dd[0].details.get("photo_check") == "no_photo",
          str([(f.severity, f.details.get("photo_check")) for f in dd]))
    ee = [f for f in on("E-1") if f.details.get("pair_work_id") == "E-2"]
    check("and so do two works with different documents and no photos",
          len(ee) == 1 and ee[0].details.get("held") and ee[0].details.get("photo_check") == "no_photo")
    b1 = [f for f in on("B-1") if f.details.get("photo_match")]
    check("in a batch, the two works sharing a photo file are raised",
          len(b1) == 1 and b1[0].details.get("pair_work_id") == "B-2" and b1[0].severity == "critical"
          and [f for f in on("B-2") if f.details.get("photo_match")], str([f.details.get("photo_cluster") for f in b1]))
    check("the other works of the batch are not, including one whose photo only looks alike",
          not [f for w in ("B-3", "B-4", "B-5") for f in on(w) if f.details.get("photo_match")])
    batch_finding = next(f for f in on("B-1") if f.details.get("batch"))
    check("the batch records what the photo check found, listing look-alikes to compare",
          batch_finding.details.get("photo_summary")
          == {"checked": 5, "with_photos": 4, "with_documents": 0, "shared_groups": 1,
              "look_alike_pairs": 2, "look_alike": [["B-1", "B-5"], ["B-2", "B-5"]]},
          str(batch_finding.details.get("photo_summary")))
    kinds = sorted((g["kind"], tuple(g["work_ids"])) for g in agent.last_groups)
    check("the held groups are recorded for the next photo check",
          ("batch", ("B-1", "B-2", "B-3", "B-4", "B-5")) in kinds and ("pair", ("Q-1", "Q-2")) in kinds
          and ("pair", ("P-1", "P-2")) in kinds and ("pair", ("L-1", "L-2")) in kinds, str(kinds))

    flags = {f.entity_id: f for f in Orchestrator()._aggregate(found, works)}
    check("a shared photo file alone makes a case an alert", flags["P-1"].alert and flags["P-1"].risk_score == 40,
          f"score {flags['P-1'].risk_score}")
    check("a pair whose photos only look alike scores nothing", flags["L-1"].risk_score == 0,
          f"score {flags['L-1'].risk_score}")
    check("a held pair with different photos still scores nothing", flags["Q-1"].risk_score == 0)
    en, hi = humanize(p[0].model_dump(), "en"), humanize(p[0].model_dump(), "hi")
    check("the explanation names the shared photo file, in English and Hindi",
          en["headline"] == "The same photo file is recorded for another work" and "P-2" in en["plain"]
          and "P-2" in hi["plain"] and any("ऀ" <= ch <= "ॿ" for ch in hi["headline"]),
          en["headline"])
    held_en = humanize(q[0].model_dump(), "en")
    check("a held pair's explanation says its photos differ", "different pictures" in held_en["plain"])
    alike_en, alike_hi = humanize(lk[0].model_dump(), "en"), humanize(lk[0].model_dump(), "hi")
    check("a look-alike pair's explanation asks for a comparison by eye, in English and Hindi",
          "compare them by eye" in alike_en["plain"] and "आँख से" in alike_hi["plain"])

    plain = EntityResolutionAgent()
    plain_found = plain.run(works, pd.DataFrame())
    check("without photo checks nothing is raised and every pair is held, not run",
          not any(f.details.get("photo_match") for f in plain_found)
          and all(f.details.get("photo_check") == "not_run" for f in plain_found
                  if f.details.get("held") and not f.details.get("batch")))


def main() -> int:
    print("=" * 74)
    print("ASTRA photo evidence for held duplicate matches")
    print("=" * 74)
    try:
        db.init_db(force=True)
        images = test_fingerprints()
        test_portal_ids()
        test_check_works(images)
        test_due()
        test_analysis(images)
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
