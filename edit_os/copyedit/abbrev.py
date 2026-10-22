"""Abbreviations: defined at first use, used only after their definition, defined once.

Definitions "full form (ABBR)" are found with the Schwartz-Hearst algorithm (Schwartz & Hearst,
"A simple algorithm for identifying abbreviation definitions in biomedical text", PSB 2003).
The abstract and the main text are separate scopes, as the journals expect an abbreviation to
be expanded again in the main text. Front matter before the abstract, tables and the reference
list are not checked, and a common-abbreviations allowlist (units, statistics, well-known
terms; `data/common_abbreviations.txt` plus `copyedit.abbreviations.allow`) is never queried.
Neither are a second copy of the reference list left in the text, author initials ("Rao KL, Moss T"),
contribution lines ("Concept: AB, CD"), words in capitals that form a heading or label, dotted approval
codes, a product with its model number or maker ("SCANX 700", "(VIEWER V60, Osaka, Japan)"), and
abbreviations expanded after them ("DBS (deep brain stimulation)"). On 160 articles this cut the
"Kindly expand" queries from 868 to 580 and kept all 22 that the copy editors raised.
Back-matter statements (contributions, funding, conflicts, the use of AI tools), key words, titles, degrees
("MD, DM") and named tools ("the TREND statement") are not queried either; abbreviation lists and table notes
are never a second definition. With `defined_in_article` (JEHP: "Look for undefined acronyms"), an acronym
the article expands anywhere -- the abstract, an abbreviation list or table note, "(SCOFF)" after its
description -- is not undefined, and the lists themselves and institution names are not queried
(17 JEHP articles: 92 -> 16 "Kindly expand" queries).

Output:
  * author queries, in the copy editors' wording, for abbreviations never expanded;
  * a query, or (`move_definition: true`) changes moving the expansion to the first use, for
    abbreviations used before their definition;
  * for repeated definitions in one scope, a query, or (`redefinition: change`) a change
    "full form (ABBR)" -> "ABBR" (the copy editors make that change about half the time);
  * for a definition whose abbreviation is never used again in its scope, a change dropping the
    "(ABBR)" (`unused_definition`; instructions to all journals: "If a term is not used again, an
    abbreviation is not needed"). Tables and legends count as uses of the main text's definitions;
  * for the full form written out again after its definition, a change to the abbreviation
    (`full_form_after_definition`; stylesheets: "only abbreviation should be used thereafter").
    Headings keep the full form.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache

from ..changes import Change, Layer
from ..docmodel import Manuscript, Paragraph
from ..queries import Query
from ..style import JournalStyle
from .common import change, is_numeric, overlaps, protected_spans

TEXT = {
    "abbrev.undefined": "{abbr}: Kindly expand the abbreviation.",
    "abbrev.before_definition": "{abbr}: Kindly expand the abbreviation at first use.",
    "abbrev.defined_twice": "{abbr}: The abbreviation has already been expanded. Kindly check.",
    "abbrev.unused": "{abbr}: The abbreviation is not used again in the text. Kindly check if it can be deleted.",
    "abbrev.full_form": "{abbr}: The abbreviation has already been defined. Kindly use the abbreviation.",
}

DATA = os.path.join(os.path.dirname(__file__), "data", "common_abbreviations.txt")


@lru_cache(maxsize=1)
def common_abbreviations() -> frozenset[str]:
    out = set()
    with open(DATA, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                out.update(w.strip() for w in line.split(",") if w.strip())
    return frozenset(out)


# --- Schwartz-Hearst -------------------------------------------------------------------

def is_short_form(sf: str) -> bool:
    if not 2 <= len(sf) <= 10 or len(sf.split()) > 2 or not sf[0].isalnum():
        return False
    if not any(c.isalpha() for c in sf):
        return False
    upper = sum(c.isupper() for c in sf)
    return upper >= 2 or (upper >= 1 and any(c.isdigit() for c in sf)) or (len(sf) <= 3 and sf[0].isupper())


def best_long_form(sf: str, lf: str) -> str | None:
    """Schwartz-Hearst: match the short form's characters right to left in the candidate long
    form; the first character must start a word. Returns the shortest matching long form."""
    s, l = len(sf) - 1, len(lf) - 1
    while s >= 0:
        c = sf[s].lower()
        if not c.isalnum():
            s -= 1
            continue
        while l >= 0 and (lf[l].lower() != c or (s == 0 and l > 0 and lf[l - 1].isalnum())):
            l -= 1
        if l < 0:
            return None
        l -= 1
        s -= 1
    return lf[lf.rfind(" ", 0, l + 1) + 1:]


@dataclass
class Definition:
    para_id: str
    short: str
    long: str
    start: int       # start of the long form
    sf_start: int    # start of the short form inside the parentheses
    end: int         # just after the closing parenthesis
    paren_start: int


# "full form (ABBR)", and "full form [ABBR]" (one word in square brackets: not "[12]", "[Figure 1]")
_PAREN = re.compile(r"\(([^()]{1,40})\)|\[([A-Za-z][A-Za-z0-9\-]{1,9})\]")


def definitions(p: Paragraph) -> list[Definition]:
    out = []
    text = p.text
    for m in _PAREN.finditer(text):
        g = 1 if m.group(1) is not None else 2
        inner = m.group(g)
        sf = re.split(r"\s*[;,]\s*", inner.strip())[0]
        sf_plain = re.sub(r"(?<=[A-Z])s$", "", sf)  # "(RCTs)" defines RCT
        if not is_short_form(sf_plain):
            continue
        before = text[:m.start()].rstrip()
        # Candidate long form: the words before "(", within the sentence, at most min(|SF|+5, 2|SF|).
        seg_start = max(before.rfind(". "), before.rfind("; "), before.rfind("("), before.rfind(")"),
                        before.rfind(": "), before.rfind("["), before.rfind("]")) + 1
        seg = before[seg_start:]
        words = seg.split()
        n = min(len(sf_plain) + 5, 2 * len(sf_plain))
        cand = " ".join(words[-n:]) if words else ""
        if not cand:
            continue
        lf = best_long_form(sf_plain, cand)
        if not lf or len(lf) <= len(sf_plain) or sf_plain.lower() in lf.lower().split():
            continue
        if len(lf.split()) > n:
            continue
        lf_start = before.rfind(lf)
        if lf_start < 0:
            continue
        out.append(Definition(p.id, sf_plain, lf, lf_start, m.start(g) + inner.find(sf), m.end(), m.start()))
    return out


# --- scopes and uses -------------------------------------------------------------------

_HEAD = re.compile(r"^\s*(?:<H\d>)?\s*(?:\d+\.?\s*)?([A-Za-z][A-Za-z ]{2,40}?)\s*:?\s*$")
_ABSTRACT = re.compile(r"^\s*(?:<H\d>)?\s*(?:abstract|summary)\b", re.I)
_KEYWORDS = re.compile(r"^\s*(?:<H\d>)?\s*key\s*-?words?\b", re.I)
_MAIN = {"introduction", "background", "case report", "case presentation", "case description",
         "materials and methods", "methods", "dear editor", "dear sir", "main text", "review"}


def _is_heading(p: Paragraph) -> bool:
    return p.text.lstrip().startswith("<H") or (len(p.text) < 80 and not p.text.rstrip().endswith("."))


def _main_heading(p: Paragraph) -> bool:
    text = p.text.strip()
    m = _HEAD.match(text)
    if m and m.group(1).strip().lower() in _MAIN:
        return True
    return text.lower().startswith(("dear editor", "dear sir", "sir,"))


def scopes(ms: Manuscript) -> list[list[Paragraph]]:
    """Paragraph groups in which an abbreviation is expanded once: the abstract, any box between
    the key words and the main text ("Highlights", "What is known?"), and the main text. Front
    matter before the abstract, tables and the reference list are left out."""
    from ..rules import body_paragraphs

    paras = _before_references(body_paragraphs(ms))
    ab = next((i for i, p in enumerate(paras) if _ABSTRACT.match(p.text)), None)
    lo = 0 if ab is None else ab + 1
    kw = next((i for i in range(lo, len(paras)) if _KEYWORDS.match(paras[i].text)), None) if ab is not None else None
    # A structured abstract may have its own "Background" / "Methods" lines: look after the key words.
    lo = kw + 1 if kw is not None else lo
    main = next((i for i in range(lo, len(paras)) if _main_heading(paras[i])), None)
    if kw is not None and kw + 1 < len(paras) and not _is_heading(paras[kw + 1]):
        main = kw + 1  # body text right after the key words: an unheaded Introduction (IJO drops it), not a box
    if ab is None:
        return [paras[main:]] if main is not None else [paras]
    if main is None:
        return [paras[ab:kw + 1], paras[kw + 1:]] if kw is not None else [paras[ab:]]
    if kw is not None and kw + 1 < main:
        return [paras[ab:kw + 1], paras[kw + 1:main], paras[main:]]
    return [paras[ab:main], paras[main:]]


def _before_references(paras: list[Paragraph]) -> list[Paragraph]:
    """Up to the first reference-list heading (a manuscript may hold the list twice: the last heading
    marks the reference list for the rules), without paragraphs that read as reference entries
    ("Rao KL, Moss T. A title ... 2006;90:262-7.")."""
    from .. import references

    out, back = [], False
    for p in paras:
        if references._HEADING.match(p.text):
            break
        if _is_heading(p):
            back = bool(_BACK_MATTER.match(p.text))
        if back or _BACK_MATTER_LABEL.match(p.text) or _reference_like(p.text) or _contributions(p.text):
            continue
        out.append(p)
    return out


# Back-matter statements after the main text: who did what, funding, conflicts, the use of AI tools. Their
# initials ("SM contributed to ..."), company and tool names ("ChatGPT (OpenAI)") are not queried.
_BACK_WORDS = (r"acknowledge?ments?|authors?['’]?s?['’]?\s+contributions?|contributions?\s+of\s+(?:the\s+)?authors|contributors"
               r"|conflicts?\s+of\s+interests?|competing\s+interests?|declaration\s+of\s+(?:competing|conflicting)"
               r"|financial\s+support|funding|sources?\s+of\s+(?:funding|support)|sponsorship"
               r"|(?:use|usage)\s+of\s+(?:generative\s+)?(?:ai|artificial\s+intelligence)\b.*|ai\s+(?:policy|use|tools?|statement)"
               r"|(?:compliance\s+with\s+)?\w*\s*ai\s+policy.*|declaration\s+of\s+(?:generative\s+)?ai\b.*|using\s+ai|disclosure"
               r"|generative\s+ai.*|data\s+availability(?:\s+statement)?|availability\s+of\s+data.*")
_BACK_MATTER = re.compile(r"^\s*(?:<H\d>)?\s*(?:\d+\.?\s*)?(?:" + _BACK_WORDS + r")\s*:?\s*$", re.I)
_BACK_MATTER_LABEL = re.compile(r"^\s*(?:<H\d>)?\s*(?:" + _BACK_WORDS.replace(".*", "[^:]{0,40}") + r")\s*[:.\-–]\s", re.I)


def _reference_like(text: str) -> bool:
    from .. import references

    body = text[references._LABEL.match(text).end():] if references._LABEL.match(text) else text
    return bool(references._LOOKS_LIKE_REFERENCE.match(body) and references._YEAR.search(body)
                and len(_NAME_ITEM.findall(body)) >= 2)


def _contributions(text: str) -> bool:
    return len(text) < 400 and bool(_CONTRIBUTIONS.search(text)) and len(_INITIALS_LIST.findall(text)) >= 2


_NAME_ITEM = re.compile(r"[A-Z][\w’'\-]+\s[A-Z]{1,3}(?:-[A-Z])?[,.]")  # "Rao KL,", "LEE B,", "KIM J-P,"
# Who did what: "Authors' contributions: JAG, MAM, IAM", "Concept, design: GGO, ZK;", "ISS<tab>Formal analysis".
_CONTRIBUTIONS = re.compile(r"contribut|conceptuali[sz]|formal analysis|data (?:curation|acquisition|collection)"
                            r"|literature search|manuscript (?:preparation|editing|review)|writing|supervision", re.I)


_USE = re.compile(r"(?<![\w\-/.])([A-Za-z0-9][A-Za-z0-9\-/&]*[A-Za-z0-9]|[A-Z])(?![\w\-/])")


def _abbr_key(tok: str) -> str | None:
    """The abbreviation a token stands for ("RCTs" -> "RCT", "post-COVID" -> "COVID"), or None
    if it is not one (ordinary words, Roman numerals, codes such as "C6-T4" or "PM2", approval
    numbers such as "IEC/0078/2022")."""
    tok = re.sub(r"^(?:[a-z]{2,}-)+", "", tok.rstrip("-"))  # lowercase prefixes: post-, non-, anti- (not "t-SNE")
    base = re.sub(r"(?<=[A-Z0-9])s$", "", tok)
    if len(base) < 2 or len(base) > 12 or base.isdigit():
        return None
    if sum(c.isdigit() for c in base) >= 3 or base.count("/") > 1:
        return None
    upper = sum(c.isupper() for c in base)
    letters = sum(c.isalpha() for c in base)
    if upper < 2 or upper < letters / 2:
        return None  # ordinary words, "McDonald"
    if re.fullmatch(r"[IVXLC]+", base) or re.fullmatch(r"[A-Z]{1,2}\d{1,2}(?:[-/][A-Z]{0,2}\d{1,2})*", base):
        return None  # Roman numerals; spinal levels, figure parts, criteria codes
    return base


_PLACE = re.compile(r"[A-Z][a-z]+,\s*$")  # "Armonk, NY": a state or country code after a city
# Author initials after a surname: "Rao KL, Moss T", "Lin JHK," (a name list has two or more).
_INITIALS = re.compile(r"(?<![\w-])[A-Z][\w’'\-]*[a-z][\w’'\-]*\s+([A-Z]{1,3}(?:-[A-Z])?)(?=\s*[,.;]|\s+et al|\s*$)")
# Words in capitals that are a heading or label, not abbreviations: "STUDY DESIGN AND SETTING",
# "MANUSCRIPT TYPE:", "(KEYWORDS: ...".
_INITIALS_LIST = re.compile(r"(?<![\w-])[A-Z]{2,4}(?=\s*(?:[,;]|and\b|\t|$))")  # "DB, SS, RH;"
# A product's maker or place in brackets: "(VIEWER V60, Osaka, Japan)", "(Example Trade GmbH)".
_MAKER = re.compile(r"\([^()]*\b(?:Inc|Ltd|LLC|GmbH|AG|S\.?A|Corp|Corporation|Co|Company|Meditec|Medical|Healthcare"
                    r"|USA|U\.S\.A|UK|Japan|Germany|France|Italy|Switzerland|China|India|Korea|Netherlands|Sweden"
                    r"|Denmark|Canada|Australia|Israel|Spain|Austria|Belgium|Finland|Ireland|Taiwan|Singapore)\b[^()]*\)")
_MODEL = re.compile(r"\s+-?\s?\d{3,4}(?:[A-Z][A-Za-z0-9]*)?(?![\d,.%])(?!\s*(?:%|(?:mg|g|kg|mm|cm|m|ml|mL|L|µm|μm|um|nm|ms|s|min|h|Hz|kHz|MHz|kDa|IU|U|mmHg|cells|patients|eyes)\b))|\s+\d[A-Z]{1,3}\d")
# Words in capitals that are not abbreviations: search operators, licence and label words.
_NOT_ABBR = {"AND", "OR", "NOT", "BY", "THE", "OF", "IN", "FIGURE", "FIGURES", "TABLE", "TABLES", "GROUP", "NOTE",
             "NOTES", "CASE", "REPORT", "NA", "NIL", "ONLY", "ALL", "NONE", "MUST", "NEVER", "ALWAYS", "BOTH", "EACH",
             "ANY", "ALSO", "VERY", "YES", "ONE", "TWO"}
# Two or more degrees after a name: "Ajay Kumar Jha, MD, DM" (DM alone is diabetes mellitus).
_DEGREE = r"(?:MBBS|MD|MS|DM|MCh|M\.Ch|PhD|DNB|MPH|MSc|BSc|MDS|BDS|FRCS|FRCP|FRCOG|MRCP|MRCOG|FACS|DCH|DGO|DO|RN|BSN|MSN|DrPH|MA|BA)"
_DEGREES = re.compile(r"(?<![\w-])" + _DEGREE + r"(?:\s*,\s*" + _DEGREE + r")+(?![\w-])")
# What follows a name that is not an abbreviation to expand: a reporting statement, program, software or database
# ("the TREND statement", "MAXQDA software"); with `defined_in_article`, an institution ("SRM Medical College":
# other journals' copy editors expanded "GMERS Medical College" once in 4).
_NAMED = re.compile(r"\s+(?:statement|program|programme|software|package|database|platform)\b")
_INSTITUTION = re.compile(r"\s+(?:[A-Z][a-z]+\s+){0,2}(?:College|University|Hospital|Institute|Foundation|Trust|Corporation)\b")
_CAPS_RUN = re.compile(r"(?<![\w-])[A-Z]{2,}(?:\s+[A-Z]{2,})+(?![\w-])")
_LABEL = re.compile(r"(?:^\s*|\(\s*|[.?!]\s+)((?:[A-Z]{2,}\s+)*[A-Z]{2,})\s*:")


def uses(p: Paragraph, lenient: bool = False):
    if p.text.strip().isupper() or is_numeric(p.text):
        return  # all-caps headings and number rows
    prot = protected_spans(p.text)
    names = list(_INITIALS.finditer(p.text))
    if len(names) >= 2:
        prot += [(m.start(1), m.end(1)) for m in names]
    prot += [(m.start(), m.end()) for m in _CAPS_RUN.finditer(p.text)
             if max(len(w) for w in m.group(0).split()) >= 5]
    prot += [(m.start(1), m.end(1)) for m in _LABEL.finditer(p.text)]
    prot += [(m.start(), m.end()) for m in _MAKER.finditer(p.text)]
    prot += [(m.start(), m.end()) for m in _DEGREES.finditer(p.text)]
    for m in _USE.finditer(p.text):
        key = _abbr_key(m.group(1))
        if not key or key in _NOT_ABBR or overlaps(m.start(), m.end(), prot):
            continue
        if re.match(r"\.[A-Za-z0-9]", p.text[m.end():m.end() + 2]):
            continue  # a dotted code: "IR.XYZ.REC.1400.123"
        if _MODEL.match(p.text, m.end()):
            continue  # a product and its model number: "Acme SCANX 700", "QRX 700 HD", "VTS -300"
        if len(key.replace("&", "")) == 2 and _PLACE.search(p.text[:m.start()]):
            continue  # "Armonk, NY", "Jammu, J&K"
        if _NAMED.match(p.text, m.end()) or lenient and _INSTITUTION.match(p.text, m.end()):
            continue  # a named tool ("the COACH program"), an institution ("SRM Medical College")
        if m.start() == len(p.text) - len(p.text.lstrip()) and p.text[m.end():m.end() + 1] == ":" and key.isalpha():
            continue  # "KEYWORDS:" label
        yield key, m.start(1), m.end(1)


# --- check -----------------------------------------------------------------------------

def _outside_scopes(ms: Manuscript, scoped: set[str]) -> list[Paragraph]:
    """Tables and the legends after the reference list: where the main text's abbreviations are
    also used. Reference entries are left out."""
    from .. import references

    refs = {r.para_id for r in references.extract(ms)}
    head = references.find_heading(ms)
    head_id = ms.paragraphs[head].id if head is not None else None
    after_refs = head is None
    out = []
    for p in ms.paragraphs:
        if p.id == head_id:
            after_refs = True
            continue
        if p.id in scoped or p.id in refs or not p.text.strip():
            continue
        if p.in_table or after_refs:
            out.append(p)
    return out


def _paren_only(p: Paragraph, d: Definition) -> bool:
    return p.text[d.paren_start:d.end] in (f"({d.short})", f"({d.short}s)")


def _unused(ms: Manuscript, key: str, ds: list[Definition], mode: str, changes: list, queries: list) -> None:
    """The abbreviation is never used after being defined: keep the full form, drop "(ABBR)"."""
    for d in ds:
        p = ms.by_id(d.para_id)
        lf_end = d.start + len(d.long)
        if not _paren_only(p, d) or p.text[lf_end:d.paren_start].strip():
            continue
        if mode == "change":
            changes.append(change(p, lf_end, d.end, "", "abbreviations", reason="abbreviation not used again"))
        elif mode == "query":
            queries.append(Query(p.id, d.paren_start, d.end, TEXT["abbrev.unused"].format(abbr=key), "abbrev.unused"))


def _full_forms(scope: list[Paragraph], order: dict, key: str, d0: Definition, mode: str,
                changes: list, queries: list, n_uses: int = 0) -> None:
    """The full form written out again after the definition: use the abbreviation (not in headings
    or key words). A change only when the author already uses the abbreviation more often than the
    full form after defining it: editors leave a term the author keeps writing out ("retinoblastoma"
    in a paper about it) and shorten the stray ones ("intraocular pressure" among many "IOP")."""
    pat = re.compile(r"(?<![\w\-])" + re.escape(d0.long) + r"(s?)(?![\w\-])(?!\s*\()", re.I)
    listed = re.compile(r"(?<![\w\-])" + re.escape(key) + r"s?\s*[=:,–—]\s*$")
    listed_after = re.compile(r"\s*[=:,–—]\s*" + re.escape(key) + r"(?![\w\-])")
    found = []
    for p in scope:
        if order[p.id] < order[d0.para_id] or _is_heading(p) or _KEYWORDS.match(p.text):
            continue
        spans = [(d.start, d.end) for d in definitions(p)] + protected_spans(p.text)
        for m in pat.finditer(p.text):
            if p.id == d0.para_id and m.start() < d0.end or overlaps(m.start(), m.end(), spans):
                continue
            if listed.search(p.text[:m.start()]) or listed_after.match(p.text[m.end():]):
                continue  # an abbreviation list or table note: "CBE = Competency-Based Education"
            found.append((p, m))
    if n_uses <= len(found):
        return
    for p, m in found:
        if mode == "change":
            changes.append(change(p, m.start(), m.end(), key + m.group(1), "abbreviations",
                                  reason="use abbreviation after its definition"))
        elif mode == "query":
            queries.append(Query(p.id, m.start(), m.end(), TEXT["abbrev.full_form"].format(abbr=key),
                                 "abbrev.full_form"))


# "ABBR (full form)": the abbreviation first, then its expansion. (Lists of abbreviations and table notes,
# "IRB: institutional review board", do not count: the copy editors still expand those in the text.)
_REVERSE = re.compile(r"(?<![\w\-/])(?P<sf>[A-Za-z][A-Za-z0-9\-/&]{0,11}[A-Za-z0-9])\s*\(\s*(?P<lf>[A-Za-z][^()]{2,80})\)")


def _expands(sf: str, phrase: str) -> bool:
    """`phrase` starts with a full form of `sf` (Schwartz-Hearst, matched from its first word)."""
    words = phrase.split()
    n = min(len(sf) + 5, 2 * len(sf))
    for k in range(1, min(n, len(words)) + 1):
        cand = " ".join(words[:k])
        lf = best_long_form(sf, cand)
        if lf == cand and len(lf) > len(sf) and sf.lower() not in lf.lower().split():
            return True
    return False


def expanded_elsewhere(ms: Manuscript) -> set[str]:
    """Abbreviations the text expands with the abbreviation first ("DBS (deep brain stimulation)",
    "TOPAZ (Trial Of Pressure And Zinc)"): the copy editors do not ask for these.
    Not tables and legends: an abbreviation their notes expand is still expanded in the text."""
    from .. import rules_figures

    out = set()
    for p in ms.paragraphs:
        if p.in_table or rules_figures.caption_match(p) is not None:
            continue
        for m in _REVERSE.finditer(p.text):
            key = _abbr_key(m.group("sf"))
            if key and _expands(key, m.group("lf")):
                out.add(key)
    return out


# An abbreviation list or table note: "Abbreviations: CI, confidence interval; SD: standard deviation",
# "Note. CE = Concrete Experience; RO = Reflective Observation".
_GLOSS_LABEL = re.compile(r"^\s*(?:<H\d>)?\s*(?:list\s+of\s+)?(?:abbreviations?|acronyms?|notes?)\b\s*(?:used)?\s*[:.\-–]?", re.I)
_GLOSS_PAIR = re.compile(r"(?<![\w\-/])(?P<sf>[A-Za-z][A-Za-z0-9\-/&]{0,11}[A-Za-z0-9]|[A-Z])\s*[:=,\u202f]\s*\u202f?"
                         r"(?P<lf>[A-Za-z][^;:=,\u202f]{2,100})")


def glossary(p: Paragraph) -> dict[str, tuple[int, int]]:
    """Abbreviations a list or table note defines ("CFI, Comparative Fit Index"; "CE = Concrete Experience"),
    with the span of each; {} for a paragraph that is not such a list (a label, or two such pairs at least)."""
    out = {}
    label = _GLOSS_LABEL.match(p.text)
    for m in _GLOSS_PAIR.finditer(p.text, label.end() if label else 0):
        key = _abbr_key(m.group("sf"))
        # the full form runs to the next comma, or (after ":" / "=") to the next semicolon or colon:
        # "KAP: Knowledge, Attitude and Practice; IEC: ..."
        wide = re.match(r"[^;:=\u202f]{2,100}", p.text[m.start("lf"):])
        lf = wide.group(0) if wide else m.group("lf")
        if key and (_expands(key, m.group("lf")) or _expands(key, lf) or lf.split()[0].lower() == key.lower()):
            out.setdefault(key, (m.start("sf"), m.end("sf")))
    labelled = label is not None
    if labelled and re.match(r"\s*(?:<H\d>)?\s*(?:list\s+of\s+)?(?:abbreviations?|acronyms?)", p.text, re.I):
        for d in definitions(p):  # "Abbreviations: Reproductive Tract Infections (RTI), AOR (Adjusted Odds Ratio)"
            out.setdefault(d.short, (d.sf_start, d.sf_start + len(d.short)))
        for m in _REVERSE.finditer(p.text):
            if (key := _abbr_key(m.group("sf"))) and _expands(key, m.group("lf")):
                out.setdefault(key, (m.start("sf"), m.end("sf")))
    return out if out and (len(out) >= 2 or labelled) else {}


# "(SCOFF)" right after two or more words that describe it: the author's name for the thing, given in context.
_NAMED_IN_BRACKETS = re.compile(r"(?<=[A-Za-z]{3})\s+\(([A-Za-z][A-Za-z0-9\-/&]{0,11}[A-Za-z0-9])\)")


def _title_like(p: Paragraph) -> bool:
    """A title or running title in the abstract page ("Uric Acid-to-HDL Ratio in ACS"): short, no final
    full stop, most words capitalised."""
    text = re.sub(r"^\s*<H\d>", "", p.text).strip()
    words = [w for w in re.findall(r"[A-Za-z][\w\-]*", text) if w.lower() not in _MINOR]
    return 0 < len(text) < 150 and not text.endswith(".") and len(words) >= 3 \
        and sum(w[0].isupper() for w in words) >= 0.8 * len(words)


_MINOR = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "with", "by", "from", "vs", "versus", "among"}


def collect(ms: Manuscript, style: JournalStyle) -> tuple[list[Change], list[Query]]:
    cfg = style.get("copyedit.abbreviations", {}) or {}
    if not cfg.get("enabled", True):
        return [], []
    allow = set(common_abbreviations()) | set(cfg.get("allow") or [])
    allow_upper = {a.upper() for a in allow}
    changes: list[Change] = []
    queries: list[Query] = []
    queried: set[str] = set()
    all_scopes = scopes(ms)
    expanded = expanded_elsewhere(ms) if cfg.get("query_undefined", True) else set()
    outside = _outside_scopes(ms, {p.id for s in all_scopes for p in s})
    lists = {p.id: g for p in ms.paragraphs if (g := glossary(p))}  # abbreviation lists, table notes
    # Defined somewhere in the article, for the "undefined" query (JEHP S: "Look for undefined acronyms"):
    # in another scope (the abstract), an abbreviation list or table note, or named in brackets after its
    # description ("risk of eating disorders (SCOFF)", "maximum likelihood method (FIML)").
    known: set[str] = set()
    lenient = bool(cfg.get("defined_in_article"))
    if lenient:
        for p in ms.paragraphs:
            known.update(d.short for d in definitions(p))
            known.update(lists.get(p.id, {}))
            for m in _NAMED_IN_BRACKETS.finditer(p.text):
                if (k := _abbr_key(m.group(1))):
                    known.add(k)
    for n_scope, scope in enumerate(all_scopes):
        defs: dict[str, list[Definition]] = {}
        for p in scope:
            if p.id in lists or _KEYWORDS.match(p.text):
                continue  # "Abbreviations: Reproductive Tract Infections (RTI), ...", "Keywords: ... (RTI)"
            for d in definitions(p):
                defs.setdefault(d.short, []).append(d)
        order = {p.id: i for i, p in enumerate(scope)}
        first_def = {k: (order[v[0].para_id], v[0].sf_start) for k, v in defs.items()}
        first_use: dict[str, tuple[Paragraph, int, int]] = {}
        used: set[str] = set()  # used outside a definition (allowlisted too; before the definition counts)
        n_uses: dict[str, int] = {}  # uses after the definition
        for p in scope:
            spans = [(d.start, d.end) for d in definitions(p)]
            for key, s, e in uses(p, lenient):
                if overlaps(s, e, spans):
                    continue
                used.add(key)
                if key in first_def and (order[p.id], s) > first_def[key]:
                    n_uses[key] = n_uses.get(key, 0) + 1
                if key in allow or key.upper() in allow_upper:
                    continue
                if _KEYWORDS.match(p.text) or _title_like(p) or lenient and p.id in lists:
                    continue  # key words, titles; with `defined_in_article` the list itself ("CFI, Comparative Fit
                    # Index; GFI, ..."; other journals' copy editors expand such list items in the text)
                first_use.setdefault(key, (p, s, e))
        if n_scope == len(all_scopes) - 1:  # the main text: its abbreviations are used in tables and legends too
            for p in outside:
                used.update(k for m in _USE.finditer(p.text) if (k := _abbr_key(m.group(1))))  # short cells too

        unused = {k for k in defs if k not in used}
        unused_mode = cfg.get("unused_definition", "change")
        full_mode = cfg.get("full_form_after_definition", "change")
        for key in unused:
            if unused_mode != "off":
                _unused(ms, key, defs[key], unused_mode, changes, queries)
        main_text = n_scope == len(all_scopes) - 1  # not in the abstract
        for key, ds in defs.items():
            if key not in unused and full_mode != "off" and main_text:
                _full_forms(scope, order, key, ds[0], full_mode, changes, queries, n_uses.get(key, 0))

        # Defined twice in one scope: shorten the later definitions to the abbreviation, or query.
        mode = cfg.get("redefinition", "query")
        for key, ds in defs.items():
            if key in unused and unused_mode != "off":
                continue  # its definitions lose "(ABBR)" instead
            for d in ds[1:]:
                p = ms.by_id(d.para_id)
                if p.text[d.paren_start:d.end] != f"({key})" or _is_heading(p):
                    continue  # "(ABBR; n = 12)" and headings: leave for the editor
                if mode == "change":
                    changes.append(change(p, d.start, d.end, key, "abbreviations",
                                          reason="abbreviation defined twice"))
                elif mode == "query":
                    queries.append(Query(p.id, d.start, d.end, TEXT["abbrev.defined_twice"].format(abbr=key),
                                         "abbrev.defined_twice"))

        for key, (p, s, e) in first_use.items():
            if key in defs:
                if (order[p.id], s) < first_def[key]:
                    d = defs[key][0]
                    if cfg.get("move_definition") and p.text[s:e] == key and ms.by_id(d.para_id).text[d.paren_start:d.end] == f"({key})":
                        long = d.long
                        before = p.text[:s].rstrip()
                        if long[:1].isupper() and long[1:2].islower() and before and before[-1] not in ".?!:":
                            long = long[0].lower() + long[1:]  # capitalised only as a sentence start
                        changes.append(change(p, s, e, f"{long} ({key})", "abbreviations",
                                              reason="expand abbreviation at first use"))
                        dp = ms.by_id(d.para_id)
                        changes.append(change(dp, d.start, d.end, key, "abbreviations",
                                              reason="expand abbreviation at first use"))
                    elif cfg.get("query_before_definition", True):
                        queries.append(Query(p.id, s, e, TEXT["abbrev.before_definition"].format(abbr=key),
                                             "abbrev.before_definition"))
            elif key not in queried and cfg.get("query_undefined", True) and not re.search(r"[\d/-]", key) \
                    and key not in expanded and key not in known:
                # Codes with digits or hyphens (gene names, scales such as GDS-15) are rarely queried.
                queried.add(key)  # once per manuscript
                queries.append(Query(p.id, s, e, TEXT["abbrev.undefined"].format(abbr=key), "abbrev.undefined"))
    return changes, queries


def check(ms: Manuscript, style: JournalStyle) -> list[Change]:
    return collect(ms, style)[0]
