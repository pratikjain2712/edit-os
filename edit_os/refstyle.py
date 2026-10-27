"""Journal articles that PubMed / CrossRef could not verify, styled from the author's own text.

The owner's rule: "For references the base is styling. If verification is not possible we have to style it
as per the guide." Verification only adds or corrects facts; an entry that is not verified is still built in
the journal's reference style, by the same formatter as a verified one (`refengine.format_record`), from the
fields the author wrote:

- the entry is split deterministically (Vancouver "Surname II. Title. Journal. 2020;7(1):1-26", APA
  "Surname, I. I., & Surname, I. (2020). Title. Journal, 7(1), 1–26." and its Harvard variant, EndNote
  "Surname, I., I. Surname, and I. Surname, Title. Journal, 2020. 7(1): p. 1-26."); Claude's split helps only
  where its title is written in the entry (ai_refs.check_fields);
- the journal becomes NLM's abbreviation when NLM lists the name the author wrote (exact, after normalising),
  else stays the complete name (stylesheets: "Use complete name of the journal for non-indexed journals");
- nothing is invented: `invented` checks that every name, number and title word of the styled entry occurs
  in the author's entry (allowed additions: punctuation, "et al.", NLM's abbreviation of the author's journal,
  "Suppl"); an entry that fails, or whose split is uncertain, is left to the light cleanup (rules.py).

A missing field is never guessed: what is there is styled and the field is queried. The entry keeps its
"not verified" query (queries.py) and its provenance says it was styled from the author's text.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field

from . import biblio, journals, refengine
from .style import JournalStyle

PROVENANCE_LABEL = "Styled from the author's text (not verified)"

_PAGES = r"[A-Za-z]{0,2}\d+[A-Za-z]?(?:\s*[-–—]\s*[A-Za-z]{0,2}\d+[A-Za-z]?)?"
_YEAR = r"(?:19|20)\d{2}"
_MON = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|Spring|Summer|Autumn|Fall|Winter)\.?")
_DATE = rf"(?:\s+{_MON}(?:\s*[-–/]\s*{_MON})?(?:\s+\d{{1,2}})?)?"
_VOL = r"(?P<vol>\d+[A-Za-z]?(?:\s*\(?\s*Suppl(?:ement)?\.?\s*\d*\)?)?)"
_ISSUE = r"(?:\s*\((?P<issue>[^()]{1,30})\))?"

# Vancouver: "Journal. 2022 Jun 30;39(3):386–96", "Journal 2025 Dec;62.", "Journal. 2023;19:2242218"
CITE_V = re.compile(rf"(?P<year>{_YEAR}){_DATE}\s*;\s*{_VOL}{_ISSUE}(?:\s*:\s*(?P<pages>{_PAGES}))?(?=\s*[.,;]?\s*(?:$|\S))")
# APA / Harvard (year in the head): "Journal, 7(1), 1–26." / "Journal, 4(2)." / "Journal, 7(2), pp. 1-10"
CITE_APA = re.compile(rf",\s*{_VOL}{_ISSUE}\s*(?:[,:]\s*(?:pp?\.\s*)?(?P<pages>{_PAGES}))?\s*\.?\s*$")
# EndNote "Numbered": "Journal, 2020. 10(1): p. 59-66."
CITE_EN = re.compile(rf",\s*(?P<year>{_YEAR})\.\s*{_VOL}{_ISSUE}\s*(?::\s*(?:pp?\.\s*)?(?P<pages>{_PAGES}))?\s*\.?\s*$")
# What may follow the citation and is dropped: identifiers, epub notes, links.
_TRAILER = re.compile(
    r"^[\s.,;]*(?:(?:doi\s*:?\s*|https?://\S+|www\.\S+|10\.\d{4,9}/\S+|(?:PubMed\s+)?PMID\s*:?\s*\d+|PMCID\s*:?\s*PMC\d+"
    r"|\[?\s*Epub\s+(?:ahead\s+of\s+print)?[^\]]*\]?|\[?\s*Epub\b[^.\]]*\]?|Published\s+online[^.]*|Available\s+(?:from|at)\s*:?"
    r"|e-?collection[^.]*|\[(?:Internet|Online)\])[\s.,;]*)*$", re.I)
_ERRATUM = re.compile(r"\s*\b(?:Erratum|Corrigendum|Correction)\s+in\s*:", re.I)

# One Vancouver author: "Shields CL", "Park H-H", "van der Berg JA", "Smith J.K.", "Sk K"
_V_NAME = re.compile(
    r"\s*(?P<sur>(?:[a-z]{1,4}\s+){0,2}[^\W\d_][\w'’\-]*(?:\s+[^\W\d_][\w'’\-]*){0,2}?)\s+"
    r"(?P<ini>[A-Z](?:\.\s?-?[A-Z]){1,3}|[A-Z]{1,4}(?:-[A-Z]{1,2})?)(?P<sep>\.?\s*,\s*|\s+(?=et\.?\s*al\b)|\.\s+|\.?\s*$)")
_ET_AL = re.compile(r"\s*et\.?\s*al\.?\s*[,.]?\s*", re.I)
_GROUP = refengine._GROUP_WORDS


@dataclass
class Article:
    authors: list[str] = field(default_factory=list)  # "Surname Initials", or a group name
    et_al: bool = False
    authors_raw: str = ""   # the author block as written, when it could not be put in "Surname Initials"
    full_names: bool = False  # the author wrote the names in full ("Atoosa Bonyani"): put as "Bonyani A", queried
    title: str = ""
    journal: str = ""
    year: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""


# --- authors ---------------------------------------------------------------------------------------

def _initials(s: str) -> str:
    """"J.K." / "J. K." / "J-P" -> "JK" / "JK" / "J-P"."""
    s = re.sub(r"\.\s*-\s*|\s*-\s*", "-", s.strip())
    return re.sub(r"[.\s]", "", s).strip("-")


def vancouver_authors(head: str) -> tuple[list[str], bool, int] | None:
    """Names at the start of a Vancouver entry: (names, et al., where the title starts), or None."""
    names, pos, et_al = [], 0, False
    while pos < len(head):
        m = _ET_AL.match(head, pos)
        if m and names:
            return names, True, m.end()
        m = _V_NAME.match(head, pos)
        if not m or re.search(r"\d", m.group("sur")) or _bad_surname(m.group("sur")):
            break
        names.append(f"{m.group('sur').strip()} {_initials(m.group('ini'))}")
        pos = m.end()
        if not m.group("sep").strip() and _ET_AL.match(head, pos):  # "Herrera A et al."
            continue
        if "," not in m.group("sep"):
            return names, et_al, pos
    if not names:  # a group author: "World Health Organization. Title..."
        g = re.match(r"\s*([A-Z][^.,;:]{2,80}?)\.\s+(?=[A-Z0-9])", head)
        if g and _GROUP.search(g.group(1)) and not re.search(r"\d", g.group(1)):
            return [g.group(1).strip()], False, g.end()
    return None


_FULL_NAME = re.compile(r"\s*(?P<name>[A-Z][a-z'’]+(?:-[A-Z]?[a-z'’]+)?(?:\s+[A-Z][a-z'’]+(?:-[A-Z]?[a-z'’]+)?){1,2})"
                        r"(?P<sep>\s*,\s*|\.\s+)")


def full_name_authors(head: str) -> tuple[list[str], bool, int] | None:
    """Names written in full, given name first ("Atoosa Bonyani, Leila Safaeian, Mojtaba Chehrazi. Title"):
    (["Bonyani A", "Safaeian L", "Chehrazi M"], et al., where the title starts). Two names at least, each of
    two or three capitalised words; the last word is taken as the surname (Vancouver: "Surname Initials")."""
    names, pos = [], 0
    while True:
        e = _ET_AL.match(head, pos)
        if e and names:
            return (names, True, e.end()) if len(names) >= 2 else None
        m = _FULL_NAME.match(head, pos)
        if not m:
            return None
        words = m.group("name").split()
        if any(w.lower() in refengine._MINOR_WORDS for w in words) or _GROUP.search(m.group("name")):
            return None
        names.append(f"{words[-1]} {''.join(w[0] for w in words[:-1])}")
        pos = m.end()
        if "," not in m.group("sep"):
            return (names, False, pos) if len(names) >= 2 else None


def _bad_surname(sur: str) -> bool:
    words = sur.split()
    # "In", "The", "A": a title word, not a surname; every word lower case, or one letter ("M, K."): not a name
    return not any(w[:1].isupper() for w in words) or sur.strip().lower() in {"in", "the", "a", "an", "on", "of"} \
        or len(re.sub(r"[^\w]", "", sur)) < 2


def apa_authors(block: str) -> tuple[list[str], bool] | None:
    """"Mu, T., Wang, F., & Xu, Y." / "Smith, J. and Jones, K." / "Rahayu, W. R., et al." -> names, et al."""
    et_al = bool(re.search(r"\bet\.?\s*al\b", block))
    names = refengine.apa_authors(block)
    if not names:  # a one-name author first: "Karyanti, Atmoko, A., & Hitipieuw, I." (997)
        m = re.match(r"\s*(?P<mono>[A-Z][a-z][\w'’\-]+),\s*(?=[^\W\d_][\w'’\-]*(?:\s+[^\W\d_][\w'’\-]*)?,\s*[A-Z]\.)", block)
        rest = refengine.apa_authors(block[m.end():]) if m else []
        names = [m.group("mono")] + rest if rest else []
    return (names, et_al) if names else None


_EN_FIRST = re.compile(r"\s*(?P<sur>[^\W\d_][\w'’\-]*(?:\s+[^\W\d_][\w'’\-]*){0,2}?),\s*(?P<ini>(?:[A-Z]\.?\s*-?\s*){1,4})(?=,)")
_EN_NEXT = re.compile(r",\s*(?:(?:and|&)\s+)?(?P<ini>(?:[A-Z]\.\s*-?\s*){1,4})\s*(?P<sur>[^\W\d_][\w'’\-]*(?:\s+[^\W\d_][\w'’\-]*){0,2}?)(?=,)")


def endnote_authors(head: str) -> tuple[list[str], bool, int] | None:
    """"Delpasand, T., A. Behbood, and A. Faghihi, Title" / "Sadrollahi, A., et al., Title":
    (names, et al., where the title starts)."""
    m = _EN_FIRST.match(head)
    if not m or _bad_surname(m.group("sur")):
        return None
    names, pos = [f"{m.group('sur').strip()} {_initials(m.group('ini'))}"], m.end()
    while True:
        e = re.match(r",\s*et\.?\s*al\.?(?=,)", head[pos:])
        if e:
            pos += e.end()
            return names, True, pos + len(re.match(r",\s*", head[pos:]).group(0))
        n = _EN_NEXT.match(head, pos)
        if not n or _bad_surname(n.group("sur")):
            break
        names.append(f"{n.group('sur').strip()} {_initials(n.group('ini'))}")
        pos = n.end()
    rest = re.match(r",\s*", head[pos:])
    return (names, False, pos + rest.end()) if rest else None


# --- title and journal -------------------------------------------------------------------------------

def _looks_like_journal(name: str) -> bool:
    """A journal name: capitalised words (minor words aside), a few at most, no sentence inside."""
    words = re.findall(r"[^\W\d_][\w'’\-]*|\d+", name)
    if not words or len(words) > 14 or not (name[:1].isupper() or name[:1].isdigit()):
        return False
    minor = refengine._MINOR_WORDS | {"de", "la", "et", "y", "des", "du", "der", "und", "di", "e", "i"}
    lower = [w for w in words if w[:1].islower() and w.lower() not in minor]
    return len(lower) == 0 or (len(lower) == 1 and len(words) >= 6)


def _plain_name(name: str) -> bool:
    """After the author's own title: a short name with no sentence in it ("Journal of military medicine")."""
    return bool(name[:1].isupper()) and len(name.split()) <= 10 and not re.search(r"[.?!:;]\s", name)


