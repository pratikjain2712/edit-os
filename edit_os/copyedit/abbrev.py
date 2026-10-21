"""Abbreviations: defined at first use, used only after their definition, defined once.

Definitions "full form (ABBR)" are found with the Schwartz-Hearst algorithm (Schwartz & Hearst,
"A simple algorithm for identifying abbreviation definitions in biomedical text", PSB 2003).
The abstract and the main text are separate scopes, as the journals expect an abbreviation to
be expanded again in the main text. Front matter before the abstract, tables and the reference
list are not checked, and a common-abbreviations allowlist (units, statistics, well-known
terms; `data/common_abbreviations.txt` plus `copyedit.abbreviations.allow`) is never queried.

Output:
  * author queries, in the copy editors' wording, for abbreviations never expanded;
  * a query, or (`move_definition: true`) changes moving the expansion to the first use, for
    abbreviations used before their definition;
  * for repeated definitions in one scope, a query, or (`redefinition: change`) a change
    "full form (ABBR)" -> "ABBR" (the copy editors make that change about half the time).
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


_PAREN = re.compile(r"\(([^()]{1,40})\)")


def definitions(p: Paragraph) -> list[Definition]:
    out = []
    text = p.text
    for m in _PAREN.finditer(text):
        inner = m.group(1)
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
        out.append(Definition(p.id, sf_plain, lf, lf_start, m.start(1) + inner.find(sf), m.end(), m.start()))
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

    paras = body_paragraphs(ms)
    ab = next((i for i, p in enumerate(paras) if _ABSTRACT.match(p.text)), None)
    lo = 0 if ab is None else ab + 1
    kw = next((i for i in range(lo, len(paras)) if _KEYWORDS.match(paras[i].text)), None) if ab is not None else None
    # A structured abstract may have its own "Background" / "Methods" lines: look after the key words.
    lo = kw + 1 if kw is not None else lo
    main = next((i for i in range(lo, len(paras)) if _main_heading(paras[i])), None)
    if ab is None:
        return [paras[main:]] if main is not None else [paras]
    if main is None:
        return [paras[ab:kw + 1], paras[kw + 1:]] if kw is not None else [paras[ab:]]
    if kw is not None and kw + 1 < main:
        return [paras[ab:kw + 1], paras[kw + 1:main], paras[main:]]
    return [paras[ab:main], paras[main:]]


_USE = re.compile(r"(?<![\w\-/.])([A-Za-z0-9][A-Za-z0-9\-/&]*[A-Za-z0-9]|[A-Z])(?![\w\-/])")


def _abbr_key(tok: str) -> str | None:
    """The abbreviation a token stands for ("RCTs" -> "RCT", "post-COVID" -> "COVID"), or None
    if it is not one (ordinary words, Roman numerals, codes such as "C6-T4" or "PM2", approval
    numbers such as "IEC/0078/2022")."""
    tok = re.sub(r"^(?:[a-z]+-)+", "", tok.rstrip("-"))  # lowercase prefixes: post-, non-, anti-
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


def uses(p: Paragraph):
    if p.text.strip().isupper() or is_numeric(p.text):
        return  # all-caps headings and number rows
    prot = protected_spans(p.text)
    for m in _USE.finditer(p.text):
        key = _abbr_key(m.group(1))
        if not key or overlaps(m.start(), m.end(), prot):
            continue
        if len(key) == 2 and _PLACE.search(p.text[:m.start()]):
            continue
        if m.start() == len(p.text) - len(p.text.lstrip()) and p.text[m.end():m.end() + 1] == ":" and key.isalpha():
            continue  # "KEYWORDS:" label
        yield key, m.start(1), m.end(1)


# --- check -----------------------------------------------------------------------------

def collect(ms: Manuscript, style: JournalStyle) -> tuple[list[Change], list[Query]]:
    cfg = style.get("copyedit.abbreviations", {}) or {}
    if not cfg.get("enabled", True):
        return [], []
    allow = set(common_abbreviations()) | set(cfg.get("allow") or [])
    allow_upper = {a.upper() for a in allow}
    changes: list[Change] = []
    queries: list[Query] = []
    queried: set[str] = set()
    for scope in scopes(ms):
        defs: dict[str, list[Definition]] = {}
        for p in scope:
            for d in definitions(p):
                defs.setdefault(d.short, []).append(d)
        order = {p.id: i for i, p in enumerate(scope)}
        first_def = {k: (order[v[0].para_id], v[0].sf_start) for k, v in defs.items()}
        first_use: dict[str, tuple[Paragraph, int, int]] = {}
        for p in scope:
            spans = [(d.start, d.end) for d in definitions(p)]
            for key, s, e in uses(p):
                if key in allow or key.upper() in allow_upper or overlaps(s, e, spans):
                    continue
                first_use.setdefault(key, (p, s, e))

        # Defined twice in one scope: shorten the later definitions to the abbreviation, or query.
        mode = cfg.get("redefinition", "query")
        for key, ds in defs.items():
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
            elif key not in queried and cfg.get("query_undefined", True) and not re.search(r"[\d/-]", key):
                # Codes with digits or hyphens (gene names, scales such as GDS-15) are rarely queried.
                queried.add(key)  # once per manuscript
                queries.append(Query(p.id, s, e, TEXT["abbrev.undefined"].format(abbr=key), "abbrev.undefined"))
    return changes, queries


def check(ms: Manuscript, style: JournalStyle) -> list[Change]:
    return collect(ms, style)[0]
