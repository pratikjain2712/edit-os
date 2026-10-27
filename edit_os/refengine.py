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
    styled: bool = False  # not verified, but `formatted` is the entry styled from the author's own text (refstyle)


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


# APA: "Pekrun, R., Goetz, T., & Perry, R. P. (2002). Title. Journal, 37(2), 91–105."
APA_HEAD = re.compile(r"^\s*(?P<authors>[^()]{2,600}?)\s*\((?P<year>(?:19|20)\d{2})[a-z]?(?:,[^)]*)?\)\.\s*(?P<rest>.+)$", re.S)
APA_NAME = re.compile(r"(?:^|,\s*|&\s*|\band\s+)(?:&\s*)?(?P<surname>[^\W\d][^,&()]*?),\s*(?P<initials>(?:[A-Z][a-z]?\.\s*-?\s*)+)(?=,|\s*&|\s*$|\s*\(|\s+and\b)")


def apa_authors(head: str) -> list[str]:
    """"Mu, T., Wang, F., & Xu, Y." -> ["Mu T", "Wang F", "Xu Y"]; [] when it is not such a list."""
    head = re.sub(r",?\s*et al\.?", "", head).strip().rstrip(",")
    out, pos = [], 0
    for m in APA_NAME.finditer(head):
        if head[pos:m.start()].strip(" ,&"):
            return []
        out.append(m.group("surname").strip() + " " + re.sub(r"[^A-Z-]", "", m.group("initials")).strip("-"))
        pos = m.end()
    return out if out and not head[pos:].strip(" ,.&") else []


def parse(text: str) -> Parsed:
    p = Parsed(text=text)
    if m := DOI.search(text):
        p.doi = m.group(1).rstrip(".,;")
    if m := PMID.search(text):
        p.pmid = m.group(1)
    apa = APA_HEAD.match(text)
    if apa and (names := apa_authors(apa.group("authors"))):
        p.authors, p.year = names, apa.group("year")
        t = re.match(r"(.+?[.?!])\s+(?=[A-Z])", apa.group("rest"))
        p.title = (t.group(1) if t else apa.group("rest")).strip().rstrip(".")
        return p
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
    if citation(text) or DOI.search(text) or PMID.search(text):
        return True
    from .refstyle import split  # "Journal, 12(3), pp. 140-148" after a Harvard "(2019)": the author's citation
    a = split(text)
    return bool(a and a.volume and a.title and a.journal)


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
    """Same year, or one year apart (published online one year, in an issue the next), against the
    record's print year or any of its other dates (online first: "2024" for a 2026 issue)."""
    years = [y[:4] for y in [r.year] + list(r.other_years) if y[:4].isdigit()]
    return bool(p.year) and any(abs(int(p.year) - int(y)) <= 1 for y in years)


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


# APA: "(2002). Title. Educational Psychologist, 37(2), 91–105." / "Information Sciences, 622, 178–210."
APA_CITE = re.compile(r"\(((?:19|20)\d{2})[a-z]?(?:,[^)]*)?\)\..*?,\s*(\d+)\s*(?:\(([^)]*)\))?\s*,\s*"
                      r"(?:pp?\.\s*)?([A-Za-z]?\d+[A-Za-z]?(?:\s*[-–]\s*[A-Za-z]?\d+[A-Za-z]?)?)(?![\d/])")
# EndNote "Numbered": "Journal of Research and Health, 2020. 10(1): p. 59-66."
ENDNOTE_CITE = re.compile(r"\b((?:19|20)\d{2})\.\s*(\d+)\s*(?:\(([^)]*)\))?\s*:\s*(?:pp?\.\s*)?"
                          r"([A-Za-z]?\d+[A-Za-z]?(?:\s*[-–]\s*[A-Za-z]?\d+[A-Za-z]?)?)")


def citation(text: str) -> tuple[str, str, str, str] | None:
    """(year, volume, issue, pages) as the entry gives them: Vancouver "2002;37(2):91-105", APA
    "(2002). ... Journal, 37(2), 91–105" or EndNote "Journal, 2002. 37(2): p. 91-105"."""
    for rx in (YEAR_VOL, APA_CITE, ENDNOTE_CITE):
        m = rx.search(text)
        if m:
            return m.group(1), m.group(2), m.group(3) or "", m.group(4)
    return None


def citation_conflicts(text: str, r: biblio.Record) -> bool:
    """The entry's own volume and first page both differ from the record's ("Lancet 2012;380:1419"
    against "Semin Reprod Med 2012;30:432"): the same title in another journal, a reply, a reprint.
    One of the two differing is the author's slip and no conflict."""
    c = citation(text)
    vol, page = re.sub(r"\D.*", "", r.volume or ""), _first_page(r.pages)
    if not c or not vol or not page:
        return False
    return c[1].lstrip("0") != vol.lstrip("0") and _first_page(c[3]) != page


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
    c = citation(text)
    vol, page = re.sub(r"\D.*", "", r.volume or ""), _first_page(r.pages)
    return bool(c and vol and page) and c[1].lstrip("0") == vol.lstrip("0") and _first_page(c[3]) == page


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
    return t >= 90 and year or t >= 95 and near or identified(p, r)


_WORD = re.compile(r"[^\W_]+(?:['’′][^\W_]+)*")


def title_extends(short: str, long: str) -> bool:
    """`short` is `long` without its subtitle or last words: "Academic emotions in students'
    self-regulated learning and achievement" for "...: A program of qualitative and quantitative research".
    At least four words; the rest of `long` starts a subtitle (":", ",", "-", "?", "(") or is at most
    a third of it."""
    sw = [fold(w) for w in _WORD.findall(short)]
    lw = list(_WORD.finditer(long))
    if len(sw) < 4 or len(lw) <= len(sw) or [fold(m.group(0)) for m in lw[:len(sw)]] != sw:
        return False
    rest = long[lw[len(sw) - 1].end():].lstrip()
    return rest[:1] in (":", ",", "-", "–", "—", "?", "(", ".", ";") or len(lw) - len(sw) <= len(lw) / 3


def same_volume(text: str, r: biblio.Record) -> bool:
    c = citation(text)
    vol = re.sub(r"\D.*", "", r.volume or "")
    return bool(c and vol) and c[1].lstrip("0") == vol.lstrip("0")


