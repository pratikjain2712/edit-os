"""Web search fallback for journal-article references that no bibliographic database verified.

Owner's rule (2026-10-28): never decide that a reference exists from model output alone. Claude may help find a
candidate page; code must confirm it against the page (title, authors, year).

So the work is split in two:

1. Claude, with Anthropic's server-side web search tool, proposes candidate pages for the entry (the DOI link,
   PubMed / PMC, the journal's or publisher's page). Its answer is a list of {url, why}; nothing else in it is
   used, not even its reading of the page. Calls are cached by the entry's text and held to the journal's
   limits (`references.web_search`: searches per reference and per article, a cost cap per article).
2. Code opens each candidate (timeouts, a size limit, redirects followed, so a doi.org link lands on the
   publisher) and reads the article's metadata the page itself declares: Highwire `citation_*` meta tags, Dublin
   Core / PRISM, JSON-LD ScholarlyArticle; a DOI or PMID on the page (or in its URL) is looked up in CrossRef /
   DataCite / PubMed. A bot-challenge page or an error counts as nothing found (it is never worked around).
   A record built from that metadata is accepted only when its title matches the entry's (refengine.accept's
   threshold, and written in the entry), its first author is the entry's, the year is the entry's, and neither
   volume nor first page conflicts; then it goes through the same acceptance and formatting as a database record.
   It carries `web` provenance {source: "web", url, site, via, verified_fields}: "Verified on <site>".

Nothing verified: the entry is styled from the author's text and queried as before. Where a page had the entry's
title (almost word for word) but another field conflicts, the editor's query names it ("Possible source: <url>").

Runs only inside `article()` (one per manuscript: rules.lookup_references), with `references.web_search.enabled`
and an API key (EDITOS_ANTHROPIC_API_KEY, as ai_refs).
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import html as htmlmod
import json
import os
import re
import sqlite3
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

from rapidfuzz import fuzz

from . import ai_refs, biblio, journals, refengine, spend

MODEL = ai_refs.MODEL
# The basic search tool: the _20260209 variant (dynamic filtering) runs code execution, ~100 s a call on Haiku
# against ~7 s for this one, and is not listed for Haiku models.
SEARCH_TOOL = "web_search_20250305"
PRICES = {  # $ per million input / output tokens (cache reads at 0.1x input are not used: prompts are short)
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
SEARCH_USD = 0.01  # $10 per 1,000 searches
TOKENS_PER_SEARCH = 12_000  # input tokens a search adds (results with their page text), for the cost pre-check
DEFAULTS = {"enabled": False, "max_searches_per_ref": 2, "max_per_article": 20, "cost_cap_usd": 0.30}
MAX_CANDIDATES = 4
NEAR_TITLE = 90  # a page with this whole-title similarity but another field differing: named in the editor's query
MAX_PAGE_BYTES = 3_000_000
PAGE_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; edit-os/0.1; reference checking)"
PARSER_VERSION = "2"  # part of the page cache key: a change to the metadata reading re-reads the pages
STORE_PATH = os.path.join(os.path.dirname(ai_refs.CACHE_DB), "web_cache.sqlite")

USAGE = {"calls": 0, "searches": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "pages": 0,
         "looked_up": 0, "verified": 0, "possible": 0}  # this process, for cost reporting

SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"url": {"type": "string"}, "why": {"type": "string"}},
                "required": ["url", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You help check one bibliography entry from a medical journal manuscript that PubMed and CrossRef
could not match. Search the web for the published article the entry cites and return the URLs of pages about that
article: its DOI link (https://doi.org/...), its PubMed or PMC page, or its page on the journal's or publisher's
site. Prefer DOI, PubMed and PMC links. Give at most four, best first, and only URLs you saw in the search results
(or a DOI link for a DOI you saw there). Leave out search-result listings, PDFs, and pages about other articles.
If nothing you found looks like this article, return an empty list. For each URL, `why` says in a few words which
details of the entry the page shows. The URLs are checked against the pages themselves; do not correct the entry."""


# --- settings and per-article budget ---------------------------------------------------------------------------

