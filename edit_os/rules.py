"""Deterministic style-edit rules. Each rule proposes Change records; nothing is applied here.

Rules work on the paragraph text view, so every proposal carries the exact old text it
replaces. Overlapping proposals are resolved by `run_all` in rule order.
"""

from __future__ import annotations

import re
from itertools import count

import difflib

from . import journals, provenance, refengine, references, renumber, rules_figures, rules_format, rules_sections
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
            if b <= a or a == 0:
                return None  # not a citation range ("items (0-2)" is a scale)
            if b == a + 1 and style.get("citations.two_number_range_as_list", True):
                parts += [str(a), str(b)]
            else:
                parts.append(f"{a}{style.get('citations.range_dash', '-')}{b}")
        elif item.isdigit() and int(item):
            parts.append(str(int(item)))
        else:
            return None  # "(0)" is never a reference number
    return style.get("citations.multiple_delimiter", ",").join(parts)


def _max_number(nums: str) -> int:
    return max(int(n) for n in re.findall(r"\d+", nums))


_SUP_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_UNICODE_SUP = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+(?:\s*[,˒⁻–\-]\s*[⁰¹²³⁴⁵⁶⁷⁸⁹]+)*")
_PLAIN_NUMS = re.compile(r"\s*\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*\s*")


# "10⁻⁸": the minus of a negative power stands between the base and the digits. A unit right before the
# number takes it as a power: "1.73 m2", "ng/mL²", "kg m-²", "kΩ⁻¹", "m/s²" (nothing between them: "365 nm.³"
# cites).
_UNITS = r"(?:m|cm|mm|km|μm|µm|um|nm|dm|in|ft|mL|ml|dL|dl|L|µL|μL|uL|kg|mg|g|Ω|kΩ)"
_UNIT_BEFORE = re.compile(r"(?:\b" + _UNITS + r"|/(?:" + _UNITS + r"|s|h|min)|kg/m|g/m|[\d^×x]|\d\s*[⁻−]"
                          r"|\b" + _UNITS + r"\s*[-−]|⁻)\s*$")
# A power in a formula: "n = (1.96)²", "p q/ d²", "n = N/(1 + Ne²)" ("mean 35 (1.69)¹²" and "Li et al. (2024)⁴"
# cite).
_FORMULA_BEFORE = re.compile(r"(?:[=+×÷*/^]\s*\(\s*\d+(?:\.\d+)?\s*\)|[=+×÷*^]\s*\(?[A-Za-z]{1,2}|/\s*[A-Za-z])\s*$")
# A statistic symbol or name standing alone right before the number: "p¹ = 0.129", "the p1 values", "n2 < 5",
# "Cohen's f²", "Q²predict", "X2, df", "Chi2", "ΔΧ²(10)", "eta²", "I2 statistics"; any Greek letter too
# ("η² = 0.42", "partial η²", "χ²") and a mathematical letter ("𝝌2-test"); "P within¹", "P for trend²".
_STAT_BEFORE = re.compile(r"(?:(?<![\w.])|(?<=[Δ∆]))(?:[pPnNrRtFzZdUHIXfQ\u0370-\u03ff\U0001D400-\U0001D7FF]"
                          r"|chi|Chi|CHI|eta|omega|df)$"
                          r"|(?<![\w.])[Pp][\s\-_]*(?:within|between|for\s+trend|trend|interaction|heterogeneity)$")
# A superscript followed by a comparison is an index or a power, never a citation: "Ne² =", "R²=0.4".
_OPERATOR_AFTER = re.compile(r"\s*[=<>≤≥]")


# A year standing alone right before the number: "Vision 2025⁹⁴", "et al., 2013⁸⁸" (table cells).
_YEAR_BEFORE = re.compile(r"(?:^|[\s,;(])(?:19|20)\d{2}$")


def _is_exponent(text: str, start: int) -> bool:
    """10⁹, mm², kg/m², ng/mL², (1.96)² and the like are powers, not citations; a year's superscript is a
    citation."""
    if _YEAR_BEFORE.search(text[max(0, start - 8):start]):
        return False
    before = text[max(0, start - 12):start]
    return bool(_UNIT_BEFORE.search(before) or _FORMULA_BEFORE.search(before))


def _is_statistic_index(text: str, start: int, end: int) -> bool:
    """p¹, p2 (formatted superscript), n², χ², Chi2, f² after a lone statistic symbol or name: an index or a
    power, not a citation."""
    if len(text[start:end].strip()) > 2:
        return False
    return bool(_STAT_BEFORE.search(text[max(0, start - 24):start]) or _OPERATOR_AFTER.match(text, end))


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


