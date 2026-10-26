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

from . import refengine, references, refs_other
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
    "references.no_pages": "Kindly provide page numbers.",
    "references.record_differs": "The {what} given here differs from the published article. Kindly check.",
    "references.author_style": "The author names could not be set in the journal's style (surname followed by initials). Kindly check and provide them.",
    "references.author_names": "The author names could not be verified against PubMed / CrossRef. Kindly check.",
    "references.book_et_al": "Kindly provide minimum 6 author names before et al.",
    "references.link_dead": "Link is not working. Kindly check.",
    "references.link_redirect": "The link now opens a different page ({url}). Kindly check and provide the correct URL.",
    "references.access_date": "Kindly provide last accessed date.",
    "references.link_broken": "The URL is broken by spaces. Kindly check and provide the complete URL.",
    "references.publisher_name": "Kindly provide publisher name and location.",
    "references.publisher_location": "Kindly provide publisher location.",
    "references.book_year": "Kindly check the year of publication; the book is listed with {years}.",
    "references.book_unverified": "This book could not be verified. Kindly check the authors, title, edition, publisher, city and year.",
    "references.unverified": "This reference could not be verified. Kindly check and confirm the details.",
    "keywords.missing": "Kindly provide key words.",
    "keywords.too_few": "Kindly provide minimum three key words.",
    "sections.conclusion": "Kindly provide the conclusion.",
    "figures.legend_missing": "Kindly provide the legend for Figure {n}.",
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
        from .rules_sections import TEXT as SECTION_TEXT

        q = Query(para_id, start, end, {**SECTION_TEXT, **TEXT}[check].format(**fmt), check)
        self.queries.append(q)
        return q

    def number(self, ms: Manuscript) -> "QuerySet":
        order = {p.id: i for i, p in enumerate(ms.paragraphs)}
        self.queries.sort(key=lambda q: (order.get(q.para_id, 10 ** 9), q.start))
        for i, q in enumerate(self.queries, 1):
            q.id = f"AQ{i}"
        return self


def rules_sections_xml(ms: Manuscript) -> list[str]:
    from .rules_sections import xml_keywords  # the submission's own key words fill an empty list

    return xml_keywords(ms)


def collect(ms: Manuscript, style: JournalStyle) -> QuerySet:
    from .rules import lookup_references  # rules imports this module's neighbours; avoid a cycle

    qs = QuerySet()
    refs = references.extract(ms)
    numbered = {r.number: r for r in refs if r.number is not None}

    # Citations against the reference list: the citations renumbering reads (renumber.occurrences: the
    # citation rule's own decisions on round brackets and superscripts), so the queries, renumbering and QC
    # agree. Counts, list markers, statistics and the reference list's own "(4)" issues are not citations.
    from .renumber import occurrences

    cited: set[int] = set()
    for o in occurrences(ms, style, 0):
        cited.update(o.numbers)
        missing = [n for n in o.numbers if numbered and n not in numbered]
        if missing:
            # anchored on the brackets as the author wrote them (a converted citation's own text)
            text = ms.by_id(o.para_id).text
            lo = o.start if o.end > o.start else max(0, o.start - 40)
            spans = list(re.finditer(r"[\[(][^\[\]()]*[\])]", text[lo:max(o.end, o.start)]))
            s, e = (lo + spans[-1].start(), lo + spans[-1].end()) if spans else (o.start, o.end)
            for n in missing:
                qs.add(o.para_id, "references.missing_entry", s, e, n=n)
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
            elif refengine.DIFFERS in res.message:
                what = res.message.split(": the ", 1)[1].split(" " + refengine.DIFFERS)[0]
                qs.add(ref.para_id, "references.record_differs", what=what, **span)
            elif "no volume" in res.message:
                qs.add(ref.para_id, "references.no_volume", **span)
            elif has_et_al and few_authors:
                qs.add(ref.para_id, "references.not_found_et_al", **span)
            else:
                qs.add(ref.para_id, "references.not_found", **span)
        elif res.status in refs_other.STATUSES and refs_other.classify(ref.text, res.parsed.kind) in ("book", "book_chapter") \
                and has_et_al and few_authors:
            qs.add(ref.para_id, "references.book_et_al", **span)
        added = {q.check for q in qs.queries if q.para_id == ref.para_id}
        for check, fmt in res.queries:  # books, links, reports (refs_other); fields missing from a styled entry
            if check not in added:
                qs.add(ref.para_id, check, **fmt, **span)

    # Key words.
    from .rules_sections import layout, listed_keywords

    kw = layout(ms).keywords  # also a "Keywords:" line ending an abstract paragraph
    if kw is None:
        kw = next((p for p in ms.paragraphs if re.match(r"^\s*(?:<H\d>)?\s*key\s*-?words?\b", p.text, re.I)), None)
    if kw is not None:
        # the list as the key-words rule reads it ("Diabetes Mellitus, Type 2; ..." is one key word with its comma);
        # "Key words" on its own line, the list below it
        words = listed_keywords(ms) if kw is layout(ms).keywords else [
            w for w in re.split(r"[;,]", re.sub(r"^\s*(?:<H\d>)?\s*key\s*-?words?\s*[:.\-–]?", "", kw.text, flags=re.I))
            if w.strip()]
        if not words and not (style.get("sections.keywords.from_xml") and rules_sections_xml(ms)):
            qs.add(kw.id, "keywords.missing")
        elif len(words) < style.get("keywords.min", 3):
            qs.add(kw.id, "keywords.too_few")

    # Abstract, key words and back matter.
    from . import rules_sections

    rules_sections.add_queries(ms, style, qs)

    # Expected sections.
    # each line: a heading after a manual line break in a longer paragraph ("... [18].\nConclusion") is one too
    heads = {re.sub(r"^<H\d>|:$", "", line.strip()).strip().lower() for p in ms.paragraphs
             for line in p.text.split("\n") if len(line) < 80}
    if "conclusion" in style.get("expected_h1", []) and not heads & {"conclusion", "conclusions"}:
        # the Discussion heading as written: "Discussion", "<H1>Discussion", "Discussion:", "4. Discussion"
        target = next((p for p in ms.paragraphs if re.fullmatch(r"\s*(?:<H\d>)?\s*(?:\d+\.?\s*)?discussion\s*[:.]?\s*",
                                                                p.text, re.I)), None)
        if target is not None:
            qs.add(target.id, "sections.conclusion")
    from . import renumber, rules_figures
    renumber.add_queries(ms, style, qs)
    rules_figures.add_queries(ms, style, qs)
    from .rules import add_author_queries  # "Radziwill et al. [6]" where reference 6 is Bae: S "Match author names"
    add_author_queries(ms, style, qs)
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