def identified(p: Parsed, r: biblio.Record) -> bool:
    """Accepted on more than the title: the first author and the year, the journal named in the entry,
    and either the entry's title is the record's without its subtitle (same volume), or the entry's
    own volume and first page are the record's and every word of the entry's title is in the record's
    ("environmental health monitoring" for "environmental health/accessibility monitoring services")."""
    t, author, year = score(p, r)
    if not (author and year and p.title and journal_agrees(p.text, r)) or journal_conflicts(p, r):
        return False
    if same_volume(p.text, r) and (title_extends(p.title, r.title) or title_extends(r.title, p.title)):
        return True
    return same_citation(p.text, r) and _words_within(p.title, r.title)


def _stem(w: str) -> str:
    return re.sub(r"(?<=[a-z]{3})(?:ies|es|s)$", "", w)


def _words_within(short: str, long: str) -> bool:
    """Every word of `short` (four at least) is in `long`: the author dropped words of the title, changed
    none ("health monitoring" for "health/accessibility monitoring services"). "Effects of intermittent
    tendon pressure" is not within "Effect of tendon pressure": another paper."""
    sw = [_stem(w) for w in fold(short).split()]
    lw = {_stem(w) for w in fold(long).split()}
    return len(sw) >= 4 and all(w in lw for w in sw)


def _best(p: Parsed, records: list[biblio.Record]) -> biblio.Record | None:
    ok = [r for r in records if accept(p, r)]
    # The best title; among equal ones, the record of the entry's year and with a volume (the article, not
    # a dataset or preprint of it dated by another year: "PsycTESTS Dataset 2011" for a 1988 paper).
    return max(ok, key=lambda r: (score(p, r)[0], score(p, r)[2], bool(r.volume))) if ok else None


def _usable(r: biblio.Record | None) -> bool:
    return r is not None and bool(r.volume and r.year)


def _confirmed(p: Parsed, r: biblio.Record) -> bool:
    """A record found by the wider searches must, besides passing `accept`, carry the entry's first
    author and have its title written in the entry itself: a title that only matches Claude's split
    of the entry, or a shorter title contained in the author's, is not enough."""
    _, author, _ = score(p, r)
    return author and (fuzz.partial_ratio(fold(r.title), fold(p.text)) >= 90 or identified(p, r))


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
    if not found:  # an abbreviated title ("home OCT guided ... nAMD"): the entry's own citation finds it
        r = _by_citation(p)
        if r:
            return r, ""
    return (found, "") if found else (None, why)


# --- title-only search ---------------------------------------------------------------------
#
# The entry's first author can be wrong (an author list copied from another paper, the authors in another
# order) and then every search above, held to the first author, finds nothing although the paper is indexed.
# A search by the title alone is used only under stricter terms: the whole title (almost) word for word, the
# year, the journal the entry names (and none of its volume / first page against the record), and either the
# entry's first author somewhere in the record's author list or a long title with the entry's own volume and
# first page. One paper only: two papers with that title leave the entry to the editor.

TITLE_ONLY_MIN = 95       # whole-title similarity to accept
TITLE_NEAR_MIN = 97       # ... to tell the editor the paper with this title differs from the entry
TITLE_LONG_WORDS = 10     # a title this long, with the entry's volume and first page, needs no author


def _title_words(title: str) -> int:
    return len(fold(title).split())


def _clean_title(title: str) -> str:
    """The title without a year the regex split ran into ("... study. 2024")."""
    return re.sub(r"[.,;]?\s*\(?(?:19|20)\d{2}\)?\.?\s*$", "", title or "").strip().rstrip(".")


def _whole_title(p: Parsed, r: biblio.Record) -> float:
    t = _clean_title(p.title)
    return fuzz.token_sort_ratio(fold(t), fold(_clean_title(r.title))) if t and r.title else 0


def _same_paper(a: biblio.Record, b: biblio.Record) -> bool:
    if a.doi and b.doi:
        return a.doi.lower() == b.doi.lower()
    if a.pmid and b.pmid:
        return a.pmid == b.pmid
    vol = lambda r: re.sub(r"\D.*", "", r.volume or "")  # noqa: E731
    return a.year == b.year and vol(a) == vol(b) and _first_page(a.pages) == _first_page(b.pages)


def _same_last_page(text: str, r: biblio.Record) -> bool:
    """The entry's last page is the record's, where both give a range ("54-60" is not "54-56")."""
    c = citation(text)
    mine, theirs = (re.split(r"\s*[-–]\s*", x.strip()) for x in ((c[3] if c else ""), r.pages or ""))
    if len(mine) != 2 or len(theirs) != 2:
        return True
    a, b = re.sub(r"\D", "", mine[1]), re.sub(r"\D", "", theirs[1])
    full = lambda first, last: first[:max(0, len(first) - len(last))] + last  # noqa: E731  "54-6" -> "56"
    return full(re.sub(r"\D", "", mine[0]), a) == full(re.sub(r"\D", "", theirs[0]), b)


def _author_in_list(p: Parsed, r: biblio.Record) -> bool:
    """The entry's first author is one of the record's authors (the author list in another order)."""
    if not p.authors or _is_group(p.authors[0]):
        return False
    first = p.authors[0]
    return any(i and (same_surname(s, p.first_surname) or same_surname(s, first)) for s, i in r.authors)


def _names_journal_in_entry(p: Parsed) -> bool:
    return bool(citation(p.text) or biblio.journal_hint(p.text, p.title))


def title_candidates(p: Parsed) -> list[biblio.Record]:
    """PubMed by the title's words, CrossRef by the title and year (both cached; PubMed's is the search `_find`
    already made)."""
    title = _clean_title(p.title)
    words = " ".join(w for w in re.sub(r"[^\w\s-]", " ", title).split() if len(w) > 2)
    out = biblio.pubmed_fetch(biblio.pubmed_search(f"{words}[ti]")) if words else []
    flat = re.sub(r"\s*[:.]\s+", ": ", title)
    out += biblio.crossref_search(" ".join(x for x in (flat, p.year) if x))
    return out


def _only_paper(p: Parsed, r: biblio.Record) -> bool:
    """No other paper (with a volume) has the entry's title: an entry without its journal cannot say which of two
    is meant (the same title and authors in two journals)."""
    return not any(x.title and _usable(x) and _whole_title(p, x) >= TITLE_ONLY_MIN and not _same_paper(x, r)
                   for x in title_candidates(p))


