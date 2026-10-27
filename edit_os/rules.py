"""Deterministic style-edit rules. Each rule proposes Change records; nothing is applied here.

Rules work on the paragraph text view, so every proposal carries the exact old text it
replaces. Overlapping proposals are resolved by `run_all` in rule order.
"""

from __future__ import annotations

import re
from itertools import count

import difflib

from . import journals, provenance, refengine, references, renumber, rules_figures, rules_format, rules_sections
from . import rules_tables  # table body layout (indentation, cell paragraphs)
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
# "[14.15]": a full stop typed for the comma between two reference numbers (the second the larger, no leading zero;
# after a word only when they are consecutive).
_SQUARE_DOT = re.compile(r"(\s*)\[([1-9]\d{0,2})\.([1-9]\d{0,2})\]([.,;:]?)")
# A year right before a round-bracket number: "previously reported in 2008 (7), presented" cites (a count after a
# number, "45 (12)", does not).
_YEAR_BEFORE_ROUND = re.compile(r"(?:\b(?:in|since|from|until|by|during)|\bet\.?\s+al\.?,?)\s+(?:1[89]|20|21)\d{2}$", re.I)


class _DotCitation:
    """A "[14.15]" match read as "[14,15]": the span as typed, the numbers with their comma."""

    def __init__(self, m: re.Match):
        self._m = m

    def start(self, g: int = 0) -> int:
        return self._m.start()

    def end(self, g: int = 0) -> int:
        return self._m.end()

    def span(self, g: int = 0) -> tuple[int, int]:
        return self._m.span()

    def group(self, g: int = 0) -> str:
        m = self._m
        return {0: m.group(0), 1: m.group(1), 2: f"{m.group(2)},{m.group(3)}", 3: m.group(4)}[g]


def _dot_citations(text: str) -> list[_DotCitation]:
    out = []
    for m in _SQUARE_DOT.finditer(text):
        a, b, prev = int(m.group(2)), int(m.group(3)), text[:m.start()].rstrip()
        # "failure.[14.15]" or "shown [14.15]" cite; "the ratio [1.5]" is a value
        if b > a and (re.search(r"[.,;:)]$", prev) or (b == a + 1 and re.search(r"[A-Za-z]$", prev))):
            out.append(_DotCitation(m))
    return out


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
    least = style.get("citations.compress_from")
    if least:  # "[3,4,5]" -> "[3-5]", "[1,3,4,5]" -> "[1,3-5]" (numbers in ascending order only)
        nums = renumber.expand(",".join(parts))
        if nums and all(b > a for a, b in zip(nums, nums[1:])):
            return renumber.compress(nums, style, least)
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
# A power in a formula: "n = (1.96)²", "p q/ d²", "n = N/(1 + Ne²)", "n = N / (1 + N(e)²)" ("mean 35 (1.69)¹²" and
# "Li et al. (2024)⁴" cite).
_FORMULA_BEFORE = re.compile(r"(?:[=+×÷*/^]\s*\(\s*\d+(?:\.\d+)?\s*\)|[=+×÷*^]\s*\(?[A-Za-z]{1,2}|/\s*[A-Za-z]"
                             r"|[=+×÷*/^(]\s*[A-Za-z]{0,2}\s*\(\s*[A-Za-z]{1,2}\s*\))\s*$")
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
        elif (messy := _messy_citation(p.text, s, e)) is not None:
            if not any(a <= messy[0] < b for a, b in taken):
                out.append(messy)
    return sorted(out)


# Superscript citations typed with stray punctuation (S310, S316): " 3,5, ", " 3, ,9", "5,6.7", "17.", ".24-30" (the
# sentence's full stop set in superscript with the numbers). The numbers run from the first digit to the last; the
# citation rule takes the stray punctuation around them.
_MESSY_SUP = re.compile(r"[\s.,;:]*(\d{1,3}(?:\s*[,.;\-–]\s*(?:[,.;]\s*)*\d{1,3})*)[\s.,;:]*")


def _messy_citation(text: str, s: int, e: int) -> tuple[int, int, str] | None:
    """(start, end, numbers) of a formatted superscript span holding citation numbers with stray punctuation, or
    None. A full stop between two numbers counts as a comma only when the second is larger ("5,6.7"; "1.5" is a
    value)."""
    m = _MESSY_SUP.fullmatch(text, s, e)
    if not m or s == 0 or not re.search(r"[A-Za-z)\].,;:%]\s*$", text[:s]):
        return None
    parts: list[str] = []
    for i, (seps, tok) in enumerate(re.findall(r"([^\d]*)(\d+)", m.group(1))):
        seps = re.sub(r"\s", "", seps)
        if i and "-" not in seps and "–" not in seps and set(seps) == {"."} and int(tok) <= int(re.findall(r"\d+", parts[-1])[-1]):
            return None  # "1.5": a value
        if i and ("-" in seps or "–" in seps):
            parts[-1] += "-" + tok
        else:
            parts.append(tok)
    if not parts or _is_exponent(text, m.start(1)) or _is_statistic_index(text, m.start(1), m.end(1)):
        return None
    return m.start(1), m.end(1), ",".join(parts)


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


_SCALE_CONTEXT = re.compile(r"\b(?:likert|scale|scored?|scoring|rated|rating|responses?|options?|answers?)\b", re.I)


