"""PubMed (NCBI E-utilities) and CrossRef lookups, with a local response cache.

NCBI allows 3 requests a second without a key (10 with NCBI_API_KEY); calls are spaced
accordingly. Responses are cached in SQLite so re-running a manuscript costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
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


class _Http:
    def __init__(self, path: str = CACHE_DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.con = sqlite3.connect(path)
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
        if not year:
            year = a.findtext("ArticleDate/Year") or ""
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
        ))
    return out


def _crossref_record(item: dict) -> Record:
    year = ""
    for k in ("published-print", "published-online", "issued"):
        parts = item.get(k, {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            year = str(parts[0][0])
            break
    authors = []
    for au in item.get("author", []):
        if au.get("family"):
            initials = "".join(w[0] for w in (au.get("given") or "").replace("-", " ").replace(".", " ").split() if w)
            authors.append((au["family"], initials.upper()))
        elif au.get("name"):
            authors.append((au["name"], ""))
    container = item.get("short-container-title") or item.get("container-title") or [""]
    return Record(
        source="crossref", title=(item.get("title") or [""])[0], authors=authors, journal=container[0],
        year=year, volume=item.get("volume", ""), issue=item.get("issue", ""),
        pages=item.get("page", "") or item.get("article-number", ""), doi=item.get("DOI", ""),
        issn=item.get("ISSN", []),
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
