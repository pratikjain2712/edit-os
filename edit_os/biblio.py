"""PubMed (NCBI E-utilities) and CrossRef lookups, with a local response cache.

NCBI allows 3 requests a second without a key (10 with NCBI_API_KEY); calls are spaced
accordingly. Responses are cached in SQLite so re-running a manuscript costs nothing.
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
CACHE_DB = os.path.join(os.path.dirname(DEFAULT_DB), "http_cache.sqlite")


@dataclass
class Record:
    source: str  # "pubmed" or "crossref"
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


class _Http:
    def __init__(self, path: str = CACHE_DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.con = sqlite3.connect(path, check_same_thread=False)  # shared by worker threads (serialized SQLite)
        self.con.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, body TEXT)")
        self.last: dict[str, float] = {}

    def get(self, url: str, host_gap: float) -> str | None:
        key = hashlib.sha256(url.encode()).hexdigest()
        row = self.con.execute("SELECT body FROM cache WHERE key = ?", (key,)).fetchone()
        if row:
            return row[0]
        host = urllib.parse.urlparse(url).netloc
        wait = self.last.get(host, 0) + host_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        body = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "edit-os/0.1 (reference checking)"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read().decode("utf-8", errors="replace")
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    body = ""
                    break
                time.sleep(1 + attempt * 2)  # 429 / 5xx: back off and retry
            except (urllib.error.URLError, TimeoutError):
                time.sleep(1 + attempt * 2)
        self.last[host] = time.monotonic()
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
    return html.unescape(_MARKUP.sub("", s or "")).strip()


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