# A table note that explains a marker: "³ Not reported ...", "1N not missing", "1: DSM-5 criteria, 2: in months",
# "... failure. ¹: ICU admission diagnosis; ²: ...". The marker opens the note or follows a separator, and words
# follow it; plain digits count only with a colon or equals sign after them ("Model 1: adjusted" is not one).
_NOTE_SEP = r"(?:^\s*|[.;,]\s+|\s{2,}|\t)"
_NOTE_PLAIN_MARKER = re.compile(_NOTE_SEP + r"(\d{1,2})\s*[:=]\s*[^\W\d]")
# Unicode markers may follow a single space ("... (unweighted) ² n (%) ³ Pearson's ...") when a space follows too.
_NOTE_SUP_MARKER = re.compile(r"(?:" + _NOTE_SEP + r"([⁰¹²³⁴⁵⁶⁷⁸⁹]{1,2})\s*[:=.)]?\s*|(?<=\s)([⁰¹²³⁴⁵⁶⁷⁸⁹]{1,2})\s+)[^\W\d]")
_NOTES_AFTER_TABLE = 6  # non-empty paragraphs under a table read as its notes (up to a caption or the next table)


def _note_markers(p: Paragraph) -> list[tuple[int, int, int]]:
    """(start, end, number) of the footnote markers a table note explains (see _NOTE_SUP_MARKER)."""
    out = []
    for m in _NOTE_PLAIN_MARKER.finditer(p.text):
        out.append((m.start(1), m.end(1), int(m.group(1))))
    for m in _NOTE_SUP_MARKER.finditer(p.text):
        g = 1 if m.group(1) else 2
        out.append((m.start(g), m.end(g), int(_sup_plain(m.group(g)))))
    for s, e in p.superscript:  # digits formatted superscript: "1N not missing", "... failure. 1: ICU ..."
        lead = len(p.text[s:e]) - len(p.text[s:e].lstrip())
        s2, e2 = s + lead, s + len(p.text[s:e].rstrip())
        if re.fullmatch(r"\d{1,2}", p.text[s2:e2]) and re.search(_NOTE_SEP + r"$", p.text[:s2]) \
                and re.match(r"\s*[:=.)]?\s*[^\W\d]", p.text[e2:]):
            out.append((s2, e2, int(p.text[s2:e2])))
    return sorted(out)


# An abbreviation list under a table: "OR: Odds ratio; CI: ...", "BMI = body mass index".
_ABBREVIATION_NOTE = re.compile(r"^\s*[A-Za-z][\w/.′'\-]{0,15}\s*[:=]\s")


def _is_table_note(p: Paragraph) -> bool:
    """A paragraph under a table reads as one of its notes ("Note: ...", "* P<0.05", "OR: Odds ratio; ...",
    "¹ Not reported ..."); running text after the table does not, so its citations stay citations."""
    marks = _note_markers(p)
    return bool(renumber._TABLE_NOTE.match(p.text) or _ABBREVIATION_NOTE.match(p.text)
                or (marks and marks[0][0] == len(p.text) - len(p.text.lstrip())))


def table_note_markers(ms: Manuscript) -> set[tuple[str, int]]:
    """(paragraph id, start) of superscript numbers in tables and their notes that are footnote markers,
    not citations ("N = 4391 (100%)²", "p value³" under "² n (%) ³ Pearson's Chi-squared test"):
    - the table's notes (cells that open with a marker, and the paragraphs right under the table)
      explain that number; the markers in the notes themselves go too;
    - or the same number stands in three or more cells of the table ("Not reported³" down a column)
      in a manuscript whose text does not cite in superscript.
    A "Reference" column ("Garg et al., 2018¹⁰", numbers not explained by the notes) still cites."""
    cached = getattr(ms, "_table_note_markers", None)
    if cached is not None:
        return cached
    paras = ms.paragraphs
    groups: list[tuple[object, list[int]]] = []
    for i, p in enumerate(paras):
        if p.in_table:
            t = renumber._outer_table(p)
            if groups and groups[-1][0] is t:
                groups[-1][1].append(i)
            else:
                groups.append((t, [i]))
    body_sup = None
    out: set[tuple[str, int]] = set()
    for _, idxs in groups:
        cells = [paras[i] for i in idxs]
        notes = [p for p in cells if (m := _note_markers(p)) and m[0][0] == len(p.text) - len(p.text.lstrip())]
        k, seen = idxs[-1] + 1, 0
        while k < len(paras) and seen < _NOTES_AFTER_TABLE and not paras[k].in_table:
            q = paras[k]
            if q.text.strip():
                if rules_figures.caption_match(q) is not None or q.style.lower().startswith("heading"):
                    break
                if _is_table_note(q):
                    notes.append(q)
                seen += 1
            k += 1
        explained: set[int] = set()
        for q in notes:
            for s, e, n in _note_markers(q):
                explained.add(n)
                out.add((q.id, s))
        found = [(p, s, renumber.expand(nums) or []) for p in cells for s, e, nums in superscript_citations(p)]
        in_cells: dict[int, set[str]] = {}
        for p, s, nums in found:
            if len(nums) == 1:
                in_cells.setdefault(nums[0], set()).add(p.id)
        for p, s, nums in found:
            if not nums:
                continue
            if set(nums) <= explained:
                out.add((p.id, s))
                continue
            if len(nums) == 1 and len(in_cells.get(nums[0], ())) >= 3 \
                    and not _YEAR_BEFORE.search(p.text[:s].rstrip()) and not re.search(r"\bal\.?,?$", p.text[:s].rstrip()):
                if body_sup is None:
                    body = [q for q in body_paragraphs(ms) if q.id not in front_matter_ids(ms)]
                    n_sup = sum(len(superscript_citations(q)) for q in body)
                    n_other = sum(len(_SQUARE.findall(q.text)) + len(_ROUND.findall(q.text)) for q in body)
                    body_sup = n_sup > n_other
                if not body_sup:
                    out.add((p.id, s))
    ms._table_note_markers = out
    return out


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
# Numbered questions or items introduced by a colon: "three questions: (1) How do ... ? (2) What ...".
_ENUMERATION_COLON = re.compile(r"[:;]\s*\(1\)\s+[A-Za-z].*?\(2\)\s+[A-Za-z]", re.S)


