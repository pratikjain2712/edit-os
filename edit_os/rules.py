"""Deterministic style-edit rules. Each rule proposes Change records; nothing is applied here.

Rules work on the paragraph text view, so every proposal carries the exact old text it
replaces. Overlapping proposals are resolved by `run_all` in rule order.
"""

from __future__ import annotations

import re
from itertools import count

import difflib

from . import journals, refengine, references, rules_format
from .changes import Change, ChangeSet, Layer, Source
from .docmodel import Manuscript, Paragraph
from .style import JournalStyle

_ids = count(1)


def _change(p: Paragraph, start: int, end: int, new: str, layer: Layer, rule: str,
            fmt: dict | None = None) -> Change:
    return Change(id=f"r{next(_ids)}", para_id=p.id, start=start, end=end, old=p.text[start:end],
                  new=new, layer=layer, source=Source.RULE, reason=rule, format=fmt)


def body_paragraphs(ms: Manuscript) -> list[Paragraph]:
    """Paragraphs before the reference list, outside tables."""
    end = references.find_heading(ms)
    paras = ms.paragraphs if end is None else ms.paragraphs[:end]
    return [p for p in paras if not p.in_table and p.text.strip()]


# --- citations -----------------------------------------------------------------------

_NUMS = r"\d{1,3}(?:\s*[,–—\-]\s*\d{1,3})*"
_ROUND = re.compile(r"(\s*)\((" + _NUMS + r")\)([.,;:]?)")
_SQUARE = re.compile(r"(\s*)\[(" + _NUMS + r")\]([.,;:]?)")


def normalise_citation(inner: str, style: JournalStyle) -> str | None:
    parts = []
    for item in re.split(r"\s*,\s*", inner.strip()):
        m = re.fullmatch(r"(\d+)\s*[–—\-]\s*(\d+)", item)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if b <= a:
                return None  # not a citation range
            if b == a + 1 and style.get("citations.two_number_range_as_list", True):
                parts += [str(a), str(b)]
            else:
                parts.append(f"{a}{style.get('citations.range_dash', '-')}{b}")
        elif item.isdigit():
            parts.append(str(int(item)))
        else:
            return None
    return style.get("citations.multiple_delimiter", ",").join(parts)


def _max_number(nums: str) -> int:
    return max(int(n) for n in re.findall(r"\d+", nums))


_SUP_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_UNICODE_SUP = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+(?:\s*[,˒⁻–\-]\s*[⁰¹²³⁴⁵⁶⁷⁸⁹]+)*")
_PLAIN_NUMS = re.compile(r"\s*\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*\s*")


_UNIT_BEFORE = re.compile(r"(?:\b(?:m|cm|mm|km|μm|um|nm|dm|in|ft|kg/m|g/m|/m|/cm|/mm)|[\d^×x])\s*$")


def _is_exponent(text: str, start: int) -> bool:
    """10⁹, mm², kg/m² and the like are powers, not citations."""
    return bool(_UNIT_BEFORE.search(text[max(0, start - 8):start]))


def superscript_citations(p: Paragraph) -> list[tuple[int, int, str]]:
    """(start, end, numbers as plain text) for citations written as superscript: Unicode
    superscript digits ("worldwide.¹") or digits formatted superscript. Powers are skipped."""
    out = []
    for m in _UNICODE_SUP.finditer(p.text):
        if _is_exponent(p.text, m.start()):
            continue
        plain = "".join(str(_SUP_DIGITS.index(c)) if c in _SUP_DIGITS else {"⁻": "-", "˒": ","}.get(c, c)
                        for c in m.group(0))
        out.append((m.start(), m.end(), plain))
    for s, e in p.superscript:
        if _PLAIN_NUMS.fullmatch(p.text[s:e]) and not _is_exponent(p.text, s):
            lead = len(p.text[s:e]) - len(p.text[s:e].lstrip())
            out.append((s + lead, s + len(p.text[s:e].rstrip()), p.text[s:e].strip()))
    return sorted(out)


