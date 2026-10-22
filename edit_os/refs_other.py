"""References that are not journal articles: websites, books and chapters, conference papers,
reports and theses.

Journal articles are rebuilt from PubMed / CrossRef records (refengine). These entries are kept in
the author's own words; only template fixes are made (guide: "Available from: URL. [Last accessed
on YYYY Mon DD].", "nth ed.", "City: Publisher; Year. p. xx-xx"), and what the author must supply
or confirm becomes a query:

- links are opened (status, redirects, final URL, page title); a dead page, a soft 404 or a
  redirect to some other page is queried. An access date is moved into the template, never made up;
  a missing one is queried.
- books and chapters are looked up in CrossRef (book / chapter records) and Open Library (title,
  author, year); a book found in neither is queried.
- conference papers, reports and theses get the template fixes and an "unverified" query.
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from . import biblio, refengine
from .style import JournalStyle

OTHER_VERIFIED = "other_verified"  # checked against a record or a live page; template fixes only
OTHER_FIXED = "other_fixed"        # template fixes only, nothing to check it against
STATUSES = (refengine.NOT_JOURNAL, OTHER_VERIFIED, OTHER_FIXED)

WEBSITE, BOOK, CHAPTER, CONFERENCE, REPORT, THESIS, OTHER = (
    "website", "book", "book_chapter", "conference", "report", "thesis", "other")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
URL = re.compile(r"(?:https?://|www\.)[^\s<>\"\]]+", re.I)
_URL_PREFIX = re.compile(
    r"\[?\s*(?:\b(?:Available|Accessible|Retrieved|Accessed)\s*(?:online\s*)?(?:from|at|on|in|via)?\s*:?"
    r"|\bURL\s*:|\bOnline\s*:|\bfrom\s*:)\s*$", re.I)
# Access-date phrases, with or without brackets: [cited 2025 Oct 4], Accessed on 25th March 2026,
# [Last accessed on 2010 Jul 10], Accessed: Apr. 20, 2025, [Accessed 1 May 2018], Last assessed on ...
_ACCESS = re.compile(
    r"(?P<open>[(\[]?)\s*(?:date\s+)?(?:(?:last|as)\s+)?(?P<word>cited|accessed|assessed|retrieved|viewed|visited)"
    r"(?:\s+(?:on|at))?\s*[:\-]?\s*(?:on\s+)?", re.I)
_NOTE = re.compile(r"\s*\[(?:Internet|Online|monograph on the Internet|homepage on the Internet|database on the Internet"
                   r"|serial online|E-?book)\]", re.I)  # "[Updated 2025 Mar 4]" stays, as the editors keep it
_BARE_SITE = re.compile(r"(?<=\bAvailable from )(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?", re.I)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
             "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20}
_EDITION = re.compile(
    r"(?:(?<=[.,]\s)|^)(?:(?P<num>\d{1,2})\s*(?:st|nd|rd|th)?|(?P<word>" + "|".join(_ORDINALS) + r"))\s*(?:edition|edn|ed)\b\.?(?=\s*(?:[,;.]|$))"
    r"|(?:(?<=[.,]\s)|^)(?:edition|edn|ed)\.?\s+(?P<num2>\d{1,2})\b\.?(?=\s*(?:[,;.]|$))", re.I)
_CHALLENGE = re.compile(r"just a moment|checking your browser|attention required|recaptcha|access denied|captcha"
                        r"|are you a robot|security check|cloudflare|request rejected|forbidden", re.I)
_SOFT_404 = re.compile(r"\b(404|page not found|not found|does not exist|no longer available|page unavailable"
                       r"|error page|cannot be found|can't be found|could not be found|search results)\b", re.I)


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# --- kinds ---------------------------------------------------------------------------------

def classify(text: str, kind: str = "") -> str:
    """Source type: Claude's kind when it gave one, else from the wording."""
    if kind and kind != "journal_article":
        return kind
    if refengine.YEAR_VOL.search(text):
        return OTHER  # a journal citation (year;volume:pages) the journal path turned away: leave it
    if re.search(r"\b(thesis|dissertation)\b", text, re.I):
        return THESIS
    if re.search(r"\b(conference|congress|symposium|proceedings|annual meeting|paper presented|presented at"
                 r"|poster presented|abstract presented|workshop)\b", text, re.I):
        return CONFERENCE
    if re.search(r"\bIn\s*:\s|,\s*editors?\b|\(eds?\.?\)", text):
        return CHAPTER
    if _EDITION.search(text) or re.search(r"\b(Press|Publishers?|Publishing|Publications)\b", text) \
            or re.search(r"[A-Z][\w .()]{2,40}:\s*[A-Z][^;:]{2,60};\s*(?:19|20)\d\d\b", text):
        return BOOK
    if re.search(r"\b(report|guidelines?|World Health Organization|Ministry|Government|survey|fact ?sheet"
                 r"|census|policy|programme|program)\b", text, re.I):
        return REPORT
    if URL.search(text):
        return WEBSITE
    return OTHER


