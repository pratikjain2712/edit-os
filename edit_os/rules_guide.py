"""The pre-editing guide's SEARCH AND REPLACE table, as deterministic rules.

Only the text replacements are here (italics, Symbol font and the like are formatting).
Each entry of the table is a finder `text -> [(start, end, new), ...]`, switched on or off
under `guide_replace:` in the journal style. Defaults follow what the style editors were
measured to do on real articles (see base.yaml).

Scope: body paragraphs only (before the reference list); tables only when
`guide_replace.include_tables` is set. URLs, DOIs and e-mail addresses are never touched,
and the arithmetic rules leave formula-looking paragraphs alone.

Interplay with `statistics.space_around_operators`: that rule owns spacing around
= < > ≤ ≥ between operands. `operator_spacing` here only handles + and ±, and
`unary_comparison` only closes up a comparison that has no left operand, so a journal
that turns the statistics rule off (IJO) does not get binary operator spacing back.

Measured on 160 articles (each rule's proposals on the original text, scored against the
style editor's text and the final copy-edited text: helped / unchanged / hurt, body text):

    rule                 style editor   final text   default
    et_al_period         51/3/3         28/4/2       on
    ampersand            5/0/25         22/0/4       on   (copy editors make it)
    operator_spacing     25/0/7         25/0/7       on   (follows statistics.space_around_operators)
    percent_space        2/0/17         14/1/2       on   (copy editors)
    value_unit_space     13/0/28        35/0/3       on   (copy editors)
    bracket_inner_space  14/4/6         14/5/3       on
    double_period        5/0/2          5/1/0        on
    multiplication       1/8/9          5/8/5        on   (most "unchanged" are a Symbol-font ×); off for IJO
    nbsp                 75/25/74       68/28/62     off  (about half are left)
    unary_minus          0/0/53         15/17/20     off  ("unchanged" = editor used an en dash)
    unary_comparison     2/0/30         17/0/12      off
    value_bracket_space  0/3/20         0/3/19       off
    degree_space         0/0/1          0/1/0        off  (too rare to tell)
    technetium           0/0/0          0/0/0        off  (no occurrences)
    p_value_capital      0/3/546        154/5/384    off  (copy editors, a third of the time)
"""

from __future__ import annotations

import re
from itertools import count

from . import references
from .changes import Change, Layer, Source
from .docmodel import Manuscript, Paragraph
from .style import JournalStyle

_ids = count(1)

_NUM = r"\d+(?:\.\d+)?"
_SP = r"[ \u00a0]"  # an ordinary or non-breaking space (never a tab or line break)

# --- protected spans -------------------------------------------------------------------

_PROTECT = re.compile(
    r"(?:https?://|ftp://|www\.)\S+"           # URLs
    r"|\b(?:doi:\s*)?10\.\d{4,9}/\S+"           # DOIs
    r"|\S+@\S+\.\w+",                           # e-mail addresses
    re.I,
)
# A paragraph that is mostly a formula: "BMI = weight (kg)/height (m)^2", "Y = a + bX - c".
_FORMULA = re.compile(r"^[^.!?]{0,160}?[A-Za-z]\w*\s*=\s*[^.!?]*?[\w)]\s*(?:[+−*/^×÷]|\s-\s)\s*[\w(√∑Σ]")


# A reference entry that escaped the reference-list detection: "2013;52(1):53-61", "J Med 2001;3:1-9".
_REFERENCE_LIKE = re.compile(r"\b(?:19|20)\d{2}(?:\s+[A-Z][a-z]{2}(?:\s+\d{1,2})?)?\s*;\s*\d+\s*(?:\([^)]*\))?\s*:\s*[A-Za-z]?\d+")


def looks_like_reference(text: str) -> bool:
    return bool(_REFERENCE_LIKE.search(text)) and len(text) < 600


def _protected(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _PROTECT.finditer(text)]


def _looks_like_formula(text: str) -> bool:
    t = text.strip()
    return len(t) < 200 and bool(_FORMULA.match(t)) and len(re.findall(r"[A-Za-z]{4,}", t)) < 8


# --- finders ---------------------------------------------------------------------------