def find_by_title(p: Parsed) -> tuple[biblio.Record | None, str, list[tuple[str, dict]]]:
    """(record, why, queries) from a title-only search, held to the terms above. A record accepted whose first
    author is not the entry's comes with a query; a paper with this (long) title that differs from the entry in
    its journal, year or volume is no match, and the editor is told which fields differ."""
    if not p.title or _title_words(_clean_title(p.title)) < 6 or not p.year:
        return None, "", []
    # (a preprint or a dataset of the paper, with no volume, is not another paper)
    close = [r for r in title_candidates(p) if r.title and _usable(r) and _whole_title(p, r) >= TITLE_ONLY_MIN]
    papers: list[biblio.Record] = []
    for r in close:
        if not any(_same_paper(r, q) for q in papers):
            papers.append(r)
    if len(papers) != 1:
        return None, "", []  # nothing, or two papers with this title: not ours to choose
    # the PubMed record of the paper when there is one (its journal abbreviation and author list)
    r = next((x for x in close if x.source == "pubmed" and _same_paper(x, papers[0])), papers[0])
    named = _names_journal_in_entry(p)
    author = author_agrees(p, r)
    year = bool(p.year) and (p.year == r.year or year_close(p, r) and same_volume(p.text, r))
    journal = journal_agrees(p.text, r) and not citation_conflicts(p.text, r) and not journal_conflicts(p, r) \
        if named else author  # an entry without its journal (incomplete): the first author must be the record's
    long_unique = _title_words(r.title) >= TITLE_LONG_WORDS and same_citation(p.text, r) and _same_last_page(p.text, r)
    who = author or _author_in_list(p, r) or long_unique
    if year and journal and who and _usable(r):
        return r, "", [] if author else [("references.record_differs", {"what": "first author"})]
    if _whole_title(p, r) >= TITLE_NEAR_MIN and _title_words(r.title) >= 8:
        diff = [x for x, ok in (("first author", author), ("year", year_close(p, r)),
                                ("journal", not named or journal_agrees(p.text, r)),
                                ("volume", not citation(p.text) or same_volume(p.text, r))) if not ok]
        if diff:
            return None, f"Title search: the {' and '.join(diff)} {DIFFERS}", []
    return None, "", []


def incomplete_article(text: str, p: Parsed) -> bool:
    """An entry with authors, a title and a year but no journal or source ("Miri F, ... A quasi-experimental
    study. 2024"): a journal article left unfinished, which the title search may complete."""
    from . import refs_other

    if YEAR_VOL.search(text) or refs_other.URL.search(text) or refs_other.classify(text, p.kind) != refs_other.OTHER:
        return False
    return bool(p.authors and p.year and _title_words(_clean_title(p.title)) >= 6)


_ACRONYM = re.compile(r"^[a-z]?[A-Z]{2,6}[a-z]?s?$")


def _words_for(acronym: str, words: list[str]) -> list[str] | None:
    """The run of `words` whose initials spell `acronym` ("OCT": optical coherence tomography)."""
    letters = acronym.lower().rstrip("s") if acronym.endswith("s") and acronym[:-1].isupper() else acronym.lower()
    n = len(letters)
    for i in range(len(words) - n + 1):
        if "".join(w[:1].lower() for w in words[i:i + n]) == letters:
            return words[i:i + n]
    return None


def expand_acronyms(title: str, full: str) -> tuple[str, int]:
    """`title` with its acronyms written out as `full` (the record's title) has them; and how many were."""
    spaced = [w.strip(".,:;()[]") for w in full.split()]
    split = [x for w in spaced for x in w.split("-")]
    out, n = [], 0
    for w in title.split():
        core = w.strip(".,:;()[]")
        run = _words_for(core, split) or _words_for(core, spaced) if _ACRONYM.match(core) else None
        if run:
            out.append(" ".join(run))
            n += 1
        else:
            out.append(w)
    return " ".join(out), n


def abbreviated_title_matches(p: Parsed, r: biblio.Record) -> bool:
    """The entry is the record's paper with acronyms in its title: first author, year, journal and volume all
    the record's, no first page that differs, and the title word for word once its acronyms are written out."""
    _, author, year = score(p, r)
    if not (author and year and p.title and journal_agrees(p.text, r)) or not _volume_agrees(p.text, r):
        return False
    c = citation(p.text)
    if c and c[3] and _first_page(r.pages) and _first_page(c[3]) != _first_page(r.pages):
        return False
    expanded, n = expand_acronyms(p.title, r.title)
    return n > 0 and fuzz.token_sort_ratio(fold(expanded), fold(r.title)) >= 90


_ENTRY_VOLUME = re.compile(r"\b((?:19|20)\d{2})[^;:\n]{0,20};\s*(\d+)")


def _volume_agrees(text: str, r: biblio.Record) -> bool:
    m = _ENTRY_VOLUME.search(text)
    vol = re.sub(r"\D.*", "", r.volume or "")
    return bool(m and vol) and m.group(2).lstrip("0") == vol.lstrip("0")


def _by_citation(p: Parsed) -> biblio.Record | None:
    """PubMed by first author, year and volume, for an entry whose title PubMed's search cannot match."""
    m = _ENTRY_VOLUME.search(p.text)
    if not (m and p.first_surname and p.title and p.year) or not any(_ACRONYM.match(w.strip(".,:;()[]"))
                                                                    for w in p.title.split()):
        return None
    term = f"{p.first_surname}[au] AND {p.year}[dp] AND {m.group(2)}[vi]"
    ok = [r for r in biblio.pubmed_fetch(biblio.pubmed_search(term)) if abbreviated_title_matches(p, r)]
    return ok[0] if len(ok) == 1 and _usable(ok[0]) else None


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
    if m.group(2) == m.group(4) and m.group(3) in ("", m.group(1)):  # "123-123", "e68918-e68918": one page
        return f"{m.group(1)}{m.group(2)}"
    last = _compress(m.group(2), m.group(4)) if style.get("references.page_range_compress", True) else m.group(4)
    prefix2 = "" if m.group(3) == m.group(1) else m.group(3)  # e524-e532 -> e524-32
    return f"{m.group(1)}{m.group(2)}{dash}{prefix2}{last}"