# --- dates ---------------------------------------------------------------------------------

def _month(s: str) -> int | None:
    s = s.strip(". ").lower()[:3]
    for i, m in enumerate(MONTHS):
        if m.lower() == s:
            return i + 1
    return None


def access_date(s: str) -> str | None:
    """'2025 Oct 4', '25th March 2026', 'Apr. 20, 2025', '2026-03-25', '25/03/2026' -> '2026 Mar 25'.
    None when the date is missing, partial or ambiguous (03/04/2025): never guessed."""
    s = re.sub(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", " ", s.strip())  # "2021Dec10"
    s = re.sub(r"(\d)\s?(st|nd|rd|th)\b", r"\1", s, flags=re.I).replace(",", " ")
    s = " ".join(s.split())
    y = r"((?:19|20)\d\d)"
    for pat, order in ((rf"^{y}\s+({_MONTH})\s+(\d{{1,2}})$", "ymd"), (rf"^(\d{{1,2}})\s+({_MONTH})\s+{y}$", "dmy"),
                       (rf"^({_MONTH})\s+(\d{{1,2}})\s+{y}$", "mdy")):
        m = re.match(pat, s, re.I)
        if m:
            g = dict(zip(order, m.groups()))
            month, day = _month(g["m"]), int(g["d"])
            if month and 1 <= day <= 31:
                return f"{g['y']} {MONTHS[month - 1]} {day:02d}"
    m = re.match(rf"^{y}[-/.](\d{{1,2}})[-/.](\d{{1,2}})$", s)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31:
        return f"{m.group(1)} {MONTHS[int(m.group(2)) - 1]} {int(m.group(3)):02d}"
    m = re.match(rf"^(\d{{1,2}})[-/.](\d{{1,2}})[-/.]{y}$", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 and 1 <= b <= 12:
            return f"{m.group(3)} {MONTHS[b - 1]} {a:02d}"
        if b > 12 and 1 <= a <= 12:
            return f"{m.group(3)} {MONTHS[a - 1]} {b:02d}"
    return None


# --- template fixes ------------------------------------------------------------------------

@dataclass
class Fixed:
    text: str
    kind: str
    url: str = ""
    accessed: str | None = None      # normalised date, if the author gave one we could read
    access_raw: str = ""             # the author's access phrase, when it could not be read
    title: str = ""
    notes: list[str] = field(default_factory=list)


def _tidy(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s+([,;:.])", r"\1", s)
    s = re.sub(r"(?<!\.)\.\s*\.(?!\.)", ".", s)   # "..": one full stop (keeps "...")
    s = re.sub(r",\s*\.", ".", s)
    s = re.sub(r";\s*\.", ".", s)
    s = re.sub(r"(?<=[a-z]{3})\.(?=[A-Z][a-z])", ". ", s)
    s = re.sub(r"([.?!][”\"’'])\.", r"\1", s)  # '..."Title."' + '.'
    s = re.sub(r"([”\"’'])\.\.", r"\1.", s)  # "Ireland.London" -> "Ireland. London"
    return s.strip()


def _edition(m: re.Match) -> str:
    n = int(m.group("num") or m.group("num2") or 0) or _ORDINALS[m.group("word").lower()]
    return f"{ordinal(n)} ed."


def fix_edition(body: str) -> str:
    return re.sub(r"\bed\.\.(?!\.)", "ed.", _EDITION.sub(_edition, body))


def fix_editors(body: str) -> str:
    """In: Cotran RS, Kumar V (eds). -> In: Cotran RS, Kumar V, editors."""
    body = re.sub(r"\s*\((eds?|editors?)\.?\)\s*[.,:]?", lambda e: ", editor." if e.group(1).lower() in ("ed", "editor")
                  else ", editors.", body, flags=re.I)
    m = re.search(r"\bIn\s*:\s*", body)
    if not m:
        return body
    head, rest = body[:m.start()], body[m.end():]
    rest = re.sub(r"(?<=[A-Z]),?\s+eds\.?(?=\s)", ", editors.", rest, count=1)
    return f"{head}In: {rest}"


def fix_publication(body: str, pages: bool = True) -> str:
    """City: Publisher; Year. p. xx-xx (punctuation only; nothing is added that the author did not give)."""
    years = list(re.finditer(r"(?<![\d-])(?:19|20)\d\d(?![\d-])", body))
    if not years:
        return body
    y = years[-1]
    before, year, after = body[:y.start()], y.group(0), body[y.end():]
    # "2023 Mar." / "2026 Jan-" / "2020 Dec 8" as the publication date: the year only.
    after = re.sub(rf"^\s+{_MONTH}(?:\s+\d{{1,2}})?\s*-?(?=\s*(?:[.;,\[]|$))", "", after, flags=re.I)
    after = re.sub(r"^\s*-(?=\s*(?:[.;\[]|$))", "", after)
    pub = _PUBLISHER.search(before)
    if pub and re.fullmatch(r"\s*(?:[.,;:]?\s*(?:pp?\.?\s*)?[A-Za-z]?\d+(?:\s*[-–]\s*[A-Za-z]?\d+)?)?\s*\.?\s*", after):
        # "New York : Oxford University Press, 2001" -> "New York: Oxford University Press; 2001"
        before = before[:pub.start("city")] + f"{pub.group('city')}: {pub.group('pub')}; "
    # Pages after the year: ", p. 264-274" / ":264-74" / "; pp 291-316" -> ". p. 264-74"
    m = re.match(r"^\s*[.,;:]?\s*(pp?\.?\s*)?([A-Za-z]?\d+(\s*[-–]\s*[A-Za-z]?\d+)?)\s*\.?\s*$", after)
    if m and pages and (m.group(1) or m.group(3)):
        dash = re.search(r"[-–]", m.group(2))  # keep the author's dash; the editors do
        pg = refengine.format_pages(m.group(2), _PageStyle(dash.group(0) if dash else "-"))
        after = f". p. {pg}"
    return before + year + after


_PUBLISHER = re.compile(
    r"(?:^|(?<=[.?!]\s))(?P<city>[A-Z][A-Za-z .'()-]{1,35}?(?:,\s*[A-Z][A-Za-z.]{1,15})?)\s*:\s*"
    r"(?P<pub>[A-Z][^:;?]{1,80}?)\s*(?:[,.;]\s*|;)$")


class _PageStyle:
    def __init__(self, dash: str = "-"):
        self.dash = dash

    def get(self, key, default=None):
        return self.dash if key == "references.page_range_dash" else default


def _split_access(text: str) -> tuple[str, str | None, str]:
    """Remove access-date phrases. Returns (text, normalised date or None, unreadable raw phrase)."""
    date, raw, cuts = None, "", []
    for k in _ACCESS.finditer(text):
        tail = text[k.end():k.end() + 45]
        for i in range(len(tail), 3, -1):  # the longest stretch that reads as a date
            if i < len(tail) and not re.match(r"[\s.,;)\]\[]", tail[i]):
                continue
            d = access_date(tail[:i].rstrip(" .,;"))
            if d:
                end = k.end() + len(tail[:i].rstrip(" .,;"))
                if k.group("open"):
                    end += len(re.match(r"\s*[)\]]?", text[end:]).group(0))
                cuts.append((k.start(), end))
                date = d
                break
        else:
            if k.group("open") or k.group("word").lower() in ("cited", "accessed", "assessed"):
                raw = text[k.start():k.end() + len(re.match(r"[^\[\]();]*?(?=[\]);]|\.\s|$)", text[k.end():]).group(0))]
    for start, end in reversed(cuts):  # an unreadable date stays where the author wrote it
        text = text[:start] + text[end:]
    return text, date, raw


_SMALL = {"a", "an", "the", "and", "but", "or", "nor", "for", "so", "yet", "of", "in", "on", "at", "to", "by", "up",
          "as", "via", "vs", "with", "from", "into", "over", "per", "versus", "than", "et", "al"}


def title_case(title: str) -> str:
    """Capitalise the main words of a book title ("Insomnia: Psychological Assessment and Management");
    nothing is lower-cased, so acronyms and the author's capitals stay."""
    out, first = [], True
    for w in re.split(r"(\s+)", title):
        if w.strip():
            parts = w.split("-")
            for i, part in enumerate(parts):
                m = re.match(r"^([(\[\"“‘']?)([a-z])([a-z']*)(.*)$", part)
                if m and (first or i or m.group(2) + m.group(3) not in _SMALL) and not re.search(r"[A-Z0-9]", m.group(3)):
                    parts[i] = m.group(1) + m.group(2).upper() + m.group(3) + m.group(4)
            first = w.endswith((":", "?", "!", "."))
            w = "-".join(parts)
        out.append(w)
    return "".join(out)


def book_title_case(body: str, title: str = "") -> str:
    """Title case for the book's title: the title of a whole book, or the book after 'In: ..., editors.'."""
    m = re.search(r"\bIn: .*?,\s*editors?\.\s*(.+?)(?=\.\s|$)", body)
    if m:
        span = m.span(1)
    elif "In:" in body:
        return body
    else:
        a = refengine.AUTHORS_END.search(body)
        start = a.end() if a else 0
        if title and title in body:
            start = body.index(title)
        t = re.match(r"(.+?)(?=\.\s|$)", body[start:])
        after = body[start + t.end():] if t else ""
        if not t or not re.search(r"[A-Z][\w .()]*:\s*[A-Z]|\b\d+(?:st|nd|rd|th) ed\b", after) \
                or re.search(r"\bjournal\b|\bdoi\b", body, re.I):
            return body  # no edition or publisher after it: not sure this is the book title
        span = (start, start + t.end())
    return body[:span[0]] + title_case(body[span[0]:span[1]]) + body[span[1]:]


def template(text: str, kind: str, title: str = "") -> Fixed:
    """Template fixes for a non-journal reference, from the author's own data only."""
    kind = classify(text, kind)
    f = Fixed(text=text, kind=kind)
    body = _NOTE.sub("", text)
    body, f.accessed, f.access_raw = _split_access(body)
    if f.access_raw:
        f.notes.append("access date unreadable")
    body = re.sub(r"\[([A-Z][\w ]{2,30})\]:\s", r"\1: ", body)  # "[Indonesia]: Publisher" -> "Indonesia: ..."
    urls = [u for u in URL.finditer(body)  # a DOI written as a link is not a web page reference
            if not re.search(r"doi\.org/", u.group(0), re.I) and not re.search(r"doi\s*:?\s*$", body[:u.start()], re.I)]
    urls = urls or list(_BARE_SITE.finditer(body))
    m = next((u for u in urls if _URL_PREFIX.search(body[:u.start()])), urls[0] if urls else None)
    rest = ""
    if m:
        f.url = m.group(0).rstrip(".,;:")
        rest = body[m.start() + len(f.url):]
        body = body[:m.start()]
        pre = _URL_PREFIX.search(body)
        if pre:
            body = body[:pre.start()]
        rest = _tidy(rest).lstrip(".").strip()
    body = _tidy(body).rstrip(" .,;:")
    if kind in (BOOK, CHAPTER, REPORT, THESIS, CONFERENCE, WEBSITE):
        body = fix_edition(body) if kind in (BOOK, CHAPTER) else body
        body = fix_editors(body) if kind in (BOOK, CHAPTER, REPORT) else body
        if kind in (BOOK, CHAPTER, REPORT, WEBSITE, THESIS):
            body = fix_publication(body, pages=kind in (BOOK, CHAPTER))
        if kind in (BOOK, CHAPTER):
            body = book_title_case(body, title)
        body = _tidy(body).rstrip(" ,;:")
    if f.url and not body and rest:
        # A URL at the start with the description after it: keep the author's order.
        out = f"{f.url}{'' if rest.startswith(';') else ' '}{rest}".rstrip(" .") + "."
    elif f.url:
        out = (body + (". " if body and not body.endswith((".", "?", "!")) else " " if body else "")
               + f"Available from: {f.url}" + (f" {rest}" if rest else "")).rstrip(" .") + "."
    else:
        out = body + ("" if body.endswith((".", "?", "!")) else ".")
    if f.accessed:
        out += f" [Last accessed on {f.accessed}]."
    f.text = _tidy(out) if out.strip(".") else text
    return f


# --- links -----------------------------------------------------------------------------------

@dataclass
class LinkCheck:
    url: str
    status: int | None = None
    final_url: str = ""
    title: str = ""
    content_type: str = ""
    error: str = ""

    @property
    def verdict(self) -> str:
        """ok, dead, redirected (to another page; often a soft 404) or unknown (blocked, timed out)."""
        if self.error:
            # 502 from the proxy / a failed name lookup: the host does not exist. Certificate problems
            # and time-outs are left to the editor (browsers often open such pages).
            return "dead" if re.search(r"\b502\b|name or service|nodename|getaddrinfo|no address|unknown host",
                                       self.error, re.I) else "unknown"
        if self.status in (401, 403, 405, 406, 429) or (self.status or 0) >= 500 or self.status is None:
            return "unknown"
        if self.status >= 400:
            return "dead"
        if self.title and _CHALLENGE.search(self.title):
            return "unknown"
        if self.title and _SOFT_404.search(self.title):
            return "dead"
        if self.moved_to_other_page:
            return "redirected"
        return "ok"

    @property
    def moved_to_other_page(self) -> bool:
        a, b = urllib.parse.urlparse(self.url), urllib.parse.urlparse(self.final_url or self.url)
        host = lambda u: u.netloc.lower().split(":")[0].removeprefix("www.")  # noqa: E731
        path = lambda u: u.path.rstrip("/").lower()  # noqa: E731
        if host(a) == host(b) and path(a) == path(b):
            return False  # http -> https, www, trailing slash, port
        if path(a) and not path(b):
            return True  # a deep link landing on a home page: the page has gone
        return host(a) != host(b) and not host(b).endswith("." + host(a)) and not host(a).endswith("." + host(b))


def _fetch_link(url: str) -> dict | None:
    """Open the link (redirects followed) and read its title. None: the request could not be made."""
    full = url if re.match(r"https?://", url, re.I) else "http://" + url
    req = urllib.request.Request(full, headers={
        "User-Agent": "Mozilla/5.0 (compatible; edit-os/0.1; reference checking)",
        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8"})
    out = {"url": url}
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
                out.update(status=r.status, final_url=r.geturl(), content_type=r.headers.get("content-type", ""))
                head = r.read(300_000).decode("utf-8", errors="replace") if "html" in out["content_type"] else ""
            m = re.search(r"<title[^>]*>(.*?)</title>", head, re.S | re.I)
            out["title"] = _unescape(" ".join(m.group(1).split()))[:300] if m else ""
            return out
        except urllib.error.HTTPError as e:
            out.update(status=e.code, final_url=e.geturl() or full)
            return out
        except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError, ValueError) as e:
            out["error"] = str(getattr(e, "reason", e))
            if "502" in out["error"] or attempt:
                return out
            time.sleep(2)
    return out


def _unescape(s: str) -> str:
    import html
    return html.unescape(s)


def check_link(url: str) -> LinkCheck:
    """Link check, cached with the other HTTP responses (biblio's cache); transient failures are not cached."""
    con = biblio.http().con
    key = "link:" + hashlib.sha256(url.encode()).hexdigest()
    row = con.execute("SELECT body FROM cache WHERE key = ?", (key,)).fetchone()
    data = json.loads(row[0]) if row else _fetch_link(url)
    if data is None:
        return LinkCheck(url, error="not checked")
    lc = LinkCheck(url, data.get("status"), data.get("final_url", ""), data.get("title", ""),
                   data.get("content_type", ""), data.get("error", ""))
    if not row and lc.verdict != "unknown":
        con.execute("INSERT OR REPLACE INTO cache VALUES (?, ?)", (key, json.dumps(data)))
        con.commit()
    return lc


def title_match(cited: str, page_title: str) -> float:
    if not cited or not page_title:
        return 0.0
    a, b = refengine.fold(cited), refengine.fold(page_title)
    return max(fuzz.token_set_ratio(a, b), fuzz.partial_ratio(a, b) if min(len(a), len(b)) >= 15 else 0)


# --- books -----------------------------------------------------------------------------------

@dataclass
class BookCheck:
    verified: bool = False
    source: str = ""
    title: str = ""
    years: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)  # fields the record confirms


def book_parts(text: str, title: str = "") -> dict:
    """Book title, editors' surname, edition, city, publisher, year from a (templated) book entry."""
    out = {"chapter": "", "book": "", "editor": "", "edition": "", "city": "", "publisher": "", "year": ""}
    m = re.search(r"\bIn:\s*(.+)", text)
    if m:
        out["chapter"] = title
        rest = m.group(1)
        e = re.match(r"(.+?),\s*editors?\.\s*(.+)", rest)
        if e:
            out["editor"] = e.group(1).split(",")[0].split()[0] if e.group(1).split() else ""
            rest = e.group(2)
        out["book"] = re.split(r"\.\s+(?=[A-Z0-9])", rest, maxsplit=1)[0].strip()
    else:
        out["book"] = title
    if m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th) ed\b", text):
        out["edition"] = m.group(1)
    if m := re.search(r"(?:^|[.?]\s)([A-Z][^.:;]{1,40}(?:\([A-Z]{2}\))?):\s*([^:;]{2,90});\s*((?:19|20)\d\d)\b", text):
        out["city"], out["publisher"], out["year"] = m.group(1).strip(), m.group(2).strip(), m.group(3)
    elif m := re.search(r"([^.:;]{2,90});\s*((?:19|20)\d\d)\b", text):
        out["publisher"], out["year"] = m.group(1).strip(), m.group(2)
    return out


def _similar(a: str, b: str) -> float:
    a, b = refengine.fold(a), refengine.fold(b)
    if not a or not b:
        return 0.0
    s = fuzz.token_sort_ratio(a, b)
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 20 and long_.startswith(short):  # "Syndromes of the head and neck" vs "...: a guide"
        s = max(s, 95)
    return s


def _surname_in(surname: str, names: list[str]) -> bool:
    s = refengine.fold(surname)
    return bool(s) and any(s in refengine.fold(n).split() for n in names)


def verify_book(title: str, surnames: list[str], year: str, chapter: str = "", text: str = "") -> BookCheck:
    """Find the book in CrossRef (book / chapter records) or Open Library: title and an author or
    editor surname must agree. Years, publishers and places of the matched record are returned so
    the caller can confirm the author's data; nothing is copied into the entry."""
    out = BookCheck()
    if not title or not surnames:
        return out
    q = " ".join(x for x in (chapter, title, surnames[0], year) if x)
    url = biblio.CROSSREF + "?" + urllib.parse.urlencode({
        "query.bibliographic": q[:300], "rows": 5,
        "filter": "type:book,type:book-chapter,type:monograph,type:edited-book,type:reference-book,type:book-set"})
    body = biblio.http().get(url, 0.1)
    try:
        items = json.loads(body)["message"]["items"] if body else []
    except (ValueError, KeyError):
        items = []
    for it in items:
        names = [a.get("family") or a.get("name", "") for a in it.get("author", []) + it.get("editor", [])]
        t = (it.get("title") or [""])[0]
        container = (it.get("container-title") or [""])[0]
        title_ok = _similar(title, t) >= 90 or (chapter and _similar(chapter, t) >= 90 and _similar(title, container) >= 85)
        if title_ok and any(_surname_in(s, names) for s in surnames):
            parts = (it.get("issued", {}).get("date-parts") or [[None]])[0]
            out = BookCheck(True, "crossref", t, [str(parts[0])] if parts and parts[0] else [],
                            [it.get("publisher", "")], [it.get("publisher-location", "")] if it.get("publisher-location") else [])
            break
    main, _, sub = title.partition(":")
    # Open Library matches its "title" field, which holds the main title only: "Self-efficacy: The exercise
    # of control" is found as "Self-efficacy". Then the main title, the subtitle when the record has one,
    # an author and the year must all agree.
    for query in [title] + ([main.strip()] if sub.strip() and len(main.strip()) >= 5 else []):
        if out.verified:
            break
        url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode({
            "title": query[:200], "author": surnames[0], "limit": 5,
            "fields": "title,subtitle,author_name,publisher,publish_place,publish_year,isbn"})
        body = biblio.http().get(url, 0.5)
        try:
            docs = json.loads(body)["docs"] if body else []
        except (ValueError, KeyError):
            docs = []
        for d in docs:
            full = d.get("title", "") + (": " + d["subtitle"] if d.get("subtitle") else "")
            years = [str(y) for y in d.get("publish_year", [])]
            if not any(_surname_in(s, d.get("author_name", [])) for s in surnames):
                continue
            whole = max(_similar(title, d.get("title", "")), _similar(title, full)) >= 90
            by_main = query != title and _similar(main, d.get("title", "")) >= 95 and bool(year) and year in years \
                and (_similar(sub, d["subtitle"]) >= 80 if d.get("subtitle") else _publisher_in(d.get("publisher", []), text))
            if whole or by_main:
                out = BookCheck(True, "openlibrary", full, years, d.get("publisher", []), d.get("publish_place", []))
                break
    if out.verified:
        out.confirmed = ["title", "author"]
        if year and year in out.years:
            out.confirmed.append("year")
    return out


def verify_paper(title: str, surnames: list[str], year: str) -> str:
    """A conference paper in CrossRef's proceedings: title, an author's surname and the year must all agree.
    Returns the record's title, or "" when not found."""
    if not title or not surnames or not year:
        return ""
    url = biblio.CROSSREF + "?" + urllib.parse.urlencode({
        "query.bibliographic": " ".join((title, surnames[0], year))[:300], "rows": 5,
        "filter": "type:proceedings-article"})
    body = biblio.http().get(url, 0.1)
    try:
        items = json.loads(body)["message"]["items"] if body else []
    except (ValueError, KeyError):
        items = []
    for it in items:
        r = biblio._crossref_record(it)
        names = [a for a, _ in r.authors]
        years = {y[:4] for y in [r.year] + r.other_years}
        if _similar(title, r.title) >= 90 and year in years and any(_surname_in(x, names) for x in surnames):
            return r.title
    return ""


def _publisher_in(publishers: list[str], text: str) -> bool:
    """One of the record's publishers is named in the entry ("W.H. Freeman", "Harvard University Press")."""
    words = set(refengine.fold(text).split())
    skip = {"press", "publishers", "publisher", "publishing", "inc", "ltd", "co", "company", "the", "and", "of", "books", "group"}
    for pub in publishers:
        core = [w for w in refengine.fold(pub).split() if w not in skip and len(w) > 1]
        if core and all(w in words for w in core):
            return True
    return False


# --- resolution ------------------------------------------------------------------------------

def verify_bookshelf(title: str, surnames: list[str], nbk: str = "") -> BookCheck:
    """NCBI Bookshelf chapters (StatPearls, GeneReviews, ...) are indexed in PubMed: find the chapter
    by its NBK accession or by title and author."""
    terms = [nbk] if nbk else []
    words = " ".join(w for w in re.sub(r"[^\w\s-]", " ", title).split() if len(w) > 2)
    if words and surnames:
        terms.append(f"({words}[ti]) AND {surnames[0]}[au]")
    for term in terms:
        ids = biblio.pubmed_search(term, retmax=3)
        body = biblio._ncbi("efetch.fcgi", db="pubmed", retmode="xml", id=",".join(ids)) if ids else None
        if not body:
            continue
        for doc in re.findall(r"<BookDocument>.*?</BookDocument>", body, re.S):
            t = re.sub(r"<[^>]+>", "", (re.search(r"<ArticleTitle[^>]*>(.*?)</ArticleTitle>", doc, re.S) or
                                        re.search(r"<BookTitle[^>]*>(.*?)</BookTitle>", doc, re.S)).group(1))
            t = re.sub(r"\s*\(Archived\)$", "", t.strip())
            names = re.findall(r"<LastName>(.*?)</LastName>", doc)
            acc = re.findall(r'IdType="bookaccession">(NBK\d+)<', doc)
            author = any(_surname_in(x, names) for x in surnames)
            if (_similar(title, t) >= 90 and (author or not names)) or (nbk and nbk in acc and (author or _similar(title, t) >= 80)):
                year = re.search(r"<PubDate><Year>(\d{4})", doc)
                return BookCheck(True, "pubmed bookshelf", t, [year.group(1)] if year else [],
                                 re.findall(r"<PublisherName>(.*?)</PublisherName>", doc),
                                 re.findall(r"<PublisherLocation>(.*?)</PublisherLocation>", doc), ["title", "author"])
    return BookCheck()


def _surnames(p: refengine.Parsed, parts: dict) -> list[str]:
    out = []
    for a in p.authors[:3]:
        words = [w for w in a.split() if not re.fullmatch(r"[A-Z]{1,4}\.?|[A-Z]\.([A-Z]\.)*", w)]
        if words:
            out.append(words[-1] if len(words) > 1 and "," not in a else words[0])
    if parts.get("editor"):
        out.append(parts["editor"])
    return out


def resolve(text: str, style: JournalStyle, p: refengine.Parsed) -> refengine.Resolution:
    cfg = lambda k, d=True: style.get(f"references.other.{k}", d)  # noqa: E731
    if not cfg("enabled"):
        return refengine.Resolution(refengine.NOT_JOURNAL, p, message="Book, website or other non-journal source")
    kind = classify(text, p.kind)
    fixed = template(text, kind, p.title)
    res = refengine.Resolution(refengine.NOT_JOURNAL, p)
    broken = not fixed.url and re.search(r"https?://\s|\bwww\.\s", text, re.I)
    if not broken and ((kind == OTHER and not fixed.url) or not (fixed.url or re.search(r"\b(?:19|20)\d\d\b", text))):
        res.message = "Not a recognisable reference type; left as written"
        return res
    if fixed.text != text:
        res.formatted = fixed.text
    verified, notes = False, []

    if fixed.url and cfg("check_links"):
        lc = check_link(fixed.url)
        v = lc.verdict
        tm = title_match(p.title, lc.title)
        notes.append(f"link {v} ({lc.status or lc.error})" + (f" -> {lc.final_url}" if lc.moved_to_other_page else "")
                     + (f", page title '{lc.title[:80]}' {tm:.0f}%" if lc.title else ""))
        if v == "dead":
            res.queries.append(("references.link_dead", {}))
        elif v == "redirected" and tm < 80:
            res.queries.append(("references.link_redirect", {"url": lc.final_url}))
        elif v == "ok" and (tm >= 80 or lc.content_type.startswith("application/pdf")):
            verified = True
    if broken:
        res.queries.append(("references.link_broken", {}))
    if fixed.url and not fixed.accessed and not fixed.access_raw:
        res.queries.append(("references.access_date", {}))

    if kind in (BOOK, CHAPTER):
        parts = book_parts(fixed.text, p.title)
        nbk = re.search(r"\bNBK\d+\b", text)
        if not verified and cfg("verify_books") and (nbk or re.search(r"StatPearls|GeneReviews|Bookshelf", text)):
            bc = verify_bookshelf(p.title, _surnames(p, parts), nbk.group(0) if nbk else "")
            if bc.verified:
                verified = True
                notes.append(f"chapter found in {bc.source}: {bc.title[:80]}")
        if not verified and cfg("verify_books"):
            bc = verify_book(parts["book"] or p.title, _surnames(p, parts), parts["year"] or p.year,
                             parts["chapter"] if kind == CHAPTER else "", text)
            if bc.verified:
                verified = True
                notes.append(f"book found in {bc.source}: {bc.title[:80]}; confirms {', '.join(bc.confirmed)}")
                if parts["year"] and bc.years and parts["year"] not in bc.years and not parts["edition"]:
                    res.queries.append(("references.book_year", {"years": ", ".join(sorted(set(bc.years))[:4])}))
        if not fixed.url and parts["year"]:
            if not parts["publisher"]:
                res.queries.append(("references.publisher_name", {}))
            elif not parts["city"]:
                res.queries.append(("references.publisher_location", {}))
        if not verified and not fixed.url:
            res.queries.append(("references.book_unverified", {}))
    elif kind == CONFERENCE and not verified and cfg("verify_books") and p.title:
        found = verify_paper(p.title, _surnames(p, {}), p.year)
        if found:
            verified = True
            notes.append(f"paper found in crossref: {found[:80]}; confirms title, author, year")
    if kind in (CONFERENCE, REPORT, THESIS) and not verified and cfg("query_unverified"):
        res.queries.append(("references.unverified", {}))

    res.status = OTHER_VERIFIED if verified else OTHER_FIXED if res.formatted else refengine.NOT_JOURNAL
    res.message = f"{kind}: " + ("; ".join(notes) or "not checked")
    return res