def _scale_options(text: str, matches: list[re.Match]) -> list[re.Match]:
    """Round-bracket numbers in a list of answer options: "never (0), sometimes (1), often (2)",
    "very good (4), good (3), poor (2)": a run of two or more small values a few words apart, read as
    options when an option is a scale word, or the paragraph speaks of a scale and the run is made of
    option words, or the values run down ("(5) ... (4) ... (3)") or start at zero."""
    out: list[re.Match] = []
    run: list[re.Match] = []

    def close():
        if len(run) < 2:
            return
        vals = [int(m.group(2)) if m.group(2).isdigit() else None for m in run]
        if None in vals or any(v > 10 for v in vals):
            return
        words = []
        for m in run:
            w = re.search(r"([A-Za-z]+)\s*$", text[:m.start()])
            words.append(w.group(1).lower() if w else "")
        strong = any(w in SCALE_WORDS for w in words)
        weak = all(w in SCALE_WORDS or w in SCALE_WEAK_WORDS for w in words) and _SCALE_CONTEXT.search(text)
        down = all(b == a - 1 for a, b in zip(vals, vals[1:]))
        zero = vals[0] == 0 and all(b == a + 1 for a, b in zip(vals, vals[1:]))
        if strong or weak or down or zero:
            out.extend(run)

    for m in matches:
        if run:
            gap = text[run[-1].end():m.start()]
            if len(gap) > 40 or re.search(r"[.;:\[\]()]", gap) or len(gap.split()) > 4:
                close()
                run = []
        run.append(m)
    close()
    return out


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
        out.update((pid, m.start()) for m in _scale_options(p.text, matches))
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
_NUMBER_WORDS["zero"] = 0


# A scale's answer options or score values: "strongly agree (5) to strongly disagree (1)", "never (0)", "a score (3)",
# "level (2)": the number is the option's value, not a reference (round brackets, small values only).
SCALE_WORDS = frozenset("""neutral undecided score scores scored scale scales point points level levels grade grades
    likert option options rating""".split())
# Words that are a scale's options only next to other options or a scale ("poor (1), fair (2), good (3)", "never (0),
# rarely (1)"; "agree (5)" after "strongly"): alone, "studies agree (3)" and "occurs rarely (4)" cite.
SCALE_WEAK_WORDS = frozenset("""agree disagree never always rarely sometimes often usually seldom frequently
    occasionally very mild moderate severe low high poor fair good excellent satisfied dissatisfied unsatisfied
    important unimportant likely unlikely none all most some little much yes""".split())
_STRONGLY = re.compile(r"\b(?:strongly|somewhat|completely|totally|partially|mostly|slightly)\s+(?:dis)?agree$", re.I)


def _small_values(inner: str, limit: int = 10) -> bool:
    nums = re.findall(r"\d+", inner)
    return bool(nums) and all(int(n) <= limit for n in nums)


def not_citation_before(prev: str, is_round: bool, inner: str = "") -> bool:
    """Is a bracketed number after `prev` (the text before it, right-stripped) a label, a statistic,
    a count or a scale value rather than a citation? One answer for the citation rule, renumbering and QC."""
    if not prev:
        return is_round  # "(1) ..." starting a paragraph is list numbering; "[23, 39]" alone in a table cell cites
    if _LABEL_BEFORE.search(prev) or _STAT_BEFORE_BRACKET.search(prev):
        return True
    if not is_round:
        return False
    if prev[-1] in "=<>≤≥:":  # "agree = (5)", "score: (3)"
        return True
    word = re.search(r"([A-Za-z]+)$", prev)
    if not word:
        return False
    w = word.group(1).lower()
    if w in _NUMBER_WORDS:  # "six (6) weeks", "one (2)": a number written twice, not a reference
        return inner.strip().isdigit() and (_NUMBER_WORDS.get(w, 0) == int(inner) or int(inner) <= 12)
    return (w in SCALE_WORDS or bool(_STRONGLY.search(prev))) and _small_values(inner)


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
    # Running text the author typed in a Word heading style (a long paragraph) is body text all the same.
    paras = [p for p in renumber.citation_paragraphs(ms)
             if (not p.style.lower().startswith("heading") or len(p.text) > 150) and p.id not in front] if n_refs else []
    candidates: list[tuple[Paragraph, re.Match, bool]] = []
    after_number: list[tuple[Paragraph, re.Match]] = []  # "alpha of 0.88 (19)": a count, unless the sequence says not
    for p in paras:
        for pattern, is_round in ((_ROUND, True), (_SQUARE, False)):
            if is_round and (style.get("citations.brackets") != "square" or p.in_table):
                continue  # in tables, "Male (12)" is a count
            found = list(pattern.finditer(p.text)) + ([] if is_round else _dot_citations(p.text))
            for m in found:
                prev = p.text[:m.start()].rstrip()
                if not prev or not_citation_before(prev, is_round, m.group(2)):
                    continue  # (the rule leaves a paragraph's opening "[23, 39]" as written)
                cites_after = (prev.endswith("%") and not is_round) or (is_round and _YEAR_BEFORE_ROUND.search(prev))
                if re.search(r"[\d%]$", prev) and not cites_after:  # "2% [4]," and "in 2008 (7)," cite
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
            # The punctuation takes the place of the citation: never its superscript ("[13]." -> ".[13]").
            out.append(_change(p, start, end, punct, Layer.REFERENCES, "citations", {"superscript": False}))
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
            out += _superscript_citation(p, s, e, norm, style, sup)
    return out


def _in_superscript(p: Paragraph, i: int) -> bool:
    return any(a <= i < b for a, b in p.superscript)