def _other_citations(p: Paragraph) -> list[tuple[int, list[int]]]:
    """(offset, numbers) of the square-bracket and superscript citations in a paragraph."""
    out = [(m.start(), renumber.expand(m.group(2)) or []) for m in _SQUARE.finditer(p.text)
           if not not_citation_before(p.text[:m.start()].rstrip(), False)]
    out += [(s, renumber.expand(n) or []) for s, e, n in superscript_citations(p)]
    return sorted((s, n) for s, n in out if n)


def _round_not_citations(ms: Manuscript, found: list[tuple[Paragraph, re.Match]]) -> set[tuple[str, int]]:
    """Round-bracket numbers that are list markers or counts, not citations: "(1) a history of
    ... (2) recent ..."; and, in a manuscript that cites in square brackets or superscript,
    "RTA (77) followed by head injury (8)": in a paragraph with no other citation, round-bracket
    numbers count as citations only when they rise in order ("lens (33), ... ring (34)"); next to
    the paragraph's other citations, only when they fit their order ("[1] ... (2) ... [3]", not
    "[2] ... Klebsiella (3) ... [3]")."""
    out = set()
    n_other = sum(len(_other_citations(p)) for p in body_paragraphs(ms))
    dominant = n_other >= len(found)
    by_para: dict[str, list[re.Match]] = {}
    paras = {}
    for p, m in found:
        by_para.setdefault(p.id, []).append(m)
        paras[p.id] = p
    for pid, matches in by_para.items():
        p = paras[pid]
        # A list marker stands before its item, with no punctuation after the bracket: "(2) assessing",
        # not "engagement (5). Similarly".
        markers = [m for m in matches if len(m.group(2)) == 1 and m.group(2) != "0" and not m.group(3)]
        if _ENUMERATION.search(p.text):
            out.update((pid, m.start()) for m in markers if re.match(r"\s+[\"“‘']?[a-z]", p.text[m.end():]))
        if _ENUMERATION_COLON.search(p.text):
            out.update((pid, m.start()) for m in markers if re.match(r"\s+[\"“‘']?[A-Za-z]", p.text[m.end():]))
        if not dominant:
            continue
        others = _other_citations(p)
        if others:
            for m in matches:
                nums = renumber.expand(m.group(2)) or [0]
                before = [max(n) for s, n in others if s < m.start()]
                after = [min(n) for s, n in others if s > m.start()]
                if (before and min(nums) <= max(before)) or (after and max(nums) >= min(after)):
                    out.add((pid, m.start()))
            continue
        nums = [int(m.group(2)) for m in matches if m.group(2).isdigit()]
        if any(y <= x for x, y in zip(nums, nums[1:])):
            out.update((pid, m.start()) for m in matches)
    return out


# A full stop (or comma) that already ends the words before a citation: "gets older. (1).", "features.[1]."
# (not "et al. (5).": there the period is the abbreviation's).
_PUNCT_BEFORE = re.compile(r"(?:(?<![\w.])(?!(?:al|etc|vs|approx|fig|figs|ref|refs|no|eq)\b)[A-Za-z]{3,}|[\])%\d])[.,;:]\s*$",
                           re.I)


def _trailing_space(p: Paragraph, end: int, rule: str) -> list[Change]:
    """A citation restyled at the end of a paragraph takes the spaces after it with it:
    "glaucoma [5]. " -> "glaucoma.[5]" (style editors: 79 of 84 such paragraph ends, 160 articles)."""
    rest = p.text[end:]
    if not rest or rest.strip():
        return []
    return [_change(p, end, len(p.text), "", Layer.REFERENCES, rule)]


# What stands right before a bracketed number that makes it something other than a citation.
# A label: "Table (2)", "Fig. [3]", "Ref. 4", "n (12)".
_LABEL_BEFORE = re.compile(r"\b(?:table|tables|fig|figure|figures|ref|reference|n|no|eq|equation)\.?$", re.I)
# A test statistic's degrees of freedom: "t(48)", "F (1,57)", "F[4,164]", "r (198)", "χ²(2)", "df (3)".
_STAT_BEFORE_BRACKET = re.compile(r"(?:(?<![\w.])(?:[tFrHUWZQ]|χ²|χ2|Χ²|X²|X2)|\bdf)$")
# A number spelt out and repeated in figures: "six (6) weeks" (round brackets only).
_NUMBER_WORDS = {w: i for i, w in enumerate("one two three four five six seven eight nine ten eleven twelve".split(), 1)}


