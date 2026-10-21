"""Front matter: running head, manuscript type, title, author group, affiliations, correspondence,
history dates and the cite line, styled the way the pre-editing guide and the journal ask.

Where the inputs keep it: the title page is usually a separate file (FirstPage.docx, "Title Page.docx")
next to the manuscript; the submission system's XML holds the article type, the history dates and the
authors; some manuscripts also start with a title page of their own. The style editor rebuilds the
block at the top of the manuscript from all three, so most of it is new paragraphs.

How it works:
1. Claude Haiku splits the title-page text into fields (structured output) and names the proper
   nouns and abbreviations in the title. It never writes text that goes into the file: every name,
   affiliation part, address part and e-mail it returns is checked against the source text and
   dropped if it is not there (`verify`).
2. Code builds each paragraph from those fields and the XML: surnames for the running head,
   sentence or title case, initials, affiliation numbering and grouping, country, PIN format, dates.
3. The result is proposed, never applied: manuscript paragraphs that already hold front matter get
   `Change` records (text and tracked formatting); missing paragraphs are `NewParagraph` records,
   inserted as tracked paragraphs by `render_inserts`; what only the author can supply becomes an
   author query in the guide's wording.

Without an API key only the XML is used.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import io
import json
import os
import re
import sqlite3
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from . import ai_refs, queries
from .changes import Change, Layer, Source, Status
from .docmodel import Manuscript, Paragraph
from .style import JournalStyle, _merge

# Author queries, in the guide's wording.
queries.TEXT.update({
    "front.running_head": "Kindly provide running head.",
    "front.article_type": "Kindly provide article type.",
    "front.confirm_type": "Kindly confirm article type.",
    "front.full_name": "Kindly provide full name.",
    "front.country": "Kindly provide the country name for the affiliation.",
})

MODEL = os.environ.get("EDITOS_MODEL", "claude-haiku-5-5")
USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0}  # this process, for cost reporting
MISSING = "???"
_ids = count(1)

ROLES = ["running_title", "article_type", "title", "authors", "affiliation", "correspondence", "label",
         "other_front_matter", "body"]
_S = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "article_type": _S, "title": _S, "running_title": _S,
        "authors": {"type": "array", "items": {
            "type": "object",
            "properties": {"given_names": _S, "surname": _S, "prefix": _S,
                           "affiliations": {"type": "array", "items": {"type": "integer"}},
                           "corresponding": {"type": "boolean"}},
            "required": ["given_names", "surname", "prefix", "affiliations", "corresponding"],
            "additionalProperties": False}},
        "affiliations": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "department": _S, "institution": _S, "city": _S,
                           "state": _S, "country": _S},
            "required": ["id", "department", "institution", "city", "state", "country"],
            "additionalProperties": False}},
        "correspondence": {
            "type": "object",
            "properties": {"prefix": _S, "given_names": _S, "surname": _S,
                           "address_parts": {"type": "array", "items": _S},
                           "city": _S, "state": _S, "postal_code": _S, "country": _S, "email": _S},
            "required": ["prefix", "given_names", "surname", "address_parts", "city", "state", "postal_code",
                         "country", "email"],
            "additionalProperties": False},
        "proper_nouns": {"type": "array", "items": _S},
        "paragraphs": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": _S, "role": {"type": "string", "enum": ROLES}},
            "required": ["id", "role"], "additionalProperties": False}},
    },
    "required": ["article_type", "title", "running_title", "authors", "affiliations", "correspondence",
                 "proper_nouns", "paragraphs"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You split the title page of a medical journal manuscript into fields.
Paragraphs are given with ids: F = the separate title-page file, M = the start of the manuscript file.
Copy every value exactly as it is written in the text (same spelling and capitals). Never add, guess,
complete, translate or correct anything; use "" or [] when something is not there.
- article_type: the manuscript type if stated (e.g. "Original Article", "Case Report"), without its label.
- title: the article title, without a label such as "Title:". If an M paragraph holds the title, copy
  it from there (the manuscript's own title wins over the title-page file).
- running_title: the running title / short title / running head, without its label.
- authors: in author-list order. given_names: first and middle names or initials as written;
  surname: the family name (keep particles such as van, von, de, Al, El and compound surnames whole).
  Leave out titles (Dr, Prof, Mr), degrees (MBBS, MD, MS, PhD, DNB, ...), designations, markers and
  asterisks. prefix: "Dr" or "Prof" if written before the name, else "". affiliations: ids of this
  author's affiliations, from superscript markers or from the affiliation written with the author's
  name. corresponding: true if the author is marked as corresponding author.
- affiliations: each institutional affiliation once (id 1, 2, ...), as parts copied from the text:
  department (department, unit, division or service, without designations such as "Professor",
  "Senior Resident", "Consultant", "Head"), institution (hospital, college or university; may contain
  commas), city, state, country. Leave out postal codes, street addresses, e-mail and phone.
- correspondence: the corresponding author's details from the correspondence section (or from that
  author's own entry if there is no such section): prefix, given_names, surname, address_parts
  (department, institution, street, area ... in order, each as written, without city, state, postal code,
  country, phone, fax, e-mail or web address), city, state, postal_code, country, email.
- proper_nouns: words in the title and running title that keep capital letters in sentence case:
  proper nouns (people, places, eponyms such as "Valsalva"), abbreviations and acronyms ("OCT",
  "COVID-19"), brand names. Disease, organism, anatomy and method names are not proper nouns unless
  they contain a person's or place's name ("granular dystrophy", "cytomegalovirus" are not; "Valsalva
  maneuver" is). Words capitalised only because the title is written in Title Case are not proper
  nouns. Give each word in its correct capitalisation.
- paragraphs: the role of every M paragraph: running_title, article_type, title, authors, affiliation,
  correspondence, label (a heading or label with no content of its own, e.g. "Title page", "Authors:"),
  other_front_matter (word counts, funding, conflicts, key words, any other title-page content) or
  body (abstract or main text)."""


# --- sources -------------------------------------------------------------------------------

@dataclass
class Meta:
    """What the submission system's XML says (portal format or JATS)."""
    article_type: str = ""
    title: str = ""
    running_title: str = ""
    authors: list[dict] = field(default_factory=list)  # given, surname, prefix, email, corresponding, aff
    dates: dict = field(default_factory=dict)  # received / revised / accepted -> datetime

    def text(self) -> str:
        parts = [self.article_type, self.title, self.running_title]
        for a in self.authors:
            parts += [a.get("given", ""), a.get("surname", ""), a.get("email", "")]
            parts += list((a.get("aff") or {}).values())
        return "\n".join(p for p in parts if p)


@dataclass
class Sources:
    first_page: list[str] = field(default_factory=list)  # paragraphs of the title-page file
    meta: Meta = field(default_factory=Meta)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_paragraphs(data: bytes) -> list[str]:
    from .docmodel import paragraph_text
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))
    return [t for t in (paragraph_text(p).strip() for p in root.iter(_W + "p")) if t]


_FIRST_PAGE = re.compile(r"first\s*page|title[\s._-]*page|^fp\.docx$|^cover\s*page", re.I)