def find_et_al(text: str, cfg: dict) -> list:
    """1. "et al" -> "et al." (the period is part of the abbreviation)."""
    return [(m.end() - 2, m.end(), "al.") for m in re.finditer(r"\bet\s+al\b(?![.])", text)]


# What follows the second of two authors' surnames: a year or a citation number ("Chaturvedi & Tripathi [8]",
# "Sun & Kao (2024)", "(Pham & Taylor, 1999)", "Goss & Creutzfeldt¹²").
_AUTHOR_PAIR_AFTER = re.compile(r"[\w’'\-]*(?:\s+[A-Z]{1,3}\b\.?)?\s*(?:\(\s*(?:19|20)\d{2}|,\s*(?:19|20)\d{2}\b|\[\s*\d|[⁰¹²³⁴⁵⁶⁷⁸⁹]"
                                r"|\(\s*\d{1,3}\s*(?:[,\-–]\s*\d{1,3}\s*)*\))")


def find_ampersand(text: str, cfg: dict) -> list:
    """4. "&" -> "and" outside references. Joined forms (H&E, R&D) and "&" between two
    capitalised words (names, places, titles: "Jammu & Kashmir") are kept, unless they are two authors' surnames
    followed by the year or the citation ("Chaturvedi & Tripathi [8]" -> "Chaturvedi and Tripathi [8]")."""
    out = []
    for m in re.finditer(r"(?<=\w)(" + _SP + r"+)&(" + _SP + r"+)(?=\w)", text):
        left = re.search(r"(\w+)$", text[:m.start()]).group(1)
        right = re.match(r"\w+", text[m.end():]).group(0)
        if left[:1].isupper() and right[:1].isupper() and not _AUTHOR_PAIR_AFTER.match(text, m.end()):
            continue  # names and titles: "H & E", "Jammu & Kashmir", "Johnson & Johnson"
        if f"{left} & {right}".lower() == "review & editing":
            continue  # CRediT role, written with "&"

        out.append((m.start(), m.end(), " and "))
    return out


def find_operator_spacing(text: str, cfg: dict) -> list:
    """7. "a+b" -> "a + b" for + and ± between numbers ("5.2±1.3" -> "5.2 ± 1.3").
    Markers such as CD4+, ER+, 2+ (grades) and +ve have no number on both sides."""
    ops = re.escape(cfg.get("operators", "+±"))
    out = []
    for m in re.finditer(r"(?<=\d)(" + _SP + r"*)([" + ops + r"])(" + _SP + r"*)(?=\d)", text):
        start = m.start()
        # the left number must be a value, not part of a name like CD4 or IL6
        lead = re.search(r"[\w.,]*$", text[:start]).group(0)
        if not re.fullmatch(_NUM, lead):
            continue  # CD4+8, 42,3±13,5 (decimal comma) and the like
        if m.group(2) == "+" and re.match(r"\d+\s*(?:weeks?|wks?|days?|d)\b", text[m.end():]):
            continue  # gestational age "32+3 weeks"
        new = f" {m.group(2)} "
        if m.group(0) != new:
            out.append((start, m.end(), new))
    if "±" in ops:  # "Mean±SD" (a table head, 1559) -> "Mean ± SD"
        for m in re.finditer(r"\b(?:Mean|mean|MEAN|M)(" + _SP + r"*±" + _SP + r"*)(?=(?:SD|SE|SEM|S\.D\.|S\.E\.)\b)", text):
            if m.group(1) != " ± ":
                out.append((m.start(1), m.end(1), " ± "))
    return out


def find_nbsp(text: str, cfg: dict) -> list:
    """8. non-breaking space -> keyboard space."""
    return [(m.start(), m.end(), " ") for m in re.finditer("\u00a0", text)]


def find_percent(text: str, cfg: dict) -> list:
    """9. "50 %" -> "50%"."""
    return [(m.start(), m.end(), "") for m in re.finditer(r"(?<=\d)" + _SP + r"+(?=%)", text)]


_UNARY_CMP_LEFT = re.compile(r"(?:^|[(\[:,;]|\b(?:of|with|to|was|were|is|are|by|from|at|and|or|between|"
                             r"than|in|be|if|aged|either|as|having|had|have|showing)\b)$", re.I)  # not "P value < 0.05",
# "age ≥ 6": there the word before is the left operand (as in "BMI > 30")