def not_citation_before(prev: str, is_round: bool, inner: str = "") -> bool:
    """Is a bracketed number after `prev` (the text before it, right-stripped) a label, a statistic or a
    count rather than a citation? One answer for the citation rule, renumbering and QC."""
    if not prev:
        return is_round  # "(1) ..." starting a paragraph is list numbering; "[23, 39]" alone in a table cell cites
    if _LABEL_BEFORE.search(prev) or _STAT_BEFORE_BRACKET.search(prev):
        return True
    word = re.search(r"([A-Za-z]+)$", prev)
    return is_round and bool(word) and inner.strip().isdigit() and _NUMBER_WORDS.get(word.group(1).lower()) == int(inner)


def _citation_candidates(ms: Manuscript, style: JournalStyle) -> tuple[int, list[Paragraph], list[tuple]]:
    """(number of references, the paragraphs whose citations count, (paragraph, match, is_round) for each
    bracketed number the citation rule reads as a citation)."""
    cached = getattr(ms, "_citation_candidates", None)
    if cached is not None and cached[0] is style:
        return cached[1]
    refs = references.extract(ms)
    n_refs = max((r.number or 0 for r in refs), default=len(refs)) or len(refs)
    front = front_matter_ids(ms)
    # The body and the tables (wherever they are; their notes too): "Garg et al., 2018[10]".
    paras = [p for p in renumber.citation_paragraphs(ms)
             if not p.style.lower().startswith("heading") and p.id not in front] if n_refs else []
    candidates: list[tuple[Paragraph, re.Match, bool]] = []
    after_number: list[tuple[Paragraph, re.Match]] = []  # "alpha of 0.88 (19)": a count, unless the sequence says not
    for p in paras:
        for pattern, is_round in ((_ROUND, True), (_SQUARE, False)):
            if is_round and (style.get("citations.brackets") != "square" or p.in_table):
                continue  # in tables, "Male (12)" is a count
            for m in pattern.finditer(p.text):
                prev = p.text[:m.start()].rstrip()
                if not prev or not_citation_before(prev, is_round, m.group(2)):
                    continue  # (the rule leaves a paragraph's opening "[23, 39]" as written)
                if re.search(r"[\d%]$", prev):
                    if is_round and re.search(r"\d\.\d+$", prev):
                        after_number.append((p, m))
                    continue  # counts "45 (12)"; the rule also leaves "1985[64]" as written
                candidates.append((p, m, is_round))
    # Numbers beyond the list still tell counts apart ("RTA (77) followed by head injury (8)"), then go:
    # "Germany (178)" in a list of 26 references, "items (0-2)".
    not_cited = _round_not_citations(ms, [(p, m) for p, m, r in candidates if r])
    candidates = [(p, m, r) for p, m, r in candidates if not (r and (
        (p.id, m.start()) in not_cited or _max_number(m.group(2)) > n_refs or normalise_citation(m.group(2), style) is None))]
    after_number = [(p, m) for p, m in after_number
                    if _max_number(m.group(2)) <= n_refs and normalise_citation(m.group(2), style) is not None]
    candidates += _next_in_sequence(ms, candidates, after_number)
    result = (n_refs, paras, candidates)
    ms._citation_candidates = (style, result)
    return result


def _next_in_sequence(ms: Manuscript, candidates: list[tuple], after_number: list[tuple]) -> list[tuple]:
    """Round-bracket numbers after a decimal ("Cronbach's alpha was 0.88 (19).") in a manuscript that cites
    in round brackets: a citation when it is the next reference in order and cited nowhere else
    (... (18) ... 0.88 (19) ... (21)); otherwise a value, as "mean 3.2 (4)" usually is."""
    rounds = [(p, m) for p, m, r in candidates if r]
    if not after_number or len(rounds) <= sum(not r for _, _, r in candidates) + \
            sum(len(superscript_citations(p)) for p in body_paragraphs(ms)):
        return []
    items = [(renumber.citation_key(ms, p.id, m.start()), renumber.expand(m.group(2)) or [], (p, m))
             for p, m, _ in candidates]
    cited = {n for _, nums, _ in items for n in nums}
    out = []
    for p, m in after_number:
        nums = renumber.expand(m.group(2)) or []
        key = renumber.citation_key(ms, p.id, m.start())
        before = max((n for k, ns, _ in items if k < key for n in ns), default=0)
        if nums and not cited & set(nums) and min(nums) == before + 1:
            out.append((p, m, True))
            cited.update(nums)
            items.append((key, nums, (p, m)))
    return out


