"""Reference engine: parse an entry, find the published record, and rebuild it in journal style.

A record is used only when it matches strictly (title, first author, year). Anything less
becomes a query for the editor; the entry text is never replaced on a guess.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from . import biblio, journals
from .style import JournalStyle

DOI = re.compile(r"(?:doi:?\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/[^\s\]>]+)", re.I)
PMID = re.compile(r"\bPMID:?\s*(\d{5,9})", re.I)
AUTHORS_END = re.compile(r"(?<=[A-Za-z])\.\s+(?=[A-Z0-9\"“‘'(\[])|\bet al\.?,?\s+")
YEAR_VOL = re.compile(r"\b((?:19|20)\d{2})[^;:\n]{0,20};\s*(\d+)\s*(?:\(([^)]*)\))?\s*:\s*([A-Za-z]?\d+[A-Za-z]?(?:\s*[-–]\s*[A-Za-z]?\d+[A-Za-z]?)?)")
YEAR = re.compile(r"\b(19|20)\d{2}\b")

MATCHED, FORMAT_FIXED, QUERY, NOT_JOURNAL = "matched", "format_fixed", "query", "not_journal"


@dataclass
class Parsed:
    text: str
    authors: list[str] = field(default_factory=list)
    title: str = ""
    year: str = ""
    doi: str = ""
    pmid: str = ""

    @property
    def first_surname(self) -> str:
        return self.authors[0].split()[0] if self.authors and self.authors[0].split() else ""


@dataclass
class Resolution:
    status: str
    parsed: Parsed
    record: biblio.Record | None = None
    formatted: str = ""
    message: str = ""
    score: float = 0.0


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def parse(text: str) -> Parsed:
    p = Parsed(text=text)
    if m := DOI.search(text):
        p.doi = m.group(1).rstrip(".,;")
    if m := PMID.search(text):
        p.pmid = m.group(1)
    if m := AUTHORS_END.search(text):
        p.authors = [a.strip() for a in text[:m.start()].split(",") if a.strip() and "et al" not in a]
        rest = text[m.end():]
        # Title runs to the next sentence end that is followed by the journal.
        t = re.match(r"(.+?[.?!])\s+(?=[A-Z])", rest)
        p.title = (t.group(1) if t else rest).strip().rstrip(".")
    if m := YEAR_VOL.search(text) or YEAR.search(text):
        p.year = m.group(1) if m.re is YEAR_VOL else m.group(0)
    return p


def looks_like_journal_article(text: str) -> bool:
    if re.search(r"\b(ed\.|edition|editors?|publishers?|press|available from|accessed|thesis|dissertation)\b", text, re.I):
        return False
    return bool(YEAR_VOL.search(text)) or bool(DOI.search(text) or PMID.search(text))


def score(p: Parsed, r: biblio.Record) -> tuple[float, bool, bool]:
    """(title similarity 0-100, first author matches, year matches)."""
    t = fuzz.token_sort_ratio(fold(p.title), fold(r.title)) if p.title else 0
    if len(r.title) > 25:  # our title split can stop early ("Melanoma. Clinical features...")
        t = max(t, fuzz.partial_ratio(fold(r.title), fold(p.text)))
    author = bool(r.authors) and bool(p.first_surname) and fold(r.authors[0][0]).split()[-1:] == fold(p.first_surname).split()[-1:]
    if r.authors and p.first_surname and not author:  # compound surnames: "van der Berg" vs "Berg"
        author = fold(p.first_surname) in fold(r.authors[0][0]) or fold(r.authors[0][0]) in fold(p.first_surname)
    year = bool(p.year) and p.year == r.year
    return t, author, year


def accept(p: Parsed, r: biblio.Record, by_id: bool = False) -> bool:
    t, author, year = score(p, r)
    if by_id:  # the author gave a DOI/PMID: only check it points at the same paper
        return t >= 70 or (author and year)
    return t >= 95 and (author or year) or t >= 90 and author and year


def _best(p: Parsed, records: list[biblio.Record]) -> biblio.Record | None:
    ok = [r for r in records if accept(p, r)]
    return max(ok, key=lambda r: score(p, r)[0]) if ok else None


def find(p: Parsed) -> tuple[biblio.Record | None, str]:
    if p.pmid:
        for r in biblio.pubmed_fetch([p.pmid]):
            if accept(p, r, by_id=True):
                return r, ""
            return None, f"PMID {p.pmid} points to a different article"
    if p.doi:
        ids = biblio.pubmed_search(f'"{p.doi}"[aid]', retmax=1)
        recs = biblio.pubmed_fetch(ids) or [r for r in [biblio.crossref_doi(p.doi)] if r]
        for r in recs:
            if accept(p, r, by_id=True):
                return r, ""
            return None, f"DOI {p.doi} points to a different article"
    if not p.title:
        return None, "Could not identify the title"
    words = " ".join(w for w in re.sub(r"[^\w\s-]", " ", p.title).split() if len(w) > 2)
    terms = []
    if p.first_surname and p.year:
        terms.append(f"({words}) AND {p.first_surname}[au] AND {p.year}[dp]")
    if p.first_surname:
        terms.append(f"({words}) AND {p.first_surname}[au]")
    terms.append(f"{words}[ti]")
    for term in terms:
        r = _best(p, biblio.pubmed_fetch(biblio.pubmed_search(term)))
        if r:
            return r, ""
    r = _best(p, biblio.crossref_search(p.text))
    if r:
        return r, ""
    return None, "Not found in PubMed or CrossRef"


# --- formatting ------------------------------------------------------------------------

def _compress(first: str, last: str) -> str:
    if not (first.isdigit() and last.isdigit()) or len(first) != len(last) or int(last) <= int(first):
        return last
    i = 0
    while i < len(first) - 1 and first[i] == last[i]:
        i += 1
    return last[i:]


def format_pages(pages: str, style: JournalStyle) -> str:
    dash = style.get("references.page_range_dash", "-")
    m = re.fullmatch(r"\s*([A-Za-z]?)(\d+)\s*[-–]\s*([A-Za-z]?)(\d+)\s*", pages)
    if not m:
        return pages.strip()
    last = _compress(m.group(2), m.group(4)) if style.get("references.page_range_compress", True) else m.group(4)
    return f"{m.group(1)}{m.group(2)}{dash}{m.group(3)}{last}"


def journal_name(r: biblio.Record, style: JournalStyle) -> str:
    name = r.journal
    if r.source == "crossref" and journals.directory():
        j = None
        for issn in r.issn:
            j = journals.directory().lookup(issn)
            if j:
                break
        j = j or journals.directory().lookup(name)
        if j:
            name = j.med_abbr
    if style.get("references.journal_drop_qualifier", True):
        name = re.sub(r"\s*\([^)]*\)$", "", name)
    return name.replace(".", "")


def _usable_authors(r: biblio.Record, p: Parsed | None) -> list[str]:
    names = [f"{s} {i}".strip() for s, i in r.authors]
    if p and r.source == "crossref" and len(names) < min(len(p.authors), 6):
        return p.authors  # CrossRef author lists are often incomplete; keep the author's
    return names


def _usable_title(r: biblio.Record, p: Parsed | None) -> str:
    title = r.title
    if p and p.title and r.source == "crossref" and (title.isupper() or len(title) < 0.6 * len(p.title)):
        return p.title  # ALL-CAPS or truncated CrossRef titles: keep the author's wording
    return title


def format_record(r: biblio.Record, style: JournalStyle, p: Parsed | None = None) -> str:
    max_a = style.get("references.max_authors", 6)
    names = _usable_authors(r, p)
    if max_a and len(names) > max_a:
        names = names[:max_a] + [style.get("references.et_al", "et al.").rstrip(".")]
    title = re.sub(r"\s+", " ", _usable_title(r, p)).strip()
    if title.endswith("."):
        title = title[:-1]
    if style.get("references.capitalize_after_colon"):
        title = re.sub(r":\s([a-z])", lambda m: ": " + m.group(1).upper(), title)
    end = "" if title.endswith(("?", "!")) else "."
    issue = "" if style.get("references.drop_issue", True) or not r.issue else f"({r.issue})"
    pages = format_pages(r.pages, style)
    cite = f"{journal_name(r, style)} {r.year};{r.volume}{issue}"
    cite += f":{pages}" if pages else ""
    return f"{', '.join(names)}. {title}{end} {cite}."


def resolve(text: str, style: JournalStyle) -> Resolution:
    p = parse(text)
    if not looks_like_journal_article(text):
        return Resolution(NOT_JOURNAL, p, message="Book, website or other non-journal source")
    record, why = find(p)
    if not record:
        return Resolution(QUERY, p, message=why)
    if not (record.volume and record.year):
        return Resolution(QUERY, p, record, message="Record has no volume yet (ahead of print?)")
    formatted = format_record(record, style, p)
    t, _, _ = score(p, record)
    if fold(formatted) == fold(text) and formatted.rstrip() == text.rstrip():
        return Resolution(MATCHED, p, record, formatted, score=t)
    return Resolution(FORMAT_FIXED, p, record, formatted, score=t)
