"""The site's languages: every translation file is complete and safe to show.

    python tests/test_translations.py

English (frontend/src/i18n/locales/en.json) defines every key. For each other
language in the menu (frontend/src/i18n/languages.ts) this checks the file is complete, has no stray keys, keeps every
{placeholder} exactly, is written in the language's own script, and has no
sentence left in English. It also checks the pages only use keys that exist,
and that no English key sits unused. Offline; no browser needed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"
LOCALES = SRC / "i18n" / "locales"

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []

#: Unicode ranges of each language's script; a language added to the menu needs one
SCRIPTS = {
    "hi": (0x0900, 0x097F),
}
#: Keys whose text is a name, code or unit that stays as written in English
KEEP_ENGLISH = {"parity.col.astra", "parity.col.astraRs", "case.ai.latency", "live.tip.start",
                "ops.svc.api"}
#: Keys built from data at run time rather than written out in the code
DYNAMIC_PREFIXES = ("tier.", "status.", "risk.", "agent.", "stage.", "basis.", "rule.",
                    "action.", "unchecked.", "field.", "ledger.", "fallback.", "strength.",
                    "measure.", "tile.", "house.", "entity.", "nav.", "dup.")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((PASS if cond else FAIL, name, detail))
    print(f"  [{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def english_words(text: str) -> int:
    stripped = PLACEHOLDER.sub(" ", text)
    return len(re.findall(r"\b[A-Za-z]{3,}\b", stripped))


def in_script(text: str, code: str) -> bool:
    low, high = SCRIPTS[code]
    return any(low <= ord(ch) <= high for ch in text)


def main() -> int:
    print("=" * 74)
    print("ASTRA site languages — translation files")
    print("=" * 74)
    en = load(LOCALES / "en.json")
    languages = re.findall(r'code: "(\w+)"', (SRC / "i18n" / "languages.ts").read_text(encoding="utf-8"))

    print("\n[1] English and the code agree")
    check("English defines every text as a non-empty string",
          all(isinstance(v, str) and v.strip() for v in en.values()), f"{len(en)} keys")
    others = languages[1:]
    check("the language menu lists English first", languages[:1] == ["en"], ", ".join(languages))
    check("the menu offers at least one other language", bool(others), ", ".join(others))
    check("each other language has its script defined here",
          all(lang in SCRIPTS for lang in others), ", ".join(l for l in others if l not in SCRIPTS))
    check("every translation file belongs to a language in the menu",
          {p.stem for p in LOCALES.glob("*.json")} <= set(languages),
          ", ".join(sorted({p.stem for p in LOCALES.glob("*.json")} - set(languages))))

    code = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.ts*")
                     if "locales" not in p.parts)
    used = set(re.findall(r'\bt\(\s*"([^"]+)"', code)) | set(re.findall(r'\bk="([^"]+)"', code)) \
        | set(re.findall(r'\btn\(\s*"([^"]+)"', code)) | set(re.findall(r'label: "([a-z.]+)"', code))
    missing = sorted(k for k in used if k not in en and f"{k}_other" not in en)
    check("every key the pages use exists in English", not missing, ", ".join(missing[:10]))
    plural_bases = {k.rsplit("_", 1)[0] for k in en if k.endswith(("_one", "_other"))}
    unused = sorted(k for k in en if k not in used and not k.startswith(DYNAMIC_PREFIXES)
                    and k.rsplit("_", 1)[0] not in (used & plural_bases))
    check("no English text sits unused", not unused, ", ".join(unused[:10]))
    check("every plural has both forms in English",
          all(f"{b}_one" in en and f"{b}_other" in en for b in plural_bases))

    for lang in [l for l in others if l in SCRIPTS]:
        print(f"\n[{lang}]")
        path = LOCALES / f"{lang}.json"
        if not check(f"{lang}: translation file exists", path.exists(), str(path.name)):
            continue
        try:
            table = load(path)
        except ValueError as exc:
            check(f"{lang}: file is valid JSON", False, str(exc))
            continue
        missing = [k for k in en if k not in table]
        extra = [k for k in table if k not in en]
        check(f"{lang}: every key translated", not missing, f"{len(missing)} missing: {missing[:5]}")
        check(f"{lang}: no keys English does not have", not extra, str(extra[:5]))
        empty = [k for k, v in table.items() if not isinstance(v, str) or not v.strip()]
        check(f"{lang}: no empty text", not empty, str(empty[:5]))
        wrong = [k for k in en if k in table
                 and set(PLACEHOLDER.findall(en[k])) != set(PLACEHOLDER.findall(table[k]))]
        check(f"{lang}: every {{placeholder}} kept exactly", not wrong, str(wrong[:5]))
        untranslated = [k for k in en if k in table and k not in KEEP_ENGLISH
                        and table[k] == en[k] and english_words(en[k]) >= 2]
        check(f"{lang}: no sentence left in English", not untranslated, str(untranslated[:5]))
        off_script = [k for k, v in table.items() if k not in KEEP_ENGLISH
                      and english_words(en.get(k, "")) >= 1 and not in_script(v, lang)]
        check(f"{lang}: written in its own script", not off_script, str(off_script[:5]))

    print("\n" + "=" * 74)
    failed = [r for r in results if r[0] == FAIL]
    print(f"  {len(results) - len(failed)} passed, {len(failed)} failed")
    for _, name, detail in failed:
        print(f"    FAILED: {name} — {detail[:160]}")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
