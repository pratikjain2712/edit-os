"""Bibliographic lookups -- PubMed (NCBI E-utilities), CrossRef, Europe PMC, OpenAlex, Semantic Scholar
and DOAJ -- with a local response cache.

NCBI allows 3 requests a second without a key (10 with NCBI_API_KEY); calls are spaced
accordingly, and so are the other services' (polite limits below). Responses are cached in SQLite so
re-running a manuscript costs nothing. Requests carry a User-Agent with a contact address from
EDITOS_MAILTO when it is set (never a hard-coded one); OPENALEX_API_KEY and SEMANTIC_SCHOLAR_API_KEY are
optional keys. A service that refuses us (rate limit used up, key refused, down) is skipped for a while
rather than retried on every reference.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import sqlite3
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field

from lxml import etree

from .journals import DEFAULT_DB

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
CROSSREF = "https://api.crossref.org/works"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OPENALEX = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper"
DOAJ = "https://doaj.org/api/search/articles/"

# Every database a reference is checked against, most trusted first (refengine prefers the first that agrees).
# PubMed and CrossRef always run; the others only when those two found nothing (refengine.find), per journal
# (`references.sources`).
SOURCES = ("pubmed", "crossref", "europepmc", "openalex", "semantic_scholar", "doaj")
EXTRA_SOURCES = SOURCES[2:]
SOURCE_NAMES = {"pubmed": "PubMed", "crossref": "CrossRef", "europepmc": "Europe PMC", "openalex": "OpenAlex",
                "semantic_scholar": "Semantic Scholar", "doaj": "DOAJ"}
# Sources whose author names are one display string ("Zohreh Nabizadeh-Gharghozar"), split here by `split_name`;
# refengine.align_names re-splits them the way the entry writes a compound surname.
DISPLAY_NAME_SOURCES = frozenset({"openalex", "semantic_scholar", "doaj"})
CACHE_DB = os.path.join(os.path.dirname(DEFAULT_DB), "http_cache.sqlite")


@dataclass
class Record:
    source: str  # one of SOURCES (or "author": refstyle's record of the author's own fields)
    title: str
    authors: list[tuple[str, str]] = field(default_factory=list)  # (surname, initials) or (group name, "")
    journal: str = ""  # PubMed: MedlineTA; CrossRef: container title
    year: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    pmid: str = ""
    doi: str = ""
    issn: list[str] = field(default_factory=list)
    other_years: list[str] = field(default_factory=list)  # online / issued years besides `year`
    journal_full: str = ""  # CrossRef: the full container title ("Interactive Technology and Smart Education")
    full_names: list[str] = field(default_factory=list)  # display-name sources: the names as given, for `authors`
    found_via: str = ""  # a lower-priority source that found the paper this record (PubMed / CrossRef) was taken for

    def __post_init__(self):
        if self.source != "author":  # the author's own fields stay as written
            self.title = strip_title_markers(self.title)


# A footnote marker the database kept at the end of a title, glued to its last word: PubMed 11408124 reads
# "... with simulated cataract(2)(2)." (the journal's footnote "2", once per source field). Only after a word of
# three or more lower-case letters, so a subscript the database writes the same way stays ("vitamin D(3)", "CO(2)").
_TITLE_MARKERS = re.compile(r"(?<=[a-z]{3})(?:\(\d{1,3}\)|\[\d{1,3}\])+(?=[.?!]?\s*$)")


def strip_title_markers(title: str) -> str:
    """A record's title without trailing footnote markers: "... simulated cataract(2)(2)." -> "... simulated
    cataract."."""
    return _TITLE_MARKERS.sub("", title or "") if title else title


BREAKER_SECONDS = 600  # how long a service that refused us is left alone


def user_agent() -> str:
    """"edit-os/0.1 (reference checking; mailto:...)", the address from EDITOS_MAILTO (polite pools of CrossRef /
    OpenAlex); without one, no address at all."""
    mail = os.environ.get("EDITOS_MAILTO", "").strip()
    return f"edit-os/0.1 (reference checking; mailto:{mail})" if mail else "edit-os/0.1 (reference checking)"


