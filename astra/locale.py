"""Case text in the reader's language.

Every sentence the backend writes about a case — the plain-language brief, the
agent trace, the fallback synthesis, the permitted-action reasons — comes from
a template in `config/locales/<language>.yaml`, filled with values the agents
produced. English is the reference catalogue; a translation has exactly the
same keys and the same {placeholders}, so a Hindi brief states the same facts,
figures and paragraph numbers as the English one (tests/test_case_language.py).

Nothing is machine-translated at run time. Text the analysis stored in English
(a context note, a data-confidence reason, a clause) is matched back to the
template that wrote it and re-rendered, and left in English if no template
matches — never guessed.

    text("hi", "R-TIME-01.metric", days=482)   -> "स्वीकृति के बाद 482 दिन"
    rerender("hi", stored_english, ["confidence.missing", ...])
"""
from __future__ import annotations

import functools
import re
import string
from typing import Any, Iterable

import yaml

from .config import PROJECT_ROOT

LOCALE_DIR = PROJECT_ROOT / "config" / "locales"
DEFAULT = "en"
LANGUAGES: tuple[str, ...] = ("en", "hi")


def normalise(lang: str | None) -> str:
    """A supported language code; anything else reads as English."""
    return lang if lang in LANGUAGES else DEFAULT


def _flatten(node: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in node.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, full + "."))
        else:
            out[full] = value
    return out


@functools.lru_cache(maxsize=None)
def catalogue(lang: str) -> dict[str, Any]:
    """{dotted.key: template or list of templates} for one language."""
    path = LOCALE_DIR / f"{normalise(lang)}.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        return _flatten(yaml.safe_load(fh) or {})


class _Formatter(string.Formatter):
    """str.format, except a value that is already text ignores the format spec.

    Values read back out of stored text (see `rerender`) arrive as strings
    ("1,234") while the template asks for "{n:,}"; they are already formatted.
    """

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, str):
            return value
        return super().format_field(value, format_spec)


_FORMAT = _Formatter()


def template(lang: str, key: str) -> Any:
    """The raw template, falling back to English when a translation lacks it."""
    table = catalogue(normalise(lang))
    if key in table:
        return table[key]
    return catalogue(DEFAULT)[key]


def has(lang: str, key: str) -> bool:
    return key in catalogue(normalise(lang))


def text(lang: str, key: str, **values: Any) -> str:
    return _FORMAT.format(template(lang, key), **values)


def texts(lang: str, key: str, **values: Any) -> list[str]:
    return [_FORMAT.format(t, **values) for t in template(lang, key)]


def text_or(lang: str, key: str, fallback: str, **values: Any) -> str:
    """For keys built from data (a field name, a measure): the data itself if no key."""
    if has(lang, key) or has(DEFAULT, key):
        return text(lang, key, **values)
    return fallback


def money(value: Any, lang: str = DEFAULT) -> str:
    """Indian-convention money: "₹2.45 lakh", "₹1.20 crore", "₹48,000"."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1e7:
        return text(lang, "money.crore", amount=f"{v / 1e7:,.2f}")
    if abs(v) >= 1e5:
        return text(lang, "money.lakh", amount=f"{v / 1e5:,.2f}")
    return f"₹{v:,.0f}"


@functools.lru_cache(maxsize=None)
def pattern(key: str) -> re.Pattern | None:
    """A regex matching any English rendering of one template."""
    raw = catalogue(DEFAULT).get(key)
    if not isinstance(raw, str):
        return None
    out, names = [], set()
    for literal, field, _spec, _conv in string.Formatter().parse(raw):
        out.append(re.escape(literal))
        if field is not None:
            if field in names:
                out.append(f"(?P={field})")
            else:
                names.add(field)
                out.append(f"(?P<{field}>.+?)")
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def rerender(lang: str, english: str | None, keys: Iterable[str]) -> str | None:
    """Stored English text, re-rendered in `lang` from the template that wrote it.

    Returns the original text when the language is English or no template
    matches, so an unrecognised sentence is shown as written rather than lost.
    """
    if english is None or normalise(lang) == DEFAULT:
        return english
    for key in keys:
        compiled = pattern(key)
        match = compiled.match(english) if compiled else None
        if match:
            return text(lang, key, **match.groupdict())
    return english


def placeholders(template_text: str) -> set[str]:
    return {field for _l, field, _s, _c in string.Formatter().parse(template_text)
            if field is not None}