def _superscript_citation(p: Paragraph, s: int, e: int, norm: str, style: JournalStyle, sup: dict | None) -> list[Change]:
    """"worldwide¹." -> "worldwide.[1]"; stray punctuation typed in the superscript goes: "procedures. ³˒⁵˒ Given" ->
    "procedures.[3,5] Given", "high¹⁷." -> "high.[17]", "CR.²⁴⁻³⁰" (full stop in superscript) -> "CR.[24-30]" with the
    full stop on the line; "outcomes. ³Simulation" -> "outcomes.[3] Simulation" (S310: "after punctuations")."""
    text = p.text
    after_punct = style.get("citations.after_punctuation", True)
    out = []
    end = e
    tail = re.match(r"[\s,;]*", text[e:]).group(0).rstrip()
    if tail and all(_in_superscript(p, i) for i in range(e, e + len(tail))):
        end = e + len(tail)  # " 3,5, ": the commas after the numbers are the author's typing
    before = text[:s].rstrip()
    gap = len(text[:s]) - len(before)
    for i in range(len(before) - 1, -1, -1):  # ".24-30": the sentence's full stop typed in superscript
        if before[i] in ".,;:" and _in_superscript(p, i):
            out.append(_change(p, i, i + 1, before[i], Layer.REFERENCES, "citations", {"superscript": False}))
        else:
            break
    nxt = text[end:end + 1]
    punctuated = bool(re.search(r"[.,;:?!]$", before))
    sentence_end = bool(re.search(r"[.?!]$", before))
    if after_punct and nxt and nxt in ".,;:":
        if punctuated and _in_superscript(p, end):
            out.append(_change(p, s - gap, s, "", Layer.REFERENCES, "citations"))  # "region.¹." : one stop
        else:
            # punctuation before, taking the space in front of the citation ("malformations ².")
            out.append(_change(p, s - gap, s, nxt, Layer.REFERENCES, "citations"))
            sentence_end = nxt == "."
        out.append(_change(p, s, end, f"[{norm}]", Layer.REFERENCES, "citations", sup))
        out.append(_change(p, end, end + 1, "", Layer.REFERENCES, "citations"))  # and not after
        rest = end + 1
        out += _trailing_space(p, rest, "citations")
    else:
        if after_punct and punctuated and gap:
            out.append(_change(p, s - gap, s, "", Layer.REFERENCES, "citations"))  # "outcomes. ³" -> "outcomes.[3]"
        out.append(_change(p, s, end, f"[{norm}]", Layer.REFERENCES, "citations", sup))
        rest = end
    if sentence_end and re.match(r"[A-Z]", text[rest:rest + 1]):
        out.append(_change(p, rest, rest, " ", Layer.REFERENCES, "citations"))  # "manner.⁴Maternal"
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
            poss = re.match(r"[’']s\b", text[point:])
            if poss:  # "Roy et al.’s JEHP case study" -> "Roy et al.’s[16] JEHP case study", not "al.[16]’s"
                point += poss.end()
            if point >= s or not text[point:s].strip(" \u00a0.,;:") or len(_SENTENCE_GAP.findall(text[point:s])) >= max_sentences:
                # already at the name: "by Braun and Clarke.[15]" (only punctuation between)
                continue
            out += edits
            # never the italic of the "et al." it follows
            out.append(_change(p, point, point, new_cite, Layer.REFERENCES, "author_citations",
                               {**(sup or {}), "italic": False}))
            # Take the citation out; punctuation right after it is kept but covered, so the citation
            # rule's bracket insertion after it ("worldwide¹." -> "worldwide.[1]") cannot come back.
            trail = re.match(r"[.,;:]?", after).group(0)
            end = e + len(trail)
            if not text[end:].strip():  # it ended the paragraph: the spaces after it go too
                end = len(text)
            out.append(_change(p, s, end, trail, Layer.REFERENCES, "author_citations"))
    return out


# --- "surname, year" citations ----------------------------------------------------------------
#
# S320 (JEHP): 'If citations are present in "surname, year" format and not in numbers then change them to respective
# reference numbers'. "Sorte et al. (2025) reported" -> "Sorte et al.[4] reported" when the numbered reference list has
# exactly one entry whose first author is Sorte and whose year is 2025; nothing is guessed. A year next to the number
# goes: "Nagarajappa et al. (2015) [7]" -> "Nagarajappa et al.[7]", "Wagner et al. (2016), ... Castillo et al. (2021)
# (11,31,32)" -> "Wagner et al., ... Castillo et al.[11,31,32]" (the name is the first author of one of the numbers);
# the sheet is silent there, the editors drop it (1044, 1562, 1580, 1751, 1764). A study column's "Saulnier et al.,
# 2022.[9]" keeps its year (1410: 22 of 22).

_AY_PAREN = re.compile(r"\s*\((?P<year>(?:19|20)\d{2})[a-z]?\)")
_AY_COMMA = re.compile(r",\s*(?P<year>(?:19|20)\d{2})[a-z]?\b(?![\d\-–/:;)])")
_AY_ET_AL = re.compile(r"\bet\.?\s+al\b\.?$")
_AY_PAIR = re.compile(r"\b(?P<a>[A-ZÀ-Ý][\w’'\-]+)\s+(?:and|&)\s+(?P<b>[A-ZÀ-Ý][\w’'\-]+)$")
_AY_NUMBER_AFTER = re.compile(r"(?P<dot>\.?)\s*(?=\[\s*\d|\(\s*\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*\s*\)|[⁰¹²³⁴⁵⁶⁷⁸⁹])")


def _ay_mention(before: str, paren: bool) -> tuple[list[str], str | None] | None:
    """(surname words of the first author, the second author's surname for "A and B") of the name that ends `before`,
    or None. The comma form ("Saulnier et al., 2022") is read after "et al." only ("Tehran, Iran, 2020" is not)."""
    m = _AY_ET_AL.search(before)
    if m:
        words = _surname_words_before(before, m.start())
        return (words, None) if words else None
    if not paren:
        return None
    m = _AY_PAIR.search(before)
    if m:
        return [m.group("a")], m.group("b")
    words = _surname_words_before(before, len(before))
    return (words, None) if words and len(words) <= 2 else None


def _ay_matches(ref_text: str, words: list[str], second: str | None) -> bool:
    keys = _author_keys(ref_text)
    if not keys or not _mention_matches(words, keys[0]):
        return False
    return second is None or (len(keys) >= 2 and _name_key(second) == keys[1])


