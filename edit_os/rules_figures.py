"""Figure and table citations in the text, figure and table captions, and their author queries.

Body citations: round/curly brackets -> square, "Fig"/"Figure" per journal, "Picture" -> "Figure",
Roman -> Arabic, part labels in the journal's case, "Figure 1a to 1d" -> "Figure 1a-d",
"Figure 1 and Figure 2" -> "Figures 1 and 2". Captions: "Table #: " / "Figure #: " labels, no end
period. Queries: citations out of order, a citation with no caption, a caption never cited.

Settings live under `figures:` in the style files. This module imports `rules` lazily, because
`rules` registers these functions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import difflib

from .changes import Change, Layer
from .docmodel import Manuscript, Paragraph
from .style import JournalStyle

FIG, TABLE, GRAPH = "Figure", "Table", "Graph"


def _q():
    from . import queries  # queries imports rules, which imports this module
    return queries


QUERY_TEXT = {
    "figures.order": "{kind} citation not in order.",
    "figures.no_caption": "Kindly provide the caption for Figure {n}.",
    "figures.no_table": "Table {n} is cited in the text but not provided. Kindly provide.",
    "figures.uncited": "{kind} {n} has not been cited in the text part. Kindly cite.",
    "figures.part_case": "Figure part labels have been changed to lower case. Kindly check.",
}


# --- parsing -------------------------------------------------------------------------

_ROMAN = {"I": 1, "V": 5, "X": 10}


def roman_to_int(s: str) -> int | None:
    if not s or not re.fullmatch(r"X{0,3}(?:IX|IV|V?I{0,3})", s):
        return None
    total = 0
    for i, c in enumerate(s):
        v = _ROMAN[c]
        total += -v if i + 1 < len(s) and _ROMAN[s[i + 1]] > v else v
    return total if 0 < total < 40 else None


_LABEL = re.compile(
    r"(?P<supp>(?:Supplementary|Suppl\.?|Supplemental)\s+)?"
    r"(?P<word>Fig(?:ure)?s?|FIG(?:URE)?S?|Pictures?|PICTURES?|Tables?|TABLES?)(?![A-Za-z])(?P<dot>\.?)",
)
_LABEL_ANYCASE = re.compile(
    r"(?P<supp>(?:Supplementary|Suppl\.?|Supplemental)\s+)?"
    r"(?P<word>fig(?:ure)?s?|pictures?|tables?)(?![a-z])(?P<dot>\.?)", re.I)
_NUM = re.compile(r"\d{1,2}(?!\d)|[IVX]{1,5}(?![A-Za-z])")
_PART = re.compile(r"\(([a-zA-Z])\)|-?([A-Za-z])(?![A-Za-z0-9])")
_BARE_PART = re.compile(r"\(([A-Za-z])\)|([A-Za-z])(?=\s*(?:[)\]},;.:]|and\b|&|$))")
_RANGE = re.compile(r"\s*(-|–|—|\bto\b)\s*")
_JOIN = re.compile(r"\s*(,|;|\band\b|&)\s*")
_FIRST_SEP = re.compile(r"(?:\s*[-_]\s*|\s+|)(?:No\.?\s*)?", re.I)


def _kind(word: str) -> str:
    w = word.lower()
    return TABLE if w.startswith("tab") else FIG


@dataclass
class Item:
    num: int | None  # None: a bare part sharing the previous number ("2A and B")
    part: str = ""
    to_num: int | None = None
    to_part: str = ""
    dash: str = ""  # original range separator


@dataclass
class Segment:
    kind: str
    supp: str
    items: list[Item] = field(default_factory=list)
    joins: list[str] = field(default_factory=list)  # join before each item after the first
    join_before: str = ""  # join before this segment's label (segments after the first)


@dataclass
class Group:
    start: int
    end: int
    segments: list[Segment]


def _parse_num(text: str, i: int):
    m = _NUM.match(text, i)
    if not m:
        return None, i
    s = m.group(0)
    n = int(s) if s.isdigit() else roman_to_int(s)
    return n, m.end() if n else i


def _parse_part(text: str, i: int):
    m = _PART.match(text, i)
    if not m:
        return "", i
    return m.group(1) or m.group(2), m.end()


def _parse_item(text: str, i: int, bare_ok: bool):
    """One item at `i`: 1, 1a, 1(a), 1-A, 1a-d, 1a to 1d, 1-3, II; or a bare part if allowed."""
    n, j = _parse_num(text, i)
    if n is None:
        if bare_ok:
            m = _BARE_PART.match(text, i)
            if m:
                return Item(None, m.group(1) or m.group(2)), m.end()
        return None, i
    part, j = _parse_part(text, j)
    item = Item(n, part)
    r = _RANGE.match(text, j)
    if r:
        k = r.end()
        n2, k2 = _parse_num(text, k)
        if n2 is not None:
            p2, k3 = _parse_part(text, k2)
            if n2 > n or (n2 == n and part and p2):
                item.to_num, item.to_part, item.dash, j = n2, p2, r.group(1), k3
        elif part:
            m = _BARE_PART.match(text, k)
            if m:
                item.to_part, item.dash, j = m.group(1) or m.group(2), r.group(1), m.end()
    return item, j


def parse_groups(text: str, anycase: bool = False) -> list[Group]:
    """Figure/table citation groups in `text`, e.g. "Table 2, Figure 1" or "Figs. 2a and 2b"."""
    label_re = _LABEL_ANYCASE if anycase else _LABEL
    out: list[Group] = []
    pos = 0
    while True:
        m = label_re.search(text, pos)
        if not m:
            break
        start = m.start()
        segs: list[Segment] = []
        i = m.end()
        lab = m
        join_before = ""
        bracketed = text[:start].rstrip(" ")[-1:] in ("(", "[", "{")
        while lab is not None:
            plural = bool(re.search(r"(?:s|S)$", lab.group("word")))
            seg = Segment(_kind(lab.group("word")), lab.group("supp") or "", join_before=join_before)
            k = _FIRST_SEP.match(text, lab.end()).end()
            item, k = _parse_item(text, k, False)
            if item is None:
                break
            seg.items.append(item)
            i = k
            lab = None
            while True:
                j = _JOIN.match(text, i)
                if not j:
                    break
                after = j.end()
                nl = label_re.match(text, after)
                if nl:  # "Table 2, Figure 1" / "Figure 1 and Figure 2"
                    probe = _FIRST_SEP.match(text, nl.end()).end()
                    if _parse_num(text, probe)[0] is not None:
                        lab, join_before = nl, j.group(1)
                        break
                    break
                has_part = any(it.part for it in seg.items)
                nxt, k = _parse_item(text, after, has_part and j.group(1) != ";")
                if nxt is None:
                    break
                # "Table 1 and 25 patients": a further bare number needs a plural label, brackets,
                # or part letters on both sides ("Fig. 2a and 2b").
                if nxt.num is not None and not (plural or bracketed or (has_part and nxt.part)):
                    break
                seg.items.append(nxt)
                seg.joins.append(j.group(1))
                i = k
            segs.append(seg)
        if segs:
            out.append(Group(start, i, segs))
            pos = i
        else:
            pos = m.end()
    return out


# --- rendering -----------------------------------------------------------------------

def _numbers(seg_items: list[Item]) -> set[int]:
    nums: set[int] = set()
    last = None
    for it in seg_items:
        if it.num is None:
            continue
        last = it.num
        if it.to_num and it.to_num > it.num:
            nums.update(range(it.num, it.to_num + 1))
        else:
            nums.add(last)
    return nums


def _case(part: str, style: JournalStyle) -> str:
    mode = style.get("figures.part_labels", "lower")
    return part.lower() if mode == "lower" else part.upper() if mode == "upper" else part


def _render_item(it: Item, kind: str, style: JournalStyle) -> str:
    part = _case(it.part, style) if kind == FIG else it.part
    if it.num is None:
        return part
    s = f"{it.num}{part}"
    if it.to_num is None and it.to_part:
        dash = "-" if it.dash == "to" else it.dash
        return f"{s}{dash}{_case(it.to_part, style) if kind == FIG else it.to_part}"
    if it.to_num is not None:
        to_part = _case(it.to_part, style) if kind == FIG else it.to_part
        if it.to_num == it.num:  # "1a to 1d" -> "1a-d"
            return f"{s}-{to_part}"
        dash = style.get("figures.range_dash", "–") if it.dash == "to" else it.dash
        return f"{s}{dash}{it.to_num}{to_part}"
    return s


def _label(kind: str, plural: bool, style: JournalStyle) -> str:
    if kind == TABLE:
        return "Tables" if plural else "Table"
    if plural:
        return style.get("figures.figure_label_plural", "Figures")
    return style.get("figures.figure_label", "Figure")


def _join(j: str) -> str:
    return {",": ", ", ";": "; ", "and": " and ", "&": " and "}[j]


def render_group(g: Group, style: JournalStyle) -> str:
    # Merge runs of segments of the same kind ("Figure 1 and Figure 2" -> "Figures 1 and 2").
    runs: list[Segment] = []
    for seg in g.segments:
        prev = runs[-1] if runs else None
        if prev and prev.kind == seg.kind and prev.supp == seg.supp and seg.join_before in ("and", "&", ","):
            prev.joins.append(seg.join_before)
            prev.items.extend(seg.items)
            prev.joins.extend(seg.joins)
        else:
            runs.append(Segment(seg.kind, seg.supp, list(seg.items), list(seg.joins), seg.join_before))
    out = []
    for i, seg in enumerate(runs):
        if i:
            out.append(_join(seg.join_before))
        plural = len(_numbers(seg.items)) > 1
        if seg.kind == TABLE:  # "Tables 1A and 1B" are two tables
            plural = plural or len({(it.num, it.part) for it in seg.items if it.num is not None}) > 1
        supp = re.sub(r"\s+", " ", seg.supp)
        out.append(f"{supp}{_label(seg.kind, plural, style)} ")
        nums = [it.num for it in seg.items]
        if (style.get("figures.compress_lists") and len(nums) >= 3 and all(n is not None for n in nums)
                and not any(it.part or it.to_num or it.to_part for it in seg.items)
                and all(b == a + 1 for a, b in zip(nums, nums[1:]))):
            out.append(f"{nums[0]}{style.get('figures.range_dash', '–')}{nums[-1]}")  # Tables 1, 2 and 3 -> 1–3
            continue
        for k, it in enumerate(seg.items):
            if k:
                out.append(_join(seg.joins[k - 1]))
            out.append(_render_item(it, seg.kind, style))
    return "".join(out)


# --- change helpers ------------------------------------------------------------------

def _diff(p: Paragraph, base: int, old: str, new: str, rule: str) -> list[Change]:
    from .rules import _change

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
            out.append(_change(p, base + starts[i1], base + starts[i2], "".join(b[j1:j2]), Layer.TABLES_FIGURES, rule))
    return out


# --- captions ------------------------------------------------------------------------

_CAPTION = re.compile(
    r"^(?P<lead>\s*)(?P<pre>(?:Legends?\s+(?:to|for|of)\s+(?:the\s+)?)?)"
    r"(?P<word>Fig(?:ure)?s?|FIG(?:URE)?S?|Pictures?|PICTURES?|Graphs?|GRAPHS?|Tables?|TABLES?)(?![A-Za-z])\.?"
    r"(?P<sp>\s*(?:[-_]\s*)?(?:No\.?\s*)?)"
    r"(?P<num>\d{1,2}(?!\d)|[IVX]{1,5}(?![A-Za-z]))(?P<part>[A-Za-z](?![A-Za-z]))?"
    r"(?P<sep>\s*[.:\-–—]+\s*|\s+)(?P<rest>\S.*)$", re.S)


@dataclass
class Caption:
    para_id: str
    kind: str
    number: int
    match: re.Match


def caption_match(p: Paragraph) -> re.Match | None:
    if p.style.lower().startswith("heading"):
        return None
    m = _CAPTION.match(p.text)
    if not m:
        return None
    rest = m.group("rest")
    if not re.search(r"\w", rest):
        return None  # "Figure 2..   ": a label with no caption text (a placeholder), left as typed
    has_punct = bool(re.search(r"[.:\-–—]", m.group("sep")))
    if not has_punct:
        # "Table 2 Detection rate" is a caption; "Table 2 shows ..." is a sentence.
        if not (rest[0].isupper() or rest[0] == "(") or re.match(r"[A-Z](?:[\s).:,-]|$)", rest):
            return None  # "Figure 2 A) axial ..." is a part label, not a caption separator
        if re.match(r"\w+\s+(?:shows?|summari[sz]es|depicts?|presents?|lists?|gives?|illustrates?)\b", rest):
            return None
    elif m.group("sep").strip().startswith(".") and rest[0].islower():
        return None
    if m.group("num").isdigit():
        n = int(m.group("num"))
    else:
        n = roman_to_int(m.group("num"))
        if n is None:
            return None
    return m


def captions(ms: Manuscript) -> list[Caption]:
    out = []
    for p in ms.paragraphs:
        m = caption_match(p)
        if m is None:
            continue
        word = m.group("word").lower()
        kind = TABLE if word.startswith("tab") else GRAPH if word.startswith("graph") else FIG
        num = int(m.group("num")) if m.group("num").isdigit() else roman_to_int(m.group("num"))
        out.append(Caption(p.id, kind, num, m))
    return out


def _end_period(rest: str) -> int | None:
    """Index of a caption's final period in `rest`, if it should go."""
    stripped = rest.rstrip()
    if not stripped.endswith(".") or stripped.endswith(".."):
        return None
    if re.search(r"(?:\bet al|\be\.g|\bi\.e|\betc|\b[A-Z])\.$", stripped):
        return None
    return len(stripped) - 1


