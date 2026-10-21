"""Deterministic style-edit rules. Each rule proposes Change records; nothing is applied here.

Rules work on the paragraph text view, so every proposal carries the exact old text it
replaces. Overlapping proposals are resolved by `run_all` in rule order.
"""

from __future__ import annotations

import re
from itertools import count

import difflib

from . import journals, refengine, references, renumber, rules_figures, rules_format, rules_sections
from .changes import Change, ChangeSet, Layer, Source
from .docmodel import Manuscript, Paragraph
from .style import JournalStyle
from . import rules_guide  # the guide's search-and-replace table

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
# A statistic letter standing alone right before the number: "p¹ = 0.129", "the p1 values", "n2 < 5".
_STAT_BEFORE = re.compile(r"(?<![\w.])[pPnNrRtFzZdUHI]$")


def _is_exponent(text: str, start: int) -> bool:
    """10⁹, mm², kg/m² and the like are powers, not citations."""
    return bool(_UNIT_BEFORE.search(text[max(0, start - 8):start]))


def _is_statistic_index(text: str, start: int, end: int) -> bool:
    """p¹, p2 (formatted superscript), n² after a lone statistic letter: an index, not a citation."""
    return bool(_STAT_BEFORE.search(text[max(0, start - 3):start])) and len(text[start:end].strip()) <= 2


def _sup_plain(s: str) -> str:
    return "".join(str(_SUP_DIGITS.index(c)) if c in _SUP_DIGITS else {"⁻": "-", "˒": ","}.get(c, c) for c in s)


# "[¹⁰]", "[¹3]": Unicode superscript digits typed inside square brackets (sometimes mixed with plain ones).
_BRACKETED_SUP = re.compile(r"\[([0-9⁰¹²³⁴⁵⁶⁷⁸⁹][0-9⁰¹²³⁴⁵⁶⁷⁸⁹\s,˒⁻–\-]*)\]")