def _directory_journal(name: str):
    d = journals.directory()
    return d.lookup(name) if d and name else None


def split_title_journal(rest: str, ai_title: str = "") -> tuple[str, str] | None:
    """`_split_title_journal`, held to Claude's title where it gave one written in the entry: a split whose
    title does not start as Claude's does is uncertain."""
    tj = _split_title_journal(rest, ai_title)
    if not tj or not re.match(r"[\w“\"‘'(\[]", tj[0]):
        return None
    if ai_title and not refengine.fold(tj[0]).startswith(refengine.fold(ai_title)[:25]):
        return None
    return tj


def _split_title_journal(rest: str, ai_title: str = "") -> tuple[str, str] | None:
    """"Title. Journal" -> (title, journal). The author's title (Claude's, when it is written at the start of
    `rest`), else the last sentence whose words name a journal NLM lists, else the last sentence that reads
    as a journal name (abbreviations with periods kept together: "J. Clin. Med.")."""
    rest = rest.strip().rstrip(".,; ")
    if ai_title:
        t = ai_title.strip().rstrip(".")
        if refengine.fold(rest).startswith(refengine.fold(t)) and len(refengine.fold(t)) >= 8:
            # the same words: find where they end in `rest`
            words = len(refengine.fold(t).split())
            ms = list(re.finditer(r"[^\W_]+", rest))
            folded = [refengine.fold(m.group(0)) for m in ms]
            k, n = 0, 0
            while k < len(ms) and n < words:
                n += len(folded[k].split()) or 1
                k += 1
            if 0 < k < len(ms):
                cut = ms[k - 1].end()
                tail = rest[cut:]
                sep = re.match(r"\s*[.?!]?[”\"’']?\s*[.]?\s*", tail)
                title, journal = rest[:cut] + re.match(r"[?!]?[”\"’']?", tail).group(0), tail[sep.end():]
                if journal and (_looks_like_journal(journal.rstrip(".")) or _plain_name(journal.rstrip("."))):
                    return title.strip(), journal.strip().rstrip(".")
    bounds = [m.end() for m in re.finditer(r"[.?!][”\"’']?\s+(?=[A-Z0-9(\[“\"'])", rest)]
    if not bounds:
        return None
    for b in bounds:  # furthest back first: "J. Orthop. Rehabil." over "Rehabil."
        cand = rest[b:].strip().rstrip(".")
        if len(cand) <= 150 and _directory_journal(cand):
            return rest[:b].strip(), cand
    k = len(bounds) - 1
    while k > 0:  # "J. Clin. Med.": one-word abbreviated pieces belong to the journal
        piece = rest[bounds[k - 1]:bounds[k]].strip()
        if re.fullmatch(r"[A-Z][a-z]{0,8}\.", piece):
            k -= 1
        else:
            break
    title, journal = rest[:bounds[k]].strip(), rest[bounds[k]:].strip().rstrip(".")
    if not _looks_like_journal(journal) or len(refengine.fold(title).split()) < 2:
        return None
    return title, journal