def find_unary_minus(text: str, cfg: dict) -> list:
    """10. "with -10" -> "with −10": hyphen to minus sign when it has no left operand.
    Ranges ("10 -20", "5% -8%") and hyphenated words ("IL-6", "anti-VEGF") are left alone."""
    out = []
    for m in re.finditer(r"(?:(?<=^)|(?<=[\s(\[]))-(?=\.?\d)", text):
        before = text[:m.start()].rstrip(" \u00a0")
        if not re.search(r"[\d)\]%]$", before):
            out.append((m.start(), m.end(), "−"))
    # G300 / G301 "no space after a unary sign": "(- 0.5 to 0.3)", "of − 2.1", a table cell "- 0.25" -> "−0.25" when
    # nothing before it is a left operand (a bracket, a comma, a function word; at the start only a lone number, not
    # a list dash "- 5 patients").
    for m in re.finditer(r"(?:(?<=^)|(?<=[\s(\[]))([-−])(" + _SP + r"+)(?=\.?\d)", text):
        before = text[:m.start()].rstrip(" \u00a0")
        if before and not _UNARY_CMP_LEFT.search(before):
            continue
        if not before and not re.fullmatch(r"[-−]" + _SP + r"+[\d.,]+\s*(?:%|\([^()]*\)|\[[^\[\]]*\])?\s*", text):
            continue
        out.append((m.start(), m.end(), "−"))
    return out


def find_unary_comparison(text: str, cfg: dict) -> list:
    """11. "> 10" -> ">10" when the comparison has no left operand: after an opening bracket,
    a comma or a function word ("(> 250 μm)", "20 to < 30 ng/mL", "was ≥ 6"). "P < 0.05",
    "BMI > 30" and "scores < 7" keep their spaces: there the operator is binary."""
    out = []
    for m in re.finditer(r"([<>≤≥])(" + _SP + r"+)(?=[−-]?\d|\.\d)", text):
        before = text[:m.start()].rstrip(" \u00a0")
        if _UNARY_CMP_LEFT.search(before):
            out.append((m.start(2), m.end(2), ""))
    return out


# The guide's units (rows 12-19, 25, 26), and the same rule for units its table does not list: mmHg, nJ, gm
# ("50mmHg", "20nJ", "1gm/day": the IJO editors space them).
_DEFAULT_UNITS = ["μg", "µg", "μs", "µs", "Hertz", "μL", "µL", "μl", "µl", "mm", "cm", "km", "mg", "rpm", "nm",
                  "mmHg", "nJ", "μJ", "µJ", "mJ", "gm"]


def find_value_unit(text: str, cfg: dict) -> list:
    """12-19, 25, 26. "123mg" -> "123 mg" for the guide's units (configurable)."""
    units = sorted(cfg.get("units") or _DEFAULT_UNITS, key=len, reverse=True)
    pat = re.compile(r"(?:(?<=[xX×])|(?<![\w.,\-/]))" + _NUM + r"(?=(?:" + "|".join(map(re.escape, units)) + r")(?![A-Za-z]))")
    return [(m.end(), m.end(), " ") for m in pat.finditer(text)]


def find_value_bracket(text: str, cfg: dict) -> list:
    """20. "20(80)" -> "20 (80)": a value directly followed by a bracketed value."""
    pat = re.compile(r"(?<![\w.,\-/])" + _NUM + r"%?(?=\(\s*\d[\d.,%\s]*\)(?![:\w]))")
    return [(m.end(), m.end(), " ") for m in pat.finditer(text)]


_BOOLEAN = re.compile(r"(?<![\w-])(?:AND|OR|NOT)(?![\w-])")  # search operators, in capitals


def find_bracket_space(text: str, cfg: dict) -> list:
    """21. "( 80 )" -> "(80)": no space inside brackets. Not in a database search string
    ('TITLE ( digital ) OR TITLE ( "cardiac rehab" )'), which is quoted as the author ran it."""
    if len(_BOOLEAN.findall(text)) >= 2:
        return []
    out = []
    for m in re.finditer(r"\((" + _SP + r"+)(?=[^\s)])", text):
        out.append((m.start(1), m.end(1), ""))
    for m in re.finditer(r"(?<=[^\s(])(" + _SP + r"+)\)", text):
        out.append((m.start(1), m.end(1), ""))
    return out