def settings(style) -> dict:
    got = style.get("references.web_search") or {}
    return {**DEFAULTS, **(got if isinstance(got, dict) else {"enabled": bool(got)})}


def _prices(model: str) -> tuple[float, float]:
    return PRICES.get(model, max(PRICES.values()))  # an unknown model: priced as the dearest, so the cap holds


def call_cost(input_tokens: int, output_tokens: int, searches: int, model: str = "") -> float:
    pin, pout = _prices(model or MODEL)
    return (input_tokens * pin + output_tokens * pout) / 1e6 + searches * SEARCH_USD


@dataclass
class Budget:
    """One article's limits and spend."""
    max_searches_per_ref: int = DEFAULTS["max_searches_per_ref"]
    max_per_article: int = DEFAULTS["max_per_article"]
    cost_cap_usd: float = DEFAULTS["cost_cap_usd"]
    searches: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    looked_up: int = 0
    verified: int = 0

    def room(self) -> int:
        """Searches the next call may make: what is left of both limits, and only as many as the cost cap still
        covers at their worst (every search used, its results read in full, a long answer)."""
        n = min(self.max_searches_per_ref, self.max_per_article - self.searches)
        while n > 0 and self.cost_usd + call_cost(3_000 + TOKENS_PER_SEARCH * n, 2_000, n) > self.cost_cap_usd:
            n -= 1
        return max(n, 0)

    def spend(self, searches: int, cost: float) -> None:
        self.calls += 1
        self.searches += searches
        self.cost_usd += cost


_current: contextvars.ContextVar[Budget | None] = contextvars.ContextVar("refweb_budget", default=None)


@contextlib.contextmanager
def article(style):
    """The web fallback is on for the references resolved inside this block (one manuscript), within its limits.
    Yields the Budget (None when the journal has it off)."""
    cfg = settings(style)
    if not cfg["enabled"]:
        yield None
        return
    budget = Budget(int(cfg["max_searches_per_ref"]), int(cfg["max_per_article"]), float(cfg["cost_cap_usd"]))
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)


# --- cache ----------------------------------------------------------------------------------------------------

class _Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.execute("CREATE TABLE IF NOT EXISTS candidates (key TEXT PRIMARY KEY, body TEXT)")
        self.con.execute("CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, body TEXT)")
        self.lock = threading.Lock()

    def get(self, table: str, key: str):
        with self.lock:
            row = self.con.execute(f"SELECT body FROM {table} WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, table: str, key: str, value) -> None:
        with self.lock:
            self.con.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?)", (key, json.dumps(value)))
            self.con.commit()


_store_obj: _Store | None = None


def _store() -> _Store:
    global _store_obj
    if _store_obj is None or getattr(_store_obj, "path", None) != STORE_PATH:
        _store_obj = _Store(STORE_PATH)
        _store_obj.path = STORE_PATH
    return _store_obj