def journal_style_name(name: str, style: JournalStyle) -> tuple[str, str]:
    """(name for the entry, NLM abbreviation used or ""): NLM's abbreviation of the name the author wrote, else
    the author's complete name (periods dropped as in the abbreviations; a publisher acronym in brackets off)."""
    name = " ".join(re.sub(r"\s*\[(?:Internet|Online|serial online)\]", "", name, flags=re.I).split()).strip(" .,;")
    j = _directory_journal(name) if style.get("references.journal_abbreviation", "nlm") == "nlm" else None
    if j:
        abbr = refengine.NLM_FIXES.get(j.med_abbr, j.med_abbr)
        if style.get("references.journal_drop_qualifier", True):
            abbr = re.sub(r"\s*\([^)]*\)$", "", abbr)
        return abbr.replace(".", ""), abbr
    out = refengine._ACRONYM_QUALIFIER.sub("", name)
    if out.isupper() and len(out) > 6:
        out = " ".join(w.lower() if k and w.lower() in refengine._MINOR_WORDS else w.capitalize()
                       for k, w in enumerate(out.split()))
    out = re.sub(r"\s*:\s*$", "", out)
    return re.sub(r"\.(?=\s|$)", "", out).replace(". ", " ").strip(), ""


# --- splitting an entry ----------------------------------------------------------------------------------