def round_citations(ms: Manuscript, style: JournalStyle) -> list[tuple[Paragraph, re.Match]]:
    """The round-bracket numbers the citation rule converts: "(12)" after a word, not a count, a list
    marker, a statistic's degrees of freedom or a number beyond the reference list. QC reads its
    "citations in round brackets" from here, so it never disagrees with the rule."""
    return [(p, m) for p, m, r in _citation_candidates(ms, style)[2] if r]


def rule_citations(ms: Manuscript, style: JournalStyle) -> list[Change]:
    n_refs, paras, candidates = _citation_candidates(ms, style)
    if not n_refs:
        return []
    sup = rules_format.citation_format(style)
    # Adjacent bracket citations become one: "[2], [3], [5]" -> "[2,3,5]", "[3][4]" -> "[3,4]".
    units: list[list] = []  # [paragraph, start, end, inner, punct]
    for p, m, is_round in sorted(candidates, key=lambda x: (x[0].id, x[1].start())):
        last = units[-1] if units else None
        if (last is not None and last[0] is p and last[5] == is_round and m.start() == last[2]
                and last[4] in ("", ",")):
            last[2], last[3], last[4] = m.end(), last[3] + "," + m.group(2), m.group(3)
            continue
        units.append([p, m.start(), m.end(), m.group(2), m.group(3), is_round])
    out = []
    for p, start, end, inner, punct, _ in units:
        if _max_number(inner) > n_refs:
            continue
        norm = normalise_citation(inner, style)
        if norm is None:
            continue
        doubled = punct and _PUNCT_BEFORE.search(p.text, 0, start) and p.text[:start].rstrip().endswith(punct) \
            and style.get("citations.after_punctuation", True)
        if doubled:
            new = f"[{norm}]"  # "older. (1). CVDs", "features.[1]. On" -> "older.[1] CVDs": the stop is there already
        elif style.get("citations.after_punctuation", True) and punct:
            new = f"{punct}[{norm}]"
        else:
            new = f"[{norm}]{punct}"
        if new == p.text[start:end]:
            continue
        if doubled:
            out.append(_change(p, start, end, new, Layer.REFERENCES, "citations", sup))
        elif sup and punct and new.startswith(punct + "["):  # bracket as its own insertion, superscript
            out.append(_change(p, start, end, punct, Layer.REFERENCES, "citations"))
            out.append(_change(p, end, end, new[len(punct):], Layer.REFERENCES, "citations", sup))
        elif sup:
            out.append(_change(p, start, end - len(punct), new[:len(new) - len(punct)],
                               Layer.REFERENCES, "citations", sup))
        else:
            out.append(_change(p, start, end, new, Layer.REFERENCES, "citations"))
        out += _trailing_space(p, end, "citations")
    markers = table_note_markers(ms)  # "N = 4391 (100%)²" under "² n (%)": a table footnote marker
    for p in paras:
        # Superscript citations: "worldwide¹." -> "worldwide.[1]" (brackets keep the run's formatting).
        for s, e, nums in superscript_citations(p):
            if s == 0 or _max_number(nums) > n_refs or (p.id, s) in markers:
                continue
            norm = normalise_citation(nums, style)
            if norm is None:
                continue
            nxt = p.text[e:e + 1]
            if style.get("citations.after_punctuation", True) and nxt in ".,;:":
                gap = len(p.text[:s]) - len(p.text[:s].rstrip())
                # punctuation before, taking the space in front of the citation ("malformations ².")
                out.append(_change(p, s - gap, s, nxt, Layer.REFERENCES, "citations"))
                out.append(_change(p, s, e, f"[{norm}]", Layer.REFERENCES, "citations", sup))
                out.append(_change(p, e, e + 1, "", Layer.REFERENCES, "citations"))  # and not after
                out += _trailing_space(p, e + 1, "citations")
            else:
                out.append(_change(p, s, e, f"[{norm}]", Layer.REFERENCES, "citations", sup))
    return out


# --- citation next to the author's name --------------------------------------------------
#
# "Özer et al. demonstrated ... choriocapillaris.[28]" -> "Özer et al.[28] demonstrated ...": when the
# text since the previous citation names one study ("Surname et al.", "Surname and Surname") and ends
# a sentence with a single citation whose reference's first author is that surname, the editors put
# the citation right after the name. Nothing moves unless the reference list confirms the author.

_PARTICLES = {"van", "von", "der", "den", "de", "da", "di", "del", "la", "le", "al", "el", "du", "dos", "ten", "ter"}
_NAME_TOKEN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-žḀ-ỿ’'\-]+\.?")
_ET_AL_MENTION = re.compile(r"\bet\.?\s+al\b(?P<dot>\.?)(?P<comma>,?)")
_AND_MENTION = re.compile(r"\b(?P<a>[A-ZÀ-Ý][\w’'\-]+)\s+(?:and|&)\s+(?P<b>[A-ZÀ-Ý][\w’'\-]+)\b")
_SENTENCE_GAP = re.compile(r"[.?!]\s+(?=[A-Z])")