def find_double_period(text: str, cfg: dict) -> list:
    """22. "80.." -> "80." (an ellipsis "..." is kept)."""
    return [(m.start() + 1, m.end(), "") for m in re.finditer(r"(?<=[\w)\]%])\.\.(?![.…])", text)]


def find_degree(text: str, cfg: dict) -> list:
    """23. "29 °C" -> "29°C" (also "29° C" and the ordinal sign "29 ºC")."""
    return [(m.start(), m.end(), "°" + m.group(1))
            for m in re.finditer(r"(?<=\d)" + _SP + r"*[°º]" + _SP + r"*([CF])\b", text)
            if m.group(0) != "°" + m.group(1)]


def find_multiplication(text: str, cfg: dict) -> list:
    """24. "10x20" -> "10 × 20": a letter x between two numbers (dimensions, magnification), and an existing
    "×" closed up between them ("3.5×3.4 mm"). Powers of ten stay as typed ("150×10⁹/L", "2×109/L")."""
    out = []
    for m in re.finditer(r"(?<![\w.,\-/])(" + _NUM + r")(" + _SP + r"*[xX×]" + _SP + r"*)(?=\d)", text):
        if "×" in m.group(2):
            if m.group(2) == " × " or re.match(r"10(?:[⁰¹²³⁴⁵⁶⁷⁸⁹\d^⁻]|[-−–]\s*\d|\s*[-−]?\s*[⁰¹²³⁴⁵⁶⁷⁸⁹])", text[m.end():]):
                continue
        out.append((m.start(2), m.end(2), " × "))
    return out


def find_technetium(text: str, cfg: dict) -> list:
    """30. "99 mTc" -> "99mTc"."""
    return [(m.start(1), m.end(1), "") for m in re.finditer(r"\b99(" + _SP + r"+)m" + _SP + r"*Tc\b", text, re.I)]


def find_p_value(text: str, cfg: dict) -> list:
    """5. probability "p" -> "P" ("p = 0.03", "p<0.05", "p = NS", "p value", "p-value"; a table head that is the
    letter alone, "p" or "p*")."""
    lone = re.fullmatch(r"\s*(p)\s*[*†‡a-c]?\s*", text)
    if lone:
        return [(lone.start(1), lone.end(1), "P")]
    return [(m.start(), m.end(), "P")
            for m in re.finditer(r"(?<![\w.\-])p(?=" + _SP + r"*[=<>≤≥]" + _SP + r"*(?:[0.]|NS\b)|[\- ][Vv]alues?\b)", text)]


def find_comparison_symbol(text: str, cfg: dict) -> list:
    """28. ">=" -> "≥", "<=" -> "≤" (G319: "less than and equal to/greater than or equal to" as the symbols)."""
    return [(m.start(), m.end(), "≥" if m.group(0)[0] == ">" else "≤")
            for m in re.finditer(r"(?<![<>=])[<>]" + _SP + r"?=(?![=>])", text)]


_NUM_RANGE = re.compile(r"(?<![\w\-/.\[,:])(\d+(?:\.\d+)?)( ?)-( ?)(\d+(?:\.\d+)?)(?![\w\-/\]]|\.\d)")


def find_range_dash(text: str, cfg: dict) -> list:
    """Numeric ranges with an en dash: "2-6 years", "(1.01-1.06)", "4 -6" -> "2–6", "(1.01–1.06)", "4–6".
    Hyphenated terms (COVID-19, IL-6), citations ([6-9], kept with a hyphen), dates written 12-05-2024 and
    spaced subtractions ("5 - 3") are left alone."""
    out = []
    # Round brackets holding only whole numbers ("(11-13)", "(23, 29-31)") are reference citations the citation
    # rule turns into "[11-13]" with a hyphen (S: "For range, en dash should be changed to hyphen").
    cites = [b.span() for b in re.finditer(r"\(\s*\d{1,3}(?:\s*[-–,;]\s*\d{1,3})*\s*\)", text)]
    for m in _NUM_RANGE.finditer(text):
        if m.group(2) and m.group(3):
            continue  # "5 - 3": arithmetic
        if any(s < m.start() and m.end() < e for s, e in cites):
            continue
        if re.match(r"-\d", text[m.end():]) or re.search(r"\d-$", text[:m.start()]):
            continue
        out.append((m.end(1), m.start(4), "–"))
    return out