def _strip_tail(text: str) -> str:
    """The entry up to its citation's end: errata notices and identifiers after it are cut."""
    m = _ERRATUM.search(text)
    return text[:m.start()] if m else text


def split(text: str, ai: dict | None = None) -> Article | None:
    """The author's fields of a journal-article entry, or None when the shape is not recognised."""
    body = _strip_tail(text).strip()
    ai_title = (ai or {}).get("title", "") or ""
    for shape in (_split_apa, _split_endnote, _split_vancouver):
        a = shape(body, ai_title, ai)
        if a is not None:
            return a
    return None


def _cut_trailer(body: str, end: int) -> bool:
    return bool(_TRAILER.match(body[end:]))


_APA_HEAD = re.compile(rf"^\s*(?P<authors>[^()]{{2,600}}?)\s*\((?P<year>{_YEAR})[a-z]?(?:,[^)]*)?\)[.,:]?\s*(?P<rest>.+)$", re.S)


def _split_apa(body: str, ai_title: str, ai) -> Article | None:
    m = _APA_HEAD.match(body)
    if not m:
        return None
    block = m.group("authors").strip()
    got = apa_authors(block)
    if got:
        names, et_al = got
    else:
        v = vancouver_authors(block + (". " if not block.endswith(".") else " "))
        if not v:
            return None
        names, et_al, _ = v
    rest = m.group("rest")
    # identifiers after the citation
    rest = _cut_identifiers(rest)
    c = CITE_APA.search(rest)
    a = Article(authors=names, et_al=et_al, year=m.group("year"))
    if c:
        a.volume, a.issue, a.pages = _vol(c.group("vol")), c.group("issue") or "", c.group("pages") or ""
        head = rest[:c.start()]
        if re.search(r"\b(?:19|20)\d{2}\b\s*;", head):
            return None
    else:
        head = rest.rstrip(" .")
        if re.search(r"\d", head[-12:]):  # numbers we cannot place
            return None
    tj = split_title_journal(head, ai_title)
    if not tj:
        return None
    a.title, a.journal = tj
    return a


def _cut_identifiers(s: str) -> str:
    """Drop links, DOIs, PMIDs and epub notes at the end of an entry."""
    m = re.search(r"[\s.,;]*(?:\bdoi\s*:|https?://|www\.|\b10\.\d{4,9}/|\b(?:PubMed\s+)?PMID\b|\bPMCID\b|\[?\s*Epub\b|\bAvailable\s+(?:from|at)\b|"
                  r"\bPublished\s+online\b)", s, re.I)
    if m and _TRAILER.match(s[m.start():]):
        return s[:m.start()]
    return s


def _split_endnote(body: str, ai_title: str, ai) -> Article | None:
    s = _cut_identifiers(body)
    c = CITE_EN.search(s)
    if not c:
        return None
    head = s[:c.start()]
    got = endnote_authors(head)
    if got:
        names, et_al, pos = got
    else:
        v = vancouver_authors(head)
        if not v:
            return None
        names, et_al, pos = v
    tj = split_title_journal(head[pos:], ai_title)
    if not tj:
        return None
    return Article(authors=names, et_al=et_al, title=tj[0], journal=tj[1], year=c.group("year"),
                   volume=_vol(c.group("vol")), issue=c.group("issue") or "", pages=c.group("pages") or "")