# Part labels inside a legend: "(A)", "(A, B)", "(C–E)", "(1A)" (its own figure's number), and bare
# "a) ... b) ..." labels. Letters a-l only: "(R)", "(T)" in a legend are seldom part labels.
_LEGEND_PART = re.compile(r"\((?P<num>\d{1,2})?\s*(?P<parts>[A-La-l](?:\s*(?:,|and|&|-|–|to)\s*\d{0,2}[A-La-l])*)\)")
_BARE_LEGEND_PART = re.compile(r"(?<![\w(\[.])(?P<l>[A-La-l])\)(?=\s)")


def _legend_parts(p: Paragraph, rest_start: int, number: int, style: JournalStyle) -> list[Change]:
    """Part labels in a figure legend in the journal's case, in round brackets, without the
    figure's number: "(1A)" -> "(a)", "(A, B)" -> "(a, b)", "a) ... b) ..." -> "(a) ... (b) ..."."""
    text = p.text
    out: list[Change] = []
    for m in _LEGEND_PART.finditer(text, rest_start):
        if m.group("num") and int(m.group("num")) != number:
            continue
        inner = m.group("parts")
        nums = re.findall(r"\d+", inner)
        if any(int(n) != number for n in nums):
            continue
        if not m.group("num") and len(inner) == 1 and inner.lower() not in "abcdefgh":
            continue
        new = "(" + re.sub(r"\d+", "", re.sub(r"[A-La-l]", lambda x: _case(x.group(0), style), inner)) + ")"
        new = re.sub(r"\s*&\s*", " and ", new) if "&" in new else new
        if new != m.group(0):
            out += _diff(p, m.start(), m.group(0), new, "fig_captions")
    bare = list(_BARE_LEGEND_PART.finditer(text, rest_start))
    letters = [b.group("l").lower() for b in bare]
    if len(bare) >= 2 and letters == [chr(ord("a") + i) for i in range(len(letters))]:
        for b in bare:
            out += _diff(p, b.start(), b.group(0), f"({_case(b.group('l'), style)})", "fig_captions")
    return out