def journal_name(r: biblio.Record, style: JournalStyle) -> str:
    """PubMed's MedlineTA; for a CrossRef record, NLM's abbreviation when NLM lists the journal, else
    (`references.non_indexed_journal: full`; stylesheets: "Use complete name of the journal for non-indexed
    journals") the full title rather than CrossRef's short one ("ITSE")."""
    name = r.journal
    if r.source == "crossref" and journals.directory():
        j = None
        for issn in r.issn:
            j = journals.directory().lookup(issn)
            if j:
                break
        if not j:
            by_name = journals.directory().lookup(name)
            if by_name and r.journal_full and not _names_journal(by_name, r.journal_full):
                by_name = None  # CrossRef's short title "pie" is NLM's "La Pie", not "Perspectives in Education"
            j = by_name or (r.journal_full and journals.directory().lookup(r.journal_full))
        if j:
            name = j.med_abbr
        elif style.get("references.non_indexed_journal", "full") == "full" and r.journal_full:
            name = _ACRONYM_QUALIFIER.sub("", r.journal_full.strip())  # "... Public Health Science (IJPHS)": the
            # publisher's acronym is no part of the journal's name (NLM's "(Lausanne)", "(Basel)" are kept)
            if name.isupper() and len(name) > 6:  # "JOURNAL OF SUSTAINABILITY SCIENCE" -> "Journal of Sustainability Science"
                name = " ".join(w.lower() if k and w.lower() in _MINOR_WORDS else w.capitalize()
                                for k, w in enumerate(name.split()))
    if style.get("references.journal_drop_qualifier", True):
        name = re.sub(r"\s*\([^)]*\)$", "", name)
    return name.replace(".", "")


# A full title's trailing acronym in brackets: "(IJPHS)", "(GMHC)", "(iJIM)"; not a place ("(Lausanne)").
_ACRONYM_QUALIFIER = re.compile(r"\s*\((?=[A-Za-z&-]*[A-Z][A-Za-z&-]*[A-Z])[A-Za-z&-]{2,12}\)$")


def _names_journal(j, full: str) -> bool:
    """NLM's journal `j` (found by CrossRef's short title) is the journal CrossRef's full title names: the
    same title, or its abbreviation abbreviates the full title word for word."""
    skip = {"the", "of", "and", "for", "in", "on", "de", "la"}
    words = fold(full.replace("&", " and ")).split()
    if fold(j.title) == " ".join(words):
        return True
    for abbr in (j.med_abbr, j.iso_abbr):
        a = [w for w in fold(re.sub(r"\s*\([^)]*\)\s*$", "", abbr or "")).split() if w not in skip]
        if a and _abbreviates(a, words, 0):
            return True
    return False


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


_AFFILIATION = re.compile(r"\b(?:department|university|hospital|faculty|school|division|institute|center|centre|"
                          r"college|research|ministry)\b", re.I)


def _name_key(name: str) -> str:
    """A name for spotting transliteration variants: letters only, accents folded, doubled letters single."""
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[^a-z]", "", fold_name(name)))


def same_written_name(a: str, b: str) -> bool:
    """Two writings of one author's name ("Esmaeili H" / "Esmeili H", "Ba'ezat F" / "Baezzat F", a record's slip
    "LIAGN Y" for "LIANG Y"): equal once folded, nearly so (ratio >= 0.85), or one transposed pair of letters."""
    ka, kb = _name_key(a), _name_key(b)
    if not ka or not kb:
        return False
    if ka == kb or fuzz.ratio(ka, kb) >= 85:
        return True
    return len(ka) == len(kb) and sorted(ka) == sorted(kb) and \
        sum(x != y for x, y in zip(ka, kb)) == 2  # "liagny" / "liangy"


def author_name_query(p: Parsed, r: biblio.Record) -> tuple[str, dict] | None:
    """The query on the author's names kept over a record whose author list is broken (affiliations in it, names
    split up, capitals): what the record says otherwise -- a name written differently, another order, a name the
    record cannot give -- or None when the record's names (affiliations aside) agree with the author's."""
    ours = [a for a in p.authors if not re.fullmatch(r"\s*et\.?\s+al\.?\s*", a, re.I)]
    people = [(s, i) for s, i in r.authors
              if s.strip() and not (not i and ("," in s or _AFFILIATION.search(s) or len(s.split()) > 4))]
    if not people:
        return ("references.author_names_unlisted", {})
    # ("HE N", "YUE Y" in capitals read as initials: a real name when the author wrote the same one)
    broken = []
    for s, i in people:
        name = f"{s} {i}".strip()
        if _bad_name(s, i) and not any(same_written_name(a, name) for a in ours):
            broken.append(name)
    if broken:
        return ("references.author_names_incomplete", {"names": ", ".join(broken)})
    theirs = [f"{s} {i}".strip() for s, i in people]
    et_al = len(ours) < len(p.authors)
    if et_al:
        theirs = theirs[:len(ours)]
    if len(ours) == len(theirs) and all(same_written_name(a, b) for a, b in zip(ours, theirs)):
        return None
    left = list(theirs)
    for a in ours:
        k = next((k for k, b in enumerate(left) if same_written_name(a, b)), None)
        if k is None:
            break
        left.pop(k)
    else:
        if not left:
            return ("references.author_order", {"names": ", ".join(theirs)})
    if len(ours) != len(theirs) and not et_al:
        return ("references.author_count", {"n": len(theirs), "names": ", ".join(theirs)})
    pairs = [f"{a} vs {b}" for a, b in zip(ours, theirs) if not same_written_name(a, b)]
    return ("references.author_names", {"pairs": "; ".join(pairs)})


# Lower-case particles a surname may open with (G63: "van", "von", "der", "de" ... are part of the surname).
_PARTICLES = frozenset("van von der den de del della di da das dos du la le ter ten bin binti al el ibn zu af av".split())


def capital_surname(surname: str) -> str:
    """A surname a record gives in lower case ("kumari"): capitalised, its particles aside ("van den Berg")."""
    words = surname.split(" ")
    if not any(w[:1].islower() and w.lower() not in _PARTICLES for w in words):
        return surname
    if all(w[:1].islower() for w in words) and words[-1].lower() in _PARTICLES:
        return surname
    out = []
    for k, w in enumerate(words):
        if w[:1].islower() and (w.lower() not in _PARTICLES or k == len(words) - 1):
            w = "-".join(x[:1].upper() + x[1:] for x in w.split("-"))
        out.append(w)
    return " ".join(out)