def _vol(v: str) -> str:
    """"45 Suppl 1" / "45(Suppl 1)" -> "45 Suppl 1" (format_record puts supplements in brackets)."""
    v = " ".join(re.sub(r"[()]", " ", v or "").split())
    return re.sub(r"(?i)suppl(?:ement)?\.?", "Suppl", v)


def _split_vancouver(body: str, ai_title: str, ai) -> Article | None:
    s = _cut_identifiers(body)
    cites = [c for c in CITE_V.finditer(s) if _cut_trailer(s, c.end())]
    a = Article()
    if cites:
        c = cites[-1]
        head = s[:c.start()]
        a.year, a.volume, a.issue, a.pages = c.group("year"), _vol(c.group("vol")), c.group("issue") or "", c.group("pages") or ""
    else:  # "Journal. 2023." with no volume: styled, the volume queried
        y = re.search(rf"(?:^|[.,]\s*|\s)(?P<year>{_YEAR}){_DATE}\s*[.;]?\s*$", s)
        if not y or y.start("year") < 20:
            return None
        head, a.year = s[:y.start("year")], y.group("year")
    head = head.rstrip()
    v = vancouver_authors(head)
    full = None if v else full_name_authors(head)
    if v:
        a.authors, a.et_al, pos = v
    elif full:  # "Atoosa Bonyani, Leila Safaeian" -> "Bonyani A, Safaeian L" (S: Vancouver "Elnaem MH, ..."), queried
        a.authors, a.et_al, pos = full
        a.full_names = True
    elif ai_title and refengine.fold(head).find(refengine.fold(ai_title)[:40]) > 0:
        k = _find_folded(head, ai_title)
        if k is None or k == 0:
            return None
        a.authors_raw, pos = head[:k].strip().rstrip(".,; "), k
    elif ai is not None and ai.get("authors") == [] and ai_title and refengine.fold(head).startswith(refengine.fold(ai_title)[:30]):
        pos = 0  # no authors: the entry starts with its title
    else:
        return None
    tj = split_title_journal(head[pos:], ai_title)
    if not tj:
        return None
    a.title, a.journal = tj
    return a


def _find_folded(text: str, value: str) -> int | None:
    """Start of `value` in `text`, comparing folded words."""
    want = refengine.fold(value).split()[:6]
    ms = list(re.finditer(r"[^\W_]+", text))
    fw = [refengine.fold(m.group(0)) for m in ms]
    for i in range(len(ms)):
        if fw[i:i + len(want)] == want:
            return ms[i].start()
    return None


# --- formatting and the no-invention guard ---------------------------------------------------------------

_ALWAYS = {"et", "al", "suppl"}


def _tokens(s: str) -> list[str]:
    return re.findall(r"[^\W_]+", refengine.fold_name(s).replace(" ", " "))


def invented(out: str, source: str, allowed: str = "") -> list[str]:
    """Words and numbers of `out` that the author's entry does not have (case and accents aside). Allowed:
    "et al.", "Suppl", the words of `allowed` (NLM's abbreviation of the author's journal), initials the
    entry writes with periods or spaces ("A. C." -> "AC"), and compressed last pages ("1706-1710" -> "1706-10")."""
    src = set(_tokens(source))
    extra = set(_tokens(allowed)) | _ALWAYS
    digits = [t for t in src if t[-1:].isdigit()]
    bad = []
    for word in re.findall(r"[^\W_]+", out):
        t = refengine.fold_name(word).replace(" ", "")
        if not t or t in src or t in extra:
            continue
        if word.isupper() and len(word) <= 4 and word.isalpha() and _initials_in(word, source):
            continue
        if word.isupper() and len(word) <= 3 and word.isalpha() and re.search(rf"\b{word}[A-Z]{{1,3}}\b", source):
            continue  # initials cut to the journal's limit: "MOS" -> "MO" (references.max_initials)
        if t.isdigit() and any(re.sub(r"\D", "", d).endswith(t) for d in digits):
            continue
        bad.append(word)
    return bad


def _initials_in(word: str, source: str) -> bool:
    """"AC" is written in the entry as initials: "A. C.", "A.C.", "A C", "A-C", or as the first letters of
    names ("Anna Clara")."""
    pat = r"(?<![^\W\d_])" + r"[^\W\d_A-Z]*\.?[\s-]*".join(re.escape(c) for c in word) + r"[^\W\d_A-Z]*(?![^\W\d_])"
    return re.search(pat, source) is not None


def author_capitals(title: str) -> set[str]:
    """Capitalised words the author wrote inside a title that is otherwise in lower case ("machine learning in
    Python", "children in East Africa"): the author marked them as names. A Title-Cased title marks nothing."""
    words = [w for w in refengine._mid_title_words(title) if re.fullmatch(r"[^\W\d_][\w'’\-]*", w)
             and w.lower() not in refengine._MINOR_WORDS]
    caps = [w for w in words if w[:1].isupper()]
    if len(words) < 3 or len(caps) * 3 > len(words):
        return set()
    return {part for w in caps for part in w.split("-") if part[:1].isupper()}