def find_trailing_space(text: str, cfg: dict) -> list:
    """Spaces at the end of a paragraph go ("... the study.  " -> "... the study.")."""
    m = re.search(_SP + r"+$", text)
    return [(m.start(), m.end(), "")] if m and text.strip() else []


def find_leading_space(text: str, cfg: dict) -> list:
    """Spaces at the start of a paragraph go (tabs are layout and stay)."""
    m = re.match(_SP + r"+", text)
    return [(m.start(), m.end(), "")] if m and text.strip() else []


def find_double_space(text: str, cfg: dict) -> list:
    """Two or more spaces between words -> one ("and  control" -> "and control")."""
    return [(m.start(), m.end(), " ") for m in re.finditer(r"(?<=\S)" + _SP + r"{2,}(?=\S)", text)]


def find_smart_quotes(text: str, cfg: dict) -> list:
    """Straight quotes -> typographic ones: "caregivers' knowledge" -> "caregivers’ knowledge", "it's" -> "it’s",
    'the "safe" place' -> 'the “safe” place'. Quotes after a digit (5'10", primes) are left as typed."""
    out = []
    for m in re.finditer(r"['\"]", text):
        i, q = m.start(), m.group(0)
        before, after = text[i - 1:i], text[i + 1:i + 2]
        if before.isdigit():
            continue
        opening = (not before or before.isspace() or before in "([{“‘—–-/") and bool(after) and not after.isspace()
        closing = bool(before) and not before.isspace()
        if q == "'":
            new = "‘" if opening else "’" if closing else None
        else:
            new = "“" if opening else "”" if closing else None
        if new:
            out.append((i, i + 1, new))
    return out


_ABBR_LIST = re.compile(r"^\s*(?:Abbreviations?|Abbr\.)\s*:\s*", re.I)


def find_abbreviation_colon(text: str, cfg: dict) -> list:
    """An abbreviation list "Abbreviations: SCT, social cognitive theory; CI, confidence interval" ->
    "SCT: social cognitive theory; CI: confidence interval" (stylesheet: "term 1: definition")."""
    m = _ABBR_LIST.match(text)
    if not m:
        return []
    out = []
    pos = m.end()
    for seg in re.split(r"(;\s*)", text[pos:]):
        a = re.match(r"([^\s,;:=]{1,15}),(?=\s)", seg)
        if a and re.search(r"[A-Z]", a.group(1)) and not seg.startswith(" "):
            out.append((pos + a.end() - 1, pos + a.end(), ":"))
        pos += len(seg)
    return out if len(out) >= 2 else []


# Owner, 2026-10-27 (JEHP; the sheet and the guide are silent): a decimal below one takes its zero ".413" -> "0.413"
# (the editors 28 times, never for a P value: ".05" after "P <" stays, 8 of 8); a four-digit number loses its comma
# "1,260" -> "1260" (five digits and more keep it: "12,345").
_P_BEFORE = re.compile(r"(?:\b[pP]|\bP[\s-]*values?(?:\s+(?:of|was|is))?)" + _SP + r"*[<>=≤≥]?" + _SP + r"*$")


def find_leading_zero(text: str, cfg: dict) -> list:
    """".413" -> "0.413" (not a P value, unless leading_zero_p is set)."""
    out = []
    for m in re.finditer(r"(?:(?<=^)|(?<=[\s(\[=<>≤≥±:;,/−–-]))\.(?=\d)", text):
        before = text[:m.start()]
        if re.search(r"\d$", before):
            continue  # "3.5", "1 .5" typed apart: not ours to read
        if not cfg.get("leading_zero_p") and _P_BEFORE.search(before):
            continue
        out.append((m.start(), m.start(), "0"))
    return out


def find_thousands_comma(text: str, cfg: dict) -> list:
    """"1,260" -> "1260"; "12,345" keeps its comma. Not inside square brackets (citations "[1,234]" do not reach
    four digits, but a list of values might)."""
    out = []
    for m in re.finditer(r"(?<![\d,.])(\d),(\d{3})(?![\d,]|\.\d{3})", text):
        if text.rfind("[", 0, m.start()) > text.rfind("]", 0, m.start()):
            continue
        out.append((m.start() + 1, m.start() + 2, ""))
    return out