def _key(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


# --- 1. Claude: candidate pages ------------------------------------------------------------------------------

@dataclass
class Candidate:
    url: str
    why: str = ""


def _call_claude(text: str, max_searches: int) -> tuple[list[dict], int, int, int]:
    """(candidates as Claude gave them, input tokens, output tokens, searches made)."""
    import anthropic

    client = anthropic.Anthropic(api_key=ai_refs._api_key(),
                                 base_url=os.environ.get("EDITOS_ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            tools=[{"type": SEARCH_TOOL, "name": "web_search", "max_uses": max_searches}],
            system=INSTRUCTIONS,
            messages=[{"role": "user", "content": text}],
        )
    except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError):
        return [], 0, 0, 0
    except anthropic.APIStatusError:
        return [], 0, 0, 0
    u = response.usage
    tool_use = getattr(u, "server_tool_use", None)
    searches = int(getattr(tool_use, "web_search_requests", 0) or 0)
    tokens_in = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
        + (getattr(u, "cache_creation_input_tokens", 0) or 0)
    if response.stop_reason in ("refusal", "max_tokens", "pause_turn"):
        return [], tokens_in, u.output_tokens or 0, searches
    body = next((b.text for b in reversed(response.content) if b.type == "text" and b.text.strip()), "")
    try:
        got = json.loads(body).get("candidates", [])
    except (ValueError, AttributeError):
        got = []
    return [c for c in got if isinstance(c, dict)], tokens_in, u.output_tokens or 0, searches


_SEARCH_PAGE = re.compile(r"//(?:www\.)?(?:google\.[a-z.]+/(?:search|url)|scholar\.google\.|bing\.com/search|"
                          r"duckduckgo\.com|semanticscholar\.org/search|researchgate\.net/search)", re.I)


def _clean(cands: list[dict]) -> list[Candidate]:
    out, seen = [], set()
    for c in cands:
        url = str(c.get("url", "")).strip()
        if not re.match(r"https?://[^\s/]+\.[^\s/]+", url) or _SEARCH_PAGE.search(url) or url in seen:
            continue
        seen.add(url)
        out.append(Candidate(url, str(c.get("why", ""))[:300]))
    return out[:MAX_CANDIDATES]


def candidates(text: str, budget: Budget) -> list[Candidate]:
    """Candidate pages for the entry: cached by its text; a live call only within the article's limits."""
    store = _store()
    key = _key(MODEL, SEARCH_TOOL, INSTRUCTIONS, json.dumps(SCHEMA, sort_keys=True), text)
    cached = store.get("candidates", key)
    if cached is not None:
        return _clean(cached)
    n = budget.room()
    if n <= 0:
        return []
    got, tin, tout, searches = _call_claude(text, n)
    cost = call_cost(tin, tout, searches)
    budget.spend(searches, cost)
    USAGE["calls"] += 1
    USAGE["searches"] += searches
    USAGE["input_tokens"] += tin
    USAGE["output_tokens"] += tout
    USAGE["cost_usd"] += cost
    spend.add("reference_web_search", MODEL, input_tokens=tin, output_tokens=tout, cost=cost, searches=searches)
    if tin:  # an answer (even an empty list) is kept; a failed request is not
        store.put("candidates", key, got)
    return _clean(got)


# --- 2. Code: the page's own metadata ------------------------------------------------------------------------

_CHALLENGE = re.compile(r"just a moment|attention required|are you a robot|captcha|access denied|"
                        r"verify you are human|request rejected|bot verification|security check", re.I)
DOI_IN = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>?#&]+)", re.I)
PUBMED_URL = re.compile(r"//(?:www\.)?(?:pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pubmed)/(\d{5,9})\b", re.I)


def _fetch_page(url: str) -> dict | None:
    """{"status", "final_url", "html"} for the page (redirects followed; at most MAX_PAGE_BYTES read; html empty
    unless the page is HTML). None: the request could not be made (network, timeout, TLS)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=PAGE_TIMEOUT, context=ssl.create_default_context()) as r:
            ctype = r.headers.get("content-type", "")
            body = r.read(MAX_PAGE_BYTES).decode("utf-8", errors="replace") if "html" in ctype or "xml" in ctype else ""
            return {"status": r.status, "final_url": r.geturl(), "html": body}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "final_url": e.geturl() or url, "html": ""}
    except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError, ValueError, OSError):
        return None


def _metas(doc) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in doc.iter("meta"):
        name = (m.get("name") or m.get("property") or "").strip().lower()
        content = (m.get("content") or "").strip()
        if name and content:
            out.setdefault(name, []).append(htmlmod.unescape(content))
    return out


def _years(*dates: str) -> list[str]:
    out = []
    for d in dates:
        m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", d or "")
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _name(raw: str) -> tuple[str, str] | None:
    """A page's author name as (surname, initials): "Kinchen, Elizabeth A." / "Elizabeth A Kinchen" /
    "Kinchen EA"; a group name whole."""
    s = " ".join(htmlmod.unescape(raw or "").split()).strip(" ,;")
    if not s:
        return None
    if refengine._GROUP_WORDS.search(s):  # "GBD 2019 Collaborators", "WHO Working Group"
        return s, ""
    if "," in s:
        sur, given = [x.strip() for x in s.split(",", 1)]
    else:
        m = re.fullmatch(r"(.+?)\s+([A-Z]{1,4})", s)
        if m:  # "Kinchen EA"
            return m.group(1), m.group(2)
        words = s.split()
        k = len(words) - 1
        while k > 0 and words[k - 1].lower() in refengine._PARTICLES:
            k -= 1
        sur, given = " ".join(words[k:]), " ".join(words[:k])
    initials = "".join(w[0] for w in re.split(r"[\s.\-]+", given) if w and w[0].isalpha()).upper()
    if sur.isupper() and len(sur) > 3:
        sur = sur.title()
    return (sur, initials) if sur else None


def _number(s: str) -> str:
    """A volume or issue as a page may write it: "Volume 11 (2020)" -> "11", "Vol. 7" -> "7"; "12A", "S1" kept."""
    s = " ".join((s or "").split())
    if re.fullmatch(r"[A-Za-z]{0,2}\d+[A-Za-z]?(?:\s*[-–/]\s*\d+)?", s):
        return s
    m = re.search(r"(?:vol(?:ume)?|no|issue|num(?:ber)?)\.?\s*([A-Za-z]?\d+[A-Za-z]?)", s, re.I) or re.search(r"\b(\d+)\b", s)
    return m.group(1) if m else ""


def _split_names(values: list[str]) -> list[str]:
    """One meta tag can hold several names: "S. K. Sripathi  and A. N. Lakshmi", "Doe J; Roe A"."""
    out = []
    for v in values:
        out += [x for x in re.split(r"\s*;\s*|\s+(?:and|&)\s+", v.strip()) if x.strip()]
    return out


def _pages(first: str, last: str) -> str:
    first, last = (first or "").strip(), (last or "").strip()
    return f"{first}-{last}" if first and last and last != first else first


def _journal(title: str, abbrev: str, issns: list[str]) -> tuple[str, str]:
    """(journal as the reference shows it, full title): NLM's abbreviation when NLM lists the journal (by ISSN or
    name), else the page's full title (stylesheets: complete name for non-indexed journals)."""
    d = journals.directory()
    j = None
    if d:
        j = next((x for x in map(d.lookup, issns) if x), None) or (title and d.lookup(title)) or \
            (abbrev and d.lookup(abbrev))
    if j:
        return refengine.NLM_FIXES.get(j.med_abbr, j.med_abbr), title or j.title
    return title or abbrev, title


