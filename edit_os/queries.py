"""Author queries: what cannot be fixed without the author, in the pre-editing guide's wording.

Queries are records like changes: anchored to exact text, numbered AQ1, AQ2, ... in reading
order, written into the file as Word comments, and listed on a query sheet. Add comments before
rendering tracked changes, so anchors are taken from the original text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from docx.text.paragraph import Paragraph as DocxParagraph
from docx.text.run import Run

from . import refengine, references
from .docmodel import Manuscript
from .style import JournalStyle

AUTHOR = "Edit OS"

# Standard wording, from the pre-editing style guide where it gives one.
TEXT = {
    "references.uncited": "Reference no. {n} has not been cited. Kindly cite.",
    "references.missing_entry": "Citation [{n}] has no entry in the reference list. Kindly provide the reference.",
    "references.not_found_et_al": "Reference not found in PubMed. Hence kindly provide minimum 6 author names before et al.",
    "references.not_found": "Reference not found in PubMed or CrossRef. Kindly check and provide complete details.",
    "references.wrong_id": "The {what} given points to a different article. Kindly check the reference.",
    "references.no_volume": "Kindly provide volume and page numbers.",
    "references.book_et_al": "Kindly provide minimum 6 author names before et al.",
    "keywords.missing": "Kindly provide key words.",
    "keywords.too_few": "Kindly provide minimum three key words.",
    "sections.conclusion": "Kindly provide the conclusion.",
}


@dataclass
class Query:
    para_id: str
    start: int  # anchor in Paragraph.text; start == end anchors the whole paragraph
    end: int
    text: str
    check: str
    id: str = ""  # AQ1, AQ2, ... assigned in reading order


@dataclass
class QuerySet:
    queries: list[Query] = field(default_factory=list)

    def add(self, para_id: str, check: str, start: int = 0, end: int = 0, **fmt) -> Query:
        q = Query(para_id, start, end, TEXT[check].format(**fmt), check)
        self.queries.append(q)
        return q

    def number(self, ms: Manuscript) -> "QuerySet":
        order = {p.id: i for i, p in enumerate(ms.paragraphs)}
        self.queries.sort(key=lambda q: (order.get(q.para_id, 10 ** 9), q.start))
        for i, q in enumerate(self.queries, 1):
            q.id = f"AQ{i}"
        return self


def _expand(nums: str) -> list[int]:
    out = []
    for part in re.split(r"\s*,\s*", nums.strip()):
        r = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
        if r and int(r.group(2)) - int(r.group(1)) < 100:
            out += list(range(int(r.group(1)), int(r.group(2)) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def _citations(ms: Manuscript):
    """(paragraph, start, end, numbers) for each [n] citation in the body."""
    from .rules import body_paragraphs

    for p in body_paragraphs(ms):
        for m in re.finditer(r"\[(\d[\d,\-–\s]*)\]", p.text):
            nums = []
            for part in re.split(r"\s*,\s*", m.group(1).strip()):
                r = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
                if r:
                    nums += list(range(int(r.group(1)), int(r.group(2)) + 1))
                elif part.isdigit():
                    nums.append(int(part))
            yield p, m.start(), m.end(), nums


def collect(ms: Manuscript, style: JournalStyle) -> QuerySet:
    from .rules import lookup_references  # rules imports this module's neighbours; avoid a cycle

    qs = QuerySet()
    refs = references.extract(ms)
    numbered = {r.number: r for r in refs if r.number is not None}

    # Citations against the reference list. Round-bracket citations are converted by the rules
    # first, so both forms count as cited.
    cited: set[int] = set()
    for p, s, e, nums in _citations(ms):
        cited.update(nums)
        for n in nums:
            if numbered and n not in numbered:
                qs.add(p.id, "references.missing_entry", s, e, n=n)
    from .rules import superscript_citations

    for m_p in ms.paragraphs:
        for m in re.finditer(r"\((\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*)\)", m_p.text):
            cited.update(_expand(m.group(1)))
        for _, _, nums in superscript_citations(m_p):
            cited.update(_expand(nums))
    if cited:
        for n, r in numbered.items():
            if n not in cited:
                qs.add(r.para_id, "references.uncited", r.offset, r.offset + len(r.text), n=n)

    # Reference lookups that need the author.
    for ref, res in lookup_references(ms, style):
        span = dict(start=ref.offset, end=ref.offset + len(ref.text))
        has_et_al = re.search(r"\bet al\b", ref.text) is not None
        few_authors = len(res.parsed.authors) < 6
        if res.status == refengine.QUERY:
            if "points to a different article" in res.message:
                what = "PMID" if res.message.startswith("PMID") else "DOI"
                qs.add(ref.para_id, "references.wrong_id", what=what, **span)
            elif "no volume" in res.message:
                qs.add(ref.para_id, "references.no_volume", **span)
            elif has_et_al and few_authors:
                qs.add(ref.para_id, "references.not_found_et_al", **span)
            else:
                qs.add(ref.para_id, "references.not_found", **span)
        elif res.status == refengine.NOT_JOURNAL and res.parsed.kind in ("book", "book_chapter") \
                and has_et_al and few_authors:
            qs.add(ref.para_id, "references.book_et_al", **span)

    # Key words.
    kw = next((p for p in ms.paragraphs if re.match(r"^\s*(?:<H\d>)?\s*key\s*-?words?\b", p.text, re.I)), None)
    if kw is not None:
        words = [w for w in re.split(r"[;,]", re.sub(r"^\s*(?:<H\d>)?\s*key\s*-?words?\s*[:.\-–]?", "", kw.text, flags=re.I))
                 if w.strip()]
        if not words:
            qs.add(kw.id, "keywords.missing")
        elif len(words) < style.get("keywords.min", 3):
            qs.add(kw.id, "keywords.too_few")

    # Expected sections.
    heads = {re.sub(r"^<H\d>|:$", "", p.text.strip()).strip().lower() for p in ms.paragraphs if len(p.text) < 80}
    if "conclusion" in style.get("expected_h1", []) and not heads & {"conclusion", "conclusions"}:
        target = next((p for p in ms.paragraphs if p.text.strip().lower() in {"discussion", "<h1>discussion"}), None)
        if target is not None:
            qs.add(target.id, "sections.conclusion")
    return qs.number(ms)


def write_comments(ms: Manuscript, qs: QuerySet, author: str = AUTHOR) -> int:
    """Add each query as a Word comment on its anchor. Returns how many were placed."""
    from .docmodel import explode_runs
    from .tracked import _cut, _spans

    placed = 0
    body = ms.document._body
    for q in qs.queries:
        p = ms.by_id(q.para_id)
        explode_runs(p.element)
        if q.end > q.start:
            _cut(p.element, q.start)
            _cut(p.element, q.end)
            runs = [r for r, s, e in _spans(p.element) if s >= q.start and e <= q.end and e > s]
        else:
            runs = [r for r, s, e in _spans(p.element) if e > s]
        if not runs:
            continue
        para = DocxParagraph(p.element, body)
        ms.document.add_comment([Run(runs[0], para), Run(runs[-1], para)], text=f"{q.id}: {q.text}",
                                author=author, initials="EO")
        placed += 1
    return placed


def write_sheet(ms: Manuscript, qs: QuerySet, path: str, article: str = "") -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Queries"
    ws.append([f"Author queries {article}".strip()])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(["No.", "Location", "Query", "Author's answer"])
    for c in ws[2]:
        c.font = Font(bold=True)
    for q in qs.queries:
        text = ms.by_id(q.para_id).text
        where = text[q.start:q.end] if q.end > q.start else text
        ws.append([q.id, where[:120], q.text, ""])
    for col, width in zip("ABCD", (8, 60, 70, 40)):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=3):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(path)