def format_article(a: Article, style: JournalStyle, text: str) -> tuple[str, list[tuple[str, dict]], str]:
    """(styled entry, queries, NLM abbreviation used) with refengine.format_record over the author's fields."""
    journal, abbr = journal_style_name(a.journal, style)
    authors = list(a.authors)
    if a.authors_raw:
        authors = [a.authors_raw]
    rec_authors = []
    for n in authors:
        m = re.fullmatch(r"(.+?)\s+([A-Z](?:-?[A-Z]){0,3})", n)
        rec_authors.append((m.group(1), m.group(2)) if m and not a.authors_raw else (n, ""))
    max_a = style.get("references.max_authors", 6)
    if a.et_al and not (max_a and len(rec_authors) > max_a):
        rec_authors.append(("et al", ""))
    r = biblio.Record(source="author", title=a.title, authors=rec_authors, journal=journal, year=a.year,
                      volume=a.volume, issue=a.issue, pages=a.pages)
    p = dataclasses.replace(refengine.parse(text), authors=[f"{s} {i}".strip() for s, i in rec_authors], title=a.title)
    out = refengine.format_record(r, style, p, keep=author_capitals(a.title))
    if not a.volume:  # "Journal 2023;." -> "Journal 2023."
        out = re.sub(rf"({_YEAR});\.$", r"\1.", out)
    queries = []
    if a.authors_raw:
        queries.append(("references.author_style", {}))
    if a.full_names:
        queries.append(("references.full_names", {}))
    if not a.volume:
        queries.append(("references.no_volume", {}))
    elif not a.pages:
        queries.append(("references.no_pages", {}))
    return out, queries, abbr


def style_unverified(res: refengine.Resolution, text: str, style: JournalStyle, ai: dict | None = None) -> refengine.Resolution:
    """`res` (a journal-article entry PubMed / CrossRef did not verify) with the entry styled from the author's
    fields, its missing fields queried, and `styled` set; unchanged when the split is uncertain or the styled
    entry would carry anything the author did not write."""
    if not style.get("references.style_unverified", True):
        return res
    ai = refengine.ai_refs.check_fields(text, ai) if ai else None
    a = split(text, ai)
    if a is None or not a.title or not a.journal or not a.year:
        return res
    out, queries, abbr = format_article(a, style, text)
    out += refengine.errata(text, style)
    if invented(out, text, abbr):
        return res
    res.formatted = out
    res.styled = True
    have = {k for k, _ in res.queries}
    res.queries += [q for q in queries if q[0] not in have]
    return res


# --- books, chapters, conference papers, reports: the author's APA / EndNote order put in the guide's ----------
# "Authors. Title. Edition. In: Editors, editors. Book. City: Publisher; Year. p. pages." (refs_other templates it)

_ED_NOTE = re.compile(r"\s*\((?P<ed>\d{1,2}(?:st|nd|rd|th)\s+ed(?:ition)?\.?|[A-Z][a-z]+\s+ed(?:ition)?\.?)\)", re.I)
_EDS = re.compile(r"\s*\((?:Eds?|Editors?)\.?\)\s*,?\s*", re.I)
_PP = re.compile(r"\s*\(\s*(?:pp?\.\s*)?(?P<pages>[A-Za-z]?\d+(?:\s*[-–—]\s*[A-Za-z]?\d+)?)\s*\)")
_LINK = re.compile(r"\s*(?:(?:Retrieved|Available)\s+(?:from|at)\s*:?\s*)?(?:https?://|www\.|doi\s*:|\b10\.\d{4,9}/)\S*.*$", re.I)
_BOOK_NOTE = re.compile(r"\s*\((?P<body>(?:[^()]*?,\s*)?(?:Vol\.\s*\d+|\d{1,2}(?:st|nd|rd|th)\s+ed(?:ition)?\.?)"
                        r"(?:\s*,\s*[^()]*)?)\)", re.I)
TEMPLATE_WORDS = {"in", "editors", "editor", "ed", "p", "available", "from", "last", "accessed", "on"}


def given_first_names(block: str) -> list[str] | None:
    """"R. E. Mayer & P. A. Alexander" / "H. M. Niegemann, D. Leutner, & R. Brunken" -> ["Mayer RE", ...]."""
    items = [x for x in re.split(r"\s*(?:,\s*(?:&|and)?|&|\band\b)\s*", block.strip()) if x.strip()]
    out = []
    for it in items:
        m = re.fullmatch(r"((?:[A-Z]\.\s*-?\s*){1,4})\s*([^\W\d_][\w'’\-]*(?:\s+[^\W\d_][\w'’\-]*){0,2})", it.strip())
        if not m or _bad_surname(m.group(2)):
            return None
        out.append(f"{m.group(2)} {_initials(m.group(1))}")
    return out or None