def _name_key(s: str) -> str:
    return re.sub(r"[^a-z]", "", refengine.fold(s))


def _is_initials(tok: str) -> bool:
    t = tok.replace(".", "").replace("-", "")
    return 0 < len(t) <= 3 and t.isalpha() and t.isupper()


def _surname_words_before(text: str, end: int) -> list[str]:
    """The capitalised words (and name particles) just before `end`, initials dropped:
    "case report by Gupta B " -> ["Gupta"]; "data by Meili Xiao " -> ["Meili", "Xiao"]."""
    words = []
    for tok in reversed(text[max(0, end - 60):end].split()):
        t = tok.rstrip(",")
        if t != tok and words:
            break  # "However, Prakash et al.": a comma before the name ends it
        if _NAME_TOKEN.fullmatch(t) and (t[:1].isupper() or t.lower() in _PARTICLES):
            words.insert(0, t)
            if t != tok:
                break
            if len(words) >= 4:
                break
        else:
            break
    while words and words[0].lower() in _PARTICLES:
        words.pop(0)
    return [w for w in words if not _is_initials(w)]


def _author_keys(ref_text: str) -> list[str]:
    """Surname keys of the first two authors: "El-Nimri NW, Moghimi S, ..." -> ["elnimri", "moghimi"]."""
    out = []
    for chunk in re.split(r"[,;]", ref_text, maxsplit=2)[:2]:
        chunk = chunk.split(".")[0] if re.match(r"\s*[^.]{2,}\.\s", chunk) and "," not in chunk else chunk
        words = [w for w in chunk.split() if not _is_initials(w)]
        if not words or len(words) > 4 or not words[0][:1].isupper():
            break
        out.append(_name_key(" ".join(words)))
    return out


def _mention_matches(words: list[str], key: str) -> bool:
    keys = {_name_key(" ".join(words[i:])) for i in range(len(words))}
    return bool(key) and key in keys


def _citation_spans(p: Paragraph, round_ok: list[tuple[int, int]]) -> list[tuple[int, int, list[int]]]:
    """(start, end, numbers) for every numbered citation in `p`: square, round (when the
    citation rule converts it) and superscript; start includes the space before it."""
    spans = []
    for m in _SQUARE.finditer(p.text):
        if re.search(r"\b(table|tables|fig|figure|figures|ref|reference|no|eq|equation)\.?$",
                     p.text[:m.start()].rstrip(), re.I):
            continue
        nums = renumber.expand(m.group(2))
        if nums:
            spans.append((m.start(), m.end() - len(m.group(3)), nums))
    for m in _ROUND.finditer(p.text):
        if any(s < m.end() and m.start() < e for s, e in round_ok):
            nums = renumber.expand(m.group(2))
            if nums:
                spans.append((m.start(), m.end() - len(m.group(3)), nums))
    for s, e, nums in superscript_citations(p):
        if s == 0:
            continue
        lead = len(p.text[:s]) - len(p.text[:s].rstrip())
        n = renumber.expand(nums)
        if n:
            spans.append((s - lead, e, n))
    return sorted(spans)


