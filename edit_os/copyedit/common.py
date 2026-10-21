"""Shared helpers for the copy-edit checkers: which paragraphs to check and which spans to leave alone."""

from __future__ import annotations

import re
from itertools import count

from ..changes import Change, Layer, Source
from ..docmodel import Manuscript, Paragraph

_ids = count(1)

# Spans no copy-edit checker may touch: URLs, e-mail addresses, DOIs, citations, typesetting tags.
_PROTECTED = re.compile(
    r"https?://\S+|www\.\S+|\S+@\S+\.\w+|\bdoi:\s*\S+|\b10\.\d{4,}/\S+"
    r"|\[\d[\d,\-–\s]*\]|<\/?[A-Za-z]{1,4}\d?>"
)


def change(p: Paragraph, start: int, end: int, new: str, rule: str,
           layer: Layer = Layer.LANGUAGE, reason: str = "") -> Change:
    return Change(id=f"ce{next(_ids)}", para_id=p.id, start=start, end=end, old=p.text[start:end],
                  new=new, layer=layer, source=Source.RULE, reason=reason or rule)


def protected_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _PROTECTED.finditer(text)]


def hidden_offsets(p: Paragraph) -> list[int]:
    """Offsets in the text view where the paragraph has content the text view leaves out
    (symbol-font characters such as ±, fields, drawings). Spacing next to them is not ours to fix."""
    from docx.oxml.ns import qn

    from ..docmodel import run_text, text_runs

    out, pos = [], 0
    hidden = {qn("w:sym"), qn("w:drawing"), qn("w:object"), qn("w:pict"), qn("w:fldChar")}
    for r in text_runs(p.element):
        if any(c.tag in hidden for c in r):
            out.append(pos)
        pos += len(run_text(r))
    return out


def near_hidden(start: int, end: int, offsets: list[int]) -> bool:
    return any(start - 1 <= o <= end + 1 for o in offsets)


def overlaps(start: int, end: int, spans) -> bool:
    return any(start < e and s < end or start == end and s <= start < e for s, e in spans)


def is_numeric(text: str) -> bool:
    """Mostly numbers and symbols (table rows, statistics lists): not prose worth checking."""
    letters = sum(c.isalpha() for c in text)
    return letters < 0.5 * len(text.replace(" ", "")) or len(re.findall(r"[A-Za-z]{3,}", text)) < 3


def prose_paragraphs(ms: Manuscript) -> list[Paragraph]:
    """Body paragraphs before the reference list, outside tables, that read as prose."""
    from ..rules import body_paragraphs

    return [p for p in body_paragraphs(ms) if not is_numeric(p.text)]
