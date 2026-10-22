"""Reference engine: parse an entry, find the published record, and rebuild it in journal style.

A record is used only when it matches strictly: its title in the entry, the entry's first author
(accents, case, particles and compound surnames aside), and the year (one year apart at most: an
epub and its issue). An entry without authors needs a near-complete title, the year and the journal.
Anything less becomes a query for the editor; the entry text is never replaced on a guess.
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


def from_fields(text: str, f: dict) -> Parsed:
    """Parsed entry from Claude's fields. Each field must occur in the entry's own text (Claude can hand
    an entry its neighbour's fields); one that does not is dropped and the regex parse used instead."""
    p = parse(text)  # regex DOI / PMID / year as a base
    no_authors = f.get("authors") == [] and bool(f.get("title"))
    f = ai_refs.check_fields(text, f)
    if f is None:  # nothing of Claude's split is in this entry: it belongs to another one
        return p
    p.kind = f.get("kind", "")
    if f.get("title"):
        p.title = f["title"].strip().rstrip(".")
    authors = [a.strip() for a in f.get("authors", []) if a.strip()]
    if authors:
        p.authors = authors
    elif no_authors and f.get("title"):  # an entry that starts with its title: the regex took title words for names
        p.authors = []
    if re.fullmatch(r"(19|20)\d{2}", f.get("year", "").strip()):
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


_PARTICLES = frozenset("van von der den de del della di da dos das du la le ten ter al el bin ben st".split())
_LETTERS = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "ł": "l",
                          "Ł": "L", "đ": "d", "Đ": "D", "ı": "i", "þ": "th", "ð": "d"})
_GROUP_WORDS = re.compile(r"\b(?:group|collaborat\w*|committee|consortium|study|society|association|organi[sz]ation|"
                          r"investigators|network|initiative|council|board|team|panel|working|task force|writing|"
                          r"foundation|institute|centers?|centres?|college|academy|federation|world|national|international)\b|\d", re.I)


def fold_name(s: str) -> str:
    """A name for comparison: accents off, ø/æ/ß/ł/ı spelled out, hyphens and apostrophes as spaces."""
    return fold(s.translate(_LETTERS).replace("-", " ").replace("'", " ").replace("’", " "))


def _name_words(name: str) -> list[str]:
    """Surname words of a name: initials after the first word ("Shields CL", "Rife DC", "Smith J-P")
    and particles ("de", "van der") left out, unless nothing else is left ("Du Z", "Le ANT")."""
    words = [w for w in re.split(r"[\s,]+", name.translate(_LETTERS).strip()) if w]
    words = words[:1] + [w for w in words[1:]
                         if not re.fullmatch(r"(?:[A-Z]\.?-?){1,4}|(?:[A-Z]\.)+[A-Z]?|Jr\.?|Sr\.?|II|III|IV|\d+(?:st|nd|rd|th)?", w)]
    if len(words) > 1 and re.fullmatch(r"(?:[A-Z]\.){1,3}", words[0]):  # "A. Roddy Mitchell"
        words = words[1:]
    out = [x for w in words for x in fold_name(w).split()]
    return [x for x in out if x not in _PARTICLES] or out


def _is_group(name: str, initials: str = "") -> bool:
    return not initials and (bool(_GROUP_WORDS.search(name)) or len(name.split()) > 3)


def same_surname(a: str, b: str) -> bool:
    """Two surnames are one person's: accents, case, hyphens and particles aside ("De Potter" / "Potter",
    "Jørgensen" / "Jorgensen", "Abdel-Latif" / "Abdel Latif"), one part of a compound surname ("Lucas
    Espeza" / "Lucas"), or a one-letter slip in a long name ("Maiwaid" / "Maiwald")."""
    wa, wb = _name_words(a), _name_words(b)
    if not wa or not wb:
        return False
    ja, jb = "".join(wa), "".join(wb)
    if ja == jb:
        return True
    if any(len(w) >= 3 and w in wb for w in wa):
        return True
    return min(len(ja), len(jb)) >= 5 and fuzz.ratio(ja, jb) >= 80


def _acronym(name: str) -> str:
    return "".join(w[0] for w in re.findall(r"[A-Za-z][\w'’-]*", name) if w[0].isupper())