def _record(via: str, title: str, authors: list[str], journal: str, abbrev: str, dates: list[str], volume: str,
            issue: str, first: str, last: str, doi: str, pmid: str, issns: list[str]) -> biblio.Record | None:
    title = " ".join(htmlmod.unescape(title or "").split())
    if not title:
        return None
    names = [n for n in map(_name, _split_names(authors)) if n]
    years = _years(*dates)
    jname, jfull = _journal(" ".join((journal or "").split()), " ".join((abbrev or "").split()), issns)
    return biblio.Record(source="web", title=title, authors=names, journal=jname, journal_full=jfull,
                         year=years[0] if years else "", other_years=years[1:], volume=_number(volume),
                         issue=_number(issue), pages=_pages(first, last), doi=(doi or "").strip(),
                         pmid=(pmid or "").strip(), issn=[i for i in issns if i])


def _highwire(m: dict[str, list[str]]) -> biblio.Record | None:
    g = lambda k: (m.get(k) or [""])[0]  # noqa: E731
    authors = m.get("citation_author", [])
    if not authors and m.get("citation_authors"):
        authors = re.split(r"\s*;\s*", m["citation_authors"][0])
    doi = g("citation_doi")
    doi = DOI_IN.search(doi).group(1) if DOI_IN.search(doi) else ""
    return _record("meta", g("citation_title"), authors, g("citation_journal_title"), g("citation_journal_abbrev"),
                   [g("citation_publication_date"), g("citation_date"), g("citation_year"), g("citation_online_date"),
                    g("citation_cover_date")],
                   g("citation_volume"), g("citation_issue"), g("citation_firstpage"), g("citation_lastpage"),
                   doi, g("citation_pmid"), m.get("citation_issn", []) + m.get("citation_eissn", []))


