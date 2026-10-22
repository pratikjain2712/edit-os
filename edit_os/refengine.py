"""Reference engine: parse an entry, find the published record, and rebuild it in journal style.

A record is used only when it matches strictly (title, first author, year). Anything less
becomes a query for the editor; the entry text is never replaced on a guess.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from . import ai_refs, biblio, journals
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
    kind: str = ""  # from Claude's split: journal_article, book, website, ...

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
    queries: list[tuple[str, dict]] = field(default_factory=list)  # (queries.TEXT key, format values)


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


def _title_in(title: str, text: str) -> bool:
    return fuzz.partial_ratio(fold(title), fold(text)) >= 90


def _author_in(author: str, text: str) -> bool:
    name = fold(biblio.first_author_surname(author) or author)  # group authors are checked whole
    return bool(name) and re.search(rf"\b{re.escape(name)}\b", fold(text)) is not None


def from_fields(text: str, f: dict) -> Parsed:
    """Parsed entry from Claude's fields. Each field is kept only if it really occurs in the text:
    Claude's split can belong to another entry (a neighbour, or the first half of an entry split over
    two paragraphs), and that entry's title and first author would find that entry's paper."""
    p = parse(text)  # regex DOI / PMID / year as a base
    p.kind = f.get("kind", "")
    if f.get("title") and _title_in(f["title"], text):
        p.title = f["title"].strip().rstrip(".")
    authors = [a.strip() for a in f.get("authors", []) if a.strip()]
    if authors and _author_in(authors[0], text):
        p.authors = authors
    if re.fullmatch(r"(19|20)\d{2}", f.get("year", "").strip()) and f["year"].strip() in text:
        p.year = f["year"].strip()
    if f.get("doi") and f["doi"].lower() in text.lower():
        p.doi = f["doi"].strip().rstrip(".")
    if f.get("pmid") and f["pmid"] in text:
        p.pmid = f["pmid"].strip()
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


def _usable(r: biblio.Record | None) -> bool:
    return r is not None and bool(r.volume and r.year)


def _confirmed(p: Parsed, r: biblio.Record) -> bool:
    """A record found by the wider searches must, besides passing `accept`, carry the entry's first
    author and have its title written in the entry itself: a title that only matches Claude's split
    of the entry, or a shorter title contained in the author's, is not enough."""
    _, author, _ = score(p, r)
    return author and fuzz.partial_ratio(fold(r.title), fold(p.text)) >= 90


def find(p: Parsed) -> tuple[biblio.Record | None, str]:
    found, why = _find(p)
    if _usable(found) or why.endswith("points to a different article"):
        return found, why
    # Nothing accepted, or only a record without volume (ahead of print, a CrossRef deposit without
    # one): wider searches, each candidate held to the same strict acceptance.
    for records in biblio.fallback_candidates(p.title, p.authors[0] if p.authors else "", p.year, p.text, found):
        r = _best(p, [r for r in records if _confirmed(p, r)])
        if _usable(r):
            return r, ""
        found = found or r
    return (found, "") if found else (None, why)


def _find(p: Parsed) -> tuple[biblio.Record | None, str]:
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
    prefix2 = "" if m.group(3) == m.group(1) else m.group(3)  # e524-e532 -> e524-32
    return f"{m.group(1)}{m.group(2)}{dash}{prefix2}{last}"


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