def sources_from_zip(path: str) -> Sources:
    """Title-page file and XML from a submission zip (original.zip)."""
    z = zipfile.ZipFile(path)
    src = Sources()
    names = z.namelist()
    fp = [n for n in names if n.lower().endswith(".docx") and _FIRST_PAGE.search(os.path.basename(n))]
    if fp:
        try:
            src.first_page = docx_paragraphs(z.read(sorted(fp, key=len)[0]))
        except (KeyError, zipfile.BadZipFile, etree.XMLSyntaxError):
            pass
    xml = [n for n in names if n.lower().endswith(".xml") and "/" not in n.strip("/")]
    if xml:
        src.meta = parse_xml(z.read(xml[0]))
    return src


def sources_from_files(first_page: str | None = None, xml: str | None = None) -> Sources:
    src = Sources()
    if first_page:
        with open(first_page, "rb") as f:
            src.first_page = docx_paragraphs(f.read())
    if xml:
        with open(xml, "rb") as f:
            src.meta = parse_xml(f.read())
    return src


def _date(s: str) -> datetime | None:
    s = (s or "").strip()
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _txt(el, path: str) -> str:
    x = el.find(path)
    return re.sub(r"\s+", " ", "".join(x.itertext())).strip() if x is not None else ""


def parse_xml(data: bytes) -> Meta:
    try:
        root = etree.fromstring(data, etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return Meta()
    if root is None:
        return Meta()
    m = Meta()
    if root.tag == "article":  # JATS
        m.article_type = _txt(root, ".//subj-group[@subj-group-type='Article Type']/subject") or root.get("article-type", "")
        m.title = _txt(root, ".//title-group/article-title")
        m.running_title = _txt(root, ".//alt-title[@alt-title-type='running-head']")
        affs = {a.get("id"): {"institution": _txt(a, "institution") or _txt(a, "."), "country": _txt(a, "country")}
                for a in root.iter("aff")}
        for c in root.iter("contrib"):
            if c.get("contrib-type") != "author":
                continue
            x = c.find("xref[@ref-type='aff']")
            m.authors.append(dict(given=_txt(c, "name/given-names"), surname=_txt(c, "name/surname").rstrip("."),
                                  prefix=_txt(c, "name/prefix"), email=_txt(c, "email"),
                                  corresponding=c.get("corresp") == "yes",
                                  aff=affs.get(x.get("rid")) if x is not None else None))
        for d in root.iter("date"):
            try:
                when = datetime(int(_txt(d, "year")), int(_txt(d, "month")), int(_txt(d, "day")))
            except ValueError:
                continue
            kind = {"received": "received", "rev-recd": "revised", "accepted": "accepted"}.get(d.get("date-type"))
            if kind and (kind != "revised" or when > m.dates.get(kind, datetime.min)):
                m.dates[kind] = when
        return m
    m.article_type = _txt(root, "articletype")
    m.title = _txt(root, "title")
    for a in root.iter("author"):
        m.authors.append(dict(
            given=" ".join(x for x in (_txt(a, "fname"), _txt(a, "mname")) if x), surname=_txt(a, "lname"),
            prefix=_txt(a, "title"), email=_txt(a, "email"), corresponding=_txt(a, "is_corresponding") == "Y",
            zip=_txt(a, "corresponding_zip") or _txt(a, "zip"),
            aff={k: _txt(a, t) for k, t in (("department", "department"), ("institution", "institute"),
                                            ("city", "city"), ("state", "state"), ("country", "country"))}))
    for key, tag in (("received", "submission_date"), ("revised", "revision_date"), ("accepted", "Accepted")):
        d = _date(_txt(root, tag))
        if d:
            m.dates[key] = d
    return m


# --- Claude --------------------------------------------------------------------------------

class _Cache:
    def __init__(self, path: str = ai_refs.CACHE_DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.con = sqlite3.connect(path, check_same_thread=False)  # shared by worker threads (serialized SQLite)
        self.con.execute("CREATE TABLE IF NOT EXISTS frontmatter (key TEXT PRIMARY KEY, body TEXT)")

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(f"{MODEL}\n{INSTRUCTIONS}\n{json.dumps(SCHEMA)}\n{text}".encode()).hexdigest()

    def get(self, text: str) -> dict | None:
        row = self.con.execute("SELECT body FROM frontmatter WHERE key = ?", (self.key(text),)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, text: str, fields: dict) -> None:
        self.con.execute("INSERT OR REPLACE INTO frontmatter VALUES (?, ?)", (self.key(text), json.dumps(fields)))
        self.con.commit()


_cache: _Cache | None = None


def _call(text: str) -> dict | None:
    import anthropic

    client = anthropic.Anthropic(
        api_key=ai_refs._api_key(),
        base_url=os.environ.get("EDITOS_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    )
    response = client.messages.create(
        model=MODEL, max_tokens=8000, system=INSTRUCTIONS,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": text}],
    )
    USAGE["calls"] += 1
    USAGE["input_tokens"] += response.usage.input_tokens
    USAGE["output_tokens"] += response.usage.output_tokens
    if response.stop_reason in ("refusal", "max_tokens"):
        return None
    try:
        return json.loads(next(b.text for b in response.content if b.type == "text"))
    except (StopIteration, ValueError):
        return None


def split(text: str) -> dict | None:
    """Claude's field split of the numbered title-page text; cached by text."""
    global _cache
    if not ai_refs.available():
        return None
    if _cache is None:
        _cache = _Cache()
    got = _cache.get(text)
    if got is None:
        got = _call(text)
        if got is not None:
            _cache.put(text, got)
    return got


# --- reading the inputs --------------------------------------------------------------------

_BODY_START = re.compile(r"^\s*(?:<H\d>)?\s*(abstract|introduction|background|summary|key\s*-?\s*words?|"
                         r"purpose|aims?|objectives?|context|dear (?:editor|sir)|sir,)\b", re.I)


_BACK_MATTER = re.compile(r"^\s*(?:<H\d>)?\s*(references?|bibliography|declaration|financial support|conflicts? of "
                          r"interest|acknowledge?ments?|figure|table|legends?)\b", re.I)
# Title-page labels the editors delete ("Title Page", "Running title:", "Authors:").
_LABEL = re.compile(r"^\s*(title\s*page|running\s*(?:title|head)|short\s*title|(?:list of\s+)?authors?(?:\s*names?)?|"
                    r"author(?:'s|s')?\s*(?:names?|details|information)|contributors?|affiliations?|"
                    r"department\(?s?\)?\s*(?:and|&)\s*institution\(?s?\)?|corresponding\s*author|"
                    r"(?:article\s*)?title|manuscript\s*type|type\s*of\s*(?:manuscript|article))\b", re.I)


def leading_paragraphs(ms: Manuscript, limit: int = 30) -> list[Paragraph]:
    """Manuscript paragraphs before the abstract / main text: where a title page inside the
    manuscript would be."""
    out = []
    for p in ms.paragraphs:
        if p.in_table or not p.text.strip():
            continue
        if _BODY_START.match(p.text) or _BACK_MATTER.match(p.text) or len(p.text) > 400 or len(out) >= limit:
            break
        out.append(p)
    return out


def prompt_text(src: Sources, lead: list[Paragraph]) -> tuple[str, dict[str, str]]:
    """The numbered text Claude sees, and M-id -> paragraph id."""
    lines, ids, size = [], {}, 0
    if src.first_page:
        lines.append("Title-page file:")
        for i, t in enumerate(src.first_page[:80], 1):
            size += len(t)
            if size > 12000:
                break
            lines.append(f"[F{i}] {t}")
    if lead:
        lines.append("\nStart of the manuscript file:")
        for i, p in enumerate(lead, 1):
            ids[f"M{i}"] = p.id
            lines.append(f"[M{i}] {p.text[:1500]}")
    return "\n".join(lines), ids


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").replace(" ", " ")
    s = re.sub(r"[‐-―−]", "-", s)
    return re.sub(r"[\s.,;:]+", "", s).casefold()


@dataclass
class Author:
    given: str
    surname: str
    affs: list[int] = field(default_factory=list)
    corresponding: bool = False
    prefix: str = ""


@dataclass
class Fields:
    article_type: str = ""
    title: str = ""
    running_title: str = ""
    authors: list[Author] = field(default_factory=list)
    affiliations: dict[int, dict] = field(default_factory=dict)
    correspondence: dict = field(default_factory=dict)
    proper_nouns: list[str] = field(default_factory=list)
    roles: dict[str, str] = field(default_factory=dict)  # manuscript para id -> role
    notes: list[str] = field(default_factory=list)  # for the editor: what was dropped and why


def verify(raw: dict, source: str, para_ids: dict[str, str]) -> Fields:
    """Keep only what is in the source text. Model output never reaches the file unchecked."""
    src = _norm(source)
    f = Fields()

    def ok(value: str, what: str) -> str:
        value = re.sub(r"\s+", " ", (value or "").strip())
        if not value:
            return ""
        if _norm(value) in src:
            return value
        f.notes.append(f"{what} {value!r} is not in the source text; left out")
        return ""

    f.article_type = ok(raw.get("article_type", ""), "article type")
    f.title = ok(raw.get("title", ""), "title")
    f.running_title = ok(raw.get("running_title", ""), "running title")
    for a in raw.get("authors", []):
        given, surname = ok(a.get("given_names", ""), "given name"), ok(a.get("surname", ""), "surname")
        if not surname:
            continue
        prefix = a.get("prefix", "") if re.fullmatch(r"(?i)dr|prof", a.get("prefix", "").strip(" .")) else ""
        f.authors.append(Author(given, surname, [i for i in a.get("affiliations", []) if isinstance(i, int)],
                                bool(a.get("corresponding")), ok(prefix.strip(" ."), "prefix")))
    for aff in raw.get("affiliations", []):
        parts = {k: ok(aff.get(k, ""), k) for k in ("department", "institution", "city", "state", "country")}
        parts["department"] = strip_designation(parts["department"])
        parts = {k: ("" if re.search(r"\d{5,}|@", v) else v) for k, v in parts.items()}
        if parts["department"] or parts["institution"]:
            f.affiliations[aff.get("id", len(f.affiliations) + 1)] = parts
    c = raw.get("correspondence") or {}
    corr = {k: ok(c.get(k, ""), f"correspondence {k}") for k in
            ("given_names", "surname", "city", "state", "postal_code", "country", "email")}
    corr["prefix"] = ok(c.get("prefix", "").strip(" ."), "prefix") \
        if re.fullmatch(r"(?i)dr|prof", c.get("prefix", "").strip(" .")) else ""
    corr["address_parts"] = [x for x in (ok(p, "address part") for p in c.get("address_parts", [])) if x]
    corr["address_parts"] = [p for p in corr["address_parts"] if not _CONTACT.search(p)]
    if any(v for v in corr.values()):
        f.correspondence = corr
    title_words = _norm(" ".join((raw.get("title", ""), raw.get("running_title", ""))))
    f.proper_nouns = [w for w in raw.get("proper_nouns", []) if w.strip() and _norm(w) in title_words]
    for item in raw.get("paragraphs", []):
        if item.get("id") in para_ids and item.get("role") in ROLES:
            f.roles[para_ids[item["id"]]] = item["role"]
    return f


_CONTACT = re.compile(r"@|https?://|www\.|\b(?:phone|ph|tel|telephone|mobile|mob|fax|cell)\b|\+\d|\d{5,}\s*\d{3,}", re.I)


def fields_from_meta(m: Meta) -> Fields:
    """Fallback without Claude: the XML's own fields."""
    f = Fields(article_type=m.article_type, title=m.title, running_title=m.running_title)
    keys: dict[tuple, int] = {}
    for a in m.authors:
        if not a.get("surname"):
            continue
        aff = {k: v for k, v in (a.get("aff") or {}).items() if v}
        ids = []
        if aff:
            k = tuple(sorted(aff.items()))
            if k not in keys:
                keys[k] = len(keys) + 1
                f.affiliations[keys[k]] = {x: aff.get(x, "") for x in ("department", "institution", "city", "state", "country")}
            ids = [keys[k]]
        f.authors.append(Author(a.get("given", ""), a["surname"], ids, a.get("corresponding", False),
                                a.get("prefix", "") if re.fullmatch(r"(?i)dr|prof", a.get("prefix", "").strip(" .")) else ""))
    corr = next((a for a in m.authors if a.get("corresponding")), None)
    if corr:
        f.correspondence = dict(prefix="", given_names=corr.get("given", ""), surname=corr.get("surname", ""),
                                address_parts=[], city="", state="", postal_code="", country="",
                                email=corr.get("email", ""))
    return f


# --- text rules ----------------------------------------------------------------------------

_MINOR = {"a", "an", "the", "and", "but", "or", "nor", "for", "so", "yet", "as", "at", "by", "in", "of", "on",
          "to", "up", "via", "vs", "with", "without", "from", "into", "than", "per", "over", "versus", "&",
          "et", "al"}
_BREAK = re.compile(r"[:?!]$|^[–—-]$")


def _mostly_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.6


def _keep_map(keep: list[str]) -> dict[str, str]:
    out = {}
    for phrase in keep:
        for w in re.findall(r"[\w'’\-]+", phrase):
            if any(c.isupper() for c in w):
                out[w.casefold()] = w
    return out


def recase(text: str, mode: str, keep: list[str], colon_capital: bool = True) -> str:
    """Sentence or title case, keeping proper nouns / abbreviations (`keep`) and words written with
    inner capitals or digits (OCT, KPro, COVID-19). The first word and the word after a colon,
    question mark or dash start with a capital."""
    if mode == "as_is":
        return text
    caps = _mostly_caps(text)
    known = _keep_map(keep)
    out, start, first = [], True, True
    for tok in re.split(r"(\s+)", text):
        if not tok or tok.isspace():
            out.append(tok)
            continue
        m = re.match(r"^([^\w]*)(.*?)([^\w]*)$", tok)
        lead, core, trail = m.groups()
        if core:
            pieces = re.split(r"([-\u2010\u2011])", core)
            parts, seps = pieces[::2], pieces[1::2]
            new = []
            for i, part in enumerate(parts):
                low = part.casefold()
                if low in known:
                    w = known[low]
                elif not caps and (any(c.isupper() for c in part[1:]) or any(c.isdigit() for c in part)):
                    w = part
                elif mode == "title" and (start and i == 0 or low not in _MINOR):
                    w = part[:1].upper() + part[1:].lower() if caps or part[:1].islower() else part
                else:
                    w = part.lower()
                if start and i == 0 and (first or colon_capital):
                    w = w[:1].upper() + w[1:]
                new.append(w)
            core = "".join(w + (seps[i] if i < len(seps) else "") for i, w in enumerate(new))
            start = first = False
        out.append(lead + core + trail)
        if _BREAK.search(tok.rstrip()):
            start = True
    return "".join(out)


def clean_title(text: str) -> str:
    t = re.sub(r"^\s*(?:article\s+)?title\s*[:\-–]\s*", "", text, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip().strip("“”\"'‘’").strip()
    t = re.sub(r"\s+([.,:;?])", r"\1", t)
    return _drop_end_period(t)


def _drop_end_period(t: str) -> str:
    """Period at the end goes, unless the last word is an abbreviation."""
    t = t.rstrip()
    if t.endswith(".") and not re.search(r"\b(?:[A-Z]\.){2,}$|\bet al\.$", t):
        t = t[:-1].rstrip()
    return t


_PARTICLES = {"van", "von", "de", "der", "den", "da", "di", "del", "della", "dos", "du", "la", "le", "ter", "ten"}


def _name_case(word: str, first: bool) -> str:
    """ALL CAPS or all lower names get capitals: "MEERA" -> "Meera", "rao" -> "Rao"."""
    if not (word.isupper() and len(word) > 1 or word.islower()):
        return word
    if not first and word.casefold() in _PARTICLES:
        return word.lower()
    return "-".join("'".join(x[:1].upper() + x[1:].lower() for x in part.split("'")) for part in word.split("-"))


def _is_initials(tok: str, all_caps_name: bool) -> bool:
    t = tok.replace(".", "")
    return bool(re.fullmatch(r"[A-Z]{1,3}", t)) and (not all_caps_name or len(t) == 1 or "." in tok)


def given_parts(given: str, all_caps: bool) -> list[tuple[str, bool]]:
    """Given names as (text, is_initial): "A.N." -> [("A", True), ("N", True)]."""
    out = []
    for tok in re.split(r"[\s]+", given.strip()):
        if not tok:
            continue
        if _is_initials(tok, all_caps):
            out += [(c, True) for c in tok.replace(".", "")]
        else:
            out.append((tok, False))
    return out


def format_name(a: Author, cfg: dict, abbreviate: bool | None = None) -> tuple[str, bool]:
    """'Given M. Surname' per the guide; returns (name, has_full_first_name)."""
    all_caps = (a.given + a.surname).isupper()
    parts = given_parts(a.given, all_caps)
    abbreviate = cfg.get("abbreviate_middle_names", False) if abbreviate is None else abbreviate
    dot = "." if cfg.get("initial_period", True) else ""
    words = []
    for i, (t, initial) in enumerate(parts):
        if initial or (i > 0 and abbreviate):
            words.append(t[0].upper() + dot)
        else:
            words.append(_name_case(t, i == 0))
    surname = " ".join(_name_case(w, False) for w in a.surname.split())
    full_first = bool(parts) and not parts[0][1]
    return " ".join(words + [surname]), full_first or not parts


def _initial_like(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]\.?", s.strip()))


def _expand(short: Author, full_names: list[Author]) -> Author | None:
    """The full form of a name written with an initial surname or fewer given names ("Meera R",
    "Meera K R"), from names written in full elsewhere in the sources."""
    g = short.given.split()
    for a in full_names:
        if not a.surname or not a.given:
            continue
        ag = a.given.split()
        if not g or g[0].casefold() != ag[0].casefold():
            continue
        if _norm(a.surname) == _norm(short.surname) and len(a.given) > len(short.given):
            return a
        if _initial_like(short.surname) and a.surname[:1].upper() == short.surname[:1].upper() and len(a.surname) > 1:
            return a
    return None


def cite_name(a: Author) -> str:
    all_caps = (a.given + a.surname).isupper()
    parts = given_parts(a.given, all_caps)
    initials = "".join(t[0].upper() for t, _ in parts)
    if parts and not parts[0][1] and re.fullmatch(r"\w+-[A-Z]\w*", parts[0][0]):  # "Wei-Ming" -> WM
        initials = initials[0] + parts[0][0].split("-")[1][0] + initials[1:]
    surname = " ".join(_name_case(w, False) for w in a.surname.split())
    return f"{surname} {initials}".strip()


_STATES_IN = {s.casefold() for s in (
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
    "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Orissa", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Tamilnadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal", "Delhi", "New Delhi",
    "Puducherry", "Pondicherry", "Chandigarh", "Jammu and Kashmir", "Ladakh")}


# Indian cities whose state the editors add when it is missing ("Pune" -> "Pune, Maharashtra").
_CITY_STATE_IN = {c.casefold(): st for st, cities in {
    "Maharashtra": "Mumbai Pune Nagpur Nashik Aurangabad Thane Navi_Mumbai Kolhapur Solapur Wardha Sangli Latur",
    "Karnataka": "Bengaluru Bangalore Mysuru Mysore Mangaluru Mangalore Manipal Hubballi Hubli Belagavi Belgaum Davangere",
    "Tamil Nadu": "Chennai Coimbatore Madurai Vellore Tiruchirappalli Trichy Salem Tirunelveli Puducherry_not Thanjavur",
    "Kerala": "Thiruvananthapuram Trivandrum Kochi Cochin Kozhikode Calicut Thrissur Kottayam Kollam Kannur Ernakulam",
    "Telangana": "Hyderabad Secunderabad Warangal",
    "Andhra Pradesh": "Visakhapatnam Vijayawada Guntur Tirupati Nellore Kurnool Kakinada",
    "West Bengal": "Kolkata Calcutta Siliguri Durgapur Howrah Barrackpore",
    "Uttar Pradesh": "Lucknow Kanpur Varanasi Agra Prayagraj Allahabad Noida Ghaziabad Meerut Gorakhpur Aligarh Bareilly Jhansi",
    "Gujarat": "Ahmedabad Surat Vadodara Rajkot Gandhinagar Bhavnagar Jamnagar",
    "Rajasthan": "Jaipur Jodhpur Udaipur Ajmer Bikaner Kota",
    "Madhya Pradesh": "Bhopal Indore Gwalior Jabalpur Ujjain",
    "Punjab": "Ludhiana Amritsar Jalandhar Patiala Mohali Bathinda",
    "Haryana": "Gurugram Gurgaon Faridabad Rohtak Ambala Panipat Hisar",
    "Bihar": "Patna Gaya Bhagalpur Muzaffarpur Darbhanga",
    "Odisha": "Bhubaneswar Cuttack Rourkela Berhampur Sambalpur Burla",
    "Uttarakhand": "Dehradun Rishikesh Haridwar Haldwani",
    "Assam": "Guwahati Dibrugarh Silchar Jorhat",
    "Jharkhand": "Ranchi Jamshedpur Dhanbad",
    "Chhattisgarh": "Raipur Bilaspur Bhilai",
    "Himachal Pradesh": "Shimla",
    "Jammu and Kashmir": "Srinagar Jammu",
    "Goa": "Panaji",
}.items() for c in cities.replace("_not", "").replace("_", " ").split()}
_CITY_STATE_IN.pop("puducherry", None)
_COUNTRY_CODES = {"IN": "India", "CN": "China", "US": "USA", "GB": "UK", "SA": "Saudi Arabia", "AE": "UAE",
                  "IR": "Iran", "IQ": "Iraq", "TR": "Turkey", "NP": "Nepal", "BD": "Bangladesh", "PK": "Pakistan",
                  "LK": "Sri Lanka", "EG": "Egypt", "NG": "Nigeria", "MY": "Malaysia", "SG": "Singapore",
                  "JP": "Japan", "KR": "South Korea", "AU": "Australia", "CA": "Canada", "DE": "Germany",
                  "FR": "France", "IT": "Italy", "NL": "Netherlands", "ES": "Spain", "BR": "Brazil", "TW": "Taiwan"}


def _country(parts: dict) -> str:
    c = (parts.get("country") or "").strip()
    if c:
        return _COUNTRY_CODES.get(c, c)
    if parts.get("state", "").casefold() in _STATES_IN or parts.get("city", "").casefold() in _STATES_IN \
            or (not parts.get("state") and parts.get("city", "").casefold() in _CITY_STATE_IN):
        return "India"
    return ""


def _state(parts: dict) -> str:
    """The state as written, or the one an Indian city is in."""
    if parts.get("state"):
        return parts["state"]
    if _country(parts).casefold() == "india":
        return _CITY_STATE_IN.get(parts.get("city", "").casefold(), "")
    return ""


_UNIT_WORDS = re.compile(r"(?i)\b(department|departments|division|unit|units|service|services|centre|center|institute|"
                         r"school|college|laborator(?:y|ies)|lab|clinic|section|faculty|program(?:me)?|hospital|"
                         r"university|foundation|cell|wing|ward)\b")


def department_form(d: str) -> str:
    """"Cornea Department" and a bare specialty ("ophthalmology") -> "Department of ..."."""
    d = d.strip()
    m = re.fullmatch(r"(?i)(.+?)\s+department", d)
    if m and not _UNIT_WORDS.search(m.group(1)):
        return "Department of " + m.group(1)
    if d and not _UNIT_WORDS.search(d) and len(d.split()) <= 5 and re.search(r"(?i)(?:logy|ics|ery|ine|ry|thesia|"
                                                                             r"thesiology|tics|ology)\b", d):
        return "Department of " + d
    return d


def _place_case(text: str) -> str:
    """"WUHAN" -> "Wuhan", "CHINA" -> "China"; short codes (USA, UK, WI) stay."""
    return " ".join(w.capitalize() if w.isupper() and len(w) > 3 else w for w in text.split())


def _cap_words(text: str) -> str:
    """Capitalise lowercase major words: "city eye hospital" -> "City Eye Hospital"."""
    words = re.split(r"(\s+)", re.sub(r"\s+", " ", text.strip()))
    out = []
    for i, w in enumerate(words):
        if w.isspace() or not w[:1].islower() or (i and (w in _MINOR or w in _PARTICLES or w in ("es", "el"))):
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return "".join(out)


_DESIGNATIONS = ("professor and head", "professor & head", "head of the department", "head of department",
                 "senior consultant", "consultant", "senior resident", "junior resident", "chief resident", "resident",
                 "associate professor", "assistant professor", "additional professor", "professor", "head", "hod",
                 "senior lecturer", "lecturer", "dean", "director", "medical superintendent", "superintendent",
                 "medical officer", "senior registrar", "registrar", "research fellow", "clinical fellow", "fellow",
                 "postgraduate student", "postgraduate resident", "postgraduate", "pg student", "pg resident",
                 "intern", "research scholar", "scientist-[a-g]", "scientist")
_DESIGNATION = re.compile(r"^(?:(?:%s)\b\.?(?:\s*[,;/]\s*|\s+(?=(?:of\s+(?:the\s+)?)?department\b)|\s+of\s+(?=(?:the\s+)?department\b)))+"
                          % "|".join(_DESIGNATIONS), re.I)
# A title directly before a speciality without a comma: "Consultant cornea and general ophthalmology".
_TITLE_WORD = re.compile(r"^(?:senior\s+|junior\s+)?(?:consultant|resident|lecturer)\s+(?!of\b)(?=\w)", re.I)


def strip_designation(department: str) -> str:
    """"Senior Resident, Department of X" -> "Department of X"; "Consultant cornea" -> "cornea"."""
    d = department.strip()
    for pattern in (_DESIGNATION, _TITLE_WORD):
        m = pattern.match(d)
        if m and m.end() < len(d):
            d = d[m.end():].lstrip(" ,;")
    d = re.sub(r"(?i)^of\s+(?=(?:the\s+)?department\b)", "", d)
    return d


def _clean_part(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip(" ,.;")
    t = re.sub(r"\s*&\s*", " and ", t)
    t = re.sub(r"(?i)^dept\.?(?:\s+of)?\s+", "Department of ", t)
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r",(?=\S)", ", ", t)
    return _cap_words(t)


# --- building the block --------------------------------------------------------------------

Run = tuple[str, dict]


@dataclass
class Block:
    role: str
    runs: list[Run]
    end: bool = False  # letters: authors to cite line go at the end, before the references

    @property
    def text(self) -> str:
        return "".join(t for t, _ in self.runs)


def _settings(style: JournalStyle) -> dict:
    cfg = dict(style.get("front_matter", {}) or {})
    over = (cfg.pop("journals", {}) or {}).get(style.code.lower(), {})
    return _merge(cfg, over)


def _imrad(ms: Manuscript) -> bool:
    heads = {re.sub(r"^<H\d>|[:.]$", "", p.text.strip()).strip().lower() for p in ms.paragraphs if len(p.text) < 60}
    need = [{"introduction"}, {"materials and methods", "methods", "patients and methods", "subjects and methods",
                               "methodology"}, {"results"}, {"discussion"}, {"conclusion", "conclusions"}]
    return all(heads & n for n in need)


def build(ms: Manuscript, f: Fields, meta: Meta, style: JournalStyle) -> tuple[list[Block], list[tuple[str, dict]]]:
    """The front-matter paragraphs in order, and the author queries (check, format args)."""
    cfg = _settings(style)
    blocks: list[Block] = []
    qs: list[tuple[str, dict]] = []
    B, I, SUP = {"bold": True}, {"italic": True}, {"superscript": True}
    plain: dict = {}

    # Manuscript type: the XML first (editors follow it), then the text, then IMRaD.
    first, second = (f.article_type, meta.article_type) if cfg.get("type_source") == "text" else (meta.article_type, f.article_type)
    atype = first or second
    if atype:
        atype = recase(re.sub(r"\s+", " ", atype).strip(" .:"), "title", [])
    elif _imrad(ms):
        atype = "Original Article"
        qs.append(("front.confirm_type", {}))
    else:
        qs.append(("front.article_type", {}))
    letter = bool(re.search(r"letter", atype or "", re.I))

    # Title.
    raw_title = clean_title(f.title or meta.title)
    keep = f.proper_nouns
    colon = cfg.get("capital_after_colon", True)
    title = recase(raw_title, cfg.get("title_case", "sentence"), keep, colon) if raw_title else ""

    # Authors.
    names = []
    for a in f.authors:
        name, full = format_name(a, cfg)
        names.append(name)
        if not full:
            qs.append(("front.full_name", {}))

    # Running head.
    surnames = [" ".join(_name_case(w, False) for w in a.surname.split()) for a in f.authors]
    short = f.running_title or meta.running_title
    short = re.sub(r"^\s*(?:running|short)\s*(?:title|head)\s*[:\-–]?\s*", "", short, flags=re.I)
    short = _drop_end_period(recase(re.sub(r"\s+", " ", short).strip(), "sentence", keep)) if short.strip() else ""
    rh: list[Run] = [("<RH>", plain)]
    if not surnames:
        rh.append((MISSING, plain))
    elif len(surnames) == 1:
        rh.append((surnames[0], plain))
    elif len(surnames) == 2:
        rh.append((f"{surnames[0]} and {surnames[1]}", plain))
    else:
        rh += [(f"{surnames[0]}, ", plain), ("et al.", I)]
    rh.append((": " + (short or MISSING), plain))
    if not short and not (letter and cfg.get("ltr_no_running_head_query")):
        qs.append(("front.running_head", {}))
    blocks.append(Block("running_head", rh))
    if atype:
        blocks.append(Block("article_type", [(atype, B)]))
    if title:
        blocks.append(Block("title", [(title, B)]))

    # Affiliations: unique, in order of use; numbered unless there is only one; grouped by institution.
    affs = f.affiliations
    order: list[int] = []
    for a in f.authors:
        for i in a.affs:
            if i in affs and i not in order:
                order.append(i)
    if not order and len(affs) == 1:
        order = list(affs)
        for a in f.authors:
            a.affs = order[:]
    key = {i: tuple(_norm(affs[i].get(k, "")) for k in ("department", "institution", "city", "state", "country"))
           for i in order}
    uniq: list[int] = []
    same: dict[int, int] = {}
    for i in order:
        hit = next((u for u in uniq if key[u] == key[i]), None)
        same[i] = hit if hit is not None else i
        if hit is None:
            uniq.append(i)
    one_each = all(len({same[i] for i in a.affs if i in same}) <= 1 for a in f.authors)
    number: dict[int, str] = {}
    if len(uniq) > 1:
        base = 0 if one_each and f.authors and f.authors[0].affs and cfg.get("affiliation_first_unnumbered", True) else 1
        number = {u: (str(n) if n else "") for n, u in enumerate(uniq, base)}

    if names:
        runs: list[Run] = []
        for k, (a, name) in enumerate(zip(f.authors, names)):
            if k:
                runs.append((", ", B))
            runs.append((name, B))
            marks = [number.get(same[i], "") for i in a.affs if i in same]
            marks = ",".join(dict.fromkeys(m for m in marks if m))
            if marks:
                runs.append((marks, {"bold": True, "superscript": True}))
        blocks.append(Block("authors", runs))

    if uniq:
        runs = []
        groups: list[list[int]] = []
        for u in uniq:  # departments of one institution and place go together
            d = department_form(affs[u].get("department", ""))
            loc = key[u][1:]
            g = next((g for g in groups if key[g[0]][1:] == loc and loc[0] and d.lower().startswith("department of ")
                      and department_form(affs[g[0]].get("department", "")).lower().startswith("department of ")),
                     None)
            if g is not None:
                g.append(u)
            else:
                groups.append([u])
        countries = [_country(affs[g[0]]) for g in groups]
        def may_share(k: int, c: str) -> bool:  # an affiliation without a country, printed with its neighbour's
            st = affs[groups[k][0]].get("state", "")
            return bool(c) and (not st or c.casefold() == "india" and st.casefold() in _STATES_IN)
        for k in list(range(1, len(countries))) + list(range(len(countries) - 2, -1, -1)):
            if not countries[k]:
                if k and may_share(k, countries[k - 1]):
                    countries[k] = countries[k - 1]
                elif k + 1 < len(countries) and may_share(k, countries[k + 1]):
                    countries[k] = countries[k + 1]
        for gi, g in enumerate(groups):
            if gi:
                runs.append((", ", plain))
            a0 = affs[g[0]]
            if len(g) > 1:
                runs.append(("Departments of ", plain))
                names = [_clean_part(re.sub(r"(?i)^department of ", "", department_form(affs[u]["department"])))
                         for u in g]
                last_join = ", " if any(" and " in n for n in names) else " and "
                for k, (u, name) in enumerate(zip(g, names)):
                    if k:
                        runs.append((last_join if k == len(g) - 1 else ", ", plain))
                    if number.get(u):
                        runs.append((number[u], SUP))
                    runs.append((name, plain))
                parts = []
            else:
                if number.get(g[0]):
                    runs.append((number[g[0]], SUP))
                parts = [department_form(a0.get("department", ""))]
            parts += [a0.get("institution", ""), _place_case(a0.get("city", "")), _place_case(_state(a0))]
            parts = [_clean_part(p) for p in parts if p and p.strip(" ,.")]
            # drop a city or state the institution already names at its end
            dedup = []
            for p in parts:
                if not any(_norm(q).endswith(_norm(p)) for q in dedup):
                    dedup.append(p)
            text = ", ".join(dedup)
            if len(g) > 1 and text:
                text = ", " + text
            last_of_country = gi == len(groups) - 1 or countries[gi + 1] != countries[gi]
            if countries[gi] and last_of_country:
                text += (", " if text else "") + _place_case(_clean_part(countries[gi]))
            elif not countries[gi] and last_of_country:
                qs.append(("front.country", {}))
            runs.append((text, plain))
        blocks.append(Block("affiliations", runs))

    # Correspondence.
    c = f.correspondence
    corr_author = next((a for a in f.authors if a.corresponding), None)
    if c or corr_author:
        given, surname = c.get("given_names", ""), c.get("surname", "")
        if not surname and corr_author:
            given, surname = corr_author.given, corr_author.surname
        full = _expand(Author(given, surname), f.authors) if surname else None
        if full is not None:  # "Author name(s) should be expanded"
            given, surname = full.given, full.surname
        name = format_name(Author(given, surname), cfg, abbreviate=False)[0] if surname else MISSING
        prefix = c.get("prefix") or (corr_author.prefix if corr_author else "")
        if not prefix and surname:
            xm = next((a for a in meta.authors if _norm(a.get("surname", "")) == _norm(surname)), None)
            if xm and re.fullmatch(r"(?i)dr|prof", xm.get("prefix", "").strip(" .")):
                prefix = xm["prefix"]
        prefix = {"dr": "Dr. ", "prof": "Prof. "}.get(prefix.strip(" .").lower(), "")
        blocks.append(Block("correspondence", [(cfg.get("correspondence_label", "Address for correspondence:"), B),
                                               (f" {prefix}{name},", plain)]))
        parts = [_clean_part(p) for p in c.get("address_parts", [])]
        city, pin = _place_case(_clean_part(c.get("city", ""))), re.sub(r"\s+", "", c.get("postal_code", ""))
        xm = next((a for a in meta.authors if surname and _norm(a.get("surname", "")) == _norm(surname)), None)
        if not pin and xm and re.fullmatch(r"\d{6}", re.sub(r"\s+", "", xm.get("zip", ""))):
            pin = re.sub(r"\s+", "", xm["zip"])
        country = _country({"country": c.get("country", ""), "state": c.get("state", ""), "city": c.get("city", "")})
        if not parts and corr_author:  # the author's own affiliation
            a = affs.get(corr_author.affs[0]) if corr_author.affs else None
            if a:
                parts = [_clean_part(a[k]) for k in ("department", "institution") if a.get(k)]
                city = city or _clean_part(a.get("city", ""))
                c = dict(c, state=c.get("state") or a.get("state", ""))
                country = country or _country(a)
        if city:
            if pin and country.casefold() == "india" and re.fullmatch(r"\d{6}", pin):
                city += " - " + (f"{pin[:3]} {pin[3:]}" if cfg.get("pin_spaced", True) else pin)
            elif pin:
                city += ", " + pin
            parts.append(city)
        state = _state({"state": c.get("state", ""), "city": c.get("city", ""), "country": country})
        if state:
            parts.append(_place_case(_clean_part(state)))
        if country:
            parts.append(_place_case(_clean_part(_COUNTRY_CODES.get(country, country))))
        dedup = []
        for p in parts:
            if p and not any(_norm(p) == _norm(q) for q in dedup):
                dedup.append(p)
        if dedup:
            blocks.append(Block("correspondence", [(", ".join(dedup) + ".", plain)]))
        email = c.get("email") or next((a.get("email", "") for a in meta.authors if a.get("corresponding")), "")
        if email:
            blocks.append(Block("correspondence", [(cfg.get("email_label", "E-mail:") + " ", plain), (email, plain)]))

    # History dates.
    dcfg = cfg.get("dates", {}) or {}
    labels = dcfg.get("labels", ["Received", "Revised", "Accepted"])
    if meta.dates:
        vals = [meta.dates.get(k) for k in dcfg.get("keys", ["received", "revised", "accepted"])]
        items = [[(f"{lab}:", B), (" " + (v.strftime(dcfg.get("format", "%d-%m-%Y")) if v else dcfg.get("missing", MISSING)), plain)]
                 for lab, v in zip(labels, vals)]
        if dcfg.get("one_line"):
            runs = []
            for k, it in enumerate(items):
                if k:
                    runs.append(("\t", plain))
                runs += it
            blocks.append(Block("dates", runs))
        else:
            blocks += [Block("dates", it) for it in items]

    # Cite line.
    if f.authors and title:
        journal = cfg.get("cite_journal", "")
        if cfg.get("cite_journal_case_reports") and re.search(r"case reports\)", atype or "", re.I):
            journal = cfg["cite_journal_case_reports"]
        cites = [cite_name(a) for a in f.authors]
        mx = cfg.get("cite_max_authors", 6)
        runs = [(cfg.get("cite_label", "How to cite this article:"), B), (" " + ", ".join(cites[:mx]), plain)]
        if len(cites) > mx:
            runs += [(", ", plain), ("et al", I)]
        cite_title = recase(raw_title, "sentence", keep, colon)
        runs.append((f". {cite_title.rstrip('.')}. {journal + ' ' if journal else ''}XX;XX:XX.", plain))
        blocks.append(Block("cite", runs))
    if letter and cfg.get("letters_at_end", True):
        for b in blocks:
            b.end = b.role not in ("running_head", "article_type", "title")
    return blocks, qs


# --- proposing the edits -------------------------------------------------------------------

@dataclass
class NewParagraph:
    """A paragraph to insert next to an existing one (tracked as an inserted paragraph)."""
    id: str
    anchor: str  # para_id
    where: str  # "before" | "after"
    runs: list[Run]
    role: str
    status: Status = Status.SUGGESTED
    source: Source = Source.RULE
    reason: str = "front_matter"

    @property
    def text(self) -> str:
        return "".join(t for t, _ in self.runs)

    def to_dict(self) -> dict:
        return dict(id=self.id, anchor=self.anchor, where=self.where, runs=[[t, f] for t, f in self.runs],
                    role=self.role, status=self.status.value, source=self.source.value, reason=self.reason)


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)
    inserts: list[NewParagraph] = field(default_factory=list)
    queries: list[queries.Query] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_queries(self, qs: queries.QuerySet, ms: Manuscript) -> queries.QuerySet:
        qs.queries += self.queries
        return qs.number(ms)


# Manuscript paragraph role -> the block role it holds.
_ROLE_TO_BLOCK = {"article_type": "article_type", "title": "title", "authors": "authors",
                  "affiliation": "affiliations", "correspondence": "correspondence"}
_DELETE_ROLES = {"running_title", "label"}


def _tokens_with_format(runs: list[Run]) -> list[tuple[str, dict]]:
    out = []
    for text, fmt in runs:
        for t in re.findall(r"\w+|\s+|[^\w\s]", text):
            out.append((t, fmt))
    return out


def _rewrite(p: Paragraph, runs: list[Run], layer: Layer) -> list[Change] | None:
    """Changes that turn paragraph `p` into `runs`: word-level text changes (inserted text carries
    its format) plus tracked formatting on kept text. None when that cannot be expressed without two
    insertions at one point (then the caller replaces the paragraph)."""
    from .rules_format import char_formats

    old = p.text
    a = re.findall(r"\w+|\s+|[^\w\s]", old)
    b = _tokens_with_format(runs)
    starts, pos = [], 0
    for t in a:
        starts.append(pos)
        pos += len(t)
    starts.append(pos)
    fmts = char_formats(p)
    out: list[Change] = []

    def mk(s, e, new, fmt=None):
        out.append(Change(id=f"fm{next(_ids)}", para_id=p.id, start=s, end=e, old=old[s:e], new=new, layer=layer,
                          source=Source.RULE, reason="front_matter", format=dict(fmt) if fmt else None))

    sm = difflib.SequenceMatcher(None, a, [t for t, _ in b], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):  # kept text: tracked formatting only
                want = b[j1 + k][1]
                s, e = starts[i1 + k], starts[i1 + k + 1]
                have = fmts[s] if s < len(fmts) else {}
                desired = {"bold": bool(want.get("bold")), "superscript": bool(want.get("superscript"))}
                if "italic" in want:
                    desired["italic"] = bool(want["italic"])
                delta = {prop: on for prop, on in desired.items() if bool(have.get(prop)) != on}
                if not delta:
                    continue
                if out and out[-1].format_only and out[-1].end == s and out[-1].format == delta:
                    s = out.pop().start
                mk(s, e, old[s:e], delta)
            continue
        segs: list[list] = []
        for t, f in b[j1:j2]:
            if segs and segs[-1][1] == f:
                segs[-1][0] += t
            else:
                segs.append([t, f])
        if not segs:
            mk(starts[i1], starts[i2], "")
            continue
        n_old = i2 - i1
        if len(segs) > 1 and n_old < len(segs):
            return None
        for k, (t, f) in enumerate(segs):
            lo = i1 + k if len(segs) > 1 else i1
            hi = lo + 1 if k < len(segs) - 1 else i2
            mk(starts[lo], starts[hi], t, f or None)
    return out


def plan(ms: Manuscript, style: JournalStyle, src: Sources | None = None) -> Plan:
    """Everything the front-matter step proposes for this manuscript."""
    cached = getattr(ms, "_front_plan", None)
    if cached is not None:
        return cached
    src = src or getattr(ms, "front_sources", None) or Sources()
    cfg = _settings(style)
    pl = Plan()
    if not cfg.get("enabled", True):
        return pl
    lead = leading_paragraphs(ms)
    text, para_ids = prompt_text(src, lead)
    raw = split(text) if cfg.get("ai_split", True) and text.strip() else None
    if raw is not None:
        source_text = "\n".join(src.first_page + [p.text for p in lead]) + "\n" + src.meta.text()
        f = verify(raw, source_text, para_ids)
        xml_names = [Author(a.get("given", ""), a.get("surname", "")) for a in src.meta.authors]
        for a in f.authors:  # "Meera K R": the submission XML has the surname in full
            full = _expand(a, xml_names) if _initial_like(a.surname) else None
            if full is not None:
                f.notes.append(f"surname {a.surname!r} expanded to {full.surname!r} from the submission XML")
                a.surname = full.surname
        if not f.authors and src.meta.authors:  # nothing usable from the text
            m = fields_from_meta(src.meta)
            f.authors, f.affiliations = m.authors, m.affiliations
            f.correspondence = f.correspondence or m.correspondence
    else:
        f = fields_from_meta(src.meta)
    pl.notes = f.notes
    if not (f.authors or f.title or src.meta.title):
        ms._front_plan = pl
        return pl
    blocks, qspecs = build(ms, f, src.meta, style)
    pl.blocks = blocks

    # Map existing manuscript paragraphs onto blocks; insert the rest in reading order.
    have: dict[str, list[Paragraph]] = {}
    front_roles = [p for p in lead if f.roles.get(p.id) in set(_ROLE_TO_BLOCK) | _DELETE_ROLES]
    for p in lead:
        role = f.roles.get(p.id)
        if role == "running_title" and front_roles and front_roles[0] is p:
            have.setdefault("running_head", []).append(p)  # a running head on top: rewrite it in place
        elif role in _ROLE_TO_BLOCK:
            have.setdefault(_ROLE_TO_BLOCK[role], []).append(p)
        elif role in _DELETE_ROLES and _LABEL.match(p.text) and (role == "running_title" or len(p.text) < 60):
            pl.changes.append(Change(id=f"fm{next(_ids)}", para_id=p.id, start=0, end=len(p.text), old=p.text,
                                     new="", layer=Layer.STRUCTURE, source=Source.RULE, reason="front_matter"))
    by_role: dict[str, list[Block]] = {}
    for b in blocks:
        by_role.setdefault(b.role, []).append(b)
    placed: dict[int, tuple[str, str]] = {}  # block index -> (para id, "in place")
    for role, paras in have.items():
        outs = [i for i, b in enumerate(blocks) if b.role == role]
        for k, p in enumerate(paras):
            if k < len(outs):
                ch = _rewrite(p, blocks[outs[k]].runs, Layer.HOUSE_STYLE)
                if ch is None:  # replace the paragraph instead
                    pl.changes.append(Change(id=f"fm{next(_ids)}", para_id=p.id, start=0, end=len(p.text), old=p.text,
                                             new="", layer=Layer.HOUSE_STYLE, source=Source.RULE, reason="front_matter"))
                    pl.inserts.append(NewParagraph(f"fi{next(_ids)}", p.id, "after", blocks[outs[k]].runs, role))
                else:
                    pl.changes += ch
                placed[outs[k]] = (p.id, "in place")
            else:  # more paragraphs than the block needs
                pl.changes.append(Change(id=f"fm{next(_ids)}", para_id=p.id, start=0, end=len(p.text), old=p.text,
                                         new="", layer=Layer.HOUSE_STYLE, source=Source.RULE, reason="front_matter"))
    first_any = next((p for p in ms.paragraphs if p.text.strip()), ms.paragraphs[0] if ms.paragraphs else None)
    first_placed = next((placed[i][0] for i in sorted(placed)), None)
    last: tuple[str, str] | None = None
    from .references import find_heading

    ref = find_heading(ms)
    end_anchor = (ms.paragraphs[ref].id, "before") if ref is not None else (
        (ms.paragraphs[-1].id, "after") if ms.paragraphs else None)
    for i, b in enumerate(blocks):
        if i in placed:
            last = (placed[i][0], "after")
            continue
        if b.end and end_anchor:
            pl.inserts.append(NewParagraph(f"fi{next(_ids)}", end_anchor[0], end_anchor[1], b.runs, b.role))
            continue
        if last is None:
            anchor = (first_placed or (first_any.id if first_any else None), "before")
        else:
            anchor = last
        if anchor[0] is None:
            continue
        pl.inserts.append(NewParagraph(f"fi{next(_ids)}", anchor[0], anchor[1], b.runs, b.role))
    anchor_q = first_placed or (first_any.id if first_any else None)
    if anchor_q:
        for check, fmt in qspecs:
            pl.queries.append(queries.Query(anchor_q, 0, 0, queries.TEXT[check].format(**fmt), check))
    ms._front_plan = pl
    return pl


def rule_front_matter(ms: Manuscript, style: JournalStyle) -> list[Change]:
    """In-place edits to front matter already in the manuscript (rules.RULES entry). The paragraphs
    to insert are in `plan(ms, style).inserts`."""
    if not style.get("front_matter.enabled", True):
        return []
    return list(plan(ms, style).changes)


# --- rendering inserted paragraphs ---------------------------------------------------------

def _run_el(text: str, fmt: dict, template_rpr):
    from .tracked import apply_format  # keeps the schema order of run properties
    r = OxmlElement("w:r")
    rpr = copy.deepcopy(template_rpr) if template_rpr is not None else OxmlElement("w:rPr")
    for tag in ("w:b", "w:bCs", "w:i", "w:iCs", "w:u", "w:vertAlign", "w:rPrChange", "w:highlight"):
        for el in rpr.findall(qn(tag)):
            rpr.remove(el)
    apply_format(rpr, {k: v for k, v in fmt.items() if v})
    if len(rpr):
        r.append(rpr)
    if text == "\t":
        r.append(OxmlElement("w:tab"))
    else:
        t = OxmlElement("w:t")
        t.text = text
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        r.append(t)
    return r


def render_inserts(ms: Manuscript, inserts: list[NewParagraph], tracked: bool = True, statuses=None,
                   author: str = "Edit OS") -> int:
    """Insert accepted NewParagraphs into the document; tracked as inserted paragraphs. Call after
    `tracked.render` (anchors are paragraph ids, which do not move). Returns how many were placed."""
    statuses = statuses or (Status.ACCEPTED,)
    date = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    ids = count(8000)
    last_after: dict[str, object] = {}
    placed = 0
    for np_ in inserts:
        if np_.status not in statuses:
            continue
        try:
            anchor = ms.by_id(np_.anchor).element
        except KeyError:
            continue
        template = next((r.find(qn("w:rPr")) for r in anchor.iter(qn("w:r")) if r.find(qn("w:rPr")) is not None), None)
        p = OxmlElement("w:p")
        ppr = OxmlElement("w:pPr")
        if tracked:
            prpr = OxmlElement("w:rPr")
            mark = OxmlElement("w:ins")
            for k, v in ((qn("w:id"), str(next(ids))), (qn("w:author"), author), (qn("w:date"), date)):
                mark.set(k, v)
            prpr.append(mark)
            ppr.append(prpr)
        p.append(ppr)
        for text, fmt in np_.runs:
            pieces = [x for x in re.split(r"(\t)", text) if x]
            for piece in pieces:
                r = _run_el(piece, fmt, template)
                if tracked:
                    w = OxmlElement("w:ins")
                    for k, v in ((qn("w:id"), str(next(ids))), (qn("w:author"), author), (qn("w:date"), date)):
                        w.set(k, v)
                    w.append(r)
                    p.append(w)
                else:
                    p.append(r)
        if np_.where == "before":
            anchor.addprevious(p)
        else:
            prev = last_after.get(np_.anchor, anchor)
            prev.addnext(p)
            last_after[np_.anchor] = p
        placed += 1
    return placed