# Owner, 2026-10-27 (b): statistical symbols in italics as the editors set them (the sheet is silent): F, t, d, r,
# R², β, η, α, Z, k, χ² standing for a statistic, before its value ("F = 74.82", "t(48) = 2.1", "η² = 0.42",
# "χ²(2) = 5.1"), as a table head ("F", "β", "Z value") and "Cronbach's α"; never inside a word or a unit.
_STAT_SYMBOLS = "FtdrRZkβηαχ"
_STAT_VALUE = re.compile(r"(?<![\w.\-/'’\u0370-\u03ff])([" + _STAT_SYMBOLS + r"])(?:[²₀₁₂₃₄₅₆₇₈₉]|2(?=" + _SP + r"*[=(<>]))?"
                         r"(?=" + _SP + r"*(?:\(\s*\d[\d.,\s]*\)" + _SP + r"*)?[=<>≤≥]" + _SP + r"*[-−]?" + _SP + r"*\.?\d)")
_STAT_HEAD = re.compile(r"^\s*([" + _STAT_SYMBOLS + r"])(?:[²2])?(?:\s*[-\s][Vv]alues?|\s+statistics?|\s*\([^()]{1,12}\))?\s*[*†‡]?\s*$")
_STAT_WORD = re.compile(r"(?<![\w.\-/'’])([FtZrχ])(?:²)?(?=[\s-][Vv]alues?\b)|\bCronbach[’']s\s+(α)")


def stat_italic_spans(text: str, in_table: bool = False) -> list[tuple[int, int]]:
    """Where a statistical symbol stands (owner, 2026-10-27 (b)): the symbol letter only."""
    out = [m.span(1) for m in _STAT_VALUE.finditer(text)]
    for m in _STAT_WORD.finditer(text):
        g = 1 if m.group(1) else 2
        out.append(m.span(g))
    if in_table:
        h = _STAT_HEAD.match(text)
        if h:
            out.append(h.span(1))
    return sorted(set(out))


FINDERS = {
    "et_al_period": find_et_al,
    "ampersand": find_ampersand,
    "operator_spacing": find_operator_spacing,
    "nbsp": find_nbsp,
    "percent_space": find_percent,
    "unary_minus": find_unary_minus,
    "unary_comparison": find_unary_comparison,
    "value_unit_space": find_value_unit,
    "value_bracket_space": find_value_bracket,
    "bracket_inner_space": find_bracket_space,
    "double_period": find_double_period,
    "degree_space": find_degree,
    "multiplication": find_multiplication,
    "technetium": find_technetium,
    "p_value_capital": find_p_value,
    "comparison_symbol": find_comparison_symbol,
    "range_dash": find_range_dash,
    "trailing_space": find_trailing_space,
    "leading_space": find_leading_space,
    "double_space": find_double_space,
    "smart_quotes": find_smart_quotes,
    "abbreviation_colon": find_abbreviation_colon,
    "leading_zero": find_leading_zero,
    "thousands_comma": find_thousands_comma,
}
# Spacing rules: never next to content the text view leaves out (symbol-font ±, fields, drawings).
_SPACING = {"trailing_space", "leading_space", "double_space"}
# Rules that read arithmetic and must not run inside formula-looking paragraphs.
_ARITHMETIC = {"operator_spacing", "unary_minus", "unary_comparison", "multiplication", "range_dash"}


def propose(text: str, cfg: dict) -> list[tuple[int, int, str, str]]:
    """(start, end, new, rule) for every enabled finder, outside protected spans."""
    if looks_like_reference(text):
        return []
    guard = _protected(text)
    formula = _looks_like_formula(text)
    out = []
    for name, finder in FINDERS.items():
        if not cfg.get(name):
            continue
        if formula and name in _ARITHMETIC:
            continue
        for s, e, new in finder(text, cfg):
            if any(gs <= s and e <= ge or (s < ge and gs < e) for gs, ge in guard):
                continue
            if text[s:e] != new:
                out.append((s, e, new, name))
    return out