def _group_match(a: str, b: str) -> bool:
    """Two group names are one group: most words shared, or one the other's acronym ("ADA" for
    "American Diabetes Association Professional Practice Committee")."""
    stop = {"the", "of", "and", "for", "on"}
    wa, wb = set(fold(a).split()) - stop, set(fold(b).split()) - stop
    if wa and wb and len(wa & wb) >= 0.6 * min(len(wa), len(wb)):
        return True
    for x, y in ((a, b), (b, a)):
        x = x.strip().rstrip(".")
        if re.fullmatch(r"[A-Z]{3,8}", x) and _acronym(y).startswith(x):
            return True
    return False


def author_agrees(p: Parsed, r: biblio.Record) -> bool:
    """The record's first author is the entry's first author. A group author the record lists first
    may stand before the people ("GBD 2019 Collaborators"), or come after them in the entry."""
    if not r.authors or not p.authors:
        return False
    first = p.authors[0]
    s, i = r.authors[0]
    if _is_group(s, i):
        if _group_match(first, s):
            return True
        people = [(s2, i2) for s2, i2 in r.authors if not _is_group(s2, i2)]
        if not people:
            return False
        s, i = people[0]
    if _is_group(first) and any(_group_match(first, g) for g, gi in r.authors if _is_group(g, gi)):
        return True
    if not re.search(r"[a-z]{2}", fold_name(s)):  # "S.B.", "K": not a surname to compare
        return False
    entry = _name_words(first)
    if same_surname(s, p.first_surname) or same_surname(s, first):
        return True
    return any(len(w) >= 3 and same_surname(s, w) for w in entry)  # "A. Roddy Mitchell", "Karl Kingsley"


def year_close(p: Parsed, r: biblio.Record) -> bool:
    """Same year, or one year apart (published online one year, in an issue the next)."""
    return bool(p.year and r.year[:4].isdigit()) and abs(int(p.year) - int(r.year[:4])) <= 1


def journal_agrees(text: str, r: biblio.Record) -> bool:
    """The record's journal is written in the entry, in full or abbreviated ("J Clin Med" in
    "Journal of Clinical Medicine")."""
    names = [r.journal]
    j = _record_journal(r)
    if j:
        names += [j.title, j.med_abbr, j.iso_abbr]
    t = fold(text.replace("&", " and ")).split()
    skip = {"the", "of", "and", "for", "in", "on", "de", "la"}
    for name in names:
        words = [w for w in fold(re.sub(r"\s*\([^)]*\)\s*$", "", name or "")).split() if w not in skip]
        if words and any(_abbreviates(words, t, i) for i in range(len(t))):
            return True
    return False


def _abbreviates(abbr: list[str], words: list[str], i: int) -> bool:
    """`abbr` ("j", "clin", "med") abbreviates the entry's words from position i ("journal of clinical
    medicine"); a letter or two may differ ("gynaecol" / "gynecology")."""
    for a in abbr:
        while i < len(words) and words[i] in ("the", "of", "and", "for", "in", "on", "de", "la") and not words[i].startswith(a):
            i += 1
        if i >= len(words):
            return False
        w = words[i]
        if not (w.startswith(a) or len(a) >= 4 and fuzz.ratio(a, w[:len(a) + 1]) >= 80):
            return False
        i += 1
    return True


def _first_page(pages: str) -> str:
    m = re.match(r"\s*[A-Za-z]?0*(\d+)", pages or "")
    return m.group(1) if m else ""


def citation_conflicts(text: str, r: biblio.Record) -> bool:
    """The entry's own volume and first page both differ from the record's ("Lancet 2012;380:1419"
    against "Semin Reprod Med 2012;30:432"): the same title in another journal, a reply, a reprint.
    One of the two differing is the author's slip and no conflict."""
    m = YEAR_VOL.search(text)
    vol, page = re.sub(r"\D.*", "", r.volume or ""), _first_page(r.pages)
    if not m or not vol or not page:
        return False
    return m.group(2).lstrip("0") != vol.lstrip("0") and _first_page(m.group(4)) != page


def _record_journal(r: biblio.Record):
    d = journals.directory()
    if not d:
        return None
    return next((x for x in map(d.lookup, r.issn) if x), None) or d.lookup(r.journal)


def journal_conflicts(p: Parsed, r: biblio.Record) -> bool:
    """The entry names a journal that NLM knows and it is not the record's ("Am J Prev Med" against
    "J Gen Intern Med"): a guideline or statement printed in several journals, or another paper.
    An unknown or unrecognised journal name is no conflict."""
    d = journals.directory()
    hint = biblio.journal_hint(p.text, p.title) if d and p.title else ""
    if not hint or journal_agrees(p.text, r) or same_citation(p.text, r):
        return False
    named, rec = d.lookup(hint), _record_journal(r)
    return bool(named and rec) and rec.nlm_id != named.nlm_id