def _dublin_core(m: dict[str, list[str]]) -> biblio.Record | None:
    g = lambda *ks: next((m[k][0] for k in ks if m.get(k)), "")  # noqa: E731
    ids = m.get("dc.identifier", []) + m.get("prism.doi", []) + m.get("dc.identifier.doi", [])
    doi = next((DOI_IN.search(i).group(1) for i in ids if DOI_IN.search(i)), "")
    return _record("dc", g("dc.title", "dcterms.title"), m.get("dc.creator", []) + m.get("dcterms.creator", []),
                   g("prism.publicationname", "dc.source", "dc.relation.ispartof"), "",
                   [g("prism.publicationdate", "dc.date", "dcterms.issued", "dc.date.issued")],
                   g("prism.volume"), g("prism.number"), g("prism.startingpage"), g("prism.endingpage"), doi, "",
                   m.get("prism.issn", []) + m.get("prism.eissn", []))


_ARTICLE_TYPES = {"scholarlyarticle", "medicalscholarlyarticle", "article"}


def _ld_nodes(data):
    if isinstance(data, list):
        for x in data:
            yield from _ld_nodes(x)
    elif isinstance(data, dict):
        yield data
        for k in ("@graph", "mainEntity"):
            if k in data:
                yield from _ld_nodes(data[k])


def _ld_text(v) -> str:
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value") or ""
    return str(v or "")


def _json_ld(doc) -> list[biblio.Record]:
    out = []
    for s in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(s.text or "")
        except ValueError:
            continue
        for n in _ld_nodes(data):
            types = n.get("@type", [])
            types = {t.lower() for t in (types if isinstance(types, list) else [types]) if isinstance(t, str)}
            if not types & _ARTICLE_TYPES:
                continue
            authors = n.get("author", [])
            authors = authors if isinstance(authors, list) else [authors]
            names = []
            for a in authors:
                if isinstance(a, dict):
                    if a.get("familyName"):
                        names.append(f"{a['familyName']}, {a.get('givenName', '')}")
                    elif a.get("name"):
                        names.append(str(a["name"]))
                elif isinstance(a, str):
                    names.append(a)
            vol = issue = journal = ""
            part = n.get("isPartOf")
            for _ in range(3):  # PublicationIssue -> PublicationVolume -> Periodical
                if not isinstance(part, dict):
                    break
                t = str(part.get("@type", "")).lower()
                if "issue" in t:
                    issue = str(part.get("issueNumber", ""))
                elif "volume" in t:
                    vol = str(part.get("volumeNumber", ""))
                elif "periodical" in t:
                    journal = _ld_text(part.get("name"))
                part = part.get("isPartOf")
            ids = [_ld_text(n.get(k)) for k in ("identifier", "sameAs", "@id", "url")]
            doi = next((DOI_IN.search(i).group(1) for i in ids if DOI_IN.search(i)), "")
            r = _record("jsonld", _ld_text(n.get("headline") or n.get("name")), names, journal, "",
                        [_ld_text(n.get("datePublished"))], vol, issue, str(n.get("pageStart", "")),
                        str(n.get("pageEnd", "")), doi, "", [])
            if r:
                out.append(r)
    return out


def page_metadata(page_html: str) -> list[biblio.Record]:
    """The article metadata a page declares, surest first: Highwire meta tags, Dublin Core / PRISM, JSON-LD."""
    if not page_html:
        return []
    from lxml import html as lhtml

    try:
        doc = lhtml.fromstring(page_html)
    except (ValueError, Exception):  # lxml raises ParserError / XMLSyntaxError on empty or broken documents
        return []
    title = doc.findtext(".//title") or ""
    if _CHALLENGE.search(title):
        return []
    m = _metas(doc)
    return _merged([r for r in (_highwire(m), _dublin_core(m)) if r] + _json_ld(doc))