def rule_citations(ms: Manuscript, style: JournalStyle) -> list[Change]:
    refs = references.extract(ms)
    n_refs = max((r.number or 0 for r in refs), default=len(refs)) or len(refs)
    if not n_refs:
        return []
    sup = rules_format.citation_format(style)
    out = []
    for p in body_paragraphs(ms):
        if p.style.lower().startswith("heading"):
            continue
        for pattern, is_round in ((_ROUND, True), (_SQUARE, False)):
            if is_round and style.get("citations.brackets") != "square":
                continue
            for m in pattern.finditer(p.text):
                lead, inner, punct = m.group(1), m.group(2), m.group(3)
                start = m.start()
                prev = p.text[:start].rstrip()
                if not prev:  # list numbering at paragraph start
                    continue
                if re.search(r"(\d|%|\b(table|tables|fig|figure|figures|ref|reference|n|no|eq|equation)\.?)$", prev, re.I):
                    continue  # statistics "45 (12)" or "Table (2)"-style labels, not citations
                if _max_number(inner) > n_refs:
                    continue
                norm = normalise_citation(inner, style)
                if norm is None:
                    continue
                if style.get("citations.after_punctuation", True) and punct:
                    new = f"{punct}[{norm}]"
                else:
                    new = f"[{norm}]{punct}"
                old = m.group(0)
                if new == old:
                    continue
                if sup and punct and new.startswith(punct + "["):  # bracket as its own insertion, superscript
                    out.append(_change(p, start, m.end(), punct, Layer.REFERENCES, "citations"))
                    out.append(_change(p, m.end(), m.end(), new[len(punct):], Layer.REFERENCES, "citations", sup))
                elif sup:
                    out.append(_change(p, start, m.end() - len(punct), new[:len(new) - len(punct)],
                                       Layer.REFERENCES, "citations", sup))
                else:
                    out.append(_change(p, start, m.end(), new, Layer.REFERENCES, "citations"))
        # Superscript citations: "worldwide¹." -> "worldwide.[1]" (brackets keep the run's formatting).
        for s, e, nums in superscript_citations(p):
            if s == 0 or _max_number(nums) > n_refs:
                continue
            norm = normalise_citation(nums, style)
            if norm is None:
                continue
            nxt = p.text[e:e + 1]
            if style.get("citations.after_punctuation", True) and nxt in ".,;:":
                out.append(_change(p, s, s, nxt, Layer.REFERENCES, "citations"))   # punctuation before
                out.append(_change(p, s, e, f"[{norm}]", Layer.REFERENCES, "citations", sup))
                out.append(_change(p, e, e + 1, "", Layer.REFERENCES, "citations"))  # and not after
            else:
                out.append(_change(p, s, e, f"[{norm}]", Layer.REFERENCES, "citations", sup))
    return out


# --- headings ------------------------------------------------------------------------

_NUMBERING = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+")