def rule_captions(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not style.get("figures.captions", True):
        return []
    out = []
    prev = None
    for cap in captions(ms):
        p = ms.by_id(cap.para_id)
        follow_on = (prev is not None and prev.kind == cap.kind == FIG and prev.number == cap.number
                     and cap.match.group("part") and prev.match.group("part"))
        prev = cap
        if p.in_table:
            continue
        m = cap.match
        rest = m.group("rest")
        rest_start = m.start("rest")
        label = {TABLE: "Table", GRAPH: "Graph", FIG: "Figure"}[cap.kind]
        suffix = ""
        new_rest_prefix = ""
        if m.group("part"):
            if cap.kind == TABLE:
                suffix = m.group("part")  # "Table 1A" is its own table
            else:
                new_rest_prefix = f"({_case(m.group('part'), style)}) "
        # "Table 3: Table 3. Individual..." -> drop the repeated label.
        dup = re.match(r"(?:Fig(?:ure)?\.?|Table|Graph)\s*" + str(cap.number) + r"[A-Za-z]?\s*[.:\-–]\s*", rest, re.I)
        if dup and not m.group("part"):
            rest_start += dup.end()
        elif cap.kind == FIG and not style.get("figures.legend_part_labels", True):
            lead_part = re.match(r"\(([A-Za-z])\)", rest)
            if lead_part and _case(lead_part.group(1), style) != lead_part.group(1):
                out += _diff(p, m.start("rest") + 1, lead_part.group(1), _case(lead_part.group(1), style), "fig_captions")
        if cap.kind == FIG and style.get("figures.legend_part_labels", True):
            out += _legend_parts(p, m.start("rest"), cap.number, style)
        sep = style.get("figures.caption_separator", ": ")
        new_label = f"{label} {cap.number}{suffix}{sep}{new_rest_prefix}"
        if follow_on:  # "Figure 1A. ..." then "Figure 1B. ...": the second is part (b) of Figure 1
            new_label = new_rest_prefix
        old_label = p.text[m.end("lead"):rest_start]
        if new_label != old_label:
            out += _diff(p, m.end("lead"), old_label, new_label, "fig_captions")
        table_end = style.get("figures.table_caption_end_period") if cap.kind == TABLE else None
        if table_end == "add":  # "Table 1: Baseline characteristics" -> "... characteristics." (MJDRDYPU stylesheet)
            body = rest.rstrip()
            if body and not body.endswith((".", "?", "!", ":")):
                from .rules import _change
                k = m.start("rest") + len(body)
                out.append(_change(p, k, k, ".", Layer.TABLES_FIGURES, "fig_captions"))
        elif not style.get("figures.caption_end_period", False) and not style.get("figures.caption_end_period_keep"):
            i = _end_period(rest)
            if i is not None:
                from .rules import _change
                k = m.start("rest") + i
                end = len(p.text) if not p.text[k + 1:].strip() else k + 1  # "folds. " -> "folds"
                out.append(_change(p, k, end, "", Layer.TABLES_FIGURES, "fig_captions"))
    return out


# --- body citations ------------------------------------------------------------------

def _citation_paragraphs(ms: Manuscript) -> list[Paragraph]:
    from .rules import body_paragraphs

    return [p for p in body_paragraphs(ms) if not p.style.lower().startswith("heading") and caption_match(p) is None]


_OPEN, _CLOSE = "([{", ")]}"
# A sentence's full stop right before a bracketed figure / table citation: "immunized. (Table 1)", "0.05). [Table 2]"
# (not "et al. (Figure 1)" or "[75]. (Figure 1)").
_STOP_BEFORE = re.compile(r"(?:(?<![\w.])(?!(?:al|etc|vs|approx|fig|figs|no)\b)[A-Za-z]{2,}|\d|%|\))(\.)\s*$")


def rule_citations(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not style.get("figures.citations", True):
        return []
    square = style.get("figures.brackets", "square") == "square"
    before_stop = style.get("figures.before_full_stop", False)
    out = []
    for p in _citation_paragraphs(ms):
        t = p.text
        for g in parse_groups(t):
            new = render_group(g, style)
            s, e = g.start, g.end
            # Bracketed: the group is the whole content of (...), {...} or a mismatched [...).
            ls = len(t[:s]) - len(t[:s].rstrip(" "))
            rs = len(t[e:]) - len(t[e:].lstrip(" "))
            if t[e + rs:e + rs + 1] == "," and t[e + rs + 1:].lstrip(" ")[:1] in (")", "}"):
                rs += 1 + len(t[e + rs + 1:]) - len(t[e + rs + 1:].lstrip(" "))  # "(Figure 1,2,3,)"
            o, c = s - ls - 1, e + rs
            bracketed = o >= 0 and c < len(t) and t[o] in _OPEN and t[c] in _CLOSE
            if bracketed and square:
                lead = ""
                if o > 0 and (t[o - 1].isalnum() or t[o - 1] == "]"):
                    lead = " "  # "71](Figure 1)" -> "71] [Figure 1]"
                old = t[o:c + 1]
                repl = f"{lead}[{new}]"
                stop = _STOP_BEFORE.search(t, 0, o) if before_stop else None
                if stop and re.match(r"\s*(?:$|[A-Z])", t[c + 1:]):
                    # "fully immunized. (Table 1) The" -> "fully immunized [Table 1]. The"
                    k = stop.start(1)
                    out += _diff(p, k, t[k:c + 1], f" [{new}].", "fig_citations")
                elif repl != old:
                    if lead:
                        from .rules import _change
                        out.append(_change(p, o, o, " ", Layer.TABLES_FIGURES, "fig_citations"))
                        repl = repl[1:]
                    out += _diff(p, o, old, repl, "fig_citations")
            elif new != t[s:e]:
                out += _diff(p, s, t[s:e], new, "fig_citations")
    return out


# --- queries -------------------------------------------------------------------------

@dataclass
class Citation:
    para_id: str
    start: int
    end: int
    kind: str
    numbers: list[int]


def citations(ms: Manuscript) -> list[Citation]:
    """Figure and table citations in the body, in reading order (supplementary ones left out)."""
    out = []
    for p in _citation_paragraphs(ms):
        for g in parse_groups(p.text, anycase=True):
            for seg in g.segments:
                if seg.supp:
                    continue
                nums = []
                for it in seg.items:
                    if it.num is None:
                        continue
                    if it.to_num and it.to_num > it.num:
                        nums += list(range(it.num, it.to_num + 1))
                    elif it.num not in nums:
                        nums.append(it.num)
                out.append(Citation(p.id, g.start, g.end, seg.kind, nums))
    return out


def add_queries(ms: Manuscript, style: JournalStyle, qs) -> None:
    """Figure/table author queries, added to a queries.QuerySet."""
    if not style.get("figures.queries", True):
        return
    queries = _q()
    queries.TEXT.update({k: v for k, v in QUERY_TEXT.items() if k not in queries.TEXT})
    cits = citations(ms)
    caps = [c for c in captions(ms)]
    for kind in (FIG, TABLE):
        kc = [c for c in cits if c.kind == kind]
        cap_nums = {c.number for c in caps if c.kind == kind}
        # Order: each number's first citation must come after every smaller cited number's.
        seen: list[int] = []
        for c in kc:
            new = [n for n in c.numbers if n not in seen]
            if any(seen and n < max(seen) for n in new):
                qs.add(c.para_id, "figures.order", c.start, c.end, kind=kind)
                break
            seen += new
        cited = {n for c in kc for n in c.numbers}
        # Cited, but no caption / table in the file (only when the file carries captions of that kind).
        if cap_nums:
            missing_done: set[int] = set()
            for c in kc:
                for n in c.numbers:
                    if n not in cap_nums and n not in missing_done:
                        missing_done.add(n)
                        check = "figures.no_caption" if kind == FIG else "figures.no_table"
                        qs.add(c.para_id, check, c.start, c.end, n=n)
        # Captioned, never cited.
        for cap in caps:
            if cap.kind == kind and cap.number not in cited:
                m = cap.match
                qs.add(cap.para_id, "figures.uncited", m.end("lead"), m.start("rest"), kind=kind, n=cap.number)
    # Part labels lower-cased in the text: tell the author, as the editors do.
    if style.get("figures.part_labels") == "lower" and style.get("figures.part_case_query", False):
        for p in _citation_paragraphs(ms):
            for g in parse_groups(p.text):
                if any(it.part.isupper() or it.to_part.isupper() for seg in g.segments if seg.kind == FIG
                       for it in seg.items):
                    qs.add(p.id, "figures.part_case", g.start, g.end)
                    return
