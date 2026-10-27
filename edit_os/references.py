"""Find the reference list in a manuscript and split it into entries.

Each Word paragraph is one entry, so a reference that wraps across lines stays
one reference. Leading labels ("1.", "[1]", "(1)", "1)") are recorded and kept
out of the entry text so edits target the reference itself.

A paragraph that only continues the entry above it -- a "doi:..." or PMID line, or the
rest of an entry broken by a hard return ("Expert Rev Respir" / "Med. 2021;15:931-48.") --
is not an entry of its own: it is joined to the previous entry as `continuation`, which the
lookup reads with the entry (`Reference.full_text`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .docmodel import Manuscript

_HEADING = re.compile(
    r"^\s*(?:<H\d>)?\s*(?:(?:\d{1,2}|[IVX]{1,4})\s*[.)]\s*)?"
    r"(references?|reference list|bibliography|literature cited|works cited|cited literature)\s*[:.\-–—_]*\s*$",
    re.I,
)
# A misspelt heading ("REFERNCES", "Refrences", "Bibliograpy"): one word within two edits of these.
_HEADING_WORDS = ("references", "reference", "bibliography")
_HEADING_WORD = re.compile(r"^\s*(?:<H\d>)?\s*(?:(?:\d{1,2}|[IVX]{1,4})\s*[.)]\s*)?([A-Za-z]{7,14})\s*[:.\-–—_]*\s*$")


def _edits(a: str, b: str) -> int:
    """Edit distance (a swap of two neighbouring letters counts as one edit)."""
    d = [[i + j if not i * j else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def is_heading(text: str) -> bool:
    """Whether a paragraph's text is a reference-list heading: "References", "REFERENCES:", "References-",
    "7. References", or a misspelling of it ("REFERNCES": IJO 505) -- one word, same first letter, within one edit
    of "reference(s)" / "bibliography" (two when it starts as they do: "Refrences", "Bibliograpy")."""
    if _HEADING.match(text):
        return True
    m = _HEADING_WORD.match(text)
    if not m:
        return False
    w = m.group(1).lower()
    if w in ("reverence", "reverences", "referee", "referees", "referral", "referrals", "inference", "inferences"):
        return False  # words of their own
    return any(w[0] == k[0] and _edits(w, k) <= (2 if len(k) >= 10 and len(w) >= 9 and w[:3] == k[:3] else 1)
               for k in _HEADING_WORDS)
# Sections that commonly follow the reference list.
_STOP = re.compile(
    r"^\s*(tables?\b|table\s+\d|figures?\b|figure\s+\d|fig\.?\s*\d|figure legends?|legends?\b"
    r"|appendix|appendices|supplementary|acknowledg|tables? and figures)",
    re.I,
)
# (a bare number is a label, but not a year opening the rest of an entry: "2025 Feb;39:238-50.")
_LABEL = re.compile(r"^\s*(\[\d+\]|\(\d+\)|\d+[.)]|(?!(?:19|20)\d{2}\s+(?:[A-Z][a-z]{2}|\d))\d+(?=\s))\s*")
# Not an entry: Word's form-field markers ("Bottom of Form") and rule lines ("______") under the list.
_NOT_ENTRY = re.compile(r"^\s*(?:(?:top|bottom)\s+of\s+form|[_\-–—=*~.\s]+)\s*$", re.I)
# "Riazi-Esfahani H, Masoomian B, ...": a surname and initials, as a reference entry starts.
_LOOKS_LIKE_REFERENCE = re.compile(r"^\s*[A-Z][\w'’\-]+(?:\s[A-Z][\w'’\-]+)?\s[A-Z]{1,3}[,.]")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
# "doi:10.1016/...", "https://doi.org/10...", "10.4103/...", "PMID: 123; PMCID: PMC456": identifiers only.
_ID_ONLY = re.compile(
    r"^\s*[.,;]?\s*(?:(?:doi\s*:?\s*|https?://(?:dx\.)?doi\.org/|doi\.org/)?10\.\d{4,9}/\S+"
    r"|(?:PMID|PMCID)\s*:?\s*\S+)(?:\s*[.;,]?\s*(?:(?:PMID|PMCID)\s*:?\s*\S+|\[?Epub[^\]]*\]?))*\s*\.?\s*$", re.I)
_URL_ONLY = re.compile(r"^\s*(?:https?://|www\.)\S+\s*$", re.I)
# The citation part of an entry: "2021;15(8):931-48", "2019;6:242", "2002; 11: 346".
_CITATION = re.compile(r"\b(?:19|20)\d{2}[^;:\n]{0,20};\s*\d+\s*(?:\([^)]*\))?\s*:\s*[A-Za-z]?\d+")
_PARTICLE_START = re.compile(r"^\s*(?:(?:van|von|de|der|den|da|di|del|della|dos|das|du|la|le|ten|ter|al|el)[\s-]+)+[A-Z]")
_APA_START = re.compile(r"^\s*[A-Z][\w'’\-]+(?:\s[A-Z][\w'’\-]+)?,\s*(?:[A-Z]\.\s*-?)+[,(&]")
_NOTE = re.compile(r"^\s*(?:conflicts? of interests?|funding|financial support|sources? of support|IEC|ethic|"
                   r"declaration|data availability|author'?s?'? contributions?)\b", re.I)
_AUTHORS_ONLY = re.compile(r"^\s*(?:[A-Z][\w'’\-]+(?:\s[A-Z][\w'’\-]+)?\s[A-Z]{1,3}\s*,\s*)+"
                           r"(?:[A-Z][\w'’\-]+(?:\s[A-Z][\w'’\-]+)?\s[A-Z]{1,3}|et al)\s*[.,]?\s*$")
_PAGES_OR_YEAR = re.compile(r"\b(?:19|20)\d{2}\b|\d+\s*[-–]\s*\d+")
# The rest of an entry: the journal and its citation ("Trans Sect Ophthalmol 1977; 83:763-70.", "BMC Ophthalmol.
# 2024 Jan 18; 24:24.") or the date opening it ("2024 Apr 17; 16."), on a paragraph of its own.
_CITATION_TAIL = re.compile(r"^[^;:]{0,90}?\b(?:19|20)\d{2}\b[^;:\n]{0,20};\s*\d+\s*(?:\([^)]*\))?\s*(?::\s*[A-Za-z]?\d+[^;]*)?\.?\s*$")
_DATE_START = re.compile(r"^\s*(?:19|20)\d{2}\s+(?:[A-Z][a-z]{2}\b|\d)")
# The author list broken off before the last initials: "..., Ben Yahia S, Zaouali" / "S, et al. Final diagnosis ..."
_INITIALS_START = re.compile(r"^\s*[A-Z]{1,3}\s*(?:,|\.\s|\s+[A-Z])")


@dataclass
class Reference:
    para_id: str
    number: int | None
    label: str  # e.g. "12. "; empty for auto-numbered or author-year lists
    text: str  # entry without the label
    offset: int  # where `text` starts in the paragraph's text view
    continuation: list[tuple[str, str]] = field(default_factory=list)  # (para_id, text) joined to this entry
    unlabelled: bool = False  # numbered in sequence after the typed labels, which the author left off (IJO_2904)

    @property
    def full_text(self) -> str:
        """The entry with the paragraphs that continue it: what the lookup reads."""
        return " ".join([self.text] + [t.strip() for _, t in self.continuation])


def continues(prev: str, raw: str) -> bool:
    """`raw` (a paragraph without a label) is the rest of the entry `prev`, not an entry of its own:
    identifiers only (doi / PMID), a link after "Available from:", or the tail of an entry broken by
    a hard return -- `prev` has no citation yet ("...; 2019;6:242") and `raw` brings one, starting
    lower case, with a year, or after an unfinished `prev`."""
    if _ID_ONLY.match(raw):
        return True
    if _URL_ONLY.match(raw):
        return bool(re.search(r"(?:available (?:from|at)|url)\s*:?\s*$", prev, re.I))
    if _NOTE.match(raw) or _NOTE.match(prev) or len(prev.split()) < 4:
        return False  # "IEC approval: Yes, 2022" after "Funding received: None"
    if _CITATION.search(prev) or _LOOKS_LIKE_REFERENCE.match(raw) or _PARTICLE_START.match(raw) \
            or _APA_START.match(raw):
        return False
    if _LOOKS_LIKE_REFERENCE.match(prev) and not _YEAR.search(prev):
        # authors and title above, no citation yet: the journal line, or the rest of the author list
        if _CITATION_TAIL.match(raw) or _DATE_START.match(raw) and len(raw) < 60:
            return True
        if not prev.rstrip().endswith((".", "?", "!")) and _INITIALS_START.match(raw):
            return True
    if _AUTHORS_ONLY.match(prev) and not _YEAR.search(prev):
        return True  # "Siransy L, Dembele B, Sekongo Y." on a line of its own: the title follows
    if not _PAGES_OR_YEAR.search(raw) or len(raw) > 200:
        return False
    if prev.rstrip().endswith((":", ",", ";", "(", "-", "–")):
        return True
    if _YEAR.search(prev):
        return False
    start = raw.lstrip()[:1]
    unfinished = not prev.rstrip().endswith((".", "?", "!"))
    return start.islower() or unfinished or bool(re.match(r"\s*(?:19|20)\d{2}\s*[;:(,]", raw))


def ends_list(raw: str, refs: list[Reference]) -> bool:
    """`raw`, an unlabelled paragraph that does not continue the entry above, is back matter after a
    numbered list ("Declaration of patient consent", "Conflicts of interest: None"), not an entry."""
    if _NOTE.match(raw):
        return not _LOOKS_LIKE_REFERENCE.match(raw)
    if not refs or any(r.number is None for r in refs) or continues(refs[-1].full_text, raw):
        return False  # an author-year list: every entry is unlabelled
    return not (_YEAR.search(raw) or _LOOKS_LIKE_REFERENCE.match(raw) or _ID_ONLY.match(raw) or _URL_ONLY.match(raw))


def find_heading(ms: Manuscript) -> int | None:
    """Index of the reference-list heading; the last match wins (skips contents pages)."""
    hits = [i for i, p in enumerate(ms.paragraphs) if not p.in_table and is_heading(p.text)]
    return hits[-1] if hits else None


def list_start(ms: Manuscript, p) -> int:
    """The number Word gives the first item of `p`'s automatic list (its level's start value, or the list's override);
    1 when it cannot be read."""
    from docx.oxml.ns import qn

    num_pr = p.element.find(f"{qn('w:pPr')}/{qn('w:numPr')}")
    try:
        num_id = num_pr.find(qn("w:numId")).get(qn("w:val"))
        ilvl_el = num_pr.find(qn("w:ilvl"))
        ilvl = ilvl_el.get(qn("w:val")) if ilvl_el is not None else "0"
        numbering = ms.document.part.numbering_part.element
    except (AttributeError, NotImplementedError, KeyError):
        return 1
    for num in numbering.findall(qn("w:num")):
        if num.get(qn("w:numId")) != num_id:
            continue
        for ov in num.findall(qn("w:lvlOverride")):
            so = ov.find(qn("w:startOverride"))
            if ov.get(qn("w:ilvl")) == ilvl and so is not None and (so.get(qn("w:val")) or "").isdigit():
                return int(so.get(qn("w:val")))
        abstract = num.find(qn("w:abstractNumId")).get(qn("w:val"))
        for an in numbering.findall(qn("w:abstractNum")):
            if an.get(qn("w:abstractNumId")) != abstract:
                continue
            for lvl in an.findall(qn("w:lvl")):
                st = lvl.find(qn("w:start"))
                if lvl.get(qn("w:ilvl")) == ilvl and st is not None and (st.get(qn("w:val")) or "").isdigit():
                    return max(1, int(st.get(qn("w:val"))))
    return 1


def extract(ms: Manuscript) -> list[Reference]:
    start = find_heading(ms)
    if start is None:
        return []
    refs: list[Reference] = []
    auto = 0
    for p in ms.paragraphs[start + 1:]:
        if p.in_table:
            continue
        raw = p.text
        if not raw.strip() or _NOT_ENTRY.match(raw):
            continue
        if _STOP.match(raw) or p.style.lower().startswith("heading") and not (
                p.numbered or _LABEL.match(raw) or _LOOKS_LIKE_REFERENCE.match(raw)):
            break  # (a numbered entry the author styled as a heading is still an entry)
        m = _LABEL.match(raw)
        if m and not raw[m.end():].strip():
            continue  # a stray label on a line of its own ("6."): no entry
        if not m and not p.numbered and ends_list(raw, refs):
            break  # "Declaration of patient consent" and its text after the list
        if not m and refs and continues(refs[-1].full_text, raw):
            refs[-1].continuation.append((p.id, raw))
            continue
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            number, label = int(digits), m.group(0)
        elif p.numbered or auto and _LOOKS_LIKE_REFERENCE.match(raw) and _YEAR.search(raw):
            # An entry the author left out of Word's numbered list still continues it.
            if not auto and p.numbered:
                auto = list_start(ms, p) - 1  # a Word list set to start at 11 after typed "1."-"10." (IJO_2904)
            auto += 1
            number, label = auto, ""
        elif refs and refs[-1].number is not None and (refs[-1].label or refs[-1].unlabelled) \
                and _LOOKS_LIKE_REFERENCE.match(raw) and _YEAR.search(raw):
            # "10. Yang Q, ..." then "Collins GS, Moons KGM, ..." with no label: the list goes on (IJO_2904, entries
            # 11-14, cited as "11-13")
            number, label = refs[-1].number + 1, ""
            refs.append(Reference(para_id=p.id, number=number, label="", text=raw.rstrip(), offset=0, unlabelled=True))
            continue
        else:
            number, label = None, ""
        refs.append(Reference(
            para_id=p.id, number=number, label=label,
            text=raw[len(label):].rstrip(), offset=len(label),
        ))
    return refs
