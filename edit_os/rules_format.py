"""Formatting rules from the pre-editing guide: italic et al., Latin terms, P values and n,
superscript bibliography citations.

Each proposal is a `Change` with `format` set: formatting-only when old == new (rendered as
Word tracked formatting), or a text change whose inserted text gets the format (p -> P).
Reference entries are never touched, nor URLs and e-mail addresses.
"""

from __future__ import annotations

import re

from . import references
from .changes import Change, Layer
from .docmodel import Manuscript, Paragraph, run_text, text_runs
from .docmodel import _RPR as RPR
from .style import JournalStyle
from docx.oxml.ns import qn

_URL = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+\.\w+|\bdoi:\s*\S+", re.I)
_ET_AL = re.compile(r"\bet al\b(\.?)")
_LATIN = re.compile(r"\b(?:[Ii]n[ -](?:vivo|vitro|situ)|[Ee]x[ -]vivo)\b")
# p / P as a probability: "p < 0.05", "(p=.03)", "p value", "p-values". Not "p." / "pp." (pages),
# not part of a word or label ("p53", "Hb-p", "5p"), not a subscript-style "p_" form.
_P_VALUE = re.compile(
    r"(?<![\w.\-/'’])[pP](?=\s*(?:[<>=≤≥]\s*[-−]?\s*(?:0?[.·]\d|\d)|[- ]values?\b))")
# n / N as a sample-size variable: "n = 34", "(n=12)".
_N_VAR = re.compile(r"(?<![\w.\-/'’])[nN](?=\s*=\s*\d)")
_CITATION = re.compile(r"\[(\d{1,3}(?:\s*[,–—\-]\s*\d{1,3})*)\]")

_TOGGLE_TAGS = {"italic": qn("w:i"), "bold": qn("w:b"), "small_caps": qn("w:smallCaps")}


def char_formats(p: Paragraph) -> list[dict[str, bool]]:
    """Direct run formatting of each character of `p.text` (italic, bold, superscript, ...)."""
    out = []
    for r in text_runs(p.element):
        rpr = r.find(RPR)
        f = {}
        if rpr is not None:
            for prop, tag in _TOGGLE_TAGS.items():
                el = rpr.find(tag)
                f[prop] = el is not None and el.get(qn("w:val"), "true") not in ("0", "false", "off")
            va = rpr.find(qn("w:vertAlign"))
            val = va.get(qn("w:val")) if va is not None else None
            f["superscript"], f["subscript"] = val == "superscript", val == "subscript"
        out += [f] * len(run_text(r))
    return out


def _has(fmts: list[dict], start: int, end: int, prop: str) -> bool:
    return all(f.get(prop) for f in fmts[start:end])


# A reference entry outside the detected list: "2019;12:101", or an author list "Lin YJ, Wang Q,".
_REFERENCE_LIKE = re.compile(r"\b(?:19|20)\d{2}\s*;\s*\d+\s*(?:\([^)]*\))?\s*:|"
                             r"(?:\b[A-Z][\w'’\-]+ [A-Z]{1,3}[,.]\s*){2,}")


def _paragraphs(ms: Manuscript) -> list[Paragraph]:
    """Every paragraph except the reference heading and entries (body, tables, legends)."""
    skip = {r.para_id for r in references.extract(ms)}
    head = references.find_heading(ms)
    if head is not None:
        skip.add(ms.paragraphs[head].id)
    return [p for p in ms.paragraphs
            if p.id not in skip and p.text.strip() and not _REFERENCE_LIKE.search(p.text)]


def _blocked(p: Paragraph) -> list[tuple[int, int]]:
    return [m.span() for m in _URL.finditer(p.text)]


def _free(blocked, start: int, end: int) -> bool:
    return not any(s < end and start < e for s, e in blocked)


def _fmt(p: Paragraph, start: int, end: int, new: str, fmt: dict, layer: Layer, rule: str) -> Change:
    from .rules import _change  # rules registers this module; import late to avoid a cycle

    c = _change(p, start, end, new, layer, rule)
    c.format = dict(fmt)
    return c


def _on(style: JournalStyle, key: str) -> bool:
    return bool(style.get("formatting.enabled", True) and style.get(f"formatting.{key}", False))


def rule_formatting(ms: Manuscript, style: JournalStyle) -> list[Change]:
    out: list[Change] = []
    n_refs = 0
    if _on(style, "superscript_citations"):
        refs = references.extract(ms)
        n_refs = max((r.number or 0 for r in refs), default=len(refs)) or len(refs)

    for p in _paragraphs(ms):
        text, blocked = p.text, _blocked(p)
        fmts = char_formats(p)
        italic = {"italic": True}

        if _on(style, "et_al_italic"):
            for m in _ET_AL.finditer(text):
                if not _free(blocked, m.start(), m.end()):
                    continue
                if not m.group(1) and _on(style, "et_al_period"):
                    out.append(_fmt(p, m.start(), m.end(), "et al.", italic, Layer.HOUSE_STYLE, "fmt_et_al"))
                elif not _has(fmts, m.start(), m.end(), "italic"):
                    out.append(_fmt(p, m.start(), m.end(), m.group(0), italic, Layer.HOUSE_STYLE, "fmt_et_al"))

        if _on(style, "latin_italic"):
            for m in _LATIN.finditer(text):
                if _free(blocked, m.start(), m.end()) and not _has(fmts, m.start(), m.end(), "italic"):
                    out.append(_fmt(p, m.start(), m.end(), m.group(0), italic, Layer.HOUSE_STYLE, "fmt_latin"))

        if _on(style, "p_values"):
            for m in _P_VALUE.finditer(text):
                s, e = m.span()
                if not _free(blocked, s, e) or fmts[s:e] and fmts[s].get("subscript"):
                    continue
                if m.group(0) == "p" and style.get("formatting.p_capital", False):
                    out.append(_fmt(p, s, e, "P", italic, Layer.NUMBERS, "fmt_p_value"))
                elif not _has(fmts, s, e, "italic"):
                    out.append(_fmt(p, s, e, m.group(0), italic, Layer.NUMBERS, "fmt_p_value"))

        if _on(style, "n_variable_italic"):
            for m in _N_VAR.finditer(text):
                s, e = m.span()
                if _free(blocked, s, e) and not _has(fmts, s, e, "italic"):
                    out.append(_fmt(p, s, e, m.group(0), italic, Layer.NUMBERS, "fmt_n"))

        if n_refs:
            for m in _CITATION.finditer(text):
                s, e = m.span()
                if not text[:s].strip() or not _free(blocked, s, e):
                    continue  # "[1] ..." at the start of a paragraph is a list label
                if max(int(n) for n in re.findall(r"\d+", m.group(1))) > n_refs:
                    continue
                if not _has(fmts, s, e, "superscript"):
                    out.append(_fmt(p, s, e, m.group(0), {"superscript": True}, Layer.REFERENCES, "fmt_citations"))
    return out


def citation_format(style: JournalStyle) -> dict | None:
    """Format for citation brackets the citations rule inserts (None: leave as the text around)."""
    return {"superscript": True} if _on(style, "superscript_citations") else None