def rule_heading_tags(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not style.get("headings.tag"):
        return []
    levels = {name: "H1" for name in style.get("headings.h1", [])}
    levels.update({name: "H2" for name in style.get("headings.h2", [])})
    out = []
    for p in ms.paragraphs:
        text = p.text
        if p.in_table or len(text) > 60 or text.lstrip().startswith("<H"):
            continue
        core = _NUMBERING.sub("", text).strip()
        key = core.rstrip(":").strip().lower()
        level = levels.get(key)
        if not level:
            continue
        lead = len(text) - len(text.lstrip())
        out.append(_change(p, lead, lead, f"<{level}>", Layer.STRUCTURE, "heading_tags"))
        if style.get("headings.strip_trailing_colon", True) and text.rstrip().endswith(":"):
            i = len(text.rstrip()) - 1
            out.append(_change(p, i, i + 1, "", Layer.STRUCTURE, "heading_tags"))
    return out


# --- statistics ----------------------------------------------------------------------

_OPERATOR = re.compile(r"(?<=[A-Za-z0-9)\]])(\s*)([=<>≤≥])(\s*)(?=[-−–]?\d|\.\d)")


def rule_statistics(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not style.get("statistics.space_around_operators"):
        return []
    out = []
    for p in body_paragraphs(ms):
        if "http" in p.text or "<" in p.text and ">" in p.text and re.search(r"<\w+>", p.text):
            continue
        for m in _OPERATOR.finditer(p.text):
            new = f" {m.group(2)} "
            if m.group(0) != new:
                out.append(_change(p, m.start(), m.end(), new, Layer.NUMBERS, "statistics"))
    return out


# --- reference list ------------------------------------------------------------------

# End of the author list: a period after an initial, followed by the start of the title.
_AUTHORS_END = re.compile(r"(?<=[A-Z])\.\s+(?=[A-Z0-9\"“‘'(])|\bet al\.?\s+")
_INITIAL_PERIOD = re.compile(r"(?<=\b[A-Z])\.(?=\s*,|\s*[A-Z]\b)")
_YEAR_VOL = re.compile(r"(?P<sep>\.?\s*)(?P<year>(?:19|20)\d{2})\s*;\s*(?P<vol>\d+)\s*(?P<issue>\([^)]*\))?\s*(?=:)")
_ET_AL_DOUBLE = re.compile(r"\bet al\.\.")
_PAGES = re.compile(r":\s*([A-Za-z]?)(\d+)\s*[–—\-]\s*([A-Za-z]?)(\d+)(?=\.?\s*$)")


def _compress(first: str, last: str) -> str:
    if len(last) != len(first) or int(last) <= int(first):
        return last
    i = 0
    while i < len(first) - 1 and first[i] == last[i]:
        i += 1
    return last[i:]


def _journal_span(text: str, lo: int, hi: int):
    """Find the journal name ending at `hi` (just before the year): try each sentence boundary
    from the furthest back, so "J. Orthop. Rehabil." wins over "Rehabil."."""
    window = max(lo, hi - 150)
    starts = [window + m.end() for m in re.finditer(r"[.?!]\s+", text[window:hi])]
    for start in starts:
        name = text[start:hi].rstrip(" .,")
        if not name or len(name) > 120:
            continue
        journal = journals.directory().lookup(name)
        if journal:
            return start, start + len(name), journal
    return None


def rule_references(ms: Manuscript, style: JournalStyle) -> list[Change]:
    out = []
    for ref in references.extract(ms):
        p = ms.by_id(ref.para_id)
        base = ref.offset
        text = ref.text
        m_end = _AUTHORS_END.search(text)
        authors_end = m_end.start() if m_end else None

        if authors_end and style.get("references.initials_without_periods", True):
            for m in _INITIAL_PERIOD.finditer(text[:authors_end]):
                out.append(_change(p, base + m.start(), base + m.end(), "", Layer.REFERENCES, "ref_initials"))

        max_authors = style.get("references.max_authors")
        if authors_end and max_authors and "et al" not in text[:authors_end + 8]:
            names = [n for n in text[:authors_end].split(",") if n.strip()]
            if len(names) > max_authors:
                cut = len(",".join(names[:max_authors]))
                et_al = style.get("references.et_al", "et al.").rstrip(".")  # the list's own period follows
                out.append(_change(p, base + cut, base + authors_end, f", {et_al}", Layer.REFERENCES, "ref_et_al"))

        for m in _ET_AL_DOUBLE.finditer(text):
            out.append(_change(p, base + m.end() - 1, base + m.end(), "", Layer.REFERENCES, "ref_et_al"))

        ym = None
        for ym in _YEAR_VOL.finditer(text):
            pass  # the last year;volume is the journal citation
        if ym:
            if style.get("references.no_period_before_year") and ym.group("sep") != " " and ym.start() > 0:
                out.append(_change(p, base + ym.start("sep"), base + ym.end("sep"), " ", Layer.REFERENCES, "ref_year"))
            if style.get("references.drop_issue") and ym.group("issue"):
                out.append(_change(p, base + ym.end("vol"), base + ym.end("issue"), "", Layer.REFERENCES, "ref_issue"))

        if ym and style.get("references.journal_abbreviation") == "nlm" and journals.directory():
            j = _journal_span(text, authors_end or 0, ym.start())
            if j:
                start, end, journal = j
                abbr = journal.med_abbr
                if style.get("references.journal_drop_qualifier", True):
                    abbr = re.sub(r"\s*\([^)]*\)$", "", abbr)  # "Cancers (Basel)" -> "Cancers"
                if text[start:end] != abbr:
                    out.append(_change(p, base + start, base + end, abbr, Layer.REFERENCES, "ref_journal"))

        if style.get("references.capitalize_after_colon") and authors_end:
            stop = ym.start() if ym else len(text)
            for m in re.finditer(r":\s([a-z])", text[authors_end:stop]):
                i = authors_end + m.start(1)
                out.append(_change(p, base + i, base + i + 1, text[i].upper(), Layer.REFERENCES, "ref_title_case"))

        if style.get("references.page_range_compress", True):
            m = _PAGES.search(text)
            if m:
                first, last = m.group(2), m.group(4)
                new_last = _compress(first, last)
                new = f":{m.group(1)}{first}{style.get('references.page_range_dash', '-')}{m.group(3)}{new_last}"
                if new != m.group(0):
                    out.append(_change(p, base + m.start(), base + m.end(), new, Layer.REFERENCES, "ref_pages"))
    return out


def lookup_references(ms: Manuscript, style: JournalStyle) -> list[tuple[references.Reference, refengine.Resolution]]:
    """Resolve every reference against PubMed / CrossRef once per manuscript."""
    if not style.get("references.lookup"):
        return []
    cached = getattr(ms, "_ref_resolutions", None)
    if cached is None:
        refs = references.extract(ms)
        cached = list(zip(refs, refengine.resolve_all([r.text for r in refs], style)))
        ms._ref_resolutions = cached
    return cached


def word_diff(p: Paragraph, base: int, old: str, new: str, rule: str) -> list[Change]:
    """Turn a whole-entry rewrite into small word-level changes, so the tracked file stays readable."""
    tok = re.compile(r"\s+|\w+|[^\w\s]")
    a, b = tok.findall(old), tok.findall(new)
    starts, pos = [], 0
    for t in a:
        starts.append(pos)
        pos += len(t)
    starts.append(pos)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            out.append(_change(p, base + starts[i1], base + starts[i2], "".join(b[j1:j2]), Layer.REFERENCES, rule))
    return out


def rule_ref_lookup(ms: Manuscript, style: JournalStyle) -> list[Change]:
    out = []
    for ref, res in lookup_references(ms, style):
        if res.status == refengine.FORMAT_FIXED:
            out += word_diff(ms.by_id(ref.para_id), ref.offset, ref.text, res.formatted, "ref_lookup")
    return out


RULES = {
    "heading_tags": rule_heading_tags,
    "citations": rule_citations,
    "statistics": rule_statistics,
    "ref_lookup": rule_ref_lookup,
    "references": rule_references,
    "formatting": rules_format.rule_formatting,
}


def run_all(ms: Manuscript, style: JournalStyle, only: list[str] | None = None) -> ChangeSet:
    """Run rules in order; drop any proposal that overlaps one already kept."""
    kept: list[Change] = []
    resolved = set()  # reference paragraphs already rebuilt from PubMed / CrossRef
    for name, rule in RULES.items():
        if only and name not in only:
            continue
        if name == "references":
            resolved = {r.para_id for r, res in lookup_references(ms, style)
                        if res.status in (refengine.MATCHED, refengine.FORMAT_FIXED)}
        for c in rule(ms, style):
            if name == "references" and c.para_id in resolved:
                continue
            clash = any(k.para_id == c.para_id and c.start < k.end and k.start < c.end for k in kept)
            same_point = any(k.para_id == c.para_id and k.start == k.end == c.start == c.end for k in kept)
            if not clash and not same_point:
                kept.append(c)
    return ChangeSet(kept)