_FILL = ("authors", "journal", "journal_full", "volume", "issue", "pages", "doi", "pmid", "issn")


def _merged(records: list[biblio.Record]) -> list[biblio.Record]:
    """Each record with the fields it lacks taken from the page's other records of the same article (the same title
    and year): a page's meta tags without the volume, its JSON-LD with it. Nothing a record has is overwritten."""
    for r in records:
        for o in records:
            if o is r or fuzz.token_sort_ratio(refengine.fold(r.title), refengine.fold(o.title)) < 95 \
                    or (r.year and o.year and r.year != o.year):
                continue
            for k in _FILL:
                if not getattr(r, k) and getattr(o, k):
                    setattr(r, k, getattr(o, k))
    return records


def datacite_doi(doi: str) -> biblio.Record | None:
    body = biblio.http().get("https://api.datacite.org/dois/" + urllib.parse.quote(doi), 0.2)
    try:
        a = json.loads(body)["data"]["attributes"] if body else None
    except (ValueError, KeyError, TypeError):
        return None
    if not a:
        return None
    names = []
    for c in a.get("creators", []):
        if c.get("familyName"):
            names.append(f"{c['familyName']}, {c.get('givenName', '')}")
        elif c.get("name"):
            names.append(c["name"])
    cont = a.get("container") or {}
    r = _record("datacite", _ld_text((a.get("titles") or [{}])[0].get("title")), names, cont.get("title", ""), "",
                [str(a.get("publicationYear", ""))], cont.get("volume", ""), cont.get("issue", ""),
                cont.get("firstPage", ""), cont.get("lastPage", ""), a.get("doi", doi), "", [])
    if r:
        r.source = "datacite"
    return r


def _by_doi(doi: str) -> list[tuple[biblio.Record, str]]:
    doi = doi.rstrip(".,;)")
    r = biblio.crossref_doi(doi)
    if r:
        return [(r, "crossref")]
    r = datacite_doi(doi)
    return [(r, "datacite")] if r else []


def _page(url: str) -> dict:
    """{"status", "final_url", "records": [Record dicts]} for a candidate URL; cached unless the request failed."""
    store = _store()
    key = _key("page", PARSER_VERSION, url)
    got = store.get("pages", key)
    if got is not None:
        return got
    USAGE["pages"] += 1
    page = _fetch_page(url)
    if page is None:
        return {"status": None, "final_url": url, "records": []}
    status = page.get("status")
    records = page_metadata(page.get("html", "")) if status == 200 else []
    blocked = status == 200 and not records and _CHALLENGE.search(page.get("html", "")[:5000] or "")
    out = {"status": "blocked" if blocked else status, "final_url": page.get("final_url") or url,
           "records": [asdict(r) for r in records]}
    if status in (200, 401, 403, 404, 410, 451):  # an answer (a bot check is one too); not a timeout or a 5xx
        store.put("pages", key, out)
    return out


def records_for(url: str) -> tuple[list[tuple[biblio.Record, str]], str]:
    """(records the candidate page leads to, each with how it was read, the page's final URL). A PubMed link is
    read through E-utilities, a DOI (in the link or declared by the page) through CrossRef / DataCite; any other
    page by its own metadata."""
    m = PUBMED_URL.search(url)
    if m:
        return [(r, "pubmed") for r in biblio.pubmed_fetch([m.group(1)])], url
    out: list[tuple[biblio.Record, str]] = []
    page = _page(url)
    final = page.get("final_url") or url
    for d in page.get("records", []):
        out.append((biblio.Record(**d), "page"))
    dois = [r.doi for r, _ in out if r.doi]
    for u in (final, url):  # a DOI link, or a publisher path with the DOI in it ("/doi/10.4103/...")
        mm = DOI_IN.search(urllib.parse.unquote(u))
        if mm:
            dois.append(mm.group(1))
    for doi in dict.fromkeys(d.lower() for d in dois):
        out += _by_doi(doi)
    pmids = dict.fromkeys(r.pmid for r, _ in out if r.pmid and r.source == "web")
    if pmids:
        out += [(r, "pubmed") for r in biblio.pubmed_fetch(list(pmids))]
    return out, final