def superscript_citations(p: Paragraph) -> list[tuple[int, int, str]]:
    """(start, end, numbers as plain text) for citations written as superscript: Unicode
    superscript digits ("worldwide.¹") or digits formatted superscript. Powers ("10⁹", "mm²")
    and statistic indices ("p¹ = 0.12") are skipped; front matter is the caller's business."""
    out = []
    taken = []
    for m in _BRACKETED_SUP.finditer(p.text):
        if any(c in _SUP_DIGITS for c in m.group(1)):
            out.append((m.start(), m.end(), _sup_plain(m.group(1))))
            taken.append((m.start(), m.end()))
    for m in _UNICODE_SUP.finditer(p.text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        if _is_exponent(p.text, m.start()) or _is_statistic_index(p.text, m.start(), m.end()):
            continue
        if p.text[m.end():m.end() + 1].isdigit() or p.text[m.start() - 1:m.start()].isdigit() and m.start():
            continue  # part of a longer number written half in superscript
        out.append((m.start(), m.end(), _sup_plain(m.group(0))))
    for s, e in p.superscript:
        if _PLAIN_NUMS.fullmatch(p.text[s:e]) and not _is_exponent(p.text, s):
            lead = len(p.text[s:e]) - len(p.text[s:e].lstrip())
            s2, e2 = s + lead, s + len(p.text[s:e].rstrip())
            if _is_statistic_index(p.text, s2, e2) or any(a <= s2 < b for a, b in taken):
                continue
            out.append((s2, e2, p.text[s:e].strip()))
    return sorted(out)


def front_matter_ids(ms: Manuscript) -> set[str]:
    """Title page, author line, affiliations, correspondence (and cover letters before them):
    paragraphs before the abstract / main text, where superscript numbers are author and
    affiliation markers, never citations. Long paragraphs are left out, so a manuscript whose
    body has no recognisable start does not lose its citations."""
    cached = getattr(ms, "_front_matter_ids", None)
    if cached is not None:
        return cached
    from . import frontmatter

    start = next((i for i, p in enumerate(ms.paragraphs)
                  if not p.in_table and frontmatter._BODY_START.match(p.text)), None)
    if start is None:
        # No abstract or main heading: only title-page-like lines (no running sentences).
        ids = {p.id for p in frontmatter.leading_paragraphs(ms)
               if len(p.text.split()) < 40 and not re.search(r"[a-z)\]]{2}[.?!][⁰¹²³⁴⁵⁶⁷⁸⁹\d\[\],\-–]*\s+[A-Z]", p.text)
               and not _ROUND.search(p.text) and not _SQUARE.search(p.text)}
    else:
        ids = {p.id for p in ms.paragraphs[:start] if len(p.text) <= 400}
    ms._front_matter_ids = ids
    return ids


_ENUMERATION = re.compile(r"\(1\)\s+[a-z].*\(2\)\s+[a-z]", re.S)


def _round_not_citations(ms: Manuscript, found: list[tuple[Paragraph, re.Match]]) -> set[tuple[str, int]]:
    """Round-bracket numbers that are list markers or counts, not citations: "(1) a history of
    ... (2) recent ..."; and, in a manuscript that cites in square brackets or superscript,
    "RTA (77) followed by head injury (8)": in a paragraph with no other citation, round-bracket
    numbers count as citations only when they rise in order ("lens (33), ... ring (34)")."""
    out = set()
    n_other = sum(len(_SQUARE.findall(p.text)) + len(superscript_citations(p)) for p in body_paragraphs(ms))
    dominant = n_other >= len(found)
    by_para: dict[str, list[re.Match]] = {}
    paras = {}
    for p, m in found:
        by_para.setdefault(p.id, []).append(m)
        paras[p.id] = p
    for pid, matches in by_para.items():
        p = paras[pid]
        if _ENUMERATION.search(p.text):
            out.update((pid, m.start()) for m in matches
                       if m.group(2) in "123456789" and re.match(r"\s+[a-z]", p.text[m.end():]))
        if not dominant or _SQUARE.search(p.text) or superscript_citations(p):
            continue
        nums = [int(m.group(2)) for m in matches if m.group(2).isdigit()]
        if any(y <= x for x, y in zip(nums, nums[1:])):
            out.update((pid, m.start()) for m in matches)
    return out


def rule_citations(ms: Manuscript, style: JournalStyle) -> list[Change]:
    refs = references.extract(ms)
    n_refs = max((r.number or 0 for r in refs), default=len(refs)) or len(refs)
    if not n_refs:
        return []
    sup = rules_format.citation_format(style)
    front = front_matter_ids(ms)
    # The body and the tables (wherever they are; their notes too): "Garg et al., 2018[10]".
    paras = [p for p in renumber.citation_paragraphs(ms)
             if not p.style.lower().startswith("heading") and p.id not in front]
    candidates: list[tuple[Paragraph, re.Match, bool]] = []
    for p in paras:
        for pattern, is_round in ((_ROUND, True), (_SQUARE, False)):
            if is_round and (style.get("citations.brackets") != "square" or p.in_table):
                continue  # in tables, "Male (12)" is a count
            for m in pattern.finditer(p.text):
                prev = p.text[:m.start()].rstrip()
                if not prev:  # list numbering at paragraph start
                    continue
                if re.search(r"(\d|%|\b(table|tables|fig|figure|figures|ref|reference|n|no|eq|equation)\.?)$", prev, re.I):
                    continue  # statistics "45 (12)" or "Table (2)"-style labels, not citations
                candidates.append((p, m, is_round))
    not_cited = _round_not_citations(ms, [(p, m) for p, m, r in candidates if r])
    out = []
    for p, m, is_round in candidates:
        if is_round and (p.id, m.start()) in not_cited:
            continue
        lead, inner, punct = m.group(1), m.group(2), m.group(3)
        start = m.start()
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
    for p in paras:
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
    in_abstract = rules_sections.abstract_ids(ms)  # "Results" there is an abstract sub-heading
    for p in ms.paragraphs:
        text = p.text
        if p.in_table or len(text) > 60 or text.lstrip().startswith("<H") or p.id in in_abstract:
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
            for m in re.finditer(r":\s([a-z])(?!ttps?:|ttp:|ww\.)", text[authors_end:stop]):  # not "from: http://"
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
        elif res.formatted and res.formatted != ref.text:  # books, links, reports: template fixes
            out += word_diff(ms.by_id(ref.para_id), ref.offset, ref.text, res.formatted, "ref_other")
    return out


RULES = {
    "heading_tags": rule_heading_tags,
    "renumber": renumber.rule_renumber,  # before "citations": renumbered citations win the overlap
    "sections_headings": rules_sections.rule_headings,
    "sections_abstract": rules_sections.rule_abstract,
    "sections_keywords": rules_sections.rule_keywords,
    "sections_back_matter": rules_sections.rule_back_matter,
    "citations": rule_citations,
    "fig_citations": rules_figures.rule_citations,
    "fig_captions": rules_figures.rule_captions,
    "statistics": rule_statistics,
    "ref_lookup": rule_ref_lookup,
    "references": rule_references,
    "formatting": rules_format.rule_formatting,  # before guide_replace: its "et al" -> "et al." is also italic
    "guide_replace": rules_guide.rule_guide,
}

# Front matter runs first: it rewrites whole title-page paragraphs (edit_os/frontmatter.py).
from . import frontmatter  # noqa: E402  (frontmatter imports nothing from this module)
RULES = {"front_matter": frontmatter.rule_front_matter, **RULES}


def _opens_paragraph(c: Change) -> bool:
    from .tracked import PARAGRAPH_BREAK
    return c.new.startswith(PARAGRAPH_BREAK)


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
            # Two insertions at one point clash, unless exactly one opens new paragraphs.
            same_point = any(k.para_id == c.para_id and k.start == k.end == c.start == c.end
                             and _opens_paragraph(k) == _opens_paragraph(c) for k in kept)
            if not clash and not same_point:
                kept.append(c)
    # Formatting on text that a kept change replaces is moot (the replacement carries its own
    # format), and showing it would split one edit into two suggestions.
    replaced = [k for k in kept if not k.format_only and k.end > k.start]
    kept = [c for c in kept if not (c.format_only and any(
        t.para_id == c.para_id and t.start <= c.start and c.end <= t.end for t in replaced))]
    return ChangeSet(kept)