def rule_author_citations(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not style.get("citations.next_to_author", False):
        return []
    refs = references.extract(ms)
    by_num = {r.number: r for r in refs if r.number is not None}
    if not by_num:
        return []
    pl = renumber.plan(ms, style)
    mapping = pl.mapping if pl.apply else {}
    sup = rules_format.citation_format(style)
    after_punct = style.get("citations.after_punctuation", True)
    max_sentences = style.get("citations.next_to_author_max_sentences", 4)
    italic = style.get("formatting.et_al_italic", True)
    front = front_matter_ids(ms)
    abstract = rules_sections.abstract_ids(ms)
    round_ok: dict[str, list[tuple[int, int]]] = {}
    for c in rule_citations(ms, style):
        if c.old.lstrip().startswith("("):
            round_ok.setdefault(c.para_id, []).append((c.start, c.end))
    out = []
    for p in body_paragraphs(ms):
        if p.id in front or p.id in abstract or p.style.lower().startswith("heading") \
                or rules_figures.caption_match(p) is not None:
            continue
        text = p.text
        prev_end = 0
        for s, e, nums in _citation_spans(p, round_ok.get(p.id, [])):
            passage_start, prev_end = prev_end, e
            if len(nums) != 1 or nums[0] not in by_num:
                continue
            # The citation closes a sentence: "... (P = 0.02) [17]." or "... action.[28]"
            before, after = text[:s].rstrip(), text[e:]
            if before.endswith((":", ",", ";")) or re.search(r"\bet\.?\s+al\.?,?$", before):
                continue  # "as follows: [13]"; "Caroff et al. [11], who": at the name already
            if not (re.match(r"\s*(?:[.?!]|$)", after) or re.search(r"[.?!]$", before)):
                continue
            passage = text[passage_start:s]
            keys = _author_keys(by_num[nums[0]].text)
            if not keys:
                continue
            mentions = []  # ("et al" match, surname words before it)
            for m in _ET_AL_MENTION.finditer(passage):
                words = _surname_words_before(passage, m.start())
                mentions.append((m, words))
            pairs = [m for m in _AND_MENTION.finditer(passage)
                     if len(keys) >= 2 and _name_key(m.group("a")) == keys[0] and _name_key(m.group("b")) == keys[1]]
            if len(mentions) + len(pairs) != 1:
                continue
            base = passage_start
            new_cite = f"[{renumber.compress([mapping.get(nums[0], nums[0])], style)}]"
            if mentions:
                m, words = mentions[0]
                if not words or not _mention_matches(words, keys[0]):
                    continue
                nxt = passage[m.end():]
                if re.match(r"\s*[\[(⁰¹²³⁴⁵⁶⁷⁸⁹\d]", nxt):
                    continue  # "Liu et al. (2019)" / already cited
                al_end = base + m.start() + len(m.group(0)) - len(m.group("dot")) - len(m.group("comma"))
                if m.group("dot"):
                    point = base + m.end() if (m.group("comma") and after_punct) else base + m.end("dot")
                    edits = []
                else:
                    et_start = base + m.start()
                    fmt = {"italic": True} if italic else None
                    edits = [_change(p, et_start, al_end, "et al.", Layer.REFERENCES, "author_citations", fmt)]
                    point = base + m.end() if (m.group("comma") and after_punct) else al_end
            else:
                m = pairs[0]
                point = base + m.end()
                if re.match(r"\s*[\[(⁰¹²³⁴⁵⁶⁷⁸⁹\d]", text[point:]):
                    continue
                edits = []
            if point >= s or not text[point:s].strip(" \u00a0.,;:") or len(_SENTENCE_GAP.findall(text[point:s])) >= max_sentences:
                # already at the name: "by Braun and Clarke.[15]" (only punctuation between)
                continue
            out += edits
            out.append(_change(p, point, point, new_cite, Layer.REFERENCES, "author_citations", sup))
            # Take the citation out; punctuation right after it is kept but covered, so the citation
            # rule's bracket insertion after it ("worldwide¹." -> "worldwide.[1]") cannot come back.
            trail = re.match(r"[.,;:]?", after).group(0)
            end = e + len(trail)
            if not text[end:].strip():  # it ended the paragraph: the spaces after it go too
                end = len(text)
            out.append(_change(p, s, end, trail, Layer.REFERENCES, "author_citations"))
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
        lead = len(text) - len(text.lstrip())  # the tag goes at the very start: leading spaces go
        out.append(_change(p, 0, lead, f"<{level}>", Layer.STRUCTURE, "heading_tags"))
        if level != "H1" and style.get("headings.subheading_case", "keep") == "sentence":
            out += rules_sections.sentence_case_changes(ms, p, "heading_tags")
        if style.get("headings.strip_trailing_colon", True) and text.rstrip().endswith(":"):
            i = len(text.rstrip()) - 1  # the colon and any spaces after it: "Conclusion: " -> "Conclusion"
            out.append(_change(p, i, len(text), "", Layer.STRUCTURE, "heading_tags"))
        elif text != text.rstrip() and lead < len(text.rstrip()):  # the colon stays (G137), the spaces go (G143)
            out.append(_change(p, len(text.rstrip()), len(text), "", Layer.STRUCTURE, "heading_tags"))
    return out


# --- statistics ----------------------------------------------------------------------

# A comparison after a plain word ("aged ≥18", "showing <5%", "pre-specified <20", "the study's <20") has no left
# operand; a statistic ("P <0.05", "BMI >30", "n=28") does.
_WORD_BEFORE = re.compile(r"(?:(?<!\w)[a-z]{2,}|\w[’']s)$")
# The left operand may be a Greek letter or end in a superscript ("β=0.44", "η²=0.42"); "P=<0.001" -> "P = <0.001".
_OPERATOR = re.compile(r"(?<=[A-Za-z0-9)\]\u0370-\u03ff²³])(\s*)([=<>≤≥])(\s*)(?=[-−–]?\d|\.\d|[<>≤≥]\s*\.?\d)")


def rule_statistics(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not style.get("statistics.space_around_operators"):
        return []
    unary_after_word = style.get("statistics.unary_after_word", True)
    out = []
    for p in body_paragraphs(ms):
        if "http" in p.text or "<" in p.text and ">" in p.text and re.search(r"<\w+>", p.text):
            continue
        for m in _OPERATOR.finditer(p.text):
            if not unary_after_word and m.group(2) != "=" and m.group(1) and _WORD_BEFORE.search(p.text, 0, m.start()):
                continue  # "aged ≥18 years", "a score >5": no left operand, left as typed
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
                # as a rebuilt entry writes them: "e123-e130" -> "e123-30", "123-123" -> "123"
                new = ":" + refengine.format_pages(f"{m.group(1)}{m.group(2)}-{m.group(3)}{m.group(4)}", style)
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
        cached = list(zip(refs, refengine.resolve_all([r.full_text for r in refs], style)))
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
        mine = []
        if res.status == refengine.FORMAT_FIXED:
            mine += word_diff(ms.by_id(ref.para_id), ref.offset, ref.text, res.formatted, "ref_lookup")
            for pid, text in ref.continuation:  # the rebuilt entry holds the rest ("doi:..." on its own line)
                p = ms.by_id(pid)
                mine.append(_change(p, 0, len(p.text), "", Layer.REFERENCES, "ref_lookup"))
        elif res.status == refengine.QUERY and res.styled and res.formatted != ref.text:
            # not verified: styled from the author's own fields (refstyle), the entry's query stays
            mine += word_diff(ms.by_id(ref.para_id), ref.offset, ref.text, res.formatted, "ref_styled")
            for pid, text in ref.continuation:
                p = ms.by_id(pid)
                mine.append(_change(p, 0, len(p.text), "", Layer.REFERENCES, "ref_styled"))
        elif res.formatted and res.formatted != ref.text:  # books, links, reports: template fixes
            mine += word_diff(ms.by_id(ref.para_id), ref.offset, ref.text, res.formatted, "ref_other")
        prov = provenance.reference(res, ref.full_text)
        for c in mine:
            c.provenance = prov
        out += mine
    return out


def _reference_provenance(ms: Manuscript, style: JournalStyle, changes: list[Change]) -> None:
    """Reference-list changes from the cleanup rules: the entry's lookup result ("Not verified" and why)."""
    found = {}
    for ref, res in lookup_references(ms, style):
        for pid in [ref.para_id] + [pid for pid, _ in ref.continuation]:
            found.setdefault(pid, (ref, res))
    for c in changes:
        if c.provenance is None and c.layer == Layer.REFERENCES and c.para_id in found:
            ref, res = found[c.para_id]
            c.provenance = provenance.reference(res, ref.full_text)


RULES = {
    "heading_tags": rule_heading_tags,
    "author_citations": rule_author_citations,  # before "renumber" and "citations": it moves whole citations
    "renumber": renumber.rule_renumber,  # before "citations": renumbered citations win the overlap
    "sections_headings": rules_sections.rule_headings,
    "subheadings": rules_sections.rule_subheadings,
    "sections_abstract": rules_sections.rule_abstract,
    "sections_keywords": rules_sections.rule_keywords,
    "sections_back_matter": rules_sections.rule_back_matter,
    "sections_salutation": rules_sections.rule_salutation,
    "citations": rule_citations,
    "fig_renumber": rules_figures.rule_renumber,  # before fig_citations / fig_captions: renumbered numbers win
    "fig_citations": rules_figures.rule_citations,
    "fig_captions": rules_figures.rule_captions,
    "statistics": rule_statistics,
    "ref_lookup": rule_ref_lookup,
    "references": rule_references,
    "formatting": rules_format.rule_formatting,  # before guide_replace: its "et al" -> "et al." is also italic
    "guide_replace": rules_guide.rule_guide,
    "format_cleanup": rules_format.rule_cleanup,  # author's colours, stray bold, labels and heads bold
}

# Front matter runs first: it rewrites whole title-page paragraphs (edit_os/frontmatter.py).
from . import frontmatter  # noqa: E402  (frontmatter imports nothing from this module)
RULES = {"front_matter": frontmatter.rule_front_matter, **RULES}
RULES = {"submission": frontmatter.submission.rule_submission, **RULES}  # cover letter, reply to reviewers: first


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
                        if res.status in (refengine.MATCHED, refengine.FORMAT_FIXED) or res.styled}
        for c in rule(ms, style):
            if name == "references" and c.para_id in resolved:
                continue
            # A formatting-only change and a text change may overlap (the renderer applies the
            # formatting first); two formatting changes clash only when they set the same property.
            clash = any(k.para_id == c.para_id and c.start < k.end and k.start < c.end
                        and (k.format_only == c.format_only)
                        and (not c.format_only or set(k.format) & set(c.format)) for k in kept)
            # Two insertions at one point clash, unless exactly one opens new paragraphs.
            same_point = any(k.para_id == c.para_id and k.start == k.end == c.start == c.end
                             and _opens_paragraph(k) == _opens_paragraph(c) for k in kept)
            # A citation moved to the author's name swallows insertions at the place it left.
            moved = c.start == c.end and any(k.para_id == c.para_id and k.reason == "author_citations"
                                             and k.start < k.end and not k.new.strip(".,;: ")
                                             and k.start <= c.start <= k.end for k in kept)
            if not clash and not same_point and not moved:
                kept.append(c)
    # Formatting on text that a kept change replaces is moot (the replacement carries its own
    # format), and showing it would split one edit into two suggestions.
    replaced = [k for k in kept if not k.format_only and k.end > k.start]
    kept = [c for c in kept if not (c.format_only and any(
        t.para_id == c.para_id and t.start <= c.start and c.end <= t.end for t in replaced))]
    if not only or "references" in only or "ref_lookup" in only:
        _reference_provenance(ms, style, kept)
    return ChangeSet(kept)