def _lead_names(head: str) -> tuple[list[str], bool, int] | None:
    """Names at the start of an EndNote entry, before ", Title": "Tatara, T., Title" / "Sharma M, Title"."""
    got = endnote_authors(head)
    if got:
        return got
    names, pos = [], 0
    while True:
        m = _V_NAME.match(head, pos)
        if not m or "," not in m.group("sep") or _bad_surname(m.group("sur")) or re.search(r"\d", m.group("sur")):
            break
        names.append(f"{m.group('sur').strip()} {_initials(m.group('ini'))}")
        pos = m.end()
    return (names, False, pos) if names else None


def _join_names(names: list[str], et_al: bool, style: JournalStyle) -> str:
    max_a = style.get("references.max_authors", 6)
    et = style.get("references.et_al", "et al.").rstrip(".")
    if max_a and len(names) > max_a:
        names, et_al = names[:max_a], True
    return ", ".join(names + ([et] if et_al else []))


def restyle_other(text: str, style: JournalStyle, ai_title: str = "") -> str | None:
    """A book, chapter, conference paper or report the author wrote in APA ("Tomlinson, C. A. (2001). Title (2nd
    ed.). ASCD.") or EndNote ("Tatara, T., Title. 2021: Routledge.") order, in the guide's order with the
    author's own words; None when the entry has neither shape or anything would be added."""
    if not style.get("references.style_unverified", True):
        return None
    out = _restyle_apa_other(text, style, ai_title) or _restyle_endnote_other(text, style) \
        or _restyle_dotted_other(text, style)
    if not out or invented(out, text, " ".join(TEMPLATE_WORDS)):
        return None
    return out


def _restyle_apa_other(text: str, style: JournalStyle, ai_title: str) -> str | None:
    m = _APA_HEAD.match(text.strip())
    if not m:
        return None
    block, year, rest = m.group("authors").strip(), m.group("year"), m.group("rest").strip()
    got = apa_authors(block)
    if got:
        authors = _join_names(got[0], got[1], style)
    elif re.fullmatch(r"[A-Z][^.,;:()]{1,80}\.?", block) and (_GROUP.search(block) or block.rstrip(".").isupper()):
        authors = block.rstrip(".")  # "UNESCO.", "World Health Organization."
    else:
        return None
    link = _LINK.search(rest)
    url = ""
    if link:
        url = link.group(0).strip()
        rest = rest[:link.start()].rstrip()
    # the title: Claude's when written at the start, else the first sentence
    title = ""
    ai_title = _ED_NOTE.sub("", ai_title or "").strip()
    if ai_title and refengine.fold(rest).startswith(refengine.fold(ai_title)):
        k = _end_of(rest, ai_title)
        if k:
            while k < len(rest) and rest[k] in ")]" and rest[:k].count(rest[k] == ")" and "(" or "[") > \
                    rest[:k].count(rest[k]):
                k += 1  # "... Differences [Research]": the bracket the title opened closes with it
            title, rest = rest[:k], rest[k:]
    if not title:
        t = re.match(r"(.+?)(?=\s*\((?:\d|[A-Z][a-z]+\s+ed|pp?\.)|\.\s+(?:In\s|[A-Z(])|\.?\s*$)", rest)
        if not t:
            return None
        title, rest = t.group(1), rest[t.end():]
    title = title.strip().rstrip(".")
    edition = ""
    e = _ED_NOTE.match(rest)
    if e:
        edition, rest = re.sub(r"(?i)\s+edition\.?|\s+ed\.?", " ed.", e.group("ed")).strip(), rest[e.end():]
    rest = rest.lstrip(" .")
    parts = [f"{authors}.", f"{title}{'' if title.endswith(('?', '!')) else '.'}"]
    if edition:
        parts.append(edition)
    pages = ""
    c = re.match(r"In:?\s+(.+)$", rest, re.S)
    if c:
        inner = c.group(1)
        eds = ""
        d = _EDS.search(inner)
        if d:
            names = given_first_names(inner[:d.start()])
            if not names:
                return None
            eds, inner = ", ".join(names) + (", editors." if len(names) > 1 else ", editor."), inner[d.end():]
        else:  # "In M. Killen & J. G. Smetana, Handbook of ...": the editors without "(Eds.)" (997)
            cut = None
            for k in re.finditer(r",\s+", inner):
                if given_first_names(inner[:k.start()]):
                    cut = k
            if cut is not None and inner[cut.end():cut.end() + 1].isupper():
                names = given_first_names(inner[:cut.start()])
                eds, inner = ", ".join(names) + (", editors." if len(names) > 1 else ", editor."), inner[cut.end():]
        notes = []
        bn = _BOOK_NOTE.search(inner)  # "(Vol. 1, pp. 1–44)", "(3rd ed., pp. 437–453)"
        if bn:
            for part in re.split(r"\s*,\s*", bn.group("body")):
                pg = re.fullmatch(r"pp?\.\s*(?P<pages>[A-Za-z]?\d+(?:\s*[-–—]\s*[A-Za-z]?\d+)?)", part)
                if pg:
                    pages = pg.group("pages")
                elif re.fullmatch(r"Vol\.\s*\d+", part, re.I):
                    notes.append(re.sub(r"(?i)vol\.\s*", "Vol. ", part))
                elif _ED_NOTE.fullmatch(f" ({part})"):
                    notes.insert(0, re.sub(r"(?i)\s+edition\.?|\s+ed\.?", " ed.", part))
                else:
                    return None
            inner = inner[:bn.start()] + inner[bn.end():]
        else:
            pp = _PP.search(inner)
            if pp:
                pages, inner = pp.group("pages"), inner[:pp.start()] + inner[pp.end():]
        b = re.match(r"(.+?)(?:\.\s+(.*))?\.?\s*$", inner.strip(), re.S)
        if not b:
            return None
        book, rest = b.group(1).strip().rstrip("."), (b.group(2) or "").strip()
        parts.append(f"In: {eds + ' ' if eds else ''}{book}.")
        parts += [n if n.endswith(".") else n + "." for n in notes]
    publisher = rest.strip().rstrip(".").strip()
    if re.search(r"\d", publisher) or len(publisher) > 120:
        return None  # numbers we cannot place (a volume, a report number)
    parts.append(f"{publisher}; {year}" if publisher else year)
    out = " ".join(parts)
    if pages:
        out += f". p. {refengine.format_pages(pages, style)}"
    out += "."
    if url:
        out += f" Available from: {re.sub(r'^(?:(?:Retrieved|Available)\s+(?:from|at)\s*:?\s*)', '', url, flags=re.I)}"
    return out


