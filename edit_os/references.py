"""Find the reference list in a manuscript and split it into entries.

Each Word paragraph is one entry, so a reference that wraps across lines stays
one reference. Leading labels ("1.", "[1]", "(1)", "1)") are recorded and kept
out of the entry text so edits target the reference itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .docmodel import Manuscript

_HEADING = re.compile(
    r"^\s*(?:<H\d>)?\s*(references?|reference list|bibliography|literature cited|works cited|cited literature)\s*:?\s*$",
    re.I,
)
# Sections that commonly follow the reference list.
_STOP = re.compile(
    r"^\s*(tables?\b|table\s+\d|figures?\b|figure\s+\d|fig\.?\s*\d|figure legends?|legends?\b"
    r"|appendix|appendices|supplementary|acknowledg|tables? and figures)",
    re.I,
)
_LABEL = re.compile(r"^\s*(\[\d+\]|\(\d+\)|\d+[.)]|\d+(?=\s))\s*")


@dataclass
class Reference:
    para_id: str
    number: int | None
    label: str  # e.g. "12. "; empty for auto-numbered or author-year lists
    text: str  # entry without the label
    offset: int  # where `text` starts in the paragraph's text view


def find_heading(ms: Manuscript) -> int | None:
    """Index of the reference-list heading; the last match wins (skips contents pages)."""
    hits = [i for i, p in enumerate(ms.paragraphs) if not p.in_table and _HEADING.match(p.text)]
    return hits[-1] if hits else None


def extract(ms: Manuscript) -> list[Reference]:
    start = find_heading(ms)
    if start is None:
        return []
    refs: list[Reference] = []
    auto = 0
    for p in ms.paragraphs[start + 1:]:
        if p.in_table:
            continue
        raw = p.text
        if not raw.strip():
            continue
        if _STOP.match(raw) or p.style.lower().startswith("heading"):
            break
        m = _LABEL.match(raw)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            number, label = int(digits), m.group(0)
        elif p.numbered:
            auto += 1
            number, label = auto, ""
        else:
            number, label = None, ""
        refs.append(Reference(
            para_id=p.id, number=number, label=label,
            text=raw[len(label):].rstrip(), offset=len(label),
        ))
    return refs