def _paragraphs(ms: Manuscript, include_tables: bool) -> list[Paragraph]:
    end = references.find_heading(ms)
    paras = ms.paragraphs if end is None else ms.paragraphs[:end]
    if include_tables and end is not None:  # tables placed after the reference list, with their captions
        from .rules_figures import caption_match

        paras = paras + [p for p in ms.paragraphs[end:] if p.in_table or caption_match(p) is not None]
    return [p for p in paras if p.text.strip() and (include_tables or not p.in_table)
            and not p.text.lstrip().startswith("<H")]


def settings(style: JournalStyle) -> dict:
    """The journal's guide_replace settings. operator_spacing follows statistics.space_around_operators
    (a journal whose editor does not space operators turns that off) unless set to "always"."""
    cfg = dict(style.get("guide_replace") or {})
    if cfg.get("operator_spacing") != "always" and not style.get("statistics.space_around_operators"):
        cfg["operator_spacing"] = False
    return cfg


def rule_guide(ms: Manuscript, style: JournalStyle) -> list[Change]:
    from .copyedit.common import hidden_offsets, near_hidden

    cfg = settings(style)
    in_tables = set(cfg.get("table_rules") or [])  # finders that also run in tables when include_tables is off
    notes: set[str] = set()
    if cfg.get("abbreviation_colon") and style.get("figures.table_footnote_abbreviation"):
        from .rules_figures import footnote_paragraphs  # table notes take the journal's own form (rule_captions)
        notes = {q.id for q in footnote_paragraphs(ms)}
    out = []
    for p in _paragraphs(ms, bool(cfg.get("include_tables") or in_tables)):
        pcfg = cfg if not p.in_table or cfg.get("include_tables") else {k: v for k, v in cfg.items() if k in in_tables}
        if p.id in notes:
            pcfg = {**pcfg, "abbreviation_colon": False}
        hidden = None
        for s, e, new, name in propose(p.text, pcfg):
            if name in _SPACING:
                hidden = hidden_offsets(p) if hidden is None else hidden
                if near_hidden(s, e, hidden):
                    continue
            out.append(Change(id=f"g{next(_ids)}", para_id=p.id, start=s, end=e, old=p.text[s:e], new=new,
                              layer=Layer.NUMBERS if name in _NUMBER_RULES else Layer.HOUSE_STYLE, source=Source.RULE,
                              reason=f"guide.{name}"))
        if pcfg.get("stat_italic") and not looks_like_reference(p.text):
            from .rules_format import char_formats

            fmts = None
            for s, e in stat_italic_spans(p.text, p.in_table):
                fmts = char_formats(p) if fmts is None else fmts
                if len(fmts) == len(p.text) and all(f.get("italic") for f in fmts[s:e]):
                    continue
                out.append(Change(id=f"g{next(_ids)}", para_id=p.id, start=s, end=e, old=p.text[s:e],
                                  new=p.text[s:e], layer=Layer.NUMBERS, source=Source.RULE, reason="guide.stat_italic",
                                  format={"italic": True}))
    return out


# --- double spaces left by other changes ----------------------------------------------------------------------------

_RUN = re.compile(r"(?<=\S)[  ]{2,}(?=\S)")