# --- verification (code only) ---------------------------------------------------------------------------------

def _num(s: str) -> str:
    return re.sub(r"\D.*", "", (s or "").strip()).lstrip("0")


@dataclass
class Check:
    verified: bool
    title: float  # whole-title similarity, 0-100
    agree: list[str] = field(default_factory=list)
    conflict: list[str] = field(default_factory=list)


def verify(p: refengine.Parsed, r: biblio.Record) -> Check:
    """The page's record is the entry's paper: the title matches (refengine.accept's threshold, and the record's
    title is written in the entry), the first author is the entry's, the year is the entry's (print or online),
    the entry's volume and first page do not differ from the record's, and refengine.accept agrees."""
    whole = fuzz.token_sort_ratio(refengine.fold(p.title), refengine.fold(r.title)) if p.title and r.title else 0
    t, author, _ = refengine.score(p, r)
    agree, conflict = [], []
    title_ok = t >= 90 and fuzz.partial_ratio(refengine.fold(r.title), refengine.fold(p.text)) >= 90
    (agree if title_ok else conflict).append("title")
    (agree if author else conflict).append("first author")
    years = {y[:4] for y in [r.year, *r.other_years] if y}
    (agree if p.year and p.year in years else conflict).append("year")
    if r.source in ("web", "datacite") and p.authors and len(r.authors) < min(len(p.authors), 6):
        conflict.append("authors")  # a page listing fewer authors than the entry: its list is not usable
    c = refengine.citation(p.text)
    if c and _num(c[1]) and _num(r.volume):
        (agree if _num(c[1]) == _num(r.volume) else conflict).append("volume")
    if c and refengine._first_page(c[3]) and refengine._first_page(r.pages):
        (agree if refengine._first_page(c[3]) == refengine._first_page(r.pages) else conflict).append("pages")
    if refengine.journal_agrees(p.text, r):
        agree.append("journal")
    elif refengine.journal_conflicts(p, r):
        conflict.append("journal")
    ok = not conflict and refengine.accept(p, r)
    return Check(ok, whole, agree, conflict)


@dataclass
class WebRecord(biblio.Record):
    """A record read from a page Claude pointed to, verified in code; `web` is its provenance."""
    web: dict = field(default_factory=dict)


def site(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


def fallback(p: refengine.Parsed, style, why: str, queries: list) -> tuple[biblio.Record | None, str]:
    """Called where the database lookup gave up on a journal-article entry: (verified record, "") or (None, why).
    May add a "possible source" query to `queries` for the editor."""
    budget = _current.get()
    if budget is None or not settings(style)["enabled"] or not ai_refs.available():
        return None, why
    if not (p.title and p.authors and p.year) or "points to a different article" in why or refengine.DIFFERS in why:
        return None, why  # nothing to check a page against, or the entry's own DOI / PMID already answered
    budget.looked_up += 1
    USAGE["looked_up"] += 1
    near: tuple[float, str] | None = None
    for cand in candidates(p.text, budget):
        recs, final = records_for(cand.url)
        for r, via in recs:
            chk = verify(p, r)
            if chk.verified:
                if r.source == "web" and r.year != p.year:  # the page gives both dates (online, issue): the entry's
                    r.other_years = [y for y in [r.year, *r.other_years] if y != p.year]
                    r.year = p.year
                budget.verified += 1
                USAGE["verified"] += 1
                url = final if via == "page" else cand.url
                web = {"source": "web", "url": url, "site": site(url), "via": via, "verified_fields": chk.agree}
                return WebRecord(**{**asdict(r), "web": web}), ""
            if chk.title >= NEAR_TITLE and chk.conflict and (near is None or chk.title > near[0]):
                near = (chk.title, final if via == "page" else cand.url)
    if near:
        USAGE["possible"] += 1
        queries.append(("references.possible_source", {"url": near[1]}))
    return None, (why + "; no web page confirmed it") if why else "No web page confirmed it"
