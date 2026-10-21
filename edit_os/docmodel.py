"""Read a .docx into paragraphs with stable IDs, without losing formatting.

The original XML stays the source of truth. Each paragraph gets an ID (p0001, ...)
and a plain-text view; edits are expressed as character offsets into that text
and written back by `tracked.write`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from docx import Document
from docx.oxml.ns import qn

# Run content elements and the text each contributes to the paragraph's text view.
_TEXT = qn("w:t")
_TAB = qn("w:tab")
_BREAKS = (qn("w:br"), qn("w:cr"))
_RPR = qn("w:rPr")
_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_NOT_OWN = {qn("w:del"), qn("w:p"), _FALLBACK}


@dataclass
class Paragraph:
    id: str
    element: object  # lxml <w:p>
    style: str
    text: str = ""
    in_table: bool = False
    numbered: bool = False  # auto-numbered list item (w:numPr)
    superscript: list[tuple[int, int]] = field(default_factory=list)  # spans formatted as superscript


@dataclass
class Manuscript:
    path: str
    document: object  # docx.Document
    paragraphs: list[Paragraph] = field(default_factory=list)

    def by_id(self, para_id: str) -> Paragraph:
        for p in self.paragraphs:
            if p.id == para_id:
                return p
        raise KeyError(para_id)

    def save(self, path: str) -> None:
        self.document.save(path)


def run_text(run) -> str:
    """Text a single <w:r> contributes; non-text content (images, fields) counts as ''."""
    out = []
    for child in run:
        if child.tag == _TEXT:
            out.append(child.text or "")
        elif child.tag == _TAB:
            out.append("\t")
        elif child.tag in _BREAKS:
            out.append("\n")
    return "".join(out)


def text_runs(p_el) -> list:
    """Runs that make up the visible text, in order, skipping existing tracked deletions."""
    runs = []
    for r in p_el.iter(qn("w:r")):
        # Only this paragraph's own runs: skip tracked deletions, runs of paragraphs nested in a
        # text box (they are paragraphs of their own), and Word's duplicate fallback copy.
        for a in r.iterancestors():
            if a is p_el:
                runs.append(r)
                break
            if a.tag in _NOT_OWN:
                break
    return runs


def superscript_spans(p_el) -> list[tuple[int, int]]:
    """Text spans whose runs are formatted superscript (direct formatting), merged when adjacent."""
    spans, pos = [], 0
    for r in text_runs(p_el):
        n = len(run_text(r))
        va = r.find(f"{_RPR}/{qn('w:vertAlign')}")
        if n and va is not None and va.get(qn("w:val")) == "superscript":
            if spans and spans[-1][1] == pos:
                spans[-1] = (spans[-1][0], pos + n)
            else:
                spans.append((pos, pos + n))
        pos += n
    return spans


def paragraph_text(p_el) -> str:
    return "".join(run_text(r) for r in text_runs(p_el))


def explode_runs(p_el) -> None:
    """Split every run so it holds exactly one content element (keeping its rPr).

    After this, a w:t run can be split by character and tab/break runs are one character.
    """
    for r in text_runs(p_el):
        content = [c for c in r if c.tag != _RPR]
        if len(content) <= 1:
            continue
        parent = r.getparent()
        idx = parent.index(r)
        for i, c in enumerate(content):
            new = copy.deepcopy(r)
            for child in list(new):
                if child.tag != _RPR:
                    new.remove(child)
            new.append(copy.deepcopy(c))
            parent.insert(idx + i, new)
        parent.remove(r)


def load(path: str) -> Manuscript:
    doc = Document(path)
    ms = Manuscript(path=path, document=doc)
    body = doc.element.body
    n = 0
    for p_el in body.iter(qn("w:p")):
        if any(a.tag == _FALLBACK for a in p_el.iterancestors()):
            continue  # duplicate copy of a text box for old Word versions
        n += 1
        style_el = p_el.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
        style = style_el.get(qn("w:val")) if style_el is not None else ""
        in_table = any(a.tag == qn("w:tc") for a in p_el.iterancestors())
        numbered = p_el.find(f"{qn('w:pPr')}/{qn('w:numPr')}") is not None
        ms.paragraphs.append(Paragraph(
            id=f"p{n:04d}", element=p_el, style=style,
            text=paragraph_text(p_el), in_table=in_table, numbered=numbered,
            superscript=superscript_spans(p_el),
        ))
    return ms