def collapse_double_spaces(ms: Manuscript, style: JournalStyle, changes: list[Change]) -> list[Change]:
    """Double spaces the output would still hold once every change is applied (where the journal's double_space
    rule is on): those the rule's own proposal lost to an overlapping change ("(1).  It" where the citation moves),
    and those another change makes ("  " once the no-break space becomes a space, a deletion between two spaces).

    Runs after all rules and checkers, on the text as the changes leave it. A run made only of the manuscript's own
    spaces gets a deletion of all but one; a run that holds spaces a change inserts keeps that one and loses the
    manuscript's; a change whose replacement itself holds two spaces in a row has them closed up (unless its
    replacement carries per-piece formatting). New changes are returned; touched changes are edited in place."""
    from .copyedit.common import near_hidden
    from .tracked import PARAGRAPH_BREAK

    cfg = settings(style)
    if not cfg.get("double_space"):
        return []
    tables = bool(cfg.get("include_tables")) or "double_space" in (cfg.get("table_rules") or [])
    by_para: dict[str, list[Change]] = {}
    for c in changes:
        if not c.format_only:
            by_para.setdefault(c.para_id, []).append(c)
    out: list[Change] = []
    for p in _paragraphs(ms, tables):
        if p.in_table and not tables or looks_like_reference(p.text):
            continue
        cs = sorted(by_para.get(p.id, []), key=lambda c: (c.start, c.end))
        if any(b.start < a.end for a, b in zip(cs, cs[1:])):
            continue  # overlapping proposals: the renderer reports them; nothing to add here
        text = p.text
        # the projected text, with each character's origin: an index into `text`, or (change, index into its new)
        proj, origin, pos = [], [], 0
        for c in cs + [None]:
            stop = len(text) if c is None else c.start
            for k in range(pos, stop):
                proj.append(text[k])
                origin.append(k)
            if c is None:
                break
            for k, ch in enumerate(c.new):
                proj.append(ch)
                origin.append((c, k))
            pos = c.end
        projected = "".join(proj)
        guard = _protected(text)
        hidden = None
        for m in _RUN.finditer(projected):
            if PARAGRAPH_BREAK in projected[max(0, m.start() - 1):m.end() + 1]:
                continue
            src = [o for o in origin[m.start():m.end()] if isinstance(o, int)]
            ins = [o for o in origin[m.start():m.end()] if not isinstance(o, int)]
            if src:
                if any(gs <= s < ge for s in src for gs, ge in guard):
                    continue
                hidden = _shown_hidden(p) if hidden is None else hidden
                if near_hidden(min(src), max(src) + 1, hidden):
                    continue
            drop = src if ins else [s for s in src if s != next((x for x in src if text[x] == " "), src[0])]
            if ins and len(ins) > 1:
                _close_up(ins)
            for lo, hi in _ranges(drop):
                if _absorb(cs, text, lo, hi):
                    continue
                out.append(Change(id=f"g{next(_ids)}", para_id=p.id, start=lo, end=hi, old=text[lo:hi], new="",
                                  layer=Layer.HOUSE_STYLE, source=Source.RULE, reason="guide.double_space"))
    return out


def _absorb(cs: list[Change], text: str, lo: int, hi: int) -> bool:
    """Take the spaces text[lo:hi] into the change right next to them, so they go only if that change is accepted
    (a deletion between two spaces: rejected, both spaces come back). Not when another change also touches that
    point (an insertion there would no longer be ordered with it) or the change formats its text piece by piece."""
    for c in cs:
        if c.end == lo:
            point, grow = lo, "end"
        elif c.start == hi:
            point, grow = hi, "start"
        else:
            continue
        if isinstance((c.segment_format or [None])[0], list) or c.new.startswith("\u2029") or \
                any(d is not c and (d.start == point or d.end == point) for d in cs):
            continue
        if grow == "end":
            c.end = hi
        else:
            c.start = lo
        c.old = text[c.start:c.end]
        return True
    return False


def _shown_hidden(p: Paragraph) -> list[int]:
    """Offsets where the paragraph shows something the text view leaves out (a Symbol-font character, a drawing,
    an object): a space there is not a double space. Field boundaries are not among them: a field's result is in
    the text view (EndNote's "(1)"), so the spaces around it are what the reader sees."""
    from docx.oxml.ns import qn

    from .docmodel import run_text, text_runs

    shown = {qn("w:sym"), qn("w:drawing"), qn("w:object"), qn("w:pict")}
    out, pos = [], 0
    for r in text_runs(p.element):
        if any(c.tag in shown for c in r):
            out.append(pos)
        pos += len(run_text(r))
    return out


def _ranges(idx: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for i in sorted(idx):
        if out and out[-1][1] == i:
            out[-1] = (out[-1][0], i + 1)
        else:
            out.append((i, i + 1))
    return out


def _close_up(ins: list) -> None:
    """Spaces from replacement texts in one run: keep the first, take the others out of their change's new text
    (a change with per-piece formatting is left as it is)."""
    for c, k in sorted(ins[1:], key=lambda o: -o[1]):
        if isinstance((c.segment_format or [None])[0], list):
            continue
        c.new = c.new[:k] + c.new[k + 1:]


_NUMBER_RULES = {"leading_zero", "thousands_comma"}