def rule_author_year(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not style.get("citations.author_year", False):
        return []
    refs = references.extract(ms)
    by_num = {r.number: r for r in refs if r.number is not None}
    if not by_num:
        return []  # an author-year list: there are no numbers to cite
    ref_ids = {r.para_id for r in refs}
    pl = renumber.plan(ms, style)
    mapping = pl.mapping if pl.apply else {}
    sup = rules_format.citation_format(style)
    fmt = {**(sup or {}), "italic": False}  # never the italic of the "et al." before it
    front = front_matter_ids(ms)
    round_ok: dict[str, list[tuple[int, int]]] = {}
    for c in rule_citations(ms, style):
        if c.old.lstrip().startswith("("):
            round_ok.setdefault(c.para_id, []).append((c.start, c.end))
    out = []
    for p in renumber.citation_paragraphs(ms):
        if p.id in front or p.id in ref_ids:
            continue
        text = p.text
        spans = None
        found = [(m, True) for m in _AY_PAREN.finditer(text)] + [(m, False) for m in _AY_COMMA.finditer(text)]
        for m, paren in sorted(found, key=lambda x: x[0].start()):
            before = text[:m.start()]
            mention = _ay_mention(before, paren)
            if mention is None:
                continue
            words, second = mention
            if spans is None:
                spans = _citation_spans(p, round_ok.get(p.id, []))
            nxt = _AY_NUMBER_AFTER.match(text, m.end())
            if nxt and not paren:
                continue  # "Saulnier et al., 2022.[9]" in a table's study column: kept (1410: 22 of 22)
            if nxt:
                # The number is there: the year goes, when the name is the cited reference's first author.
                cite = next((nums for s, e, nums in spans if s >= m.end()), None)
                if not cite or not any(n in by_num and _ay_matches(by_num[n].text, words, second) for n in cite):
                    continue
                end = m.end() + (len(nxt.group("dot")) if before.rstrip().endswith(".") else 0)
                out.append(_change(p, m.start(), end, "", Layer.REFERENCES, "author_year"))
                continue
            # A number later in the sentence for a list of names: "Wagner et al. (2016), Peimani et al. (2018), and
            # Castillo et al. (2021) (11,31,32)".
            stop = _SENTENCE_GAP.search(text, m.end())
            later = next(((s, nums) for s, e, nums in spans if m.end() <= s < (stop.start() if stop else len(text))),
                         None)
            between = re.sub(r"[.,;:]$", "", text[m.end():later[0]].rstrip()) if later else ""
            if later and any(n in by_num and _ay_matches(by_num[n].text, words, second) for n in later[1]) \
                    and not re.search(r"[.;:]", re.sub(r"\bet\.?\s+al\.", "et al", between)):
                out.append(_change(p, m.start(), m.end(), "", Layer.REFERENCES, "author_year"))
                continue
            if later:
                continue
            year = m.group("year")
            hits = [n for n, r in by_num.items()
                    if _ay_matches(r.text, words, second) and re.search(rf"\b{year}[a-z]?\b", r.text)]
            if len(hits) != 1:
                continue  # none, or more than one: never a guess
            new = f"[{renumber.compress([mapping.get(hits[0], hits[0])], style)}]"
            out.append(_change(p, m.start(), m.end(), new, Layer.REFERENCES, "author_year", fmt))
    return out


# --- author names against the reference list ---------------------------------------------
#
# S (41 of 42 sheets): "Match author names in the references list with those in citations (wherever author names are
# provided) in text"; the guide (E.g. 6) words the query. Only a name the reference list contradicts is queried:
# "Radziwill et al. [6]" where reference 6 is "Bae JS, ...".

AUTHOR_QUERY = ("Kindly confirm whether author names in the references list are matching with the citations (wherever "
                "author names are provided) in text: {name} et al. is cited as reference {n}, whose first author is "
                "{first}.")
_ET_AL_END = re.compile(r"\bet\.?\s+al\b\.?,?\s*$")


def author_name_mismatches(ms: Manuscript, style: JournalStyle) -> list[tuple[Paragraph, int, int, str, int, str]]:
    """(paragraph, start, end, cited name, reference number, the reference's first author) for each "Surname et al.
    [n]" whose reference n has a different first author."""
    if not style.get("citations.author_match_query", True):
        return []
    refs = references.extract(ms)
    by_num = {r.number: r for r in refs if r.number is not None}
    if not by_num:
        return []
    skip = front_matter_ids(ms) | rules_sections.abstract_ids(ms)
    proposals: dict[str, list[Change]] = {}
    for c in rule_citations(ms, style):
        if c.old.lstrip().startswith("("):
            proposals.setdefault(c.para_id, []).append(c)
    out = []
    for p in renumber.citation_paragraphs(ms):
        if p.id in skip or rules_figures.caption_match(p) is not None:
            continue
        round_ok = [(c.start, c.end) for c in proposals.get(p.id, [])]
        for s, e, nums in _citation_spans(p, round_ok):
            before = p.text[:s]
            m = _ET_AL_END.search(before)
            if not m or len(nums) != 1 or nums[0] not in by_num:
                continue
            words = _surname_words_before(before, m.start())
            keys = _author_keys(by_num[nums[0]].text)
            first = re.split(r"[,;]", by_num[nums[0]].text.strip(), maxsplit=1)[0].strip()
            parts = {_name_key(w) for w in re.split(r"[\s\-‐]+", first) if not _is_initials(w)}
            if not words or not keys or _mention_matches(words, keys[0]) or _name_key(words[-1]) in parts:
                continue  # "Nalcacioglu et al." for "Nalcacioglu-Yüksekkaya P"
            if any(_same_name(" ".join(words[i:]), k) for i in range(len(words)) for k in parts | {keys[0]}):
                continue  # a transliteration: "Al'Hamid et al." for "Alhammid K"
            start = before.rfind(words[0], 0, m.start())
            out.append((p, start, e, words[-1], nums[0], first))
    return out


def _translit_key(s: str) -> str:
    """A name with apostrophes, hyphens and spaces dropped, diacritics folded and doubled letters collapsed:
    "Ba’ezat" and "Baezzat" -> "baezat"."""
    return re.sub(r"(.)\1+", r"\1", _name_key(s.translate(refengine._LETTERS)))


def _same_name(a: str, b: str) -> bool:
    """Two spellings of one surname (transliteration variants): equal once folded, or nearly so (ratio >= 0.85)."""
    from rapidfuzz.fuzz import ratio

    ka, kb = _translit_key(a), _translit_key(b)
    return bool(ka) and bool(kb) and (ka == kb or ratio(ka, kb) >= 85)


def add_author_queries(ms: Manuscript, style: JournalStyle, qs) -> None:
    from . import queries

    queries.TEXT.setdefault("references.author_mismatch", AUTHOR_QUERY)
    for p, s, e, name, n, first in author_name_mismatches(ms, style):
        qs.add(p.id, "references.author_mismatch", s, e, name=name, n=n, first=first)


# --- headings ------------------------------------------------------------------------

_NUMBERING = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+")


def _front_matter_ids(ms: Manuscript, style: JournalStyle) -> set[str]:
    """Title-page paragraphs the front-matter step rewrites or deletes (the article type, title, author lines)."""
    if not style.get("front_matter.enabled", True) or getattr(ms, "_front_plan", None) is None:
        return set()
    pl = ms._front_plan
    roles = pl.fields.roles if pl.fields is not None else {}
    return {pid for pid, r in roles.items() if r not in (None, "body", "other_front_matter")} \
        | {c.para_id for c in pl.changes}


def rule_heading_tags(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not style.get("headings.tag"):
        return []
    levels = {name: "H1" for name in style.get("headings.h1", [])}
    levels.update({name: "H2" for name in style.get("headings.h2", [])})
    out = []
    in_abstract = rules_sections.abstract_ids(ms)  # "Results" there is an abstract sub-heading
    front = _front_matter_ids(ms, style)  # "Case Report" as the article type on the title page is no heading
    mains = rules_sections.main_heading_keys(style)
    ref_i = references.find_heading(ms)
    ref_id = ms.paragraphs[ref_i].id if ref_i is not None else None
    for p in ms.paragraphs:
        text = p.text
        if p.in_table or text.lstrip().startswith("<H") or p.id in in_abstract or p.id in front \
                or rules_sections.in_textbox(p):  # a PRISMA box's "Identification" is no heading
            continue
        if len(text) > 60 or "\n" in text.strip():
            # "DISCUSSION \nTo the best ..." / "... [18].\nConclusion": the heading line becomes its own paragraph
            hl = rules_sections.heading_line(text, mains)
            if hl is not None:
                s, e, b0, b1 = hl
                level = levels.get(rules_sections._key(text[s:e]))
                tag = f"<{level}>" if level else ""  # a renamed one ("Result") is tagged by sections_headings
                if b0 >= e:  # the heading first: tag it, break after it
                    if tag:
                        out.append(_change(p, 0, s, tag, Layer.STRUCTURE, "heading_tags"))
                    out.append(_change(p, b0, b1, rules_sections.BR, Layer.STRUCTURE, "heading_tags"))
                else:  # the heading last: break before it, tagged
                    out.append(_change(p, b0, b1, rules_sections.BR + tag, Layer.STRUCTURE, "heading_tags"))
                continue
            if len(text) > 60:
                continue
        core = _NUMBERING.sub("", text).strip()
        key = core.rstrip(":").strip().lower()
        level = levels.get(key)
        if not level and p.id == ref_id:  # "References-" (IJO 2733): the reference list's heading
            level = levels.get(core.rstrip(":.-–—_ ").strip().lower())
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
# A surname in capitals followed by initials, as one name of the author list: "ALIZADEH GJ", "ABDEL-LATIF M".
_CAPS_SURNAME = re.compile(r"(?:^|(?<=,\s)|(?<=,))\s*(?P<sur>[A-Z]{3,}(?:-[A-Z]{2,})*)\s+(?:[A-Z]{1,3})(?=\s*(?:,|\.?$|\.\s|\s+et al))")
_INITIAL_PERIOD = re.compile(r"(?<=\b[A-Z])\.(?=\s*,|\s*[A-Z]\b)")
_MON = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")
_YEAR_VOL = re.compile(rf"(?P<sep>\.?\s*)(?P<year>(?:19|20)\d{{2}})(?P<date>\s+{_MON}(?:\s*[-/]\s*{_MON})?(?:\s+\d{{1,2}})?)?"
                       r"(?P<semi>\s*;\s*)(?P<vol>\d+)(?P<part>\s*\(?\s*(?:Pt|Part)\.?\s*\d+\s*\)?)?\s*(?P<issue>\([^)]*\))?\s*(?=:)")
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


def _reference_edits(text: str, style: JournalStyle) -> list[tuple[int, int, str, str, dict | None]]:
    """The cleanup edits for one entry's text: (start, end, new, rule, format)."""
    out = []
    m_end = _AUTHORS_END.search(text)
    authors_end = m_end.start() if m_end else None
    italic = {"italic": True} if et_al_italic(style) else None

    if authors_end and style.get("references.initials_without_periods", True):
        for m in _INITIAL_PERIOD.finditer(text[:authors_end]):
            out.append((m.start(), m.end(), "", "ref_initials", None))

    max_i = style.get("references.max_initials")
    if authors_end and max_i:  # "Chandrasena PPCM," -> "Chandrasena PP," (JEHP: owner, 2026-10-26)
        for m in re.finditer(r"(?<![\w'’\-])[^\W\d][\w'’\-]*[a-z][\w'’\-]*\s+([A-Z]{%d,4})(?=\s*,|\s*$|\s+et al)" % (max_i + 1), text[:authors_end]):
            name = text[:m.end(1)].rsplit(",", 1)[-1].split()
            if len(name) <= 3:  # a person ("Ten Ham-Baloyi WAB"), not a group ("World Health Organization WHO")
                out.append((m.start(1) + max_i, m.end(1), "", "ref_initials", None))

    if authors_end:  # "ALIZADEH GJ" -> "Alizadeh GJ" (S: "Elnaem MH, Mohamed MH"); "WHO" and other groups stay
        for m in _CAPS_SURNAME.finditer(text[:authors_end]):
            name = m.group("sur")
            out.append((m.start("sur"), m.end("sur"),
                        " ".join("-".join(x.capitalize() for x in w.split("-")) for w in name.split()),
                        "ref_author_case", None))

    if authors_end:  # "kumari M" -> "Kumari M": a person's surname is never in lower case ("van den Berg" stays)
        for m in re.finditer(r"(?:^|(?<=,\s)|(?<=,))([a-z][\w'’\-]+(?:\s+[a-z][\w'’\-]+)*)(?=\s+[A-Z]{1,4}\b)",
                             text[:authors_end]):
            new = refengine.capital_surname(m.group(1))
            if new != m.group(1):
                out.append((m.start(1), m.end(1), new, "ref_author_case", None))

    for m in re.finditer(r"\bcovid[\s‐‑-]*19\b", text, re.I):  # "covid 19" -> "COVID-19"
        if m.group(0) != "COVID-19":
            out.append((m.start(), m.end(), "COVID-19", "ref_title_case", None))

    max_authors = style.get("references.max_authors")
    if authors_end and max_authors and "et al" not in text[:authors_end + 8]:
        names = [n for n in text[:authors_end].split(",") if n.strip()]
        if len(names) > max_authors:
            cut = len(",".join(names[:max_authors]))
            et_al = style.get("references.et_al", "et al.").rstrip(".")  # the list's own period follows
            if italic:  # the comma and space stay roman: ", " + "et al"
                gap = len(re.match(r",\s*", text[cut:]).group(0))
                out.append((cut + gap, authors_end, et_al, "ref_et_al", italic))
            else:
                out.append((cut, authors_end, f", {et_al}", "ref_et_al", None))

    for m in _ET_AL_DOUBLE.finditer(text):
        out.append((m.end() - 1, m.end(), "", "ref_et_al", None))

    ym = None
    for ym in _YEAR_VOL.finditer(text):
        pass  # the last year;volume is the journal citation
    if ym:
        if style.get("references.no_period_before_year") and ym.group("sep") != " " and ym.start() > 0:
            out.append((ym.start("sep"), ym.end("sep"), " ", "ref_year", None))
        if ym.group("date") or ym.group("semi") != ";":  # "2024 Jan 18; 24:24" -> "2024;24:24" (guide: year;volume)
            out.append((ym.end("year"), ym.start("vol"), ";", "ref_year", None))
        if ym.group("part") and style.get("references.drop_issue"):  # "2000;50 Pt 1:417-22" -> "2000;50:417-22" (guide, References e.g. 3)
            out.append((ym.end("vol"), ym.end("part"), "", "ref_issue", None))
        if style.get("references.drop_issue") and ym.group("issue") and not re.search(r"suppl", ym.group("issue"), re.I):
            out.append((ym.start("issue"), ym.end("issue"), "", "ref_issue", None))

    if ym and style.get("references.journal_abbreviation") == "nlm" and journals.directory():
        j = _journal_span(text, authors_end or 0, ym.start())
        if j:
            start, end, journal = j
            abbr = journal.med_abbr
            if style.get("references.journal_drop_qualifier", True):
                abbr = re.sub(r"\s*\([^)]*\)$", "", abbr)  # "Cancers (Basel)" -> "Cancers"
            if text[start:end] != abbr:
                out.append((start, end, abbr, "ref_journal", None))

    if style.get("references.capitalize_after_colon") and authors_end:
        stop = ym.start() if ym else len(text)
        for m in re.finditer(r":\s([a-z])(?!ttps?:|ttp:|ww\.)", text[authors_end:stop]):  # not "from: http://"
            i = authors_end + m.start(1)
            out.append((i, i + 1, text[i].upper(), "ref_title_case", None))

    if style.get("references.page_range_compress", True):
        m = _PAGES.search(text)
        if m:
            # as a rebuilt entry writes them: "e123-e130" -> "e123-30", "123-123" -> "123"
            new = ":" + refengine.format_pages(f"{m.group(1)}{m.group(2)}-{m.group(3)}{m.group(4)}", style)
            if new != m.group(0):
                out.append((m.start(), m.end(), new, "ref_pages", None))

    if italic:  # "et al." the author wrote, in italics (IJO: guide search-and-replace row 1)
        busy = [(s, e) for s, e, _, _, _ in out]
        for m in _ET_AL_WORD.finditer(text):
            if not any(s < m.end() and m.start() < e for s, e in busy):
                out.append((m.start(), m.end(), m.group(0), "ref_et_al_italic", italic))
    return out


def et_al_italic(style: JournalStyle) -> bool:
    """"et al." in italics in the reference list too (formatting.et_al_italic_references)."""
    return bool(style.get("formatting.enabled", True) and style.get("formatting.et_al_italic", False)
                and style.get("formatting.et_al_italic_references", False))


_ET_AL_WORD = re.compile(r"\bet al\b")


def _label_changes(p: Paragraph, ref: references.Reference, style: JournalStyle) -> list[Change]:
    """The typed label as "1. " (references.label_period): "1.Angus DC" -> "1. Angus DC", "1)" / "[1]" -> "1.";
    a tab after it stays."""
    if not ref.label:
        return []
    m = re.match(r"(\s*)([\[(]?)(\d+)([\])]?\.?)(\s*)$", ref.label)
    if not m:
        return []
    if style.get("references.label_auto_number"):
        # The typed label goes and the entry becomes an item of Word's automatic numbering ("1.<tab>Angus DC"
        # -> "Angus DC" numbered 1.), with any spaces typed before it (" 2.<tab>Lee M" with a no-break space:
        # G143, no space opens a paragraph). A paragraph Word numbers already only loses the typed label.
        c = _change(p, m.start(1), m.end(5), "", Layer.REFERENCES, "ref_label",
                    None if p.numbered else {"list_number": True})
        return [c]
    if not style.get("references.label_period"):
        return []
    out = []
    if m.group(2):
        out.append(_change(p, m.start(2), m.end(2), "", Layer.REFERENCES, "ref_label"))
    gap = m.group(5)
    if "\t" not in gap:  # a tab after the number is the list's own layout (the editors keep it)
        gap = " "
    if m.group(4) == ".":
        if m.group(5) != gap:
            out.append(_change(p, m.start(5), m.end(5), gap, Layer.REFERENCES, "ref_label"))
    else:
        out.append(_change(p, m.start(4), m.end(5), "." + gap, Layer.REFERENCES, "ref_label"))
    return out


def _apply_edits(text: str, edits) -> str:
    for s, e, new, _, _ in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        text = text[:s] + new + text[e:]
    return text


def rule_references(ms: Manuscript, style: JournalStyle) -> list[Change]:
    out = []
    rebuilt = {r.para_id for r, res in lookup_references(ms, style) if res.formatted}
    for ref in references.extract(ms):
        p = ms.by_id(ref.para_id)
        base = ref.offset
        edits = _reference_edits(ref.text, style)
        out += _label_changes(p, ref, style)
        if ref.continuation and ref.para_id not in rebuilt:
            # An entry broken over several paragraphs ("Gass JD. Diseases ..." / "Trans Sect Ophthalmol ... 1977;
            # 83:763-70."): joined into its first paragraph, as the editors do, and cleaned up as one entry.
            whole = ref.full_text
            fixed = _apply_edits(whole, _reference_edits(whole, style)).rstrip()
            changes = word_diff(p, base, ref.text, fixed, "ref_join")
            for pid, _ in ref.continuation:
                q = ms.by_id(pid)
                changes.append(_change(q, 0, len(q.text), "", Layer.REFERENCES, "ref_join"))
            out += changes
            continue
        for s, e, new, rule, fmt in edits:
            if rule == "ref_et_al_italic" and _italic_at(p, base + s, base + e):
                continue
            out.append(_change(p, base + s, base + e, new, Layer.REFERENCES, rule, fmt))
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


def word_diff(p: Paragraph, base: int, old: str, new: str, rule: str, et_al: bool = False) -> list[Change]:
    """Turn a whole-entry rewrite into small word-level changes, so the tracked file stays readable.
    `et_al`: "et al" in the result is italic (an inserted one and one the author wrote)."""
    tok = re.compile(r"et al\b|\s+|\w+|[^\w\s]" if et_al else r"\s+|\w+|[^\w\s]")
    a, b = tok.findall(old), tok.findall(new)
    starts, pos = [], 0
    for t in a:
        starts.append(pos)
        pos += len(t)
    starts.append(pos)
    italic = {"italic": True}
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        s, e = base + starts[i1], base + starts[i2]
        if op == "equal":
            if et_al:
                seg = "".join(a[i1:i2])
                for m in _ET_AL_WORD.finditer(seg):
                    if not _italic_at(p, s + m.start(), s + m.end()):
                        out.append(_change(p, s + m.start(), s + m.end(), m.group(0), Layer.REFERENCES, rule, italic))
            continue
        text = "".join(b[j1:j2])
        m = _ET_AL_WORD.search(text) if et_al else None
        pre, post = (text[:m.start()], text[m.end():]) if m else ("", "")
        if m and not post and pre and e > s:  # "Name G" -> "et al": the rest, then "et al" in italics after it
            out.append(_change(p, s, e, pre, Layer.REFERENCES, rule))
            out.append(_change(p, e, e, m.group(0), Layer.REFERENCES, rule, italic))
        elif m and not pre and post and e > s:
            out.append(_change(p, s, e, m.group(0), Layer.REFERENCES, rule, italic))
            out.append(_change(p, e, e, post, Layer.REFERENCES, rule))
        elif m and not pre and not post:
            out.append(_change(p, s, e, text, Layer.REFERENCES, rule, italic))
        else:
            out.append(_change(p, s, e, text, Layer.REFERENCES, rule))
    return out


def _italic_at(p: Paragraph, start: int, end: int) -> bool:
    fmts = rules_format.char_formats(p)
    return bool(fmts[start:end]) and all(f.get("italic") for f in fmts[start:end])


def rule_ref_lookup(ms: Manuscript, style: JournalStyle) -> list[Change]:
    out = []
    italic = et_al_italic(style)
    for ref, res in lookup_references(ms, style):
        mine = []
        if res.status == refengine.FORMAT_FIXED:
            mine += word_diff(ms.by_id(ref.para_id), ref.offset, ref.text, res.formatted, "ref_lookup", italic)
            for pid, text in ref.continuation:  # the rebuilt entry holds the rest ("doi:..." on its own line)
                p = ms.by_id(pid)
                mine.append(_change(p, 0, len(p.text), "", Layer.REFERENCES, "ref_lookup"))
        elif res.status == refengine.QUERY and res.styled and res.formatted != ref.text:
            # not verified: styled from the author's own fields (refstyle), the entry's query stays
            mine += word_diff(ms.by_id(ref.para_id), ref.offset, ref.text, res.formatted, "ref_styled", italic)
            for pid, text in ref.continuation:
                p = ms.by_id(pid)
                mine.append(_change(p, 0, len(p.text), "", Layer.REFERENCES, "ref_styled"))
        elif res.formatted and res.formatted != ref.text:  # books, links, reports: template fixes
            mine += word_diff(ms.by_id(ref.para_id), ref.offset, ref.text, res.formatted, "ref_other", italic)
            for pid, text in ref.continuation:  # the entry was read and rebuilt with them
                p = ms.by_id(pid)
                mine.append(_change(p, 0, len(p.text), "", Layer.REFERENCES, "ref_other"))
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
    "author_year": rule_author_year,  # "Surname et al. (2015)" -> "Surname et al.[7]" (S320)
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
    "table_indent": rules_tables.rule_indent,  # stub-column indentation as 5 typed spaces per level (G208, G209)
    "statistics": rule_statistics,
    "ref_lookup": rule_ref_lookup,
    "references": rule_references,
    "formatting": rules_format.rule_formatting,  # before guide_replace: its "et al" -> "et al." is also italic
    "guide_replace": rules_guide.rule_guide,
    "format_cleanup": rules_format.rule_cleanup,  # author's colours, stray bold, labels and heads bold
    "format_fonts": rules_format.rule_fonts,  # font family, sizes (12 pt, headings 14 pt) and alignment
    "format_symbols": rules_format.rule_symbols,  # Greek, ±, ≤, ≥, minus in the Symbol font (formatting.symbol_font)
}

# Front matter runs first: it rewrites whole title-page paragraphs (edit_os/frontmatter.py).
from . import frontmatter  # noqa: E402  (frontmatter imports nothing from this module)
from . import leftovers  # noqa: E402
# Template leftovers ("Abstract Page", the title typed again, "Bottom of Form"): right after the front matter, which
# owns the title page; no later rule's edit stays on a paragraph they delete (run_all).
RULES = {"front_matter": frontmatter.rule_front_matter, "leftovers": leftovers.rule_leftovers, **RULES}
RULES = {"submission": frontmatter.submission.rule_submission, **RULES}  # cover letter, reply to reviewers: first


_HEADING_RULES = {"heading_tags", "sections_headings", "subheadings"}


def _drops_paragraph(c: Change, lengths: dict[str, int]) -> bool:
    """`c` deletes all of its paragraph's text."""
    return not c.new and c.start == 0 and bool(c.old.strip()) and c.end == lengths.get(c.para_id, -1)


def _opens_paragraph(c: Change) -> bool:
    from .tracked import PARAGRAPH_BREAK
    return c.new.startswith(PARAGRAPH_BREAK)


def run_all(ms: Manuscript, style: JournalStyle, only: list[str] | None = None) -> ChangeSet:
    """Run rules in order; drop any proposal that overlaps one already kept."""
    kept: list[Change] = []
    lengths = {p.id: len(p.text) for p in getattr(ms, "paragraphs", None) or []}
    dropped_groups: set[str] = set()
    resolved = set()  # reference paragraphs already rebuilt from PubMed / CrossRef
    for name, rule in RULES.items():
        if only and name not in only:
            continue
        if name == "references":
            resolved = {r.para_id for r, res in lookup_references(ms, style)
                        if res.status in (refengine.MATCHED, refengine.FORMAT_FIXED) or res.styled}
        for c in rule(ms, style):
            if name == "references" and c.para_id in resolved and c.reason != "ref_label":
                continue
            if c.reason.startswith("sections_") and _drops_paragraph(c, lengths):
                # A back-matter section moved or deleted as a whole: the heading rules' tag and case edits on its
                # heading line go with it (nothing is left to tag).
                kept = [k for k in kept if not (k.para_id == c.para_id and k.reason in _HEADING_RULES)]
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
            # An entry renumbering moved here replaces the one that was here, with its own edits: the old
            # entry's edits at its edges ("." added at its end) go with it.
            moved = moved or c.reason.startswith("ref_") and c.reason != "ref_label" and any(
                k.para_id == c.para_id and k.reason == "renumber" and k.end - k.start > 3
                and k.start <= c.start <= k.end and k.start <= c.end <= k.end for k in kept)
            if not clash and not same_point and not moved:
                kept.append(c)
            elif getattr(c, "_group", None):
                dropped_groups.add(c._group)
    # Changes that only make sense together (a legend part run on: the text added to the legend and its own
    # paragraph deleted) go together: when one of them lost an overlap, all of them go.
    kept = [c for c in kept if getattr(c, "_group", None) not in dropped_groups]
    # A template leftover deleted whole (leftovers.py): no other edit stays on its paragraph.
    gone = {c.para_id for c in kept if c.reason == leftovers.REASON}
    kept = [c for c in kept if c.para_id not in gone or c.reason == leftovers.REASON]
    # Formatting on text that a kept change replaces is moot (the replacement carries its own
    # format), and showing it would split one edit into two suggestions.
    replaced = [k for k in kept if not k.format_only and k.end > k.start]
    # (The font and size such formatting sets go onto the replacement; the paragraph's alignment stays.)
    kept = rules_format.finish_fonts(ms, style, kept, replaced)
    kept = [c for c in kept if c.reason == "fmt_align" or not (c.format_only and any(
        t.para_id == c.para_id and t.start <= c.start and c.end <= t.end for t in replaced))]
    kept = rules_format.finish_symbols(style, kept)  # the symbols in text a kept change writes
    if not only or "references" in only or "ref_lookup" in only:
        _reference_provenance(ms, style, kept)
    return ChangeSet(kept)