def _surname_case(surname: str, initials: str, p: Parsed | None) -> str:
    """A surname CrossRef gives in capitals ("BLOOM", "VanLEHN"): as the entry writes it when it is the same
    name, else capitalised (all capitals only). Group names and short ones ("WHO") stay. A person's surname
    is never left in lower case ("kumari M" -> "Kumari M")."""
    if initials and re.match(r"[a-z]", surname):
        surname = capital_surname(surname)
    odd = bool(re.search(r"[a-z][A-Z]{3,}", surname))  # "VanLEHN"
    if not initials or not (surname.isupper() or odd) or len(surname) < 3:
        return surname
    for a in (p.authors if p else []):
        written = re.sub(r"\s+(?:[A-Z]\.?-?){1,4}$", "", a.strip())  # "Van Lehn K" -> "Van Lehn"
        if not written.isupper() and fold_name(written).replace(" ", "") == fold_name(surname).replace(" ", ""):
            return written
    if odd:
        return surname
    return " ".join("-".join(x.capitalize() for x in w.split("-")) for w in surname.split())


_WRITTEN_NAME = re.compile(r"(?P<sur>[^\W\d_][\w'’ -]*?)\s+(?P<ini>[A-Z]{1,4})")


def same_name_other_case(surname: str, initials: str, written: str, text: str) -> bool:
    """The author's own Vancouver name `written` (as typed in the entry `text`) is the record's (`surname`,
    `initials`) with only the case of its letters different: a particle ("van den Berg G" / CrossRef's "Van den
    Berg G"; G63: "van", "von", "der", "de" ... are kept as part of the surname). A name in capitals, other
    letters, accents or initials are the record's."""
    w = written.strip()
    m = _WRITTEN_NAME.fullmatch(w)
    if not m or not initials or w not in text:
        return False
    sur = m.group("sur").strip()
    if sur.isupper() or sur == surname or sur.lower() != surname.lower() or m.group("ini") != initials:
        return False
    # only particles differ ("van", "den", "de"): "Alvarez-iglesias" is the author's slip, not a form of the name
    return all(a == b or a.lower() in _PARTICLES for a, b in zip(sur.split(), surname.split()))


def _usable_authors(r: biblio.Record, p: Parsed | None) -> list[str]:
    # (PubMed marks some group names with a footnote star: "... Professional Practice Committee*")
    names = [f"{_surname_case(s.rstrip('*').strip(), i, p)} {i}".strip() for s, i in r.authors]
    if p and p.authors and (malformed_authors(r) or not r.authors):
        return p.authors  # the record's list is broken: keep the author's names (and query them)
    if p and r.source == "crossref" and len(names) < min(len(p.authors), 6):
        return p.authors  # CrossRef author lists are often incomplete; keep the author's
    if p and r.source == "crossref":
        # The author's own case of a particle where CrossRef's (the publisher's deposit) differs: "van den Berg G"
        # for "Van den Berg G". PubMed's names (NLM's) are kept: the JEHP editors wrote "de Sá TH" and "Ten
        # Ham-Baloyi W" from PubMed over the authors' "De Sá" and "ten". The order of a compound surname is
        # the record's either way ("Naghavi Alhosseini SS" for the author's "Alhosseini SSN": editors 4 of 5).
        for k, (s, i) in enumerate(r.authors[:len(p.authors)]):
            if same_name_other_case(s.rstrip("*").strip(), i, p.authors[k], p.text):
                names[k] = p.authors[k].strip()
    return names