def _mostly_upper(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    return len(letters) >= 12 and sum(c.isupper() for c in letters) > 0.6 * len(letters)


def _usable_title(r: biblio.Record, p: Parsed | None) -> str:
    title = r.title
    m = re.fullmatch(r"\s*\[(.+)\]\s*\.?\s*", title or "", re.S)
    if m:  # PubMed brackets titles translated into English: "[Solid limbal dermoid ...]."
        title = m.group(1).strip()
    if p and p.title and (_mostly_upper(title) or r.source == "crossref" and len(title) < 0.6 * len(p.title)):
        return p.title  # ALL-CAPS (PubMed or CrossRef) or truncated CrossRef titles: keep the author's wording
    if _mostly_upper(title):  # no author title to fall back on: sentence case it, acronyms are lost
        return title[:1] + title[1:].lower()
    return title


# Proper nouns that keep their capital in a Title-Cased title, where no sentence-cased source
# says otherwise: eponyms, instruments and places common in the journals' references. Kept short:
# a word left out here is lower-cased, as the editors do with ordinary words.
PROPER_NOUNS = frozenset("""
Doppler Heidelberg Humphrey Goldmann Snellen Fourier Gaussian Bayesian Kaplan Meier Pearson Spearman
Wilcoxon Kruskal Wallis Mann Whitney Fisher Bland Altman Cox Poisson Markov Monte Carlo Likert Delphi
Parkinson Parkinson's Alzheimer Alzheimer's Graves Graves' Behçet Behcet Stargardt Leber Fuchs Coats Sjögren Sjogren
Marfan Stevens Johnson Vogt Koyanagi Harada Bruch Bruch's Descemet Descemet's Bowman Bowman's Schlemm Schlemm's
Tenon Tenon's Müller Muller Purkinje Horner Horner's Adie Argyll Robertson Bell's Down's Hodgkin Kaposi
Wilms Ewing Merkel Langerhans Sturge Weber Hippel Lindau Moyamoya Susac Terson Purtscher Coats' Mooren Mooren's
Zeiss Topcon Optovue Cirrus Spectralis Pentacam Orbscan Medmont Nidek Haag Streit Tono-Pen Icare
Africa African America American Asia Asian Europe European Australia Australian India Indian China Chinese Japan
Korea Nepal Pakistan Bangladesh Sri Lanka Iran Iraq Turkey Egypt Nigeria Ethiopia Kenya Ghana Brazil Colombia
Mexico Canada Germany France Italy Spain Portugal Netherlands Sweden Norway Denmark Finland Switzerland Austria
Poland Greece Israel Saudi Arabia Emirates Singapore Malaysia Thailand Vietnam Indonesia Philippines Taiwan
Kong Britain England Scotland Wales Ireland Russia Ukraine Chile Peru Argentina Venezuela Delhi Mumbai Chennai
Kolkata Bangalore Bengaluru Hyderabad Pune Kerala Karnataka Gujarat Punjab Rajasthan Maharashtra Tamil Nadu
Andhra Pradesh Telangana Odisha Bengal Bihar Haryana Assam London Paris Boston Baltimore Beijing Shanghai Tokyo
""".split())

_MINOR_WORDS = {"a", "an", "the", "and", "but", "or", "nor", "for", "so", "yet", "as", "at", "by", "in", "of",
                "on", "to", "up", "via", "with", "from", "into", "over", "per", "vs", "versus"}


def _mid_title_words(title: str) -> list[str]:
    """Words of a title other than the first and those starting a subtitle (after ":", "?", "!")."""
    out, first = [], True
    for w in title.split():
        core = re.sub(r"^\W+|\W+$", "", w)
        if not first and core:
            out.append(core)
        first = w.endswith((":", "?", "!")) or (w.endswith(".") and len(core) > 3)
    return out


def is_sentence_cased(title: str) -> bool:
    """Most words lower case: a capital inside such a title marks a proper noun ("colour Doppler")."""
    words = [w for w in _mid_title_words(title) if w.isalpha() or "-" in w]
    lower = sum(1 for w in words if w[:1].islower() and w.lower() not in _MINOR_WORDS)
    caps = sum(1 for w in words if re.fullmatch(r"[A-Z][a-z]+(?:-[A-Za-z][a-z]*)*", w)
               and w.lower() not in _MINOR_WORDS)
    return lower >= 2 and caps < lower


def proper_nouns(*sources: str) -> set[str]:
    """Capitalised words inside the sentence-cased titles among `sources` (the PubMed / CrossRef
    record's title, the author's title): Doppler, Colombia, "Global Burden of Disease Study"."""
    keep = set()
    for t in sources:
        if t and is_sentence_cased(t):
            for core in _mid_title_words(t):
                for part in core.split("-"):
                    if re.fullmatch(r"[A-Z][a-z'’]+", part):
                        keep.add(part)
    return keep


def _dash_compound(core: str) -> bool:
    """"Adults-Screening" stands for "Adults—Screening": a plural noun, then a new phrase."""
    m = re.fullmatch(r"([A-Z][a-z]{3,}[^s\W]s)-([A-Z][a-z]{4,})", core)
    return m is not None


def sentence_case(title: str, keep: set[str] | frozenset = frozenset()) -> str:
    """Lower-case Title-Cased words, keeping the first word, the word after a colon, acronyms
    (ASCO, GLOBOCAN, U.S.), words with inner capitals or digits (mRNA, COVID-19) and proper nouns:
    those in `keep` (see `proper_nouns`) and in PROPER_NOUNS."""
    keep = set(keep) | PROPER_NOUNS
    words = re.split(r"(\s+)", title)
    out, first = [], True
    for w in words:
        if w.isspace() or not w:
            out.append(w)
            continue
        core = re.sub(r"^\W+|\W+$", "", w)
        plain = re.fullmatch(r"[A-Z][a-z'’]+(?:-[A-Za-z][a-z'’]*)*", core) is not None
        if first or not plain:
            out.append(w)
        elif _dash_compound(core):
            left, right = core.split("-")
            out.append(w.replace(core, (left if left in keep else left.lower()) + "-" + right, 1))
        else:
            parts = [p if p in keep else p.lower() for p in core.split("-")]
            out.append(w.replace(core, "-".join(parts), 1))
        first = w.endswith((":", "?", "!")) or (w.endswith(".") and len(core) > 3)
    return "".join(out)


def format_record(r: biblio.Record, style: JournalStyle, p: Parsed | None = None) -> str:
    max_a = style.get("references.max_authors", 6)
    names = _usable_authors(r, p)
    if max_a and len(names) > max_a:
        names = names[:max_a] + [style.get("references.et_al", "et al.").rstrip(".")]
    groups = {f"{s} {i}".strip() for s, i in r.authors if not i}
    # Group authors are separated by semicolons: "GBD 2019 Collaborators; Vision Loss Expert Group".
    author_text = ""
    for k, n in enumerate(names):
        if k:
            author_text += "; " if n in groups or names[k - 1] in groups else ", "
        author_text += n
    title = re.sub(r"\s+", " ", _usable_title(r, p)).strip()
    if title.endswith("."):
        title = title[:-1]
    if style.get("references.title_sentence_case"):
        title = sentence_case(title, proper_nouns(r.title, p.title if p else ""))
    if style.get("references.capitalize_after_colon"):
        title = re.sub(r":\s([a-z])", lambda m: ": " + m.group(1).upper(), title)
    end = "" if title.endswith(("?", "!")) else "."
    suppl = next((x for x in (r.issue, r.volume) if x and re.search(r"suppl", x, re.I)), "")
    if suppl:  # PubMed puts supplements in Volume ("45 Suppl 1") or Issue
        issue = "(" + re.sub(r"(?i)^.*?suppl\w*\.?\s*", "Suppl ", suppl).strip() + ")"
    elif r.issue and not style.get("references.drop_issue", True):
        issue = f"({r.issue})"
    else:
        issue = ""
    volume = re.sub(r"\s*\(?suppl.*$", "", r.volume, flags=re.I)
    pages = format_pages(r.pages, style)
    cite = f"{journal_name(r, style)} {r.year};{volume}{issue}"
    cite += f":{pages}" if pages else ""
    return f"{author_text}. {title}{end} {cite}."


_ERRATUM = re.compile(
    r"\b(?P<label>Erratum in|Corrigendum in|Correction in)\s*:\s*(?P<journal>[A-Z][^;:]*?)\.?\s+"
    r"(?P<year>(?:19|20)\d{2})(?:\s+[A-Z][a-z]{2,8}(?:\s+\d{1,2})?)?\s*;\s*(?P<vol>\d+)\s*(?:\((?P<issue>[^)]*)\))?"
    r"\s*:\s*(?P<pages>[A-Za-z]?\d+[A-Za-z]?(?:\s*[-–]\s*[A-Za-z]?\d+[A-Za-z]?)?)", re.I)


def errata(text: str, style: JournalStyle) -> str:
    """" Erratum in: Lancet Glob Health 2021;9:e408." for each erratum notice in the author's entry
    (PubMed's "Erratum in: Lancet Glob Health. 2021 Apr;9(4):e408."), styled like the main
    citation: no periods in the journal name, no month, issue and doi as the style says."""
    out = []
    for m in _ERRATUM.finditer(text):
        label = m.group("label")[0].upper() + m.group("label")[1:].lower()
        journal = re.sub(r"\s+", " ", m.group("journal").replace(".", " ")).strip()
        issue = f"({m.group('issue')})" if m.group("issue") and not style.get("references.drop_issue", True) else ""
        out.append(f" {label}: {journal} {m.group('year')};{m.group('vol')}{issue}:"
                   f"{format_pages(m.group('pages'), style)}.")
    return "".join(out)


def resolve(text: str, style: JournalStyle, fields: dict | None = None) -> Resolution:
    p = from_fields(text, fields) if fields else parse(text)
    journal_like = p.kind == "journal_article" if p.kind else looks_like_journal_article(text)
    if not journal_like:
        from . import refs_other  # books, links, conference papers, reports, theses

        return refs_other.resolve(text, style, p)
    record, why = find(p)
    if not record:
        return Resolution(QUERY, p, message=why)
    if not (record.volume and record.year):
        return Resolution(QUERY, p, record, message="Record has no volume yet (ahead of print?)")
    formatted = format_record(record, style, p) + errata(text, style)
    t, _, _ = score(p, record)
    if fold(formatted) == fold(text) and formatted.rstrip() == text.rstrip():
        return Resolution(MATCHED, p, record, formatted, score=t)
    return Resolution(FORMAT_FIXED, p, record, formatted, score=t)


def resolve_all(texts: list[str], style: JournalStyle) -> list[Resolution]:
    """Resolve a reference list; Claude splits the entries first when enabled and a key is set."""
    use_ai = style.get("references.ai_parse") and ai_refs.available()
    fields = ai_refs.parse_batch(texts) if use_ai else [None] * len(texts)
    return [resolve(t, style, f) for t, f in zip(texts, fields)]