def same_citation(text: str, r: biblio.Record) -> bool:
    """The entry's own volume and first page are the record's: the same paper, whatever the journal
    name the author wrote ("J Mens Health 2025;25:1150" for BMC Public Health 2025;25:1150)."""
    m = YEAR_VOL.search(text)
    vol, page = re.sub(r"\D.*", "", r.volume or ""), _first_page(r.pages)
    return bool(m and vol and page) and m.group(2).lstrip("0") == vol.lstrip("0") and _first_page(m.group(4)) == page


def score(p: Parsed, r: biblio.Record) -> tuple[float, bool, bool]:
    """(title similarity 0-100, first author matches, year matches)."""
    t = fuzz.token_sort_ratio(fold(p.title), fold(r.title)) if p.title else 0
    if len(r.title) > 25:  # our title split can stop early ("Melanoma. Clinical features...")
        t = max(t, fuzz.partial_ratio(fold(r.title), fold(p.text)))
    return t, author_agrees(p, r), bool(p.year) and p.year == r.year


def accept(p: Parsed, r: biblio.Record, by_id: bool = False) -> bool:
    """The record is the entry's paper. Title, first author and year must all agree (the title may be
    a shorter one contained in the entry's only because author and year agree too). An entry without
    authors needs its title almost word for word, the year and the journal. A DOI / PMID the author
    gave is checked the same way, with more room on the title (translated or retitled papers)."""
    t, author, year = score(p, r)
    near = year or year_close(p, r)
    if not p.authors or not r.authors:  # nobody to compare: the title, year and journal must carry it
        whole = fuzz.token_sort_ratio(fold(p.title), fold(r.title)) if p.title else 0
        if by_id:
            return max(whole, t) >= 90 and (near or not p.year)
        return whole >= 97 and near and journal_agrees(p.text, r) and not citation_conflicts(p.text, r)
    if not author:
        return False
    if not by_id and (citation_conflicts(p.text, r) or journal_conflicts(p, r)):
        return False
    if by_id:  # the author may leave out a subtitle: their title is then the start of the record's
        if len(p.title) > 25:
            t = max(t, fuzz.partial_ratio(fold(p.title), fold(r.title)))
        return t >= 70 and (near or not p.year)
    if not p.year:
        return t >= 95 and journal_agrees(p.text, r)
    return t >= 90 and year or t >= 95 and near


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


DIFFERS = "differs from the published article"


def _id_mismatch(p: Parsed, r: biblio.Record, what: str) -> str:
    """Why a record found by the entry's DOI / PMID was not used: another paper, or the paper with a
    first author or year the entry does not have (the author's slip or a wrong identifier)."""
    t, author, _ = score(p, r)
    if t >= 90:
        diff = [x for x, ok in (("first author", author or not p.authors), ("year", year_close(p, r) or not p.year)) if not ok]
        return f"{what}: the {' and '.join(diff) or 'entry'} {DIFFERS}"
    return f"{what} points to a different article"


def _find(p: Parsed) -> tuple[biblio.Record | None, str]:
    if p.pmid:
        for r in biblio.pubmed_fetch([p.pmid]):
            return (r, "") if accept(p, r, by_id=True) else (None, _id_mismatch(p, r, f"PMID {p.pmid}"))
    if p.doi:
        ids = biblio.pubmed_search(f'"{p.doi}"[aid]', retmax=1)
        recs = biblio.pubmed_fetch(ids) or [r for r in [biblio.crossref_doi(p.doi)] if r]
        for r in recs:
            return (r, "") if accept(p, r, by_id=True) else (None, _id_mismatch(p, r, f"DOI {p.doi}"))
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


def _bad_name(surname: str, initials: str) -> bool:
    """A record's author that is not a usable name: no surname, initials only ("S.B.", "B" in "B S"),
    or a person's full name in the surname with no initials ("Saima Zeb")."""
    letters = re.sub(r"[^A-Za-z\u00C0-\u024F]", "", surname)
    if not surname.strip() or len(letters) <= 1:
        return True
    if initials and (re.search(r"[,;]", surname) or re.fullmatch(r"(?:[A-Z]\.?\s*){1,3}", surname.strip())):
        return True  # "Sulistyorini,", "P S", "AP": initials or a split-up name in the surname field
    if re.fullmatch(r"\s*(?:[A-Z]\.\s*){1,4}", surname) or (not initials and re.fullmatch(r"[A-Z]{1,2}", letters)):
        return True
    if not initials and re.search(r"\b(?:department|university|hospital|faculty|school|division)\b", surname, re.I) \
            and "," in surname:
        return True  # an affiliation in the author list ("Department of ..., ... University, Tehran")
    return not initials and not _is_group(surname) and len(surname.split()) in (2, 3) \
        and all(re.fullmatch(r"[A-Z][a-z]+", w) for w in surname.split())


