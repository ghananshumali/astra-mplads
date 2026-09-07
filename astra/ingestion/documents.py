"""Document Ingestion Agent — utilization certificates & inspection reports.

Hackathon scope: pdfplumber text extraction + regex field harvesting for
digitally-generated PDFs (eSAKSHI exports are digital, not scans). The OCR
branch (pytesseract) is a declared roadmap item — the hook is here, the
dependency is not installed by default.

Extracted fields are cross-checked against the structured record for the
same work_id; mismatches become compliance findings in a later pass.
"""
from __future__ import annotations

import re
from pathlib import Path

FIELD_PATTERNS = {
    "work_id": re.compile(r"work\s*(?:id|code|no\.?)\s*[:\-]?\s*([A-Z0-9/\-]+)", re.I),
    "sanctioned_amount": re.compile(r"sanction(?:ed)?\s*(?:amount|cost)\s*[:\-]?\s*(?:rs\.?|₹)?\s*([\d,]+)", re.I),
    "expenditure": re.compile(r"(?:expenditure|utili[sz]ed)\s*[:\-]?\s*(?:rs\.?|₹)?\s*([\d,]+)", re.I),
    "completion_date": re.compile(r"complet(?:ion|ed)\s*(?:date|on)\s*[:\-]?\s*([\d/\-\.]+)", re.I),
    "ia_name": re.compile(r"implementing\s*agency\s*[:\-]?\s*(.+)", re.I),
}


def parse_certificate(pdf_path: str | Path) -> dict:
    """Extract key fields from a utilization certificate / inspection PDF."""
    import pdfplumber

    text = ""
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text += (page.extract_text() or "") + "\n"

    if len(text.strip()) < 40:
        # Scanned image PDF -> OCR branch (roadmap; requires pytesseract + tesseract)
        return {"_status": "needs_ocr", "_note": "image-only PDF; OCR pipeline is a stage-2 item"}

    out: dict = {"_status": "parsed", "_chars": len(text)}
    for field, pat in FIELD_PATTERNS.items():
        m = pat.search(text)
        if m:
            val = m.group(1).strip()
            if field in ("sanctioned_amount", "expenditure"):
                try:
                    out[field] = float(val.replace(",", ""))
                except ValueError:
                    continue
            else:
                out[field] = val.splitlines()[0][:120]
    return out
