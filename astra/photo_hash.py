"""Photo fingerprints: do two photos look alike?

A difference hash of 64 bits across rows and another down columns. A re-saved,
recompressed or resized copy of one photo stays within a few bits of the
original on both; two different photos usually differ in about half the bits
(24 to 32 of 64 between four borewell photos from one village batch, measured on
16 Sep 2026). Not always: many uploads are phone photos of a printed site photo
under the same camera stamp, and two such photos of different sites came within
1 bit on the column hash (13 on the row hash) in the 17 Sep 2026 sample. So a
close match only means the photos look alike, for a person to compare; the
analysis treats only an identical file (SHA-256) as evidence. Cropping and
rotation are not allowed for: a cropped copy reads as a different photo, so a
non-match is not proof of anything either.
"""
from __future__ import annotations

import io


def fingerprint(raw: bytes) -> dict:
    """{dhash, vhash, width, height, bytes} of one image. Raises for an unreadable one."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        width, height = img.size
        grey = img.convert("L")
        rows = grey.resize((9, 8), Image.Resampling.LANCZOS).tobytes()
        cols = grey.resize((8, 9), Image.Resampling.LANCZOS).tobytes()
    dhash = vhash = 0
    for r in range(8):
        for c in range(8):
            dhash = dhash << 1 | (rows[r * 9 + c] > rows[r * 9 + c + 1])
            vhash = vhash << 1 | (cols[r * 8 + c] > cols[(r + 1) * 8 + c])
    return {"dhash": f"{dhash:016x}", "vhash": f"{vhash:016x}", "width": width,
            "height": height, "bytes": len(raw)}


def bits_apart(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def same_photo(p: dict, q: dict, max_bits: int) -> bool:
    """Look alike, allowing for re-saving and resizing: both hashes close. The
    same picture passes; so can two different photos taken the same way."""
    return (bits_apart(p["dhash"], q["dhash"]) <= max_bits
            and bits_apart(p["vhash"], q["vhash"]) <= max_bits)