def malformed_authors(r: biblio.Record) -> bool:
    return any(_bad_name(s, i) for s, i in r.authors)


def _usable_authors(r: biblio.Record, p: Parsed | None) -> list[str]:
    names = [f"{s} {i}".strip() for s, i in r.authors]
    if p and p.authors and (malformed_authors(r) or not r.authors):
        return p.authors  # the record's list is broken: keep the author's names (and query them)
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
    # Capitalised words count by runs: a name is one ("University of Benin Teaching Hospital", "Benin City").
    caps, prev = 0, False
    for w in words:
        if w.lower() in _MINOR_WORDS:
            continue
        cap = re.fullmatch(r"[A-Z][a-z]+(?:-[A-Za-z][a-z]*)*", w) is not None
        caps += cap and not prev
        prev = cap
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


DICTIONARY_URL = "https://raw.githubusercontent.com/LibreOffice/dictionaries/master/en/en_US.dic"
_dictionary: tuple[set[str], set[str]] | None = None


def _dictionary_words() -> tuple[set[str], set[str]]:
    """(lower-case words, capitalised-only words) of LibreOffice's en_US Hunspell dictionary, which lists
    names capitalised ("Doppler", "Colombia", "Heidelberg"). Empty when it cannot be fetched (offline)."""
    global _dictionary
    if _dictionary is None:
        lower, caps = set(), set()
        try:
            from .copyedit.cache import cache_dir, download
            with open(download(DICTIONARY_URL, cache_dir("dict", "en_US.dic")), encoding="utf-8", errors="ignore") as f:
                next(f, None)
                for line in f:
                    w = line.split("/", 1)[0].strip()
                    (caps if w[:1].isupper() else lower).add(w)
        except Exception:
            pass
        caps -= {w.capitalize() for w in lower}
        _dictionary = (lower, caps)
    return _dictionary


_EPONYM_NEXT = re.compile(r"\s+(?:syndrome|disease|sign|test|phenomenon|procedure|classification|score|criteria)\b", re.I)


def dictionary_proper_nouns(title: str) -> set[str]:
    """Words of a Title-Cased title that are names: listed only capitalised in the dictionary ("Doppler"),
    an unlisted capitalised word before syndrome / disease ("Cowden Syndrome"), and "United States"."""
    lower, caps = _dictionary_words()
    keep = set()
    for m in re.finditer(r"[A-Z][a-z'’]+", title):
        w = m.group(0)
        if w in caps:
            keep.add(w)
        elif lower and w.lower() not in lower and _EPONYM_NEXT.match(title, m.end()):
            keep.add(w)
    if re.search(r"\bUnited\s+States\b", title):
        keep |= {"United", "States"}
    return keep


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
        title = sentence_case(title, proper_nouns(r.title, p.title if p else "") | dictionary_proper_nouns(title))
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
    lead = f"{author_text}. " if author_text else ""  # a paper without authors starts with its title
    return f"{lead}{title}{end} {cite}."


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
    queries = []
    if malformed_authors(record) or not record.authors and p.authors:
        if not p.authors:
            return Resolution(QUERY, p, record, message="Record's author names are malformed")
        queries.append(("references.author_names", {}))  # the author's names are kept: ask to check them
    formatted = format_record(record, style, p) + errata(text, style)
    t, _, _ = score(p, record)
    status = MATCHED if fold(formatted) == fold(text) and formatted.rstrip() == text.rstrip() else FORMAT_FIXED
    return Resolution(status, p, record, formatted, score=t, queries=queries)


def resolve_all(texts: list[str], style: JournalStyle) -> list[Resolution]:
    """Resolve a reference list; Claude splits the entries first when enabled and a key is set."""
    use_ai = style.get("references.ai_parse") and ai_refs.available()
    fields = ai_refs.parse_batch(texts) if use_ai else [None] * len(texts)
    return [resolve(t, style, f) for t, f in zip(texts, fields)]