def _end_of(rest: str, value: str) -> int | None:
    """Where the words of `value` end at the start of `rest`."""
    want = len(refengine.fold(value).split())
    ms = list(re.finditer(r"[^\W_]+", rest))
    n = 0
    for k, mm in enumerate(ms):
        n += len(refengine.fold(mm.group(0)).split()) or 1
        if n >= want:
            end = mm.end()
            q = re.match(r"[?!]", rest[end:])
            return end + (q.end() if q else 0)
    return None


def _restyle_endnote_other(text: str, style: JournalStyle) -> str | None:
    """"Tatara, T., Title. 2021: Routledge." / "Sharma M, Title. 2017: Jones & Bartlett Learning. 145-163."."""
    m = re.match(rf"^(?P<head>.+?)\.\s+(?P<year>{_YEAR})\s*:\s*(?P<pub>[^.:;]{{2,90}}?)\.(?:\s*(?:pp?\.\s*)?(?P<pages>\d+\s*[-–]\s*\d+)\.?)?\s*$",
                 text.strip())
    if not m:
        return None
    got = _lead_names(m.group("head"))
    if not got:
        return None
    names, et_al, pos = got
    title = m.group("head")[pos:].strip().lstrip(",").strip()
    if len(title.split()) < 2:
        return None
    out = f"{_join_names(names, et_al, style)}. {title}. {m.group('pub').strip()}; {m.group('year')}"
    if m.group("pages"):
        out += f". p. {refengine.format_pages(m.group('pages'), style)}"
    return out + "."


_DOTTED_NAME = re.compile(r"\s*(?P<sur>[^\W\d_][\w'’\-]*(?:\s+[^\W\d_][\w'’\-]*){0,2}?),\s*(?P<ini>(?:[A-Z]\.\s*-?\s*){1,4})"
                          r"(?:,?\s*(?:and|&)\s+)?")


def _restyle_dotted_other(text: str, style: JournalStyle) -> str | None:
    """A book whose authors are each "Surname, I." with no year after them: "Smith, J. Rao, K. lee, M. Family
    counselling skills, London: Example Press; 2015." -> "Smith J, Rao K, Lee M. Family counselling skills.
    London: Example Press; 2015." (the guide's book order, the author's own words)."""
    t = text.strip()
    names, pos = [], 0
    while True:
        m = _DOTTED_NAME.match(t, pos)
        if not m or _bad_surname(refengine.capital_surname(m.group("sur").strip())):
            break
        names.append(f"{refengine.capital_surname(m.group('sur').strip())} {_initials(m.group('ini'))}")
        pos = m.end()
    if len(names) < 2:
        return None
    rest = t[pos:].strip()
    p = re.match(rf"(?P<title>[^:;]{{3,250}}?)[,.]\s+(?P<city>[A-Z][A-Za-z .'-]{{1,35}}?)\s*:\s*(?P<pub>[^:;]{{2,90}}?)"
                 rf"\s*[;,]\s*(?P<year>{_YEAR})\.?\s*$", rest)
    if not p or len(p.group("title").split()) < 2:
        return None
    title = p.group("title").strip().rstrip(".")
    return f"{_join_names(names, False, style)}. {title}. {p.group('city').strip()}: {p.group('pub').strip()}; {p.group('year')}."