class _Http:
    def __init__(self, path: str = CACHE_DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.con = sqlite3.connect(path, check_same_thread=False)  # shared by worker threads (serialized SQLite)
        self.con.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, body TEXT)")
        self.last: dict[str, float] = {}
        self.down: dict[str, float] = {}  # host -> until when it is skipped (breaker hosts only)

    def get(self, url: str, host_gap: float, headers: dict | None = None, timeout: float = 30,
            breaker: bool = False) -> str | None:
        """The body of `url` ("" for a 404 / 400: nothing there), cached; None when it could not be fetched.
        API keys go in `headers`, never in the URL (the cache key). `breaker`: a host that rate-limits
        us for long, refuses the request (401 / 403) or fails three times running is skipped for
        BREAKER_SECONDS, so a service that is down or out of quota costs one try, not one per reference."""
        key = hashlib.sha256(url.encode()).hexdigest()
        row = self.con.execute("SELECT body FROM cache WHERE key = ?", (key,)).fetchone()
        if row:
            return row[0]
        host = urllib.parse.urlparse(url).netloc
        if breaker and self.down.get(host, 0) > time.monotonic():
            return None
        wait = self.last.get(host, 0) + host_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        body = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": user_agent(), **(headers or {})})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = r.read().decode("utf-8", errors="replace")
                break
            except urllib.error.HTTPError as e:
                if e.code == 404 or breaker and e.code in (400, 410, 422):
                    body = ""
                    break
                if breaker and e.code in (401, 403):
                    break
                if breaker and e.code == 429:
                    try:
                        retry = float(e.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry = 0
                    if retry > 30:  # a daily quota used up, not a burst
                        break
                time.sleep(1 + attempt * 2)  # 429 / 5xx: back off and retry
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(1 + attempt * 2)
        self.last[host] = time.monotonic()
        if body is None and breaker:
            self.down[host] = time.monotonic() + BREAKER_SECONDS
        if body is not None:
            self.con.execute("INSERT OR REPLACE INTO cache VALUES (?, ?)", (key, body))
            self.con.commit()
        return body


_http: _Http | None = None


def http() -> _Http:
    global _http
    if _http is None:
        _http = _Http()
    return _http


def _ncbi(endpoint: str, **params) -> str | None:
    if os.environ.get("NCBI_API_KEY"):
        params["api_key"] = os.environ["NCBI_API_KEY"]
    params.setdefault("tool", "edit-os")
    gap = 0.11 if "api_key" in params else 0.34
    return http().get(EUTILS + endpoint + "?" + urllib.parse.urlencode(params), gap)


def pubmed_search(term: str, retmax: int = 5) -> list[str]:
    body = _ncbi("esearch.fcgi", db="pubmed", retmode="json", retmax=retmax, term=term)
    try:
        return json.loads(body)["esearchresult"]["idlist"] if body else []
    except (ValueError, KeyError):
        return []


def pubmed_best_match(term: str, retmax: int = 10) -> list[str]:
    """PubMed search in Best Match order (esearch's default order is newest first)."""
    body = _ncbi("esearch.fcgi", db="pubmed", retmode="json", retmax=retmax, sort="relevance", term=term)
    try:
        return json.loads(body)["esearchresult"]["idlist"] if body else []
    except (ValueError, KeyError):
        return []


def pubmed_fetch(pmids: list[str]) -> list[Record]:
    if not pmids:
        return []
    body = _ncbi("efetch.fcgi", db="pubmed", retmode="xml", id=",".join(pmids))
    if not body:
        return []
    try:
        root = etree.fromstring(body.encode())
    except etree.XMLSyntaxError:
        return []
    out = []
    for art in root.iter("PubmedArticle"):
        mc = art.find("MedlineCitation")
        a = mc.find("Article")
        title = "".join(a.find("ArticleTitle").itertext()).strip() if a.find("ArticleTitle") is not None else ""
        authors = []
        for au in a.findall("AuthorList/Author"):
            if au.findtext("CollectiveName"):
                authors.append((au.findtext("CollectiveName").strip(), ""))
            elif au.findtext("LastName"):
                initials = (au.findtext("Initials") or "").strip()
                if au.findtext("Suffix"):
                    initials = f"{initials} {au.findtext('Suffix').strip()}".strip()  # "Cunningham ET Jr"
                authors.append((au.findtext("LastName").strip(), initials))
        ji = a.find("Journal/JournalIssue")
        year = (ji.findtext("PubDate/Year") or (ji.findtext("PubDate/MedlineDate") or "")[:4]) if ji is not None else ""
        online = a.findtext("ArticleDate/Year") or ""
        if not year:
            year = online
        doi = ""
        for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = (aid.text or "").strip()
        pages = a.findtext("Pagination/MedlinePgn") or ""
        if not pages:
            for el in a.findall("ELocationID"):
                if el.get("EIdType") == "pii" or (el.text or "").startswith("e"):
                    pages = (el.text or "").strip()
                    break
        out.append(Record(
            source="pubmed", title=title, authors=authors,
            journal=(mc.findtext("MedlineJournalInfo/MedlineTA") or a.findtext("Journal/ISOAbbreviation") or "").strip(),
            year=year.strip(), volume=(ji.findtext("Volume") or "").strip() if ji is not None else "",
            issue=(ji.findtext("Issue") or "").strip() if ji is not None else "",
            pages=pages.strip(), pmid=mc.findtext("PMID") or "", doi=doi,
            issn=[i.text for i in a.findall("Journal/ISSN") if i.text],
            other_years=[online.strip()] if online.strip() and online.strip() != year.strip() else [],
        ))
    return out


_MARKUP = re.compile(r"</?(?:scp|i|b|em|strong|sup|sub|u|span|mml:[a-z]+|jats:[a-z]+)\b[^>]*>", re.I)


def _plain(s: str) -> str:
    """A CrossRef title without the publisher's markup: "Adapting <scp>parent-focused</scp> ..." -> "Adapting
    parent-focused ..."; entities ("&amp;") unescaped."""
    s = html.unescape(_MARKUP.sub("", s or ""))
    return s.replace("\\'", "'").replace('\\"', '"').strip()  # DOAJ: "Participants\\' learning"


def _with_subtitle(item: dict) -> str:
    """CrossRef keeps a subtitle apart ("Effectiveness of intelligent tutoring systems" + "A meta-analytic
    review"): the title as printed, joined by a colon, unless the title already holds it."""
    title = _plain((item.get("title") or [""])[0])
    sub = _plain((item.get("subtitle") or [""])[0])
    if sub and title and sub.lower() not in title.lower():
        return f"{title.rstrip(':')}: {sub}"
    return title


def _crossref_record(item: dict) -> Record:
    years = []
    for k in ("published-print", "published-online", "issued"):
        parts = item.get(k, {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            years.append(str(parts[0][0]))
    year = years[0] if years else ""
    authors = []
    for au in item.get("author", []):
        if au.get("family"):
            initials = "".join(w[0] for w in (au.get("given") or "").replace("-", " ").replace(".", " ").split() if w)
            authors.append((au["family"], initials.upper()))
        elif au.get("name"):
            authors.append((au["name"], ""))
    container = item.get("short-container-title") or item.get("container-title") or [""]
    full = html.unescape((item.get("container-title") or [""])[0])
    return Record(
        source="crossref", title=_with_subtitle(item), authors=authors, journal=container[0],
        year=year, volume=item.get("volume", ""), issue=item.get("issue", ""),
        pages=item.get("page", "") or item.get("article-number", ""), doi=item.get("DOI", ""),
        issn=item.get("ISSN", []), other_years=sorted(set(years) - {year}), journal_full=full,
    )


def crossref_doi(doi: str) -> Record | None:
    body = http().get(f"{CROSSREF}/{urllib.parse.quote(doi)}", 0.1)
    try:
        return _crossref_record(json.loads(body)["message"]) if body else None
    except (ValueError, KeyError):
        return None


def crossref_search(text: str, rows: int = 5) -> list[Record]:
    body = http().get(f"{CROSSREF}?" + urllib.parse.urlencode({"query.bibliographic": text[:400], "rows": rows}), 0.1)
    try:
        return [_crossref_record(i) for i in json.loads(body)["message"]["items"]] if body else []
    except (ValueError, KeyError):
        return []



# --- fallback searches -------------------------------------------------------------------
#
# Tried only when the plain searches found no usable record. They cast a wider net (part of the
# title, typo-prone words left out, first author / year / journal to stay specific); every
# candidate still has to pass the caller's strict acceptance, so a wider net never means a
# looser match.

STOPWORDS = frozenset("""
a about after against all also among an and any are as at be been before being between both but by can
did do does during each either for from had has have how however if in into is it its may more most
new no non nor not of on or other our over per should so some such than that the their them then there
these they this those through to too under upon use used using versus via vs was we were what when where
whether which while who whom why will with within without would yet
""".split())
# British / American spellings (colour, paediatric, oesophageal, randomised, analyse) and
# words with digits, hyphens or symbols are often written differently in the database.
_VARIANT = re.compile(r"our|ae|oe|is(?:e|ed|es|ing|ation)s?$|ys(?:e|ed|es|ing)$|tre$|ogue$|ll(?:ed|ing)$")
_FIELD_TERMS = re.compile(r"[\[\]()\"]")
MAX_FALLBACK_REQUESTS = 4


def title_words(title: str) -> list[str]:
    """Distinctive title words, in order: no stopwords, punctuation or short words, and none of
    the words that are often spelled or typed differently in the database."""
    letters = [c for c in title if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) > 0.6 * len(letters):
        title = title.lower()  # an ALL-CAPS title: its words are not acronyms
    out = []
    for w in re.split(r"[\s/–—:;,.?!()\[\]{}\"“”‘’]+", title):
        w = w.strip("'-")
        if len(w) < 3 or w.lower() in STOPWORDS:
            continue
        if not w.isascii() or not w.isalpha() or "-" in w or (w.isupper() and len(w) > 1):
            continue  # COVID-19, IL-6, Sjögren, acronyms, "children's": spelled many ways
        if _VARIANT.search(w.lower()):
            continue
        out.append(w.lower())
    return out


def first_author_surname(author: str) -> str:
    """"Di Martino E" -> "Di Martino"; "Shields CL" -> "Shields"; group names and initials-only -> ""."""
    parts = [x for x in re.split(r"[\s,]+", author.replace(".", " ").strip()) if x]
    while len(parts) > 1 and re.fullmatch(r"[A-Z]{1,3}(?:-[A-Z])?", parts[-1]):
        parts.pop()
    name = " ".join(parts)
    if len(parts) > 3 or not re.search(r"[A-Za-z][a-z]", name) or re.search(r"\d", name) or _FIELD_TERMS.search(name):
        return ""
    return name


def journal_hint(text: str, title: str) -> str:
    """The journal name as the author wrote it, right after the title ("... outcomes. Int J Art Ther. 2022;")."""
    words = [w for w in re.split(r"\W+", title) if w][-3:]
    if not words:
        return ""
    m = re.search(r"\W+".join(map(re.escape, words)) + r"\W*", text, re.I)
    if not m:
        return ""
    j = re.match(r"\s*([A-Za-z][A-Za-z .&'-]{2,80}?)[.,]?\s*(?:\(?(?:19|20)\d{2}|\d|;)", text[m.end():])
    if not j:
        return ""
    name = re.sub(r"\s+", " ", j.group(1).replace(".", " ")).strip()
    return name if 1 <= len(name.split()) <= 8 and not re.search(r"\b(?:et al|doi|available|accessed)\b", name, re.I) else ""


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _share(words: list[str], frac: float) -> list[str]:
    return words[:max(3, round(len(words) * frac))]


def partial_title_query(title: str, surname: str, year: str, journal: str = "") -> str:
    """One PubMed query OR-ing partial-title searches, each held to the paper by author, year or journal:
    80% of the title words with the first author; the last 60% with first author and year (a typo
    early in the title); 50% with year and journal (first author misspelled or missing)."""
    words = title_words(title)
    if len(words) < 3:
        return ""
    au = f'"{_fold(surname)}"[1au]' if surname else ""
    yr = f"{year}[dp]" if re.fullmatch(r"(?:19|20)\d{2}", year or "") else ""
    ta = f'"{_fold(journal)}"[ta]' if journal else ""
    groups = []
    if au:
        groups.append([" ".join(_share(words, 0.8)), au])
        groups.append([" ".join(words[-max(3, round(len(words) * 0.6)):]), au, yr])
    if yr and ta:
        groups.append([" ".join(_share(words, 0.5)), yr, ta])
    elif yr and not au and len(words) >= 5:
        groups.append([" ".join(_share(words, 0.6)), yr])
    clauses = []
    for g in groups:
        c = "(" + " AND ".join(f"({x})" if i == 0 else x for i, x in enumerate(g) if x) + ")"
        if c not in clauses:
            clauses.append(c)
    return " OR ".join(clauses)


def fallback_candidates(title: str, first_author: str, year: str, text: str = "", known: Record | None = None,
                        max_requests: int = MAX_FALLBACK_REQUESTS) -> Iterator[list[Record]]:
    """Candidate records from the fallback searches, one batch per search, surest first: the other
    database by DOI when `known` (a record found without volume: ahead of print, or a CrossRef deposit
    without one) has a DOI; PubMed by partial title; CrossRef by title, first author and year.
    At most `max_requests` HTTP requests in all (cached ones included)."""
    left = max_requests
    seen = {known.pmid} if known and known.pmid else set()

    def fetch(ids: list[str]) -> list[Record]:
        nonlocal left
        new = [i for i in ids if i not in seen]
        if not new or left <= 0:
            return []
        left -= 1
        seen.update(new)
        return pubmed_fetch(new)

    if known and known.doi:
        left -= 1
        if known.source == "pubmed":
            yield [r for r in [crossref_doi(known.doi)] if r]
        else:
            yield fetch(pubmed_search(f'"{known.doi}"[aid]', retmax=1))
    surname = first_author_surname(first_author)
    hint = journal_hint(text, title) if text and title else ""
    term = partial_title_query(title, surname, year, hint)
    if term and left >= 2:
        left -= 1
        yield fetch(pubmed_best_match(term))
    if title and left >= 1:
        left -= 1
        flat = re.sub(r"\s*[:.]\s+", ": ", title.strip().rstrip("."))  # title and subtitle as one
        yield crossref_search(" ".join(x for x in (flat, surname, year) if x))
        if hint and left >= 1:  # with the journal: journals outside PubMed (APA lists: "Computers in Human Behavior")
            left -= 1
            yield crossref_search(" ".join(x for x in (flat, surname, year, hint) if x))


# --- more bibliographic databases ----------------------------------------------------------
#
# Europe PMC (MEDLINE, PMC, Agricola and journals outside MEDLINE), OpenAlex, Semantic Scholar and DOAJ (open-access
# journals, many of them not indexed in PubMed: Iranian / Indian journals). Each client returns biblio Records, its
# results cached as above; refengine holds every record to the same strict acceptance as PubMed's and CrossRef's.

EXTRA_TIMEOUT = 15  # seconds; these sources are a fallback and must not hold a manuscript up
EXTRA_ROWS = 5
_PARTICLE_WORDS = frozenset("van von der den de del della di da das dos du la le ter ten bin binti al el ibn zu af av".split())
_SUFFIX = re.compile(r"^(?:Jr|Sr|II|III|IV)\.?$")


def _initials(given: str) -> str:
    """"John A." -> "JA"; "Jean-Pierre" -> "JP"; "M." -> "M"."""
    return "".join(w[0] for w in re.split(r"[\s.\-‐]+", given) if w and w[0].isalpha()).upper()


def split_name(name: str) -> tuple[str, str]:
    """A display name as (surname, initials): "Zohreh Nabizadeh-Gharghozar" -> ("Nabizadeh-Gharghozar", "Z"),
    "M. Adib-Hajbaghery" -> ("Adib-Hajbaghery", "M"), "Ludwig van Beethoven" -> ("van Beethoven", "L"),
    "Smith, John A." -> ("Smith", "JA"), "Smith JA" (Vancouver) kept so, "Farhati Farhati" -> ("Farhati", "F"). A compound surname is cut to its last
    word ("Seyed Saeed Naghavi Alhosseini" -> "Alhosseini SSN"); refengine.align_names takes the entry's cut where
    the entry writes the same name. One word: a person known by one name ("Sulistyorini", "")."""
    name = re.sub(r"\s+", " ", _plain(name)).strip().strip(",")
    if not name:
        return "", ""
    if "," in name:
        sur, _, given = name.partition(",")
        return sur.strip(), _initials(given)
    words = name.split(" ")
    suffix = ""
    if len(words) > 2 and _SUFFIX.match(words[-1]):
        suffix = " " + words.pop().rstrip(".")
    if len(words) == 1:
        return words[0], suffix.strip()
    if len(set(words)) == 1:  # "Farhati Farhati": a one-name author (Indonesia) entered twice; NLM's "Farhati F"
        return words[0], words[0][0].upper()
    if re.fullmatch(r"[A-Z]{1,3}", words[-1]) and not re.fullmatch(r"(?:[A-Z]\.?){1,3}", words[0]):
        return " ".join(words[:-1]), words[-1] + suffix  # already "Smith JA"
    k = len(words) - 1
    while k > 1 and words[k - 1].lower() in _PARTICLE_WORDS and words[k - 1][:1].islower():
        k -= 1
    return " ".join(words[k:]), (_initials(" ".join(words[:k])) + suffix).strip()


_SPLIT_HYPHEN = re.compile(r"(?<=[a-z])- (?=[a-z]+\b)(?!(?:and|or|to|nor)\b)")


def _deposit_title(title: str) -> str:
    """A publisher-deposited title as printed: "scenario- based" (a line-end hyphen kept) -> "scenario-based"."""
    return _SPLIT_HYPHEN.sub("-", _plain(title))


def _journal_fields(full: str, issns: list[str], abbr: str = "") -> tuple[str, str]:
    """(journal, journal_full): NLM's abbreviation when NLM lists the journal (by ISSN), else the source's own
    abbreviation, else the full name (a journal NLM does not index is written in full: refengine.journal_name)."""
    from . import journals

    full = re.sub(r"\s+", " ", html.unescape(full or "")).strip()
    d = journals.directory()
    j = next((x for x in map(d.lookup, issns) if x), None) if d else None
    return (j.med_abbr if j else abbr or full), full


def _pages(first: str, last: str = "") -> str:
    first, last = (str(first or "").strip(), str(last or "").strip())
    return f"{first}-{last}" if first and last and last != first else first


def _year(v) -> str:
    m = re.search(r"(?:19|20)\d{2}", str(v or ""))
    return m.group(0) if m else ""


def _year_range(year: str) -> tuple[int, int] | None:
    return (int(year) - 1, int(year) + 1) if re.fullmatch(r"(?:19|20)\d{2}", year or "") else None


def _search_words(title: str, n: int = 10) -> list[str]:
    """Words to search a title by: its distinctive words (`title_words`), or all its plain words of three letters
    or more for a title with too few of those; at most `n`, ASCII letters and digits only (query syntax)."""
    words = title_words(title)
    if len(words) < 3:
        words = [w.lower() for w in re.findall(r"[A-Za-z0-9]{3,}", _fold(title)) if w.lower() not in STOPWORDS]
    return [w for w in words if re.fullmatch(r"[a-z0-9]+", w)][:n]


def _json(body: str | None):
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


# Europe PMC: https://europepmc.org/RestfulWebService (no key; be gentle)

def _europepmc_record(item: dict) -> Record:
    authors = []
    for au in (item.get("authorList") or {}).get("author", []):
        if au.get("lastName"):
            authors.append((au["lastName"].strip(), (au.get("initials") or "").replace(".", "").replace(" ", "")))
        elif au.get("collectiveName"):
            authors.append((au["collectiveName"].strip(), ""))
        elif au.get("fullName"):
            authors.append(split_name(au["fullName"]))
    ji = item.get("journalInfo") or {}
    jn = ji.get("journal") or {}
    issns = list(dict.fromkeys(x for x in (jn.get("issn"), jn.get("essn")) if x))
    journal, full = _journal_fields(jn.get("title") or item.get("bookOrReportDetails", {}).get("publisher", ""),
                                    issns, jn.get("medlineAbbreviation") or jn.get("isoabbreviation") or "")
    year = _year(ji.get("yearOfPublication") or item.get("pubYear"))
    online = _year(item.get("firstPublicationDate") or item.get("electronicPublicationDate"))
    return Record(
        source="europepmc", title=_plain(item.get("title") or ""), authors=authors, journal=journal, journal_full=full,
        year=year, volume=str(ji.get("volume") or ""), issue=str(ji.get("issue") or ""),
        pages=(item.get("pageInfo") or "").strip(), pmid=item.get("pmid") or "", doi=item.get("doi") or "",
        issn=issns, other_years=[online] if online and online != year else [],
    )


def _europepmc(query: str, rows: int = EXTRA_ROWS) -> list[Record]:
    url = EUROPEPMC + "?" + urllib.parse.urlencode(
        {"query": query, "format": "json", "resultType": "core", "pageSize": rows})
    data = _json(http().get(url, 0.2, timeout=EXTRA_TIMEOUT, breaker=True)) or {}
    items = (data.get("resultList") or {}).get("result") or []
    return [_europepmc_record(i) for i in items if i.get("title")]


def europepmc_search(title: str, surname: str = "", year: str = "", rows: int = EXTRA_ROWS) -> list[Record]:
    """Europe PMC by the title's words, with the first author and the year (one either side) when known."""
    words = _search_words(title)
    if len(words) < 2:
        return []
    q = f"TITLE:({' '.join(words)})"
    if surname and re.fullmatch(r"[A-Za-z' -]+", _fold(surname)):
        q += f' AND AUTH:"{_fold(surname)}"'
    yr = _year_range(year)
    if yr:
        q += f" AND PUB_YEAR:[{yr[0]} TO {yr[1]}]"
    return _europepmc(q, rows)


def europepmc_doi(doi: str) -> list[Record]:
    return _europepmc(f'DOI:"{doi.strip()}"', 2) if doi else []


# OpenAlex: https://docs.openalex.org (key from OPENALEX_API_KEY; without one, the shared free budget)

def _openalex_headers() -> dict:
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _openalex_record(w: dict) -> Record:
    full_names, authors = [], []
    for a in w.get("authorships") or []:
        name = (a.get("author") or {}).get("display_name") or a.get("raw_author_name") or ""
        if name:
            full_names.append(name)
            authors.append(split_name(name))
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    issns = list(dict.fromkeys([x for x in [src.get("issn_l")] + list(src.get("issn") or []) if x]))
    journal, full = _journal_fields(src.get("display_name") or "", issns)
    b = w.get("biblio") or {}
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", w.get("doi") or "", flags=re.I)
    pmid = re.sub(r"\D", "", ((w.get("ids") or {}).get("pmid") or "").rsplit("/", 1)[-1])
    return Record(
        source="openalex", title=_deposit_title(w.get("title") or w.get("display_name") or ""), authors=authors,
        full_names=full_names, journal=journal, journal_full=full, year=_year(w.get("publication_year")),
        volume=str(b.get("volume") or ""), issue=str(b.get("issue") or ""),
        pages=_pages(b.get("first_page"), b.get("last_page")), pmid=pmid, doi=doi, issn=issns,
    )


def openalex_search(title: str, year: str = "", rows: int = EXTRA_ROWS) -> list[Record]:
    """OpenAlex works by the title's words (and the year, one either side); the first author is checked on the
    records (refengine), not searched (OpenAlex's author names are display names)."""
    words = _search_words(title, 12)
    if len(words) < 2:
        return []
    filt = "title.search:" + " ".join(words)
    yr = _year_range(year)
    if yr:
        filt += f",publication_year:{yr[0]}-{yr[1]}"
    url = OPENALEX + "?" + urllib.parse.urlencode({"filter": filt, "per-page": rows})
    data = _json(http().get(url, 0.15, headers=_openalex_headers(), timeout=EXTRA_TIMEOUT, breaker=True)) or {}
    return [_openalex_record(w) for w in data.get("results") or [] if w.get("title") or w.get("display_name")]


def openalex_doi(doi: str) -> list[Record]:
    if not doi:
        return []
    url = f"{OPENALEX}/https://doi.org/{urllib.parse.quote(doi.strip(), safe='/')}"
    data = _json(http().get(url, 0.15, headers=_openalex_headers(), timeout=EXTRA_TIMEOUT, breaker=True))
    return [_openalex_record(data)] if data and data.get("id") else []


# Semantic Scholar Graph API: https://api.semanticscholar.org/api-docs/graph (key from SEMANTIC_SCHOLAR_API_KEY;
# without one, a shared pool that often answers 429)

S2_FIELDS = "title,authors,year,venue,journal,externalIds,publicationVenue,publicationDate"


def _s2_headers() -> dict:
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    return {"x-api-key": key} if key else {}


def _s2_record(d: dict) -> Record:
    full_names = [a.get("name") or "" for a in d.get("authors") or [] if a.get("name")]
    j = d.get("journal") or {}
    venue = d.get("publicationVenue") or {}
    issns = [x for x in [venue.get("issn")] if x]
    journal, full = _journal_fields(j.get("name") or venue.get("name") or d.get("venue") or "", issns)
    ids = d.get("externalIds") or {}
    year = _year(d.get("year"))
    online = _year(d.get("publicationDate"))
    return Record(
        source="semantic_scholar", title=_deposit_title(d.get("title") or ""), authors=[split_name(n) for n in full_names],
        full_names=full_names, journal=journal, journal_full=full, year=year, volume=str(j.get("volume") or "").strip(),
        pages=re.sub(r"\s*[-–]\s*", "-", str(j.get("pages") or "").strip()), pmid=str(ids.get("PubMed") or ""),
        doi=str(ids.get("DOI") or ""), issn=issns, other_years=[online] if online and online != year else [],
    )


def _s2_gap() -> float:
    return 1.05  # one request a second, with or without a key


def s2_match(title: str, year: str = "") -> list[Record]:
    """Semantic Scholar's single best title match (its /paper/search/match), within a year either side."""
    if len(_search_words(title)) < 2:
        return []
    params = {"query": re.sub(r"\s+", " ", title).strip()[:300], "fields": S2_FIELDS}
    yr = _year_range(year)
    if yr:
        params["year"] = f"{yr[0]}-{yr[1]}"
    body = http().get(f"{SEMANTIC_SCHOLAR}/search/match?" + urllib.parse.urlencode(params), _s2_gap(),
                      headers=_s2_headers(), timeout=EXTRA_TIMEOUT, breaker=True)
    data = _json(body) or {}
    return [_s2_record(d) for d in data.get("data") or [] if d.get("title")]


def s2_doi(doi: str) -> list[Record]:
    if not doi:
        return []
    url = f"{SEMANTIC_SCHOLAR}/DOI:{urllib.parse.quote(doi.strip(), safe='/')}?" + urllib.parse.urlencode({"fields": S2_FIELDS})
    data = _json(http().get(url, _s2_gap(), headers=_s2_headers(), timeout=EXTRA_TIMEOUT, breaker=True))
    return [_s2_record(data)] if data and data.get("title") else []


# DOAJ: https://doaj.org/api/docs (no key for searches; two requests a second)

def _doaj_record(item: dict) -> Record:
    b = item.get("bibjson") or {}
    j = b.get("journal") or {}
    full_names = [a.get("name") or "" for a in b.get("author") or [] if (a.get("name") or "").strip()]
    issns = list(dict.fromkeys(list(j.get("issns") or []) + [i.get("id") for i in b.get("identifier") or []
                                                              if i.get("type") in ("pissn", "eissn") and i.get("id")]))
    journal, full = _journal_fields(j.get("title") or "", issns)
    doi = next((i.get("id") or "" for i in b.get("identifier") or [] if (i.get("type") or "").lower() == "doi"), "")
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi.strip(), flags=re.I)
    return Record(
        source="doaj", title=_deposit_title(b.get("title") or ""), authors=[split_name(n) for n in full_names],
        full_names=full_names, journal=journal, journal_full=full, year=_year(b.get("year")),
        volume=str(j.get("volume") or ""), issue=str(j.get("number") or ""),
        pages=_pages(b.get("start_page"), b.get("end_page")), doi=doi, issn=issns,
    )


def _doaj(query: str, rows: int = EXTRA_ROWS) -> list[Record]:
    url = DOAJ + urllib.parse.quote(query, safe="") + "?" + urllib.parse.urlencode({"pageSize": rows})
    data = _json(http().get(url, 0.5, timeout=EXTRA_TIMEOUT, breaker=True)) or {}
    return [_doaj_record(i) for i in data.get("results") or [] if (i.get("bibjson") or {}).get("title")]


def doaj_search(title: str, year: str = "", rows: int = EXTRA_ROWS) -> list[Record]:
    """DOAJ articles whose title has all the title's search words (and of the year, one either side)."""
    words = _search_words(title)
    if len(words) < 2:
        return []
    q = "bibjson.title:(" + " AND ".join(words) + ")"
    yr = _year_range(year)
    if yr:
        q += f" AND bibjson.year:[{yr[0]} TO {yr[1]}]"
    return _doaj(q, rows)


def doaj_doi(doi: str) -> list[Record]:
    return _doaj(f'doi:"{doi.strip()}"', 2) if doi else []


def extra_search(source: str, title: str, surname: str = "", year: str = "") -> list[Record]:
    """One extra source's records for a title (first author and year where the source can search by them)."""
    if source == "europepmc":
        recs = europepmc_search(title, surname, year)
        return recs or (europepmc_search(title, "", year) if surname else [])  # the author's name spelled otherwise
    if source == "openalex":
        return openalex_search(title, year)
    if source == "semantic_scholar":
        return s2_match(title, year)
    if source == "doaj":
        return doaj_search(title, year)
    return []


def extra_doi(source: str, doi: str) -> list[Record]:
    """One extra source's record for a DOI."""
    return {"europepmc": europepmc_doi, "openalex": openalex_doi, "semantic_scholar": s2_doi,
            "doaj": doaj_doi}.get(source, lambda d: [])(doi)