def _mostly_upper(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    return len(letters) >= 12 and sum(c.isupper() for c in letters) > 0.6 * len(letters)


def _usable_title(r: biblio.Record, p: Parsed | None) -> str:
    title = r.title
    m = re.fullmatch(r"\s*\[(.+)\]\s*\.?\s*", title or "", re.S)
    if m:  # PubMed brackets titles translated into English: "[Solid limbal dermoid ...]."
        title = m.group(1).strip()
        own = re.fullmatch(r"\s*(\S.*?)\s*\[(.+)\]\s*\.?\s*", p.title or "", re.S) if p else None
        if own and fuzz.token_sort_ratio(fold(own.group(2)), fold(title)) >= 85:
            # the author gave the original (transliterated) title with the English one in brackets: both stay
            return f"{own.group(1)} [{own.group(2).strip()}]"
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
Ziemer Alcon Schwind Bausch Lomb Moria Abbott Allergan Visx Amaris Ellex Lumenis Iridex Oculus Keratograph
Africa African America American Asia Asian Europe European Australia Australian India Indian China Chinese Japan
Korea Nepal Pakistan Bangladesh Sri Lanka Iran Iraq Turkey Egypt Nigeria Ethiopia Kenya Ghana Brazil Colombia
Mexico Canada Germany France Italy Spain Portugal Netherlands Sweden Norway Denmark Finland Switzerland Austria
Poland Greece Israel Saudi Arabia Emirates Singapore Malaysia Thailand Vietnam Indonesia Philippines Taiwan
Kong Britain England Scotland Wales Ireland Russia Ukraine Chile Peru Argentina Venezuela Delhi Mumbai Chennai
Kolkata Bangalore Bengaluru Hyderabad Pune Kerala Karnataka Gujarat Punjab Rajasthan Maharashtra Tamil Nadu
Andhra Pradesh Telangana Odisha Bengal Bihar Haryana Assam London Paris Boston Baltimore Beijing Shanghai Tokyo
Linn Linnaeus
Tehran Ahvaz Kerman Yazd Shiraz Isfahan Esfahan Mashhad Tabriz Qom Karaj Rasht Zahedan Kermanshah Hamadan Urmia
Ardabil Sanandaj Bandar Abbas Bushehr Zanjan Qazvin Semnan Gorgan Sari Babol Arak Khorramabad Ilam Yasuj Birjand
Bojnurd Shahrekord Kashan Rafsanjan Sabzevar Neyshabur Khuzestan Fars Khorasan Sistan Baluchestan Mazandaran Gilan
Golestan Lorestan Kurdistan Hormozgan Markazi Persian Arab Arabic Afghanistan Kabul Dhaka Karachi Lahore Islamabad
Kathmandu Colombo Riyadh Jeddah Dubai Doha Kuwait Oman Qatar Bahrain Jordan Lebanon Syria Yemen Cairo Istanbul Ankara
Tanzania Uganda Rwanda Zambia Zimbabwe Malawi Mozambique Cameroon Sudan Morocco Tunisia Algeria Libya Senegal Africa
Lagos Nairobi Addis Ababa Johannesburg Toronto Montreal Vancouver Sydney Melbourne Auckland Zealand Seoul Busan
Hong Taipei Bangkok Jakarta Manila Hanoi Kuala Lumpur Myanmar Cambodia Laos Mongolia Kazakhstan Uzbekistan Azerbaijan
Armenia Georgia Belgium Hungary Romania Bulgaria Serbia Croatia Czech Slovakia Slovenia Estonia Latvia Lithuania
Iceland Scandinavia Scandinavian Latin Caribbean Cuba Jamaica Ecuador Bolivia Paraguay Uruguay Guatemala Honduras
Panama Rica Nicaragua Salvador Haiti Dominican Puerto Rico Iranian Iraqi Turkish Pakistani Afghan Saudi Emirati
Nepalese Nepali Bangladeshi Japanese Korean Swedish Norwegian Danish Finnish Dutch German French Italian Spanish
Portuguese British English Scottish Irish Welsh Russian Polish Greek Brazilian Mexican Canadian Australian Nigerian
Ethiopian Kenyan Ghanaian Egyptian Israeli Malaysian Thai Vietnamese Indonesian Filipino Taiwanese Chinese
Islamic Muslim Muslims Christian Christians Hindu Hindus Buddhist Ramadan Quran Koran Gottman Olson Bandura Ajzen
Fishbein Rogers Rosenberg Beck Erikson Maslow Freud Piaget Vygotsky Kolb Kirkpatrick Glasser Satir Bowen
Minuchin Lazarus Folkman Pender Orem Roy Neuman Watson Peplau Nightingale Henderson Leininger Becker Prochaska
DiClemente Kübler Ross Bronfenbrenner Seligman Csikszentmihalyi Goleman Sternberg Gardner
""".split())

# A word after a place's name that is part of the name: "Yazd City", "Fars Province", "Shiraz University".
_NAME_TAIL = frozenset("City Province County District State Region Island Islands University Hospital".split())
# Possessive nouns that are not names ("Women's", "Patients’"): lower-cased like other words.
_COMMON_POSSESSIVES = frozenset("""women men children child people patients patient parents parent mothers mother
    fathers father physicians physician nurses nurse doctors doctor students student teachers teacher users user
    consumers consumer today world year years girls boys adolescents caregivers family families infants infant
    society community country nation individuals individual others couples couple wives husbands spouses""".split())

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
        base = re.sub(r"[’']s?$", "", w)
        if base != w and (base in caps or base in PROPER_NOUNS):  # "Olson’s", "Johnson’s": a name's possessive
            keep.update({w, base})
        elif w in caps:
            keep.add(w)
        elif lower and w.lower() not in lower and _EPONYM_NEXT.match(title, m.end()):
            keep.add(w)
    if re.search(r"\bUnited\s+States\b", title):
        keep |= {"United", "States"}
    return keep


_GENUS_END = re.compile(r"(?:um|us|a|ae|is|ia|oides|edes|on|ix|ax|er)$")
_EPITHET_END = re.compile(r"(?:um|us|a|ae|is|ii|i|es|ensis|oides|orum|arum|ata|ica|ana|ella|ium|alis|ilis|ens|ans|or)$")
_NOT_EPITHET = frozenset("vitro vivo situ silico utero vs et al".split())


def latin_names(title: str) -> set[str]:
    """Genus names in a title, which keep their capital in sentence case: a capitalised word before a
    species epithet ("Jasminum grandiflorum", Title Case "Jasminum Grandiflorum") or before "species" /
    "sp." / "spp.". Genus and epithet must look Latin and not be words the dictionary lists in lower case
    ("Recurrent Aphthous Stomatitis" is no binomial)."""
    lower, _ = _dictionary_words()

    def latin(w: str, rx: re.Pattern) -> bool:  # Latin-looking and, when the dictionary is at hand, not English
        return bool(rx.search(w.lower())) and w.lower() not in lower and not re.search(r"(?:ous|itis)$", w.lower())

    keep = set()
    for m in re.finditer(r"\b([A-Z][a-z]{2,})\s+\(?([A-Za-z][a-z]+\.?)", title):
        genus, nxt = m.group(1), m.group(2).rstrip(".")
        if genus.lower() in _MINOR_WORDS or not latin(genus, _GENUS_END):
            continue
        if nxt.lower() in ("species", "sp", "spp"):
            keep.add(genus)
        elif nxt.lower() in _MINOR_WORDS or nxt.lower() in _NOT_EPITHET or len(nxt) < 3:
            continue
        elif latin(nxt, _EPITHET_END):  # "Jasminum grandiflorum"; Title Case "Jasminum Grandiflorum"
            keep.add(genus)
        elif nxt.islower() and len(nxt) >= 4 and nxt not in lower and lower:  # "Jasminum sambac"
            keep.add(genus)
    return keep


def sentence_case(title: str, keep: set[str] | frozenset = frozenset()) -> str:
    """Lower-case Title-Cased words, keeping the first word, the word after a colon, acronyms
    (ASCO, GLOBOCAN, U.S.), words with inner capitals or digits (mRNA, COVID-19) and proper nouns:
    those in `keep` (see `proper_nouns`) and in PROPER_NOUNS."""
    keep = set(keep) | PROPER_NOUNS
    lower_words = _dictionary_words()[0]

    def kept(core: str) -> bool:
        if core in keep:
            return True
        base = re.fullmatch(r"([A-Z][a-z]+)[’']s?", core)  # "Bowen’s Family Systems Theory": a name's possessive
        if base:
            b = base.group(1)
            return b in keep or (b.lower() not in _COMMON_POSSESSIVES and b.lower() not in lower_words
                                 and not b.lower().endswith(("ers", "ents", "ians", "ists")))
        return False

    words = re.split(r"(\s+|[–—])", title)
    out, first = [], True
    prev_kept = False
    for w in words:
        if w.isspace() or not w:
            out.append(w)
            continue
        if w in "–—":  # "years—Autism and ...", "Education–Students' perceptions": a new phrase follows
            out.append(w)
            first = True
            continue
        if w.startswith("["):  # "... stepeni [Results of ...]": a translated title in brackets starts anew
            first = True
        core = re.sub(r"^\W+|\W+$", "", w)
        plain = re.fullmatch(r"[A-Z][a-z'’]+(?:-[A-Za-z][a-z'’]*)*", core) is not None
        parts = core.split("-")
        if "-" in core and (first or not plain) and len(parts) > 1:
            # "Evidence-Based" opening the title, "23-Month-Old": the first part as it is, later plain
            # capitalised parts lower-cased ("Evidence-based", "23-month-old"); "COVID-19" and names in `keep` stay.
            rest = [x.lower() if re.fullmatch(r"[A-Z][a-z'’]+", x) and x not in keep and not _dash_compound(core)
                    else x for x in parts[1:]]
            out.append(w.replace(core, "-".join(parts[:1] + rest), 1))
        elif first or not plain:
            out.append(w)
        elif _dash_compound(core):
            left, right = core.split("-")
            out.append(w.replace(core, (left if left in keep else left.lower()) + "-" + right, 1))
        elif kept(core) or (prev_kept and core in _NAME_TAIL):  # "Yazd City"
            out.append(w)
        else:
            parts = [p if p in keep else p.lower() for p in core.split("-")]
            out.append(w.replace(core, "-".join(parts), 1))
        prev_kept = bool(core) and core[:1].isupper() and kept(core) and w == core
        first = w.endswith((":", "?", "!")) or (w.endswith(".") and len(core) > 3)
    return "".join(out)


def _issue(r: biblio.Record, style: JournalStyle) -> str:
    """The issue as the guide keeps it: a supplement as written ("(Suppl 1)", "(2 Suppl)", CrossRef's
    "Supplement_1" -> "(Suppl 1)", "s2" -> "(s2)"); an issue number only when the style keeps issues."""
    if re.search(r"suppl", r.volume or "", re.I):  # PubMed puts some supplements in Volume ("45 Suppl 1")
        return "(" + re.sub(r"(?i)^.*?suppl\w*?\.?(?:_|\s+|$)", "Suppl ", r.volume).strip() + ")"
    issue = (r.issue or "").strip()
    if re.search(r"suppl", issue, re.I):  # "Suppl 2", "2 Suppl", "Supplement_1", "Suppl."
        issue = re.sub(r"(?i)\bsuppl(?:ement)?\.?(?:_|\s+|$)", "Suppl ", issue.replace("_", " ")).strip()
        return "(" + " ".join(issue.split()) + ")"
    if re.fullmatch(r"[Ss]\d{1,2}", issue):  # "13(s2)": a supplement the journal numbers so
        return f"({issue})"
    if issue and not style.get("references.drop_issue", True):
        return f"({issue})"
    return ""


def record_pages(r: biblio.Record, p: Parsed | None) -> str:
    """The record's pages, or the author's where the record lacks them: CrossRef gives only the first
    page ("54" for the author's "54-60") or none at all. The author's pages are used only for the record's
    volume (the same first page, when the record has one)."""
    pages = (r.pages or "").strip()
    c = citation(p.text) if p else None
    if not c or not c[3] or not same_volume(p.text, r):
        return pages
    mine = c[3].strip()
    if not pages:
        return mine
    m = re.fullmatch(r"([A-Za-z]?\d+[A-Za-z]?)\s*[-–]\s*([A-Za-z]?\d+[A-Za-z]?)", mine)
    if m and re.fullmatch(r"[A-Za-z]?\d+[A-Za-z]?", pages) and _first_page(m.group(1)) == _first_page(pages) \
            and _first_page(m.group(2)) != _first_page(pages):
        return mine  # "54" in the record, "54-60" in the entry ("e68918-e68918" is the one page)
    return pages


_SAME_TEXT = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "ı": "i", "′": "'", "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-"})


def _title_key(t: str) -> str:
    """A title for comparing wordings: case, quote and dash forms, spaces around dashes and before
    punctuation, and a final full stop aside."""
    t = re.sub(r"\s*-+\s*", "-", (t or "").translate(_SAME_TEXT).lower())
    t = re.sub(r"\s+([:;,.?!])", r"\1", t)
    return " ".join(t.split()).rstrip(".")


def _author_wording(title: str, mine: str) -> str:
    """The record's title in the author's typography when the two are the same words: the author's
    curly quotes and apostrophes, dashes ("years—Autism" for PubMed's "Years - Autism") and letters
    (PubMed's "ıntelligence" typo). Capitals: the record's when it is sentence-cased or the author's title
    is not; the author's sentence case over a Title-Cased record (but for known names and inner capitals)."""
    if not mine or _title_key(title) != _title_key(mine):
        return title
    rw, aw = list(_WORD.finditer(title)), list(_WORD.finditer(mine))
    if len(rw) != len(aw):
        return title
    out = list(re.sub(r"\s+([:;,?!])", r"\1", mine))
    if out != list(mine):  # spaces before punctuation went: re-find the words
        mine = "".join(out)
        aw = list(_WORD.finditer(mine))
        if len(aw) != len(rw):
            return title
    author_cased, record_cased = is_sentence_cased(mine), is_sentence_cased(title)
    for a, r in zip(aw, rw):
        x, y = a.group(0), r.group(0)
        if len(x) != len(y):
            continue
        cap = y[:1].isupper() and x[:1].islower() and (record_cased or y in PROPER_NOUNS)
        inner = any(c.isupper() for c in y[1:])  # "COVID", "mRNA", "eHealth": the record's capitals
        src = y if not author_cased or record_cased or cap or inner else x  # a sentence-cased record decides
        for k, ch in enumerate(src):
            out[a.start() + k] = x[k].upper() if ch.isupper() else x[k].lower()
    return "".join(out)


def _quotes(title: str, style: JournalStyle, p: Parsed | None) -> str:
    """Straight quotes in a rebuilt title made typographic when the journal uses curly ones
    (`guide_replace.smart_quotes`) or the author typed curly ones in the entry: a record's "Experts'
    perspectives" never replaces the author's "Experts’ perspectives"."""
    if "'" not in title and '"' not in title:
        return title
    if not (style.get("guide_replace.smart_quotes") or p and re.search(r"[‘’“”]", p.text)):
        return title
    from .rules_guide import find_smart_quotes

    out = title
    for s, e, new in sorted(find_smart_quotes(title, {}), reverse=True):
        out = out[:s] + new + out[e:]
    return out


_NAME_INITIALS = re.compile(r"^(?P<sur>.*\S)\s+(?P<ini>[A-Z](?:-?[A-Z])+)(?P<suffix>\s+(?:Jr|Sr|II|III|IV|2nd|3rd))?$")


def cut_initials(name: str, max_initials: int) -> str:
    """"Chandrasena PPCM" -> "Chandrasena PP" (at most `max_initials` initials; "Smith J-P", "Cunningham ET Jr" kept
    within the limit)."""
    m = _NAME_INITIALS.match(name.strip())
    if not m or not re.search(r"[a-z]", m.group("sur")):
        return name
    letters = [k for k, ch in enumerate(m.group("ini")) if ch.isalpha()]
    if len(letters) <= max_initials:
        return name
    return f"{m.group('sur')} {m.group('ini')[:letters[max_initials - 1] + 1]}{m.group('suffix') or ''}"


_CONCEPT = re.compile(r"\b(?:(?:Extended\s+)?Theory of Planned Behaviou?r|Theory of Reasoned Action|Health Belief Model|"
                      r"Social Cognitive Theory|Transtheoretical Model|Protection Motivation Theory|"
                      r"Self-Determination Theory|Social Learning Theory)\b")


def format_record(r: biblio.Record, style: JournalStyle, p: Parsed | None = None, keep: frozenset | set = frozenset()) -> str:
    """The entry in journal style from a record (or, for an entry not verified, the author's own fields: refstyle).
    `keep`: title words that keep their capital besides the known names."""
    max_a = style.get("references.max_authors", 6)
    names = _usable_authors(r, p)
    max_i = style.get("references.max_initials")
    if max_i:  # JEHP (owner, 2026-10-26): "Chandrasena PPCM" -> "Chandrasena PP"; group names are left alone
        group_names = {f"{s.rstrip('*').strip()} {i}".strip() for s, i in r.authors if _is_group(s, i)}
        names = [n if n in group_names else cut_initials(n, max_i) for n in names]
    if max_a and len(names) > max_a:
        names = names[:max_a] + [style.get("references.et_al", "et al.").rstrip(".")]
    # (a one-word name without initials is a person known by one name, "Nurdiansyah", not a group)
    groups = {f"{s.rstrip('*').strip()} {i}".strip() for s, i in r.authors if _is_group(s, i) or not i and s.isupper()}
    # Group authors are separated by semicolons: "GBD 2019 Collaborators; Vision Loss Expert Group".
    author_text = ""
    for k, n in enumerate(names):
        if k:
            author_text += "; " if n in groups or names[k - 1] in groups else ", "
        author_text += n
    title = re.sub(r"\s+", " ", _usable_title(r, p)).strip()
    title = _author_wording(title, p.title if p else "")
    title = re.sub(r"\bcovid[\s‐‑-]*19\b", "COVID-19", title, flags=re.I)  # "covid 19" -> "COVID-19"
    if title.endswith("."):
        title = title[:-1]
    if style.get("references.title_sentence_case"):
        title = sentence_case(title, proper_nouns(r.title, p.title if p else "") | dictionary_proper_nouns(title)
                              | latin_names(title) | latin_names(r.title) | set(keep))
        # A theory's or model's name is no proper noun: "with the Theory of Planned Behaviour" -> "theory of planned
        # behaviour" (sentence case, as the JEHP list writes "extended theory of planned behavior"), not opening it.
        title = _CONCEPT.sub(lambda m: m.group(0) if not title[:m.start()].strip() or
                             title[:m.start()].rstrip().endswith((":", "?", "!")) else m.group(0).lower(), title)
    title = _quotes(title, style, p)
    if style.get("references.capitalize_after_colon"):
        title = re.sub(r":\s([a-z])", lambda m: ": " + m.group(1).upper(), title)
    end = "" if title.endswith(("?", "!")) else "."
    issue = _issue(r, style)
    volume = re.sub(r"\s*\(?suppl.*$", "", r.volume, flags=re.I)
    if style.get("references.drop_issue", True):  # PubMed's "50 Pt 1": the part goes with the issue (guide, e.g. 3)
        volume = re.sub(r"\s*\(?\s*(?:Pt|Part)\.?\s*\d+[A-Za-z]?\s*\)?\s*$", "", volume)
    pages = format_pages(record_pages(r, p), style)
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
    title_search = style.get("references.title_search", True)
    queries = []
    from .refstyle import style_unverified  # an entry not verified is still styled, from the author's text

    if not journal_like:
        from . import refs_other  # books, links, conference papers, reports, theses

        record = None
        if title_search and incomplete_article(text, p):  # no journal: completed only from a strict match
            q = Parsed(**{**p.__dict__, "title": _clean_title(p.title)})
            record, _ = find(q)
            if not _usable(record) or not accept(q, record):
                record, _, queries = find_by_title(q)
            if _usable(record) and author_agrees(q, record) and _only_paper(q, record):
                p = q
            else:
                record, queries = None, []
        if record is None:
            return refs_other.resolve(text, style, p)
    else:
        record, why = find(p)
        if not record and title_search and "points to a different article" not in why and DIFFERS not in why:
            record, title_why, queries = find_by_title(p)
            why = title_why or why
        if not record:
            return style_unverified(Resolution(QUERY, p, message=why), text, style, fields)
        if not (record.volume and record.year):
            return style_unverified(Resolution(QUERY, p, record, message="Record has no volume yet (ahead of print?)"),
                                    text, style, fields)
    if not record_pages(record, p):  # the guide's template needs a first page; nothing to take it from
        queries.append(("references.no_pages", {}))
    if malformed_authors(record) or not record.authors and p.authors:
        if not p.authors:
            return style_unverified(Resolution(QUERY, p, record, message="Record's author names are malformed"),
                                    text, style, fields)
        q = author_name_query(p, record)  # the author's names are kept: ask about what the record says otherwise
        if q:
            queries.append(q)
    formatted = format_record(record, style, p) + errata(text, style)
    t, _, _ = score(p, record)
    status = MATCHED if fold(formatted) == fold(text) and formatted.rstrip() == text.rstrip() else FORMAT_FIXED
    return Resolution(status, p, record, formatted, score=t, queries=queries)


def resolve_all(texts: list[str], style: JournalStyle) -> list[Resolution]:
    """Resolve a reference list; Claude splits the entries first when enabled and a key is set."""
    use_ai = style.get("references.ai_parse") and ai_refs.available()
    fields = ai_refs.parse_batch(texts) if use_ai else [None] * len(texts)
    return [resolve(t, style, f) for t, f in zip(texts, fields)]
