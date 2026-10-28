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

from . import references
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
    "figures.no_captions": "Kindly provide the captions for {figs}.",
    # G244 / S: a figure with no caption (and here no citation either)
    "figures.image_no_caption": "Kindly provide the caption for this figure and cite it in the text.",
    # S292: 'In case of equations especially display equations in image or text format, please raise a query'
    "figures.equation_image": "Please provide the equation in plain text or MathType Object format.",
    # S: table foot "Abbreviations should be as per format term = definition ... Abbreviations should be first"
    "figures.stray_citation": "This citation stands alone in its own paragraph. Kindly check where it belongs.",
    "figures.footnote_abbreviations": "Kindly provide the expansion of {abbrs} in the table footnote.",
    "figures.footnote_symbol_twice": "The symbol “{sym}” is used for more than one footnote. Kindly check.",
    "figures.footnote_symbol_missing": "The symbol “{sym}” used in the table is not explained in the footnote. "
                                       "Kindly check.",
    # a figure drawn with Word shapes and text boxes (jehp_1793): it cannot be moved or typeset as it is
    "figures.drawn_shapes": "Figure {n} is drawn with shapes and text boxes in the manuscript. Kindly provide it as an "
                            "image file (TIFF/JPEG) of good resolution.",
}

_MARKER = re.compile(r"(?:^|(?<=[\s,;.(]))([*†‡§¶#]+)\s*(?=[A-Za-z(])")
_CELL_MARKER = re.compile(r"([*†‡§¶#]+)(?=\s*[-−–]?\d)|(?<=\d)([*†‡§¶#]+)")


def _table_of(p: Paragraph) -> str | None:
    """The path of the (outermost) table holding the paragraph: a key that stays the same across calls."""
    el, found = p.element, None
    while el is not None:
        if el.tag == f"{_W}tbl":
            found = el
        el = el.getparent()
    return found.getroottree().getpath(found) if found is not None else None


def footnote_queries(ms: Manuscript, style: JournalStyle) -> list[tuple[Paragraph, int, int, str, dict]]:
    """Queries on a table's footnote: abbreviations used in the table that the footnote does not expand ("Mean",
    "SD", "95% CI" with no "SD = standard deviation, CI = confidence interval"); a symbol given two meanings
    ("**Fisher's exact test, ... **Mann-Whitney U test"); a symbol used in the cells and not explained."""
    if not style.get("figures.table_footnote_queries", False):
        return []
    from .copyedit import abbrev

    out = []
    paras = ms.paragraphs
    index = {p.id: i for i, p in enumerate(paras)}
    blocks = {}
    for block in table_footnotes(ms):
        i = index[block[0].id] - 1
        while i >= 0 and not paras[i].in_table:
            i -= 1
        if i >= 0:
            blocks[_table_of(paras[i])] = block
    tables: dict[str, list[Paragraph]] = {}
    for p in paras:
        if p.in_table:
            tables.setdefault(_table_of(p), []).append(p)
    cap_ids = {c.para_id for c in captions(ms) if c.kind == TABLE}
    for key_t, cells in tables.items():
        if len(cells) < 4:
            continue  # a one-cell box, not a data table
        block = blocks.get(key_t, [])
        foot = " ".join(q.text for q in block)
        k = index[cells[0].id] - 1
        while k >= 0 and not paras[k].text.strip():
            k -= 1
        cap = paras[k] if k >= 0 and paras[k].id in cap_ids else next((p for p in cells if p.id in cap_ids), None)
        if cap is None:
            continue  # not a numbered table (an appendix grid, a layout table)
        # on the footnote, where the expansions go; the caption when the table has none
        anchor = next((q for q in block if q.text.strip()), cap)
        # abbreviations
        defined = {d.short for p in cells for d in abbrev.definitions(p)}
        allow = set(style.get("copyedit.abbreviations.allow") or [])
        missing = []
        for p in cells:
            if p.id in cap_ids:
                continue
            # (a short cell, "SD" or "95% CI", is all capitals or "numeric" to the text checker: read it here)
            found = [k for m in abbrev._USE.finditer(p.text) if (k := abbrev._abbr_key(m.group(1)))
                     and k not in abbrev._NOT_ABBR] if len(p.text) <= 40 else [k for k, _, _ in abbrev.uses(p, True)]
            for key in found:
                if key in defined or key in allow or key in missing or re.search(r"\b" + re.escape(key) + r"\b", foot):
                    continue
                missing.append(key)
        if missing:
            abbrs = ", ".join(missing[:-1]) + " and " + missing[-1] if len(missing) > 1 else missing[0]
            out.append((anchor, 0, 0, "figures.footnote_abbreviations", {"abbrs": abbrs}))
        # symbols
        marks = [(q, m) for q in block for m in _MARKER.finditer(q.text)]
        seen: dict[str, int] = {}
        for q, m in marks:
            seen[m.group(1)] = seen.get(m.group(1), 0) + 1
            if seen[m.group(1)] == 2:
                out.append((q, m.start(1), m.end(1), "figures.footnote_symbol_twice", {"sym": m.group(1)}))
        if marks:
            used = []
            for p in cells:
                for m in _CELL_MARKER.finditer(p.text):
                    sym = m.group(1) or m.group(2)
                    if sym not in seen and sym not in [u for u, _ in used]:
                        used.append((sym, p))
            for sym, p in used:
                k = p.text.find(sym)
                out.append((p, k, k + len(sym), "figures.footnote_symbol_missing", {"sym": sym}))
    return out

_EMU_INCH = 914400


def _image_size(p: Paragraph) -> tuple[int, int] | None:
    """(width, height) in EMU of the first inline / anchored picture in the paragraph."""
    for el in p.element.iter("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}extent"):
        try:
            return int(el.get("cx")), int(el.get("cy"))
        except (TypeError, ValueError):
            return None
    return None


def _equation_like(p: Paragraph, size: tuple[int, int] | None) -> bool:
    """A picture of a display equation: a short, wide image on a line of its own (no text), e.g. a sample-size
    formula pasted as a picture (235 x 73 px)."""
    if size is None or p.text.strip():
        return False
    w, h = size
    return 0 < h <= 0.9 * _EMU_INCH and w >= 2.2 * h and w <= 6.5 * _EMU_INCH


def image_queries(ms: Manuscript, style: JournalStyle) -> list[tuple[Paragraph, str]]:
    """(paragraph, check) for pictures in the body: an equation given as an image (S292) and a figure with no caption
    next to it when the file has fewer figure captions than pictures (G244)."""
    from . import references
    from .docmodel import has_picture
    from .rules import front_matter_ids

    eq_on = style.get("figures.query_equation_images", False)
    cap_on = style.get("figures.query_uncaptioned_images", False)
    if not (eq_on or cap_on):
        return []
    front = front_matter_ids(ms)
    ref_i = references.find_heading(ms)
    paras = ms.paragraphs[:ref_i] if ref_i is not None else ms.paragraphs
    pics = [p for p in paras if not p.in_table and p.id not in front and has_picture(p.element)]
    caps = [c for c in captions(ms) if c.kind == FIG]
    legend_ids = {q.id for leg in legends(ms) for q in leg.paragraphs} | {c.para_id for c in caps}
    order = {p.id: k for k, p in enumerate(ms.paragraphs)}
    out = []
    figures = []
    for p in pics:
        size = _image_size(p)
        if _equation_like(p, size):
            if eq_on:
                out.append((p, "figures.equation_image"))
            continue
        figures.append(p)
    if cap_on and len(figures) > len(caps):
        for p in figures:
            k = order[p.id]
            near = [q for q in ms.paragraphs[max(0, k - 3):k + 4] if q is not p and q.text.strip()]
            if not any(q.id in legend_ids for q in near) and caption_match(p) is None:
                out.append((p, "figures.image_no_caption"))
    return out


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


# "Supplimentary Table 1" (1193): a misspelt supplementary label is still a supplementary one.
_SUPP = r"(?P<supp>(?:Suppl[ei]m[ae]nt(?:ary|al)|Suppl\.?)\s+)?"
_LABEL = re.compile(
    _SUPP +
    r"(?P<word>Fig(?:ure)?s?|FIG(?:URE)?S?|Pictures?|PICTURES?|Tables?|TABLES?)(?![A-Za-z])(?P<dot>\.?)",
)
_LABEL_ANYCASE = re.compile(
    _SUPP +
    r"(?P<word>fig(?:ure)?s?|pictures?|tables?)(?![a-z])(?P<dot>\.?)", re.I)
_NUM = re.compile(r"\d{1,2}(?!\d)|[IVX]{1,5}(?![A-Za-z])")
_PART = re.compile(r"\(([a-zA-Z])\)|-?([A-Za-z])(?![A-Za-z0-9])")
_BARE_PART = re.compile(r"\(([A-Za-z])\)|([A-Za-z])(?=\s*(?:[)\]},;.:]|and\b|&|$))")
# A part letter typed apart from its number: "(Figure 1 b- d)", "Figure 1 A, B" (not "Figure 1 a decrease").
_SPACED_PART = re.compile(r"\s([A-Za-z])(?=\s*(?:[-–—,;)\]}]|and\b|&|$))")
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
    no_number: bool = False  # "(Figure B-C)": parts with no figure number (`sole_figure` supplies it)


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
    if m:
        return m.group(1) or m.group(2), m.end()
    m = _SPACED_PART.match(text, i)
    return (m.group(1), m.end()) if m else ("", i)


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


# A figure part with no number, closing its brackets: "(Figure A)", "(Figure  E-F)", "(Figure B and C)".
_BARE_FIGURE = re.compile(r"([A-Z])(?:\s*(-|–|—|\bto\b)\s*([A-Z]))?(?=\s*(?:[)\]},]|and\b|&))")


def _parse_bare(text: str, i: int):
    m = _BARE_FIGURE.match(text, i)
    if not m:
        return None, i
    return Item(None, m.group(1), to_part=m.group(3) or "", dash=m.group(2) or ""), m.end()


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
            if item is None and bracketed and not segs and seg.kind == FIG and not plural:
                item, k = _parse_bare(text, k)  # "(Figure B-C)": the parts of a figure with no number
                seg.no_number = item is not None
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


def _case(part: str, style: JournalStyle, legend: bool = False) -> str:
    """A figure part label in the journal's case: `figures.part_labels` in body citations and, in legends,
    `figures.legend_part_case` when set (the guide: citations "Figure 1A", legends "(a)")."""
    if style is None:
        return part
    mode = (legend and style.get("figures.legend_part_case")) or style.get("figures.part_labels", "lower")
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


def _part_runs(seg: Segment) -> tuple[list[Item], list[str]]:
    """Items and joins with three or more consecutive parts of one figure as a range: "1A, 1B, 1C" -> "1A-C"
    (G246: "In case citation range is present then please delete number if repeated")."""
    items, joins = list(seg.items), list(seg.joins)
    out_items: list[Item] = []
    out_joins: list[str] = []
    i = 0
    while i < len(items):
        j = i
        num = items[i].num
        def simple(it):
            return len(it.part) == 1 and not it.to_num and not it.to_part
        if num is not None and simple(items[i]):
            while (j + 1 < len(items) and simple(items[j + 1]) and items[j + 1].num in (None, num)
                   and joins[j] in (",", "and", "&") and ord(items[j + 1].part.lower()) == ord(items[j].part.lower()) + 1
                   and items[j + 1].part.isupper() == items[i].part.isupper()):
                j += 1
        if j - i >= 2:
            out_items.append(Item(num, items[i].part, to_part=items[j].part, dash="-"))
        else:
            out_items += items[i:j + 1]
            out_joins += joins[i:j]
        if j + 1 < len(items):
            out_joins.append(joins[j])
        i = j + 1
    return out_items, out_joins


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
        items, joins = _part_runs(seg) if seg.kind == FIG and style.get("figures.compress_lists") else \
            (seg.items, seg.joins)
        for k, it in enumerate(items):
            if k:
                out.append(_join(joins[k - 1]))
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

_CAPTION_HEAD = (
    r"^(?P<lead>\s*)(?P<pre>(?:Legends?\s*(?:\s(?:to|for|of)\s+(?:the\s+)?|[:\-–]\s*|\s(?=Fig)))?)"
    r"(?P<word>Fig(?:ure)?s?|FIG(?:URE)?S?|Pictures?|PICTURES?|Graphs?|GRAPHS?|Tables?|TABLES?|Tabels?|TABELS?)"
    r"(?![A-Za-z])\.?(?P<sp>\s*(?:[-_]\s*)?(?:No\.?\s*)?)"
    r"(?P<num>\d{1,2}(?!\d)|[IVX]{1,5}(?![A-Za-z]))(?P<part>[A-Za-z](?![A-Za-z]))?")
_CAPTION_TAIL = r"(?P<sep>\s*[.:\-–—]+\s*|\s+|(?=\(\s*[A-La-l]\s*[),&–-]))(?P<rest>\S.*)$"
_CAPTION = re.compile(_CAPTION_HEAD + r"(?P<more>)" + _CAPTION_TAIL, re.S)
# A figure label naming several parts: "Fig 2a & 2b The goat's eye ...", "Figure 1A, B: ...", "Figure 3a-3c. ...":
# the parts after the first (`more`) are read as the legend's part labels ("Figure 2: (a and b) ...", G263).
_LABEL_MORE = r"(?:\s*(?:,\s*(?:and\s+)?|&|\band\b|-|–|—|\bto\b)\s*(?:\d{1,2})?[A-La-l](?![A-Za-z]))+"
_CAPTION_PARTS = re.compile(_CAPTION_HEAD + r"(?P<more>" + _LABEL_MORE + r")" + _CAPTION_TAIL, re.S)
_MORE_ITEM = re.compile(r"\s*(?P<j>,\s*(?:and\s+)?|&|\band\b|-|–|—|\bto\b)\s*(?P<num>\d{1,2})?(?P<part>[A-La-l])")


def label_parts(m: re.Match, style: JournalStyle | None = None) -> tuple[str, str, str]:
    """(first, last, label) of the part letters a caption label carries: "Figure 1B" -> ("B", "B", "(b)"), "Fig 2a &
    2b" -> ("a", "b", "(a and b)"), "Figure 3a-3c" -> ("a", "c", "(a–c)"); ("", "", "") when it carries none."""
    part = m.group("part") or ""
    if not part:
        return "", "", ""
    letters, out = [part], _case(part, style, True)
    for it in _MORE_ITEM.finditer(m.group("more") or ""):
        j = it.group("j").strip()
        joint = "–" if j in ("-", "–", "—", "to") else " and " if j in ("&", "and") else \
            ", and " if j.startswith(",") and j.endswith("and") else ", "
        letters.append(it.group("part"))
        out += joint + _case(it.group("part"), style, True)
    return letters[0], letters[-1], f"({out})"


def _more_ok(m: re.Match) -> bool:
    """Whether the further parts of a caption label are a figure's part list: letters running forward, each number
    (if typed) the figure's own."""
    if not m.group("more"):
        return True
    if not m.group("part") or m.group("word").lower().startswith(("tab", "graph")):
        return False
    letters = [m.group("part")]
    for it in _MORE_ITEM.finditer(m.group("more")):
        if it.group("num") and it.group("num") != m.group("num"):
            return False
        letters.append(it.group("part"))
    return all(ord(b.lower()) > ord(a.lower()) for a, b in zip(letters, letters[1:]))


@dataclass
class Caption:
    para_id: str
    kind: str
    number: int
    match: re.Match


# A figure legend that opens with its first part label: "Figure 1 a. Pre-operative map", "Fig.1 A Face photograph",
# "Figure 2 A) axial", "Figure 3.A (OD) OCT".
_LEAD_PART = re.compile(r"(?P<a>[A-La-l])(?:\s*[–-]\s*(?P<b>[A-La-l]))?(?P<punct>[.:)])?(?=\s+\S|\s*\()")
_VERB = r"(?:shows?|summari[sz]es|depicts?|presents?|lists?|gives?|illustrates?|demonstrates?|reveals?)\b"


def lead_part(rest: str) -> re.Match | None:
    """The part label a legend's text opens with ("a." / "A" / "A)"), unless a verb follows ("Figure 1 A shows")."""
    m = _LEAD_PART.match(rest)
    if m is None or re.match(r"\s*" + _VERB, rest[m.end():]):
        return None
    return m


def _beside_table(el) -> bool:
    """Whether a body paragraph sits right above or below a table (blank paragraphs between)."""
    from .docmodel import paragraph_text

    for step in ("getnext", "getprevious"):
        x = getattr(el, step)()
        while x is not None and x.tag == f"{_W}p" and not paragraph_text(x).strip():
            x = getattr(x, step)()
        if x is not None and x.tag == f"{_W}tbl":
            return True
    return False


def caption_match(p: Paragraph) -> re.Match | None:
    m = _CAPTION_PARTS.match(p.text)
    if m is not None and _more_ok(m):
        got = _caption_check(p, m)
        if got is not None:
            return got
    return _caption_check(p, _CAPTION.match(p.text))


def _caption_check(p: Paragraph, m: re.Match | None) -> re.Match | None:
    if m and p.style.lower().startswith("heading"):
        # A heading is not a caption, except a "Table 1. ..." line in a heading style right next to its table (1751).
        if not (m.group("word").lower().startswith("tab") and not p.in_table and _beside_table(p.element)):
            return None
    if not m:
        return None
    rest = m.group("rest")
    if not re.search(r"\w", rest):
        return None  # "Figure 2..   ": a label with no caption text (a placeholder), left as typed
    has_punct = bool(re.search(r"[.:\-–—]", m.group("sep")))
    is_fig = not m.group("word").lower().startswith(("tab", "graph"))
    # "Figure 1 a. Pre-operative ...", "Fig.1 A Face ...": a legend opening with its first part label
    parted = is_fig and not m.group("part") and lead_part(rest) is not None
    if not has_punct and not parted:
        # "Table 2 Detection rate" is a caption; "Table 2 shows ..." is a sentence.
        if not (rest[0].isupper() or rest[0] == "(") or re.match(r"[A-Z](?:[\s).:,-]|$)", rest):
            return None
        if re.match(r"\w+\s+" + _VERB, rest) or re.match(r"\([^)]{0,30}\)\s+" + _VERB, rest):
            return None  # "Figure 4 (above) shows ...": a sentence
    elif has_punct and m.group("sep").strip().startswith(".") and rest[0].islower() and not parted:
        # "Table 3. summarizes the scores ..." on its own line right above or below a table is that table's
        # caption all the same; elsewhere "Table 3. was ..." is a broken sentence.
        if not (m.group("word").lower().startswith("tab") and not p.in_table and len(rest) < 400
                and _beside_table(p.element)):
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


# A table caption's label typed on a line of its own, its title on the next ("Table 1." / "Baseline characteristics of
# participants (n = 110)" over the table: jehp_1793).
_SPLIT_LABEL = re.compile(r"^(?P<lead>\s*)(?P<word>Tables?|TABLES?)\s*(?P<num>\d{1,2}|[IVX]{1,5})\s*[.:\-–—]?\s*$")


def split_table_captions(ms: Manuscript) -> list[tuple[Paragraph, Paragraph]]:
    """(label paragraph, title paragraph) of each table caption typed over two lines right above its table (blank lines
    aside): one caption for moving and formatting. Cached."""
    cached = getattr(ms, "_split_captions", None)
    if cached is not None:
        return cached
    from .docmodel import paragraph_text

    by_el = {p.element: p for p in ms.paragraphs}
    out = []
    for p in ms.paragraphs:
        if p.in_table or not _SPLIT_LABEL.match(p.text) or in_textbox(p):
            continue
        x = p.element.getnext()
        while x is not None and x.tag == f"{_W}p" and not paragraph_text(x).strip() and not _has_object(x):
            x = x.getnext()
        q = by_el.get(x) if x is not None else None
        if q is None or q.in_table or caption_match(q) is not None or _SPLIT_LABEL.match(q.text) \
                or not re.search(r"[A-Za-z]{2}", q.text) or len(q.text.split()) > 60 or q.text.lstrip().startswith("<"):
            continue
        y = q.element.getnext()
        while y is not None and y.tag == f"{_W}p" and not paragraph_text(y).strip() and not _has_object(y):
            y = y.getnext()
        if y is not None and y.tag == f"{_W}tbl":
            out.append((p, q))
    ms._split_captions = out
    return out


def _has_object(el) -> bool:
    from .docmodel import has_picture
    return has_picture(el) or el.find(f".//{_W}drawing") is not None or el.find(f".//{_W}pict") is not None


def _split_caption_changes(ms: Manuscript, style: JournalStyle) -> list[Change]:
    """"Table 1." + "Baseline characteristics ..." -> "Table 1: Baseline characteristics ..." in the label's paragraph,
    the title's paragraph deleted (one suggestion: both or neither)."""
    from .rules import _change
    from .rules_format import char_formats

    out = []
    sep = style.get("figures.caption_separator", ": ")
    mode = style.get("figures.table_caption_end_period")
    if mode is None:
        keep = style.get("figures.caption_end_period", False) or style.get("figures.caption_end_period_keep")
        mode = "keep" if keep else "delete"
    for p, q in split_table_captions(ms):
        m = _SPLIT_LABEL.match(p.text)
        num = m.group("num")
        n = int(num) if num.isdigit() else roman_to_int(num)
        if n is None:
            continue
        title = q.text.strip()
        if style.get("figures.table_caption_case") == "sentence":
            from .rules_sections import sentence_case_changes
            lead = len(q.text) - len(q.text.lstrip())
            title = _apply_edits(q.text, [(c.start, c.end, c.new) for c in sentence_case_changes(
                ms, q, "fig_captions.case", start=lead,
                hyphen_parts=style.get("headings.sentence_case_hyphen_parts", False))]).strip()
        if mode == "delete" and _end_period(title) is not None:
            title = title[:_end_period(title)].rstrip()
        elif mode == "add" and not title.endswith((".", "?", "!")):
            title += "."
        label = f"Table {n}{sep}"
        fp, fq = char_formats(p), char_formats(q)
        label_bold = any(f.get("bold") for f, ch in zip(fp, p.text) if not ch.isspace())
        title_bold = any(f.get("bold") for f, ch in zip(fq, q.text) if not ch.isspace())
        c1 = _change(p, m.start("word"), len(p.text), label + title, Layer.TABLES_FIGURES, "fig_captions.join",
                     {"bold": title_bold})
        c1.segment_format = [[[len(label), {"bold": label_bold or title_bold}]]]
        c2 = _change(q, 0, len(q.text), "", Layer.TABLES_FIGURES, "fig_captions.join")
        c1._group = c2._group = f"caption-join-{p.id}"
        out += [c1, c2]
    return out


# A note right under a table that the footnote patterns miss: "Cells show Spearman ρ (p-value). ..." (jehp_1402),
# "Values in bold are significant".
_LOOSE_NOTE = re.compile(r"^\s*(?:cells?|values?|data|numbers?|figures? (?:are|in)|results? (?:are|were)|all (?:values|data)|"
                         r"p[- ]?values?|bold(?:ed)?|significant|n\s*=|footnotes?|key\s*:|legend\s*:|percentages?|"
                         r"mean|median|†|‡|§|¶|#|\*|[ᵃᵇᶜᵈᵉ])", re.I)


def loose_table_note(text: str) -> bool:
    """Whether a paragraph right under a table (no blank line between) reads as the table's note."""
    t = text.strip()
    return bool(t) and len(t.split()) <= 60 and bool(_LOOSE_NOTE.match(t)) and not re.search(
        r"\b(?:Table|Figure|Fig\.)\s*\d", t)


def _first_row_caption(p: Paragraph) -> bool:
    """A paragraph in the first row of its table, the row one cell (a caption typed or put into a merged first row)."""
    tr = next((a for a in p.element.iterancestors() if a.tag == f"{_W}tr"), None)
    tbl = tr.getparent() if tr is not None else None
    if tbl is None or tbl.tag != f"{_W}tbl" or tbl.find(f"{_W}tr") is not tr:
        return False
    return len(tr.findall(f"{_W}tc")) == 1


def table_caption_ids(ms: Manuscript) -> set[str]:
    """Ids of the table captions: "Table N: ..." paragraphs in the body, and those in a table's one-cell first row."""
    return {c.para_id for c in captions(ms) if c.kind == TABLE
            and (not ms.by_id(c.para_id).in_table or _first_row_caption(ms.by_id(c.para_id)))}


def _end_period(rest: str) -> int | None:
    """Index of a caption's final period in `rest`, if it should go."""
    stripped = rest.rstrip()
    if not stripped.endswith(".") or stripped.endswith(".."):
        return None
    if re.search(r"(?:\bet al|\be\.g|\bi\.e|\betc|\b[A-Z])\.$", stripped):
        return None
    return len(stripped) - 1


# --- figure legends: part labels, parts typed on their own lines ----------------------------
#
# G263: "In case part labels are present then they should be in small case enclosed within round brackets"; G264:
# "In case part labels and content are present in separate paragraphs please make them run-on with space as
# delimiter" (the IJO sheet says the same, S292). A legend is its "Figure N" paragraph and the paragraphs that
# continue it: the next parts of the same figure ("Figure 1B: ...", "Fig.1 B ..."), paragraphs that open with the
# next part label ("(C) ...", "b. ...", "D–F: Case 2."), and lines the author broke by hand ("  postoperative and the
# differential maps").

# Bracketed part labels: "(A)", "(A, B)", "(C–E)", "(1A)" (its own figure's number). Letters a-l only: "(R)",
# "(T)" in a legend are seldom part labels.
_LEGEND_PART = re.compile(r"\(\s*(?P<num>\d{1,2})?\s*(?P<parts>[A-La-l](?:\s*(?:,|and|&|-|–|to)\s*\d{0,2}[A-La-l])*)\s*\)")


def _part_letters_in_order(inner: str) -> bool:
    """Whether the letters of a bracketed part group run forward: "(a, b)", "(a & b)", "(c–e)" do; "(H&E)" (haematoxylin
    and eosin, IJO 2510), "(I&D)" do not -- an abbreviation, not part labels."""
    letters = re.findall(r"[A-La-l]", re.sub(r"\b(?:and|to)\b", " ", inner))
    return all(ord(b.lower()) > ord(a.lower()) for a, b in zip(letters, letters[1:]))
# Unbracketed part labels, taken only when they run in order (a, b, c, ...): "A. Toxoplasmosis B. ...", "A: Causal
# plot ... B: Funnel plot", "A–C: Case 1.", "a) ... b) ...", "B & C. ...".
_BARE_LABEL = re.compile(r"(?<![\w(\[.,/'’\-–])(?P<a>[A-La-l])(?:\s*(?P<dash>[–-])\s*(?P<b>[A-La-l]))?"
                        r"(?:(?P<punct>[.:)])(?=\s|$)|(?=\s*&\s*(?:[A-La-l](?:[.:)]|\s)|$)))")
# A heading over the figure legends: "Figure Legends", "Legends:", "Legends to Figures", "FIG LEGENDS",
# "Image description".
LEGENDS_HEADING = re.compile(
    r"^\s*(?:<H\d>)?\s*(?:(?:(?:figures?|figs?\.?|images?|photos?)\s+)?(?:legends?|captions?|descriptions?)"
    r"(?:\s+(?:to|for|of)\s+(?:the\s+)?(?:figures?|images?|photographs?))?|figures?)\s*[:.\-–—]*\s*$", re.I)
_UNNUMBERED = re.compile(r"^\s*(?P<lab>Fig(?:ure)?\.?\s*)(?=\()")


def _next(letter: str) -> str:
    return chr(ord(letter) + 1)


@dataclass
class _Token:
    seg: int  # 0: the legend paragraph, k: its k-th continuation paragraph
    start: int
    end: int
    first: str  # first part letter, lower case
    last: str
    new: str  # the label as it should read
    bracketed: bool
    weak: bool = False  # a bare letter opening the text without punctuation ("Fig.1 A Face"): needs a neighbour part
    edit: bool = True  # False: stands for a part read from the caption label ("Figure 1B: ...")


@dataclass
class Legend:
    """A figure legend: its paragraph and the paragraphs that continue it."""
    para: Paragraph
    number: int
    cap: Caption | None  # None: an unnumbered legend ("Figure (A, B): ...", a lone paragraph under a legends heading)
    label: tuple[int, int] = (0, 0)  # unnumbered: the span the "Figure 1: " label replaces
    conts: list[tuple[Paragraph, str]] = field(default_factory=list)  # (paragraph, "label" | "part" | "wrap")

    @property
    def paragraphs(self) -> list[Paragraph]:
        return [self.para] + [q for q, _ in self.conts]


def _tokens(text: str, start: int, number: int, style: JournalStyle, seg: int, opening: int | None) -> list[_Token]:
    """Part-label candidates in text[start:]; `opening`: where a bare opening label may stand ("A Face photograph")."""
    out: list[_Token] = []
    for m in _LEGEND_PART.finditer(text, start):
        if m.group("num") and int(m.group("num")) != number:
            continue
        inner = m.group("parts")
        if any(int(n) != number for n in re.findall(r"\d+", inner)):
            continue
        if not _part_letters_in_order(inner):
            continue  # "(H&E)": an abbreviation
        if not m.group("num") and len(inner) == 1 and inner.lower() not in "abcdefgh":
            continue
        new = "(" + re.sub(r"\d+", "", re.sub(r"[A-La-l]", lambda x: _case(x.group(0), style, True), inner)) + ")"
        new = re.sub(r"\s*&\s*", " and ", new) if "&" in new else new
        letters = re.findall(r"[A-La-l]", inner)
        lead = re.search(r"(?<![\w.])" + str(number) + r"\s?$", text[start:m.start()])  # "1(b) Anterior" -> "(b)"
        s0 = start + lead.start() if lead and not m.group("num") else m.start()
        out.append(_Token(seg, s0, m.end(), letters[0].lower(), letters[-1].lower(), new, True))
    taken = [(t.start, t.end) for t in out]
    for m in _BARE_LABEL.finditer(text, start):
        if any(s < m.end() and m.start() < e for s, e in taken):
            continue
        if m.group("punct") == ")" and text[:m.start()].count("(") > text[:m.start()].count(")"):
            continue  # "(Antoni A)": the end of a bracket, not a label "A)" (IJO 1745)
        a, b = m.group("a"), m.group("b")
        lab = _case(a, style, True) + (m.group("dash") + _case(b, style, True) if b else "")
        new = f"({lab})" + (":" if m.group("punct") == ":" else "")
        out.append(_Token(seg, m.start(), m.end(), a.lower(), (b or a).lower(), new, False))
    if opening is not None:
        lm = lead_part(text[opening:])
        if lm and not lm.group("punct") and not any(t.start == opening + lm.start() for t in out):
            a, b = lm.group("a"), lm.group("b")
            lab = _case(a, style, True) + ("–" + _case(b, style, True) if b else "")
            out.append(_Token(seg, opening + lm.start(), opening + lm.end(), a.lower(), (b or a).lower(), f"({lab})",
                              False, weak=True))
    return sorted(out, key=lambda t: t.start)


def _select(tokens: list[_Token]) -> list[_Token]:
    """The part labels of one legend: bracketed ones always; unbracketed ones only in letter order from (a), and only
    when the legend has at least two parts."""
    expected, acc = "a", []
    for t in tokens:
        if t.bracketed or t.first == expected:
            acc.append(t)
            expected = _next(t.last)
    out = []
    for i, t in enumerate(acc):
        if t.weak:
            prev, nxt = (acc[i - 1] if i else None), (acc[i + 1] if i + 1 < len(acc) else None)
            if not ((prev and _next(prev.last) == t.first) or (nxt and nxt.first == _next(t.last))):
                continue
        out.append(t)
    if len(out) < 2:
        out = [t for t in out if t.bracketed]
    return out


def _expected_after(text: str, start: int, number: int, style: JournalStyle, expected: str) -> str:
    """The part letter that would come next after the labels in text[start:]."""
    for t in _tokens(text, start, number, style, 0, start):
        if t.bracketed or t.first == expected:
            expected = _next(t.last)
    return expected


def _label_part(m: re.Match) -> str:
    """The part letter a caption label carries: "Figure 1B: ..." -> "B", "Fig.1 B Face ..." -> "B"."""
    if m.group("part"):
        return m.group("part")
    lp = lead_part(m.group("rest"))
    if lp and not lp.group("b"):
        return lp.group("a")
    bm = _LEGEND_PART.match(m.group("rest"))  # "Figure 1(c ): ...", "Figure 1 (a & b ): ..." (IJO 1745)
    if bm and not bm.group("num") and _part_letters_in_order(bm.group("parts")):
        return bm.group("parts").strip()[0]
    return ""


def drawn_figures(ms: Manuscript) -> list[tuple[Paragraph, int]]:
    """(paragraph to anchor the query on, figure number) for each figure whose legend sits in a text box among drawn
    shapes, no picture near it (jehp_1793): the first paragraph of text after the drawing (else the one before)."""
    from .docmodel import has_picture

    body = ms.document.element.body
    tops = [p for p in ms.paragraphs if p.element.getparent() is body]
    index = {id(p.element): i for i, p in enumerate(tops)}
    out, seen = [], set()
    for c in captions(ms):
        p = ms.by_id(c.para_id)
        if c.kind != FIG or c.number in seen or not in_textbox(p):
            continue
        host = p.element
        while host is not None and host.getparent() is not body:
            host = host.getparent()
        i = index.get(id(host)) if host is not None else None
        if i is None or any(has_picture(q.element) for q in tops[max(0, i - 3):i + 4]):
            continue
        seen.add(c.number)
        after = next((q for q in tops[i + 1:i + 12] if q.text.strip() and not q.in_table), None)
        before = next((q for q in reversed(tops[max(0, i - 30):i]) if q.text.strip() and not q.in_table), None)
        anchor = after or before
        if anchor is not None:
            out.append((anchor, c.number))
    return out


def in_textbox(p: Paragraph) -> bool:
    """A paragraph inside a drawing's text box (a figure drawn with shapes, a floating box)."""
    return any(a.tag == f"{_W}txbxContent" for a in p.element.iterancestors())


def legends(ms: Manuscript) -> list[Legend]:
    """The figure legends outside tables, in document order, each with its continuation paragraphs. Cached."""
    cached = getattr(ms, "_fig_legends", None)
    if cached is not None:
        return cached
    paras = ms.paragraphs
    caps = {c.para_id: c for c in captions(ms) if c.kind == FIG}
    out: list[Legend] = []
    used: set[str] = set()
    for i, p in enumerate(paras):
        c = caps.get(p.id)
        if c is None or p.in_table or p.id in used or in_textbox(p):
            continue  # (a legend in a text box of a figure drawn with shapes is left as typed: jehp_1793)
        leg = Legend(p, c.number, c)
        _continue(ms, leg, i, c.match.start("rest"), caps, used)
        out.append(leg)
    if not out:
        leg = _unnumbered(ms, caps, used)
        if leg is not None:
            out.append(leg)
    ms._fig_legends = out
    return out


# A legend's text broken by a hard return: the line above ends mid-phrase ("... ELC modes and " / "CBE competency
# domains.": jehp_1485).
_BROKEN_END = re.compile(r"(?:\b(?:and|or|of|with|the|in|for|to|by|from|on|at|a|an|between|versus|vs|its|their)|,)\s*$",
                         re.I)
# A note under a figure legend: "Note. CCBL = Collaborative ...", "Abbreviations: ...", "Source: ..." (jehp_1485).
_FIG_NOTE = re.compile(r"^\s*(?:notes?|abbreviations?|source|key)\s*[.:]", re.I)


def _continue(ms: Manuscript, leg: Legend, i: int, start: int, caps: dict, used: set) -> None:
    """The paragraphs that continue the legend at index i. Besides part labels and lines broken by hand that the legend
    runs on with: a line broken mid-phrase ("broken": joined), a note under it ("Note. CCBL = ...") and, after the
    reference list, the prose paragraphs right under it ("The bar chart depicts ...": IJO 2623) -- "text", kept as
    paragraphs of their own but moved with the legend."""
    from .docmodel import has_picture

    paras = ms.paragraphs
    ref_i = references.find_heading(ms)
    back = ref_i is not None and i > ref_i
    part = _label_part(leg.cap.match) if leg.cap else ""
    if leg.cap is not None and leg.cap.match.group("more"):
        part = label_parts(leg.cap.match)[1]  # "Fig 2a & 2b ...": the next part is (c)
    expected = _next(part.lower()) if part else "a"
    expected = _expected_after(leg.para.text, start, leg.number, None, expected)
    blanks = 0
    for q in paras[i + 1:]:
        t = q.text
        if q.in_table or has_picture(q.element):
            break
        if not t.strip():
            blanks += 1
            if blanks > 2:
                break
            continue
        c2 = caps.get(q.id)
        if c2 is not None:
            p2 = _label_part(c2.match)
            from_bracket = p2 and not c2.match.group("part") and lead_part(c2.match.group("rest")) is None
            if c2.number == leg.number and p2 and (not from_bracket or p2.lower() == expected):
                leg.conts.append((q, "label"))
                last = label_parts(c2.match)[1] if c2.match.group("more") else p2
                expected = _expected_after(t, c2.match.start("rest"), leg.number, None, _next(last.lower()))
            else:
                break
        elif q.style.lower().startswith("heading") or t.lstrip().startswith("<H") or LEGENDS_HEADING.match(t):
            break
        else:
            lead = len(t) - len(t.lstrip())
            bm = _LEGEND_PART.match(t, lead) or _BARE_LABEL.match(t, lead)
            first = re.search(r"[A-La-l]", bm.group(0)).group(0).lower() if bm else ""
            prev = (leg.conts[-1][0] if leg.conts else leg.para).text
            kinds = [k for _, k in leg.conts]
            other_caption = caption_match(q) is not None or re.match(r"\s*(?:tables?|figs?|figures?)\s*\d", t, re.I)
            if not blanks and not kinds and not other_caption and not bm and _BROKEN_END.search(prev) \
                    and len(t.split()) <= 25 and re.match(r"\s*[A-Za-z(]", t):
                leg.conts.append((q, "broken"))  # "... ELC modes and " / "CBE competency domains."
                used.add(q.id)
                continue
            if not blanks and not other_caption and (_FIG_NOTE.match(t) or (
                    back and not bm and len(t.split()) >= 4 and re.match(r"\s*[A-Z]", t)
                    and re.search(r"[.!?)\]]\s*$", prev) and (not kinds or kinds[-1] == "text"))):
                leg.conts.append((q, "text"))  # a note, or a paragraph of the legend's own text under it
                used.add(q.id)
                continue
            if bm and first == expected:
                leg.conts.append((q, "part"))
                expected = _expected_after(t, lead, leg.number, None, expected)
            elif q.numbered and _letter_list(ms, q) and not bm:
                leg.conts.append((q, "list"))  # "a. Preoperative" as a Word list item: the letter is numbering
                expected = _next(expected)
            elif not blanks and lead >= 2 and t.lstrip()[:1].islower():
                leg.conts.append((q, "wrap"))  # a line the author broke by hand
            else:
                break
        used.add(q.id)
        blanks = 0


def _letter_list(ms: Manuscript, p: Paragraph) -> bool:
    """Whether a numbered paragraph is lettered (a., b., ...) by Word's list numbering."""
    try:
        numbering = ms.document.part.numbering_part.element
    except (AttributeError, KeyError, NotImplementedError):
        return False
    num_pr = p.element.find(f"{_W}pPr/{_W}numPr")
    if num_pr is None:
        return False
    num_id = num_pr.find(f"{_W}numId")
    ilvl = num_pr.find(f"{_W}ilvl")
    nid = num_id.get(f"{_W}val") if num_id is not None else None
    lvl = ilvl.get(f"{_W}val") if ilvl is not None else "0"
    for num in numbering.iter(f"{_W}num"):
        if num.get(f"{_W}numId") != nid:
            continue
        aid = num.find(f"{_W}abstractNumId")
        for an in numbering.iter(f"{_W}abstractNum"):
            if aid is not None and an.get(f"{_W}abstractNumId") == aid.get(f"{_W}val"):
                for lv in an.iter(f"{_W}lvl"):
                    if lv.get(f"{_W}ilvl") == lvl:
                        fmt = lv.find(f"{_W}numFmt")
                        return fmt is not None and fmt.get(f"{_W}val") in ("lowerLetter", "upperLetter")
    return False


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _unnumbered(ms: Manuscript, caps: dict, used: set) -> Legend | None:
    """A manuscript with one figure and no "Figure N" label: "Figure (A, B): Slit-lamp ..." or the one paragraph under
    a legends heading ("Image description") -> Figure 1."""
    from .docmodel import has_picture

    paras = ms.paragraphs
    for i, p in enumerate(paras):
        if p.in_table:
            continue
        m = _UNNUMBERED.match(p.text)
        if m and _LEGEND_PART.match(p.text, m.end()):
            leg = Legend(p, 1, None, (m.start("lab"), m.end("lab")))
            _continue(ms, leg, i, m.end(), caps, used)
            return leg
    for i, h in enumerate(paras):
        if h.in_table or not LEGENDS_HEADING.match(h.text) \
                or not re.search(r"legend|caption|description", h.text, re.I):
            continue
        rest = [(j, q) for j, q in enumerate(paras[i + 1:], i + 1) if q.text.strip() or has_picture(q.element)]
        if not rest:
            return None
        j, q = rest[0]
        text = q.text.strip()
        if q.in_table or has_picture(q.element) or len(text.split()) < 4 or q.style.lower().startswith("heading") \
                or text.startswith("<H") or caption_match(q) is not None:
            return None
        leg = Legend(q, 1, None, (len(q.text) - len(q.text.lstrip()),) * 2)
        _continue(ms, leg, j, leg.label[1], caps, used)
        tail = [x for k, x in rest[1:] if x not in leg.paragraphs]
        return leg if not tail else None  # more paragraphs after it: not clearly a single legend
    return None


def _apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    for s, e, new in sorted(edits, reverse=True):
        text = text[:s] + new + text[e:]
    return text


def _legend_changes(ms: Manuscript, leg: Legend, style: JournalStyle, run_on: bool | None = None) -> list[Change]:
    from .rules import _change
    from .rules_format import char_formats

    out: list[Change] = []
    # A line broken mid-phrase is joined to the legend; notes and prose under it stay paragraphs of their own (they
    # only move with it). With run-on legends the broken line runs on like a line broken by hand.
    run_on_set = bool(style.get("figures.legend_run_on", False) if run_on is None else run_on)
    conts = [(q, k) for q, k in leg.conts if k not in ("text", "broken")]
    broken = [q for q, k in leg.conts if k == "broken"]
    if run_on_set and conts and broken:
        conts = [(q, "wrap") for q in broken] + conts
        broken = []
    run_on = run_on_set and conts
    sep = style.get("figures.caption_separator", ": ")
    p = leg.para
    # The label of the legend paragraph and where its text starts.
    if leg.cap is not None:
        m = leg.cap.match
        rest_start, part = m.start("rest"), m.group("part") or ""
        part_last, parts_label = label_parts(m, style)[1:]
        prefix = f"{parts_label} " if part else ""
        dup = re.match(r"(?:Fig(?:ure)?\.?|Table|Graph)\s*" + str(leg.number) + r"[A-Za-z]?\s*[.:\-–]\s*",
                       m.group("rest"), re.I)
        if dup and not part:  # "Figure 3: Figure 3. Fundus ..." -> drop the repeated label
            rest_start += dup.end()
        label_span = (m.end("lead"), rest_start)
    else:
        rest_start, part, prefix = leg.label[1], "", ""
        label_span = leg.label
    new_label = f"Figure {leg.number}{sep}{prefix}"
    old_label = p.text[label_span[0]:label_span[1]]
    if old_label != new_label:
        label_changes = _diff(p, label_span[0], old_label, new_label, "fig_captions.label")
        for c in label_changes:
            if c.start == c.end == label_span[0] and not old_label:
                c.format = {"bold": True}  # "Figure 1: " put in front of an unlabelled legend: bold (G260)
        out += label_changes
    # G260: 'The label should be "Figure<space>#:<space>" in 12 points font size bold style'
    fmts0 = char_formats(p)
    word = re.match(r"\s*\S+(?:\s+\d+)?\s*[:.]?", old_label) if old_label.strip() else None
    word_end = label_span[0] + len(word.group(0)) if word else 0
    if word and not all(f.get("bold") for f, ch in zip(fmts0[label_span[0]:word_end], p.text[label_span[0]:word_end])
                        if not ch.isspace()):
        out.append(_change(p, label_span[0], word_end, p.text[label_span[0]:word_end], Layer.TABLES_FIGURES,
                           "fig_captions.label", {"bold": True}))
    # Part labels across the legend and its continuations.
    tokens: list[_Token] = []
    if part:
        tokens.append(_Token(0, -1, -1, part.lower(), part_last.lower(), "", True, edit=False))
    tokens += _tokens(p.text, rest_start, leg.number, style, 0, None if part else rest_start)
    starts = {0: rest_start}
    labels: dict[int, tuple[int, int, str]] = {}  # continuation k -> (start, end, new) of its "Figure 1B: " label
    for k, (q, kind) in enumerate(conts, 1):
        if kind == "label":
            m2 = caption_match(q)
            p2 = m2.group("part") or ""
            _, p2_last, p2_label = label_parts(m2, style)
            starts[k] = m2.start("rest")
            labels[k] = (m2.end("lead"), m2.start("rest"), f"{p2_label} " if p2 else "")
            if p2:
                tokens.append(_Token(k, -1, -1, p2.lower(), p2_last.lower(), "", True, edit=False))
            tokens += _tokens(q.text, starts[k], leg.number, style, k, None if p2 else starts[k])
        elif kind == "list":
            starts[k] = len(q.text) - len(q.text.lstrip())
            tokens.append(_Token(k, starts[k], starts[k], "", "", "", False, edit=False))  # lettered in _select
        else:
            starts[k] = len(q.text) - len(q.text.lstrip())
            tokens += _tokens(q.text, starts[k], leg.number, style, k, starts[k] if kind == "part" else None)
    lettered: dict[int, str] = {}  # list-item continuation -> the letter Word's numbering shows
    expected = "a"
    for t in tokens:
        if not t.first and not t.edit:
            t.first = t.last = lettered[t.seg] = expected
        if t.first:
            expected = _next(t.last)
    chosen = [t for t in _select(tokens) if t.edit]
    texts = [p.text] + [q.text for q, _ in conts]
    for t in chosen:  # "(a)  Coronal" -> "(a) Coronal"
        gap = re.match(r"\s{2,}(?=\S)", texts[t.seg][t.end:])
        if gap:
            t.end, t.new = t.end + gap.end(), t.new + " "
    if not style.get("figures.legend_part_labels", True):  # only a bracketed label opening the legend: "(A)" -> "(a)"
        chosen = [t for t in chosen if t.seg == 0 and t.bracketed and t.start == rest_start]
    edits: dict[int, list[tuple[int, int, str]]] = {}
    for t in chosen:
        edits.setdefault(t.seg, []).append((t.start, t.end, t.new))
    # No space just inside a bracket: "( yellow arrow)" -> "(yellow arrow)", "( 20 x magnification)" (IJO 1745)
    for seg, text in enumerate(texts):
        busy = [(s, e) for s, e, _ in edits.get(seg, [])]
        for m in re.finditer(r"\([ \t\u00a0]+(?=\S)|(?<=[^\s(])[ \t\u00a0]+\)", text):
            if (seg == 0 and m.start() < rest_start) or any(s < m.end() and m.start() < e for s, e in busy):
                continue
            edits.setdefault(seg, []).append((m.start(), m.end(), m.group(0).strip()))
    # The legend paragraph's own part labels: lower case in round brackets, set plain (not bold).
    fmts = char_formats(p)
    for s, e, new in edits.get(0, []):
        bold = any(f.get("bold") for f in fmts[s:e])
        if new != p.text[s:e] or bold:
            out.append(_change(p, s, e, new, Layer.TABLES_FIGURES, "fig_captions.parts",
                               {"bold": False} if bold else None))
    # The legend text in sentence case, its first word capitalised ("Figure 2: depicts ..." -> "Depicts ...";
    # figures.figure_caption_case: the JEHP editors, the sheet is silent; S398 "all sentences start with a capital").
    if style.get("figures.figure_caption_case") == "sentence":
        from .rules_sections import sentence_case_changes
        busy = [(s, e) for s, e, _ in edits.get(0, [])]
        after_part = {m.end() for m in re.finditer(r"\([a-lA-L](?:[,–-]\s*[a-lA-L])*\)\s*", p.text)}
        for c in sentence_case_changes(ms, p, "fig_captions.case", start=rest_start,
                                       hyphen_parts=style.get("headings.sentence_case_hyphen_parts", False)):
            if any(s < c.end and c.start < e for s, e in busy) or c.start in after_part \
                    or re.search(r"[.!?]\s+$", p.text[rest_start:c.start]) \
                    or (c.new.islower() and re.match(r"[A-Za-z]+[’']s\b", p.text[c.start:])):  # "Lotka’s Law"
                continue  # a part label's own edit; the word opening a part ("(b) Axial") or a sentence keeps its capital
            c.layer = Layer.TABLES_FIGURES
            out.append(c)
    # The final period: add | delete | keep (figures.figure_caption_end_period; unset: caption_end_period).
    end_mode = style.get("figures.figure_caption_end_period")
    if end_mode is None:
        keep = style.get("figures.caption_end_period", False) or style.get("figures.caption_end_period_keep")
        end_mode = "keep" if keep else "delete"
    if run_on:
        pieces, prev_text = [], _apply_edits(p.text, edits.get(0, []))
        group = f"legend-{p.id}"
        for k, (q, kind) in enumerate(conts, 1):
            e2 = list(edits.get(k, []))
            if k in labels:
                s, e, new = labels[k]
                e2.append((0, e, new))  # "Figure 1B: " -> "(b) "; "Fig.1 " before a bare "B" -> ""
            piece = _apply_edits(q.text, e2).strip()
            if not piece:
                continue
            if kind == "list":
                piece = f"({_case(lettered.get(k, 'a'), style, True)}) " + piece
            joint = " "
            if kind != "wrap" and not re.search(r"[.;:,!?&]$", (pieces[-1] if pieces else prev_text).rstrip()):
                joint = ". "
            pieces.append(joint + piece)
            c = _change(q, 0, len(q.text), "", Layer.TABLES_FIGURES, "fig_captions.run_on")
            c._group = group
            out.append(c)
        if pieces:
            tail = "".join(pieces)
            if end_mode == "delete" and _end_period(tail) is not None:
                tail = tail[:_end_period(tail)] + tail[_end_period(tail) + 1:].rstrip()
            elif end_mode == "add" and tail.strip() and not tail.rstrip().endswith((".", "?", "!", ":")):
                tail = tail.rstrip() + "."  # the legend's last part ends it
            k = len(p.text.rstrip())
            c = _change(p, k, len(p.text), tail, Layer.TABLES_FIGURES, "fig_captions.run_on", {"bold": False})
            c._group = group
            out.append(c)
            return out
    # "add": a legend that goes on in unlabelled lines ("b. ...", a line broken by hand) ends on its last line
    unlabelled = [k for k in range(1, len(conts) + 1) if k not in labels]
    seg0_mode = "keep" if end_mode == "add" and unlabelled else end_mode
    if broken:  # "... ELC modes and " + "CBE competency domains." -> one line (jehp_1485)
        q = broken[0]
        tail = " " + q.text.strip()
        if seg0_mode == "delete" and _end_period(tail) is not None:
            tail = tail[:_end_period(tail)] + tail[_end_period(tail) + 1:].rstrip()
        elif seg0_mode == "add" and not tail.rstrip().endswith((".", "?", "!", ":")):
            tail = tail.rstrip() + "."
        k = len(p.text.rstrip())
        group = f"legend-join-{p.id}"
        for c in (_change(p, k, len(p.text), tail, Layer.TABLES_FIGURES, "fig_captions.join"),
                  _change(q, 0, len(q.text), "", Layer.TABLES_FIGURES, "fig_captions.join")):
            c._group = group
            out.append(c)
    else:
        _end_changes(out, p, rest_start, seg0_mode, edits.get(0, []))
    for k, (q, kind) in enumerate(conts, 1):
        fq = char_formats(q)
        for s, e, new in edits.get(k, []):
            bold = any(f.get("bold") for f in fq[s:e])
            if new != q.text[s:e] or bold:
                out.append(_change(q, s, e, new, Layer.TABLES_FIGURES, "fig_captions.parts",
                                   {"bold": False} if bold else None))
        if k in labels:  # "Figure 1A. ..." then "Figure 1B. ...": the second is part (b) of Figure 1
            s, e, new = labels[k]
            if q.text[s:e] != new:
                out += _diff(q, s, q.text[s:e], new, "fig_captions.label")
            _end_changes(out, q, starts[k], end_mode, edits.get(k, []))
        elif end_mode == "add" and k == len(conts):
            _end_changes(out, q, 0, "add", edits.get(k, []))
    return out


def _end_changes(out: list[Change], p: Paragraph, rest_start: int, end_mode: str, edits) -> None:
    from .rules import _change

    rest = p.text[rest_start:]
    if end_mode == "add":
        body = rest.rstrip()
        if body and not body.endswith((".", "?", "!", ":")):
            k = rest_start + len(body)
            out.append(_change(p, k, k, ".", Layer.TABLES_FIGURES, "fig_captions.period_added"))
    elif end_mode == "delete":
        i = _end_period(rest)
        if i is not None:
            k = rest_start + i
            if any(s <= k < e for s, e, _ in edits):
                return
            end = len(p.text) if not p.text[k + 1:].strip() else k + 1  # "folds. " -> "folds"
            out.append(_change(p, k, end, "", Layer.TABLES_FIGURES, "fig_captions.end_period"))




# A table footnote under a table: abbreviations ("SD, standard deviation", "SE = standard error"), symbols ("*P <
# 0.05", "a Adjusted"), "Note:", "Data are ...", "Source: ...".
_FOOTNOTE = re.compile(r"^\s*(?:[*†‡§¶#]|[a-e](?:\s|\))|(?:abbreviations?|notes?|source)\s*[:.]|(?:data|values) (?:are|were)\b|"
                       r"[A-Z][A-Za-z0-9\-/]{0,12}\s*[:=,]\s)", re.I)


def table_footnotes(ms: Manuscript) -> list[list[Paragraph]]:
    """The footnote paragraphs right under each table (S: "Table footnotes | ... Period should be deleted at end of
    last footnote")."""
    paras, out = ms.paragraphs, []
    for i in range(1, len(paras)):
        if paras[i].in_table or not paras[i - 1].in_table:
            continue
        block = []
        for q in paras[i:]:
            if q.in_table:
                break
            if not q.text.strip():
                continue
            t = q.text.strip()
            if not _FOOTNOTE.match(t) or len(t.split()) > 80 or caption_match(q) is not None or t.endswith(":"):
                break
            block.append(q)
        if block:
            out.append(block)
    return out


# --- table notes: abbreviation lists ("term = definition", S244 / S246) ---------------------------------------------
#
# A pair is "TERM<sep> definition" with <sep> ",", "=" or ":"; TERM an abbreviation ("SD", "N/A", "df", "ρA and ρC"),
# not a plain capitalised word ("Overall, ...").
_TERM = r"[A-Za-zµμρα-ω%][\w%µμρα-ω²\-/]{0,14}(?:\s+(?:and|&)\s+[A-Za-zµμρα-ω][\w%µμρα-ω²\-/]{0,14})?"
_PAIR = re.compile(r"(?P<lead>\s*(?:(?:Abbreviations?|Abbr\.)\s*:\s*)?)(?P<term>" + _TERM + r")(?P<sep>\s*[,=:]\s*)"
                   r"(?P<defn>[A-Za-z(][^;=]*?)\s*$")
_SENTENCE = re.compile(r"(?<!\b[a-z])(?<!\bvs)(?<!\bal)(?<!\be\.g)(?<!\bi\.e)\.\s+(?=\S)")


def _is_term(term: str) -> bool:
    t = term.split()[0]
    # "or" is a word, "OR" (odds ratio) an abbreviation
    if re.fullmatch(r"[A-Z][a-z]+", t) or t in _MINOR or t.lower() in ("values", "data", "note", "notes"):
        return False
    return bool(re.search(r"[A-Z]", t)) or len(t) <= 3


def abbreviation_pairs(text: str) -> list[list[re.Match]]:
    """The abbreviation lists in a table note: for each sentence that is one ("df, degrees of freedom; SD, standard
    deviation"), its pairs (matches of _PAIR with positions in `text`). Sentences with other text are left out."""
    out = []
    starts = [0] + [m.end() for m in _SENTENCE.finditer(text)]
    ends = [m.start() + 1 for m in _SENTENCE.finditer(text)] + [len(text)]
    for a, b in zip(starts, ends):
        sent = text[a:b]
        body = sent.rstrip().rstrip(".")
        segs, pos = [], a
        # pairs joined by ";", or by "," when each is "term = definition" ("DS= Diopter Sphere, D= Diopter")
        for piece in re.split(r";|,(?=\s*" + _TERM + r"\s*=\s*[A-Za-z(])", body):
            segs.append((pos, piece))
            pos += len(piece) + 1
        pairs = []
        for k, (pos, piece) in enumerate(segs):
            m = _PAIR.match(text, pos, pos + len(piece))
            # "Kf=Flat Keratometry": with "=" a short capitalised term is an abbreviation too
            if m is None or not (_is_term(m.group("term")) or m.group("sep").strip() == "=" and
                                 len(m.group("term")) <= 6) or len(m.group("defn").split()) > 12:
                break
            if m.group("lead").strip() and k:
                break
            if m.group("sep").strip() == "," and "," in m.group("defn") and len(segs) == 1:
                break  # "IQR, interquartile range, SD, standard deviation": where the pairs split is unclear
            pairs.append(m)
        else:
            if pairs and (len(pairs) >= 2 or pairs[0].group("sep").strip() != "," or
                          re.search(r"[A-Z].*[A-Z]|/", pairs[0].group("term"))):
                out.append(pairs)
    return out


def table_notes(ms: Manuscript) -> list[list[Paragraph]]:
    """table_footnotes, and the paragraph right under a table that holds an abbreviation list after free text
    ("The mean difference represents ... P < 0.05. df, degrees of freedom; SD, standard deviation")."""
    blocks = table_footnotes(ms)
    seen = {q.id for b in blocks for q in b}
    paras = ms.paragraphs
    for i in range(1, len(paras)):
        q = paras[i]
        if q.in_table or not paras[i - 1].in_table or q.id in seen or not q.text.strip():
            continue
        if caption_match(q) is None and len(q.text.split()) <= 80 and abbreviation_pairs(q.text):
            blocks.append([q])
    return blocks


def _other_sep(got: str, sep: str) -> bool:
    """A pair's separator to replace by the journal's `sep`: "," always; ":" / "=" when the journal writes the other
    (owner, 2026-10-27: "term<space>=<space>definition", G211), and the spacing round it ("DS= Diopter" -> "DS =
    Diopter")."""
    return got != sep and (got.strip() == "," or sep.strip() in ":=")


def _footnote_abbreviations(ms: Manuscript, sep: str) -> list[Change]:
    """S244 (JEHP): 'Abbreviations should be as per format "term<space>=<space>definition"', S246 "Comma should be
    delimiter between abbreviations": "df, degrees of freedom; SD, standard deviation" -> "df = degrees of freedom, SD
    = standard deviation". A "term: definition" pair takes the journal's form too (owner, 2026-10-26: JEHP " = ", IJO
    ": ")."""
    from .rules import _change

    out = []
    for block in table_notes(ms):
        for q in block:
            for pairs in abbreviation_pairs(q.text):
                for k, m in enumerate(pairs):
                    if _other_sep(m.group("sep"), sep):
                        out.append(_change(q, m.start("sep"), m.end("sep"), sep, Layer.TABLES_FIGURES,
                                           "fig_captions.footnote_abbreviation"))
                    if k:
                        semi = q.text.rfind(";", pairs[k - 1].end() - 1, m.start("term"))
                        if semi >= 0:
                            out.append(_change(q, semi, semi + 1, ",", Layer.TABLES_FIGURES,
                                               "fig_captions.footnote_delimiter"))
    return out


def _footnote_periods(ms: Manuscript) -> list[Change]:
    """G213 / G215: no period at the end of the last abbreviation definition, nor at the end of the last footnote: the
    block's last paragraph, and its last paragraph that is only an abbreviation list (abbreviations given first)."""
    from .rules import _change
    out = []
    for block in table_notes(ms):
        abbr = [q for q in block if abbreviation_only(q.text)]
        notes = [q for q in block if not abbreviation_only(q.text)]
        ends = [x[-1] for x in (abbr, notes) if x]
        for p in ends:
            i = _end_period(p.text)
            if i is not None:
                end = len(p.text) if not p.text[i + 1:].strip() else i + 1
                out.append(_change(p, i, end, "", Layer.TABLES_FIGURES, "fig_captions.footnote_period"))
    return out


def abbreviation_only(text: str) -> bool:
    """A table note that is an abbreviation list and nothing else ("Abbreviations: SD, standard deviation; CI, ...")."""
    groups = abbreviation_pairs(text)
    if len(groups) != 1:
        return False
    body = text.rstrip().rstrip(".").rstrip()
    return not text[:groups[0][0].start()].strip() and groups[0][-1].end() >= len(body)


_RUN_PROPS = ("italic", "bold", "superscript", "subscript")


def _moved_pieces(q: Paragraph, edits: list[tuple[int, int, str]], drop_period: bool) -> tuple[str, list]:
    """The text of a note paragraph with `edits` applied (and its final period dropped), as [length, format] pieces
    that keep its run formatting (italic "P", a superscript letter)."""
    from .rules_format import char_formats

    fmts = char_formats(q)
    chars = [(ch, {k: bool(f.get(k)) for k in _RUN_PROPS}) for ch, f in zip(q.text, fmts + [{}] * len(q.text))]
    for s, e, new in sorted(edits, reverse=True):
        f = chars[s][1] if s < len(chars) else (chars[-1][1] if chars else {})
        chars[s:e] = [(ch, f) for ch in new]
    text = "".join(ch for ch, _ in chars)
    lead = len(text) - len(text.lstrip())
    chars = chars[lead:len(text.rstrip())]
    text = "".join(ch for ch, _ in chars)
    if drop_period:
        i = _end_period(text)
        if i is not None:
            chars = chars[:i]
            while chars and chars[-1][0].isspace():
                chars.pop()
    pieces: list = []
    for ch, f in chars:
        if pieces and pieces[-1][1] == f:
            pieces[-1][0] += 1
        else:
            pieces.append([1, dict(f)])
    return "".join(ch for ch, _ in chars), pieces


def _footnote_order(ms: Manuscript, style: JournalStyle) -> list[Change]:
    """G214 / S: "Abbreviations should be first and footnotes next": an abbreviation-list paragraph typed under the
    other notes of its table ("*P < 0.05" then "SD = standard deviation") moves above them. One group of changes: the
    list inserted (as edited: "term = definition", commas, no final period) as new paragraphs before the first note,
    and the paragraph where it was deleted."""
    from .rules import _change
    from .tracked import PARAGRAPH_BREAK

    sep = style.get("figures.table_footnote_abbreviation")
    drop = style.get("figures.table_footnote_end_period") == "delete"
    out = []
    for block in table_footnotes(ms):
        first_note = next((k for k, q in enumerate(block) if not abbreviation_only(q.text)), None)
        if first_note is None:
            continue
        late = [q for q in block[first_note + 1:] if abbreviation_only(q.text)]
        if not late or any(not abbreviation_only(q.text) for q in block[block.index(late[0]):]):
            continue  # only a list (or lists) closing the block, nothing after them
        texts, segs = [], []
        for k, q in enumerate(late):
            edits = []
            if sep:
                for pairs in abbreviation_pairs(q.text):
                    for j, m in enumerate(pairs):
                        if _other_sep(m.group("sep"), sep):
                            edits.append((m.start("sep"), m.end("sep"), sep))
                        if j:
                            semi = q.text.rfind(";", pairs[j - 1].end() - 1, m.start("term"))
                            if semi >= 0:
                                edits.append((semi, semi + 1, ","))
            text, pieces = _moved_pieces(q, edits, drop and k == len(late) - 1)
            texts.append(text)
            segs.append(pieces)
        target = block[first_note]
        group = f"footnote-order-{target.id}"
        ins = _change(target, 0, 0, PARAGRAPH_BREAK.join(texts) + PARAGRAPH_BREAK, Layer.TABLES_FIGURES,
                      "fig_captions.footnote_order")
        ins.segment_format = segs + [None]
        ins._group = group
        out.append(ins)
        for q in late:
            c = _change(q, 0, len(q.text), "", Layer.TABLES_FIGURES, "fig_captions.footnote_order")
            c._group = group
            out.append(c)
    return out



def _footnote_delimiters(ms: Manuscript) -> list[Change]:
    """S (IJO): "Comma should be delimiter between abbreviations": "CDVA = corrected ...; SD = standard deviation" ->
    "CDVA = corrected ..., SD = standard deviation". Only between "term = definition" / "term: definition" pairs
    (whether "=" or ":" is the owner's question; the definitions are left as typed)."""
    from .rules import _change

    out, seen = [], set()
    paras = ms.paragraphs
    blocks = [q for block in table_footnotes(ms) for q in block]
    after_table = False
    for q in paras:  # the first paragraph under a table, whatever it opens with
        if q.in_table:
            after_table = True
        elif q.text.strip():
            if after_table and caption_match(q) is None:
                blocks.append(q)
            after_table = False
    for q in blocks:
        if q.id in seen:
            continue
        seen.add(q.id)
        t = q.text
        # Between two definitions of abbreviations ("CDVA = corrected ...; SD = standard deviation"), not between
        # values ("df = 154; R² = 0.167") or running text ("the journals Autism; Child: Care, Health and ...").
        for m in re.finditer(r";(?=\s*([A-Za-zµμ%][\w%µμ\-/]{0,14})\s*[=:]\s*[A-Za-z(])", t):
            before = t[:m.start()].split(";")[-1]
            b = [x.group(1) for x in re.finditer(r"(?:^|[(,.]\s*|\s)([A-Za-zµμ%][\w%µμ\-/]{0,14})\s*[=:]\s*[A-Za-z(]",
                                                 before)]
            if b and _is_term(b[-1]) and _is_term(m.group(1)):
                out.append(_change(q, m.start(), m.end(), ",", Layer.TABLES_FIGURES, "fig_captions.footnote_delimiter"))
    return out


def footnote_paragraphs(ms: Manuscript) -> list[Paragraph]:
    """The table footnotes (table_footnotes) and the first paragraph under each table, whatever it opens with."""
    out = [q for block in table_footnotes(ms) for q in block]
    after_table = False
    for q in ms.paragraphs:
        if q.in_table:
            after_table = True
        elif q.text.strip():
            if after_table and caption_match(q) is None:
                out.append(q)
            after_table = False
    seen: set[str] = set()
    return [q for q in out if not (q.id in seen or seen.add(q.id))]


_MINOR = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor", "of", "on", "or", "per", "the",
          "to", "vs", "versus", "via", "with"}


def _title_word(w: str, first: bool, acronyms: set[str]) -> str:
    if len(w) == 1 or w in acronyms or not w.isalpha():
        return w  # "P", "LASIK" (an abbreviation the text uses), "6MONTHS"
    low = w.lower()
    return low if (low in _MINOR and not first) else low.capitalize()


def _table_heads_title_case(ms: Manuscript) -> list[Change]:
    """S / G205: column heads "In case the content is in All Caps then please make it as Title Case": "1 WEEK
    POSTOPERATIVE" -> "1 Week Postoperative", "P VALUE" -> "P Value". Words the running text writes in capitals
    ("LASIK", "BCVA") stay as they are."""
    from . import docview
    from .rules import _change, body_paragraphs
    from .rules_format import _header_rows

    paras = docview.classify(ms)
    by_id = {p.id: p for p in ms.paragraphs}
    for d in paras:
        d["el"] = by_id[d["id"]].element
    heads = _header_rows(paras)
    acronyms = {w for p in body_paragraphs(ms) if not re.fullmatch(r"[^a-z]*", p.text)
                for w in re.findall(r"\b[A-Z][A-Z0-9]+\b", p.text)}
    out = []
    for d in paras:
        cell = d["cell"]
        if not cell or cell["table"] not in heads or cell["row"] not in heads[cell["table"]]:
            continue
        p = by_id[d["id"]]
        words = re.findall(r"[A-Za-z]+", p.text)
        if not words or any(w != w.upper() for w in words) \
                or not any(len(w) >= 4 and w not in acronyms for w in words):
            continue  # not all capitals, or only abbreviations ("SD", "IOP")
        first = True
        for m in re.finditer(r"[A-Za-z]+", p.text):
            new = _title_word(m.group(0), first, acronyms)
            first = False
            if new != m.group(0):
                out.append(_change(p, m.start(), m.end(), new, Layer.TABLES_FIGURES, "fig_captions.table_head_case"))
    return out


def _head_cells(ms: Manuscript) -> list[Paragraph]:
    """The paragraphs of the tables' column-head rows (rules_format._header_rows)."""
    from . import docview
    from .rules_format import _header_rows

    paras = docview.classify(ms)
    by_id = {p.id: p for p in ms.paragraphs}
    for d in paras:
        d["el"] = by_id[d["id"]].element
    heads = _header_rows(paras)
    return [by_id[d["id"]] for d in paras if d["cell"] and d["cell"]["table"] in heads
            and d["cell"]["row"] in heads[d["cell"]["table"]]]


def _table_heads_sentence_case(ms: Manuscript, style: JournalStyle) -> list[Change]:
    """S (JEHP): "Column heads | Sentence case, bold": "Study Design" -> "Study design", "Experimental Group (n = 30)"
    -> "Experimental group (n = 30)". A word stays capitalised when the running text capitalises it inside a sentence
    (names, places), as do acronyms and mixed-case words (rules_sections.sentence_case_changes); heads in ALL CAPS are
    left to `table_heads_title_case` (S: "In case the content is in All Caps then please make it as Title Case")."""
    from .rules_sections import sentence_case_changes

    out = []
    for p in _head_cells(ms):
        words = re.findall(r"[A-Za-z]+", p.text)
        if not words or all(w == w.upper() for w in words):
            continue
        got = sentence_case_changes(ms, p, "fig_captions.table_head_case",
                                    hyphen_parts=style.get("headings.sentence_case_hyphen_parts", False))
        # Words in brackets ("Control Group (Mean ± SD)"): sentence_case_changes keeps them (a maker, a place);
        # in a column head they are units and statistics: lowered when the text writes them in lower case.
        from .rules_sections import _counts
        body = getattr(ms, "_sentence_body", "") or ""
        done = {c.start for c in got}
        for b in re.finditer(r"\(([^()]*)\)", p.text):
            for w in re.finditer(r"\b[A-Z][a-z]+\b", b.group(1)):
                i = b.start(1) + w.start()
                lower, caps = _counts(w.group(0), body)
                if i not in done and i > 0 and lower > caps:
                    got.append(_change_at(p, i, w.group(0)[0].lower()))
        for c in got:
            if not re.match(r"[A-Za-z]{3}", p.text[c.start:]):
                continue  # "95% Cl" (a mistyped "CI"), "No": two-letter words keep their case
            c.layer = Layer.TABLES_FIGURES
            out.append(c)
    return out


def _change_at(p: Paragraph, i: int, new: str) -> Change:
    from .rules import _change
    return _change(p, i, i + len(new), new, Layer.TABLES_FIGURES, "fig_captions.table_head_case")


def heading_above(ms: Manuscript, i: int) -> int | None:
    """Index of the legends heading right above paragraph i (pictures and blank lines between), if there is one."""
    from .docmodel import has_picture

    for j in range(i - 1, max(-1, i - 5), -1):
        p = ms.paragraphs[j]
        if p.in_table:
            return None
        if p.text.strip():
            return j if LEGENDS_HEADING.match(p.text) else None
        if not has_picture(p.element) and j < i - 3:
            return None
    return None


def _legends_heading(ms: Manuscript, style: JournalStyle, legs: list[Legend]) -> list[Change]:
    """The author's heading over the legends in the journal's words: "Legends:", "Legends to Figures", "FIG LEGENDS",
    "Image description" -> "Figure Legends" ("Figure Legend" for one figure), bold."""
    from .rules import _change
    from .rules_format import char_formats

    many = style.get("figures.legends_heading")
    if not many or not legs:
        return []
    single = style.get("figures.legends_heading_single") or many
    head = single if len({leg.number for leg in legs}) == 1 else many
    index = {p.id: i for i, p in enumerate(ms.paragraphs)}
    h = heading_above(ms, min(index[leg.para.id] for leg in legs))
    if h is None:
        return []
    p = ms.paragraphs[h]
    s, e = len(p.text) - len(p.text.lstrip()), len(p.text.rstrip())
    if p.text.lstrip().startswith("<"):
        return []
    bold = all(f.get("bold") for f, ch in zip(char_formats(p)[s:e], p.text[s:e]) if not ch.isspace())
    if p.text[s:e] != head:
        return [_change(p, s, len(p.text), head, Layer.TABLES_FIGURES, "fig_legends_heading", {"bold": True})]
    if not bold:
        return [_change(p, s, e, head, Layer.TABLES_FIGURES, "fig_legends_heading", {"bold": True})]
    return []


def rule_captions(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not style.get("figures.captions", True):
        return []
    # the abbreviation lists moved above the other notes first: their own edits go with the move
    out = _footnote_order(ms, style) if style.get("figures.table_footnote_order") == "abbreviations_first" else []
    if style.get("figures.table_footnote_end_period") == "delete":
        out += _footnote_periods(ms)
    abbr = style.get("figures.table_footnote_abbreviation")  # " = ": "SD, standard deviation" -> "SD = standard deviation"
    if abbr:
        out += _footnote_abbreviations(ms, abbr)
    if style.get("figures.table_footnote_delimiter") == ",":
        done = {(c.para_id, c.start) for c in out}
        out += [c for c in _footnote_delimiters(ms) if (c.para_id, c.start) not in done]
    if style.get("figures.table_heads_title_case"):
        out += _table_heads_title_case(ms)
    if style.get("figures.table_heads_case") == "sentence":
        out += _table_heads_sentence_case(ms, style)
    legs = legends(ms)
    out += _legends_heading(ms, style, legs)
    out += _split_caption_changes(ms, style)
    in_legend = {q.id for leg in legs for q in leg.paragraphs}
    for leg in legs:
        out += _legend_changes(ms, leg, style)
    for cap in captions(ms):
        p = ms.by_id(cap.para_id)
        # A caption inside a table only in its one-cell first row (the caption row): "Table IV. Results" there ->
        # "Table 4: Results" too (G201)
        if (p.in_table and not _first_row_caption(p)) or p.id in in_legend or in_textbox(p):
            continue  # (a caption in a text box: its copy for older Word would not change with it: jehp_1793)
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
                new_rest_prefix = f"{label_parts(m, style)[2]} "
        # "Table 3: Table 3. Individual..." -> drop the repeated label.
        dup = re.match(r"(?:Fig(?:ure)?\.?|Table|Graph)\s*" + str(cap.number) + r"[A-Za-z]?\s*[.:\-–]\s*", rest, re.I)
        if dup and not m.group("part"):
            rest_start += dup.end()
        # "Table 3. summarizes the mean scores ..." -> "Table 3: Mean scores ...": a sentence's verb
        # typed after the label is not part of the caption.
        verb = re.match(_VERB + r"\s+(?:the\s+|an?\s+)?(?=[a-z])", p.text[rest_start:]) if cap.kind == TABLE else None
        if verb:
            rest_start += verb.end()
        sep = style.get("figures.caption_separator", ": ")
        new_label = f"{label} {cap.number}{suffix}{sep}{new_rest_prefix}"
        old_label = p.text[m.end("lead"):rest_start]
        if new_label != old_label:
            out += _diff(p, m.end("lead"), old_label, new_label, "fig_captions.label")
        # S: 'followed by caption content in bold style and Sentence Case' (table captions; figures.table_caption_case)
        end_mode = style.get("figures.table_caption_end_period" if cap.kind == TABLE else
                             "figures.figure_caption_end_period" if cap.kind == FIG else "figures.graph_caption_end_period")
        if end_mode is None:
            keep = style.get("figures.caption_end_period", False) or style.get("figures.caption_end_period_keep")
            end_mode = "keep" if keep else "delete"
        # A note typed into the caption ("Table 1. Baseline characteristics (N=100). IQR, interquartile range; SD,
        # standard deviation.") goes under the table as its footnote (figures.table_caption_notes: the JEHP editors, 1787).
        split = _caption_note(ms, p, m, end_mode, style) if cap.kind == TABLE and \
            style.get("figures.table_caption_notes") == "split" else None
        cut = split[0] if split else len(p.text)
        if cap.kind == TABLE and style.get("figures.table_caption_case") == "sentence":
            from .rules_sections import sentence_case_changes
            out += [c for c in sentence_case_changes(ms, p, "fig_captions.case", start=rest_start,
                                                     hyphen_parts=style.get("headings.sentence_case_hyphen_parts", False))
                    if c.end <= cut]
        # The final period, per kind: add | delete | keep (figures.table_caption_end_period / figure_caption_end_period);
        # unset: the older caption_end_period / caption_end_period_keep switches (delete unless kept).
        if split:
            out += split[1]
        else:
            _end_changes(out, p, m.start("rest"), end_mode, [])
    return out


def _note_text(note: str, sep: str | None) -> str:
    """A note taken out of a caption, as a table footnote: abbreviation pairs "term = definition" joined by commas
    (S244 / S246, when `sep` is set), no final period (S248 / S252)."""
    if sep:
        edits = []
        for pairs in abbreviation_pairs(note):
            for k, m in enumerate(pairs):
                if _other_sep(m.group("sep"), sep):
                    edits.append((m.start("sep"), m.end("sep"), sep))
                if k:
                    semi = note.rfind(";", pairs[k - 1].end() - 1, m.start("term"))
                    if semi >= 0:
                        edits.append((semi, semi + 1, ","))
        note = _apply_edits(note, edits)
    note = note.strip()
    i = _end_period(note)
    return note[:i].rstrip() if i is not None else note


def _caption_note(ms: Manuscript, p: Paragraph, m: re.Match, end_mode: str, style: JournalStyle):
    """(where the caption now ends, [changes]) for a table caption with a note after its first sentence, or None. The
    note becomes a paragraph of its own under the table: in the caption's paragraph when the caption sits below the
    table, else at the start of the paragraph after the table (both changes go together or not at all)."""
    from .docmodel import paragraph_text
    from .rules import _change
    from .tracked import PARAGRAPH_BREAK

    rest_start = m.start("rest")
    text = p.text
    if p.in_table or "\n" in text:
        return None
    b = next((x for x in _SENTENCE.finditer(text, rest_start)
              if text[rest_start:x.start()].count("(") == text[rest_start:x.start()].count(")")), None)
    if b is None or len(text[rest_start:b.start()].split()) < 2:
        return None
    note = text[b.end():]
    if not re.search(r"[A-Za-z]{2}", note):
        return None
    new_note = _note_text(note, style.get("figures.table_footnote_abbreviation"))
    if not new_note:
        return None
    keep_dot = end_mode != "delete"
    cut = b.start() + 1 if keep_dot else b.start()
    group = f"caption-note-{p.id}"

    def tbl_side(step):
        x = getattr(p.element, step)()
        while x is not None and x.tag == f"{_W}p" and not paragraph_text(x).strip():
            x = getattr(x, step)()
        return x if x is not None and x.tag == f"{_W}tbl" else None

    above = tbl_side("getnext")  # the caption is above this table
    below = tbl_side("getprevious")
    split_here = _change(p, cut, len(text), PARAGRAPH_BREAK + new_note, Layer.TABLES_FIGURES,
                         "fig_captions.caption_note", {"bold": False, "italic": False})
    split_here._group = group
    if below is not None and above is None:  # caption under its table: the note follows it
        return cut, [split_here]
    if above is None:
        return None
    nxt = above.getnext()
    q = next((x for x in ms.paragraphs if x.element is nxt), None) if nxt is not None and nxt.tag == f"{_W}p" else None
    if q is not None and not q.in_table and not q.text.strip():  # a blank line under the table takes the note
        a = _change(p, cut, len(text), "", Layer.TABLES_FIGURES, "fig_captions.caption_note")
        c = _change(q, 0, 0, new_note, Layer.TABLES_FIGURES, "fig_captions.caption_note",
                    {"bold": False, "italic": False})
        a._group = c._group = group
        return cut, [a, c]
    # Else the note is split off under the caption, and goes under the table when the caption goes into the table's
    # first row (legends.py, figures.table_caption_row); without that move it would stay above (no split then).
    if not style.get("figures.table_caption_row"):
        return None
    return cut, [split_here]



# --- body citations ------------------------------------------------------------------

def _citation_paragraphs(ms: Manuscript) -> list[Paragraph]:
    from .rules import body_paragraphs

    # Running text the author typed in a Word heading style (a long paragraph) is body text all the same.
    in_legend = {q.id for leg in legends(ms) for q in leg.paragraphs}
    return [p for p in body_paragraphs(ms)
            if (not p.style.lower().startswith("heading") or len(p.text) > 150) and caption_match(p) is None
            and p.id not in in_legend]


_OPEN, _CLOSE = "([{", ")]}"
# A sentence's full stop right before a bracketed figure / table citation: "immunized. (Table 1)", "0.05). [Table 2]"
# (not "et al. (Figure 1)" or "[75]. (Figure 1)").
_STOP_BEFORE = re.compile(r"(?:(?<![\w.])(?!(?:al|etc|vs|approx|fig|figs|no)\b)[A-Za-z]{2,}|\d|%|\))(\.)\s*$")


# Words before the citation inside its brackets (S260, G241, G250: citations in round brackets -> square brackets):
# "(see Figure 1)", "(shown in figure 5)", "(as presented in Table 2)".
_LEAD_WORDS = re.compile(r"[({]\s*(?P<w>(?:see(?:\s+also)?|cf\.?|(?:as\s+)?(?:shown|presented|detailed|given|listed|"
                         r"summari[sz]ed|illustrated|depicted|displayed|reported|described)\s+in|refer\s+to)\s*)$",
                         re.I)
# A supplementary table / figure numbered "S1" (the label parser reads plain numbers only): "(Supplementary Table S2)",
# "(Supplimentary Table 8)" (the author's spelling corrected with the brackets).
_SUPP_CITATION = re.compile(r"[({]\s*(?P<lead>(?:see(?:\s+also)?|(?:as\s+)?shown\s+in)\s+)?"
                            r"(?P<supp>Supplementary|Supplemental|Suppl\.|Supplimentary|Supplemetary)"
                            r"(?P<rest>\s+(?:Tables?|Fig(?:ure)?s?\.?|Files?|Appendix|Materials?)\s*"
                            r"(?:S\d{1,2}[A-Za-z]?|\d{1,2}[A-Za-z]?)(?:\s*(?:,|and|&|-|–)\s*S?\d{1,2}[A-Za-z]?)*)\s*[)}]")

# A word or two after the citation inside its brackets: "(Figure 1, top)", "(Figure 2, left panel)".
_SUFFIX = re.compile(r"\s*,\s*([a-z]+(?:\s+[a-z]+){0,2})\s*(?=[)\]}])")


def sole_figure(ms: Manuscript) -> int | None:
    """The number of the manuscript's only figure caption, if it has exactly one figure."""
    nums = {c.number for c in captions(ms) if c.kind == FIG}
    return next(iter(nums)) if len(nums) == 1 else None


def _with_number(g: Group, sole: int | None) -> bool:
    """Give "(Figure B-C)" its figure number when the manuscript has one figure; False when it cannot."""
    for seg in g.segments:
        if seg.no_number:
            if sole is None:
                return False
            seg.items[0].num = sole
    return True


def _unbold(p: Paragraph, fmts: list[dict], lo: int, hi: int) -> list[Change]:
    """Bold figure / table citation in running text -> roman: "[{b:Table 3 and Figure 1}]" (IJO editors)."""
    from .rules import _change

    t = p.text
    if len(fmts) != len(t) or not any(fmts[i].get("bold") for i in range(lo, hi)):
        return []
    if not any(ch.isalnum() and not fmts[i].get("bold") for i, ch in enumerate(t) if not lo <= i < hi):
        return []  # a bold line: its citation stays as the line is
    out, i = [], lo
    while i < hi:
        if fmts[i].get("bold") and not t[i].isspace():
            j = i
            while j < hi and (fmts[j].get("bold") or t[j].isspace()):
                j += 1
            while t[j - 1].isspace():
                j -= 1
            out.append(_change(p, i, j, t[i:j], Layer.TABLES_FIGURES, "fig_citations", {"bold": False}))
            i = j
        else:
            i += 1
    return out


def _supplementary_citations(p: Paragraph, style: JournalStyle, lead_words: str | None) -> list[Change]:
    """"(Supplementary Table S2)" -> "[Supplementary Table S2]" (S260), "(Supplimentary Table 8)" -> "[Supplementary
    Table 8]"; with figures.before_full_stop, "groups. (Supplementary Table S2)" -> "groups [Supplementary Table S2]."."""
    from .rules import _change

    t = p.text
    out = []
    for m in _SUPP_CITATION.finditer(t):
        if re.fullmatch(r"\s+(?:Tables?|Fig(?:ure)?s?\.?)\s*\d.*", m.group("rest"), re.S) \
                and m.group("supp") in ("Supplementary", "Supplemental", "Suppl."):
            continue  # numbered plainly: the label parser has it (rule_citations)
        if m.group("lead") and lead_words is None:
            continue
        lead = "" if not m.group("lead") or lead_words == "drop" else re.sub(r"\s+", " ", m.group("lead"))
        supp = "Supplementary" if m.group("supp") in ("Supplimentary", "Supplemetary") else m.group("supp")
        new = f"[{lead}{supp}{m.group('rest')}]"
        if t[m.start():m.end()] == new:
            continue
        stop = _STOP_BEFORE.search(t, 0, m.start()) if style.get("figures.before_full_stop", False) else None
        if stop and re.match(r"\s*(?:$|[A-Z])", t[m.end():]):
            out.append(_change(p, stop.start(1), m.end(), " " + new + ".", Layer.TABLES_FIGURES, "fig_citations"))
            continue
        lead_space = " " if m.start() and (t[m.start() - 1].isalnum() or t[m.start() - 1] == "]") else ""
        if lead_space or t[m.start():m.start("supp")] != "[" + lead:
            out.append(_change(p, m.start(), m.start("supp"), lead_space + "[" + lead, Layer.TABLES_FIGURES,
                               "fig_citations"))
        if supp != m.group("supp"):
            out.append(_change(p, m.start("supp"), m.end("supp"), supp, Layer.TABLES_FIGURES, "fig_citations"))
        out.append(_change(p, m.end("rest"), m.end(), "]", Layer.TABLES_FIGURES, "fig_citations"))
    return out


def _stray_citation(ms: Manuscript, prev: Paragraph | None, p: Paragraph, style: JournalStyle, sole,
                    order: dict) -> list[Change] | None:
    """A paragraph holding only a bracketed figure / table citation (" (Table 3)."), right after a body paragraph
    that ends a sentence: the citation goes before that paragraph's final stop ("... the groups [Table 3].") and the
    stray line goes (S260: square brackets; a citation is part of its sentence)."""
    from .rules import _change

    m = re.fullmatch(r"\s*[(\[{]\s*(.+?)\s*[)\]}]\s*\.?\s*", p.text)
    if not m or prev is None or not prev.text.rstrip().endswith(".") or order[prev.id] + 1 > order[p.id]:
        return None
    if any(q.text.strip() or q.in_table for q in ms.paragraphs[order[prev.id] + 1:order[p.id]]):
        return None
    groups = [g for g in parse_groups(m.group(1), anycase=True) if _with_number(g, sole)]
    if len(groups) != 1 or groups[0].start != 0 or groups[0].end != len(m.group(1)):
        return None
    stop = len(prev.text.rstrip()) - 1
    if re.search(r"\[[^\]]*\]\s*\.$", prev.text.rstrip()) and prev.text.rstrip()[:-1].rstrip().endswith(
            "[" + render_group(groups[0], style) + "]"):
        cite = None  # the same citation already closes the sentence
    else:
        cite = f" [{render_group(groups[0], style)}]"
    out = [_change(p, 0, len(p.text), "", Layer.TABLES_FIGURES, "fig_citations")]
    if cite:
        out.append(_change(prev, stop, stop, cite, Layer.TABLES_FIGURES, "fig_citations"))
    group = f"stray-{p.id}"
    for c in out:
        c._group = group
    return out


# The other content of a round bracket that ends with a figure / table citation: "(P = 0.21; Table 2)".
_MIXED_OPEN = re.compile(r"\((?=[^()\[\]]*\w)[^()\[\]]{1,80}?(?P<sep>\s*[;,]\s*)$")


def rule_citations(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not style.get("figures.citations", True):
        return []
    from .rules_format import char_formats

    square = style.get("figures.brackets", "square") == "square"
    before_stop = style.get("figures.before_full_stop", False)
    unbold = style.get("figures.unbold_citations", False)
    lead_words = style.get("figures.bracket_lead_words")  # None: left as typed; "drop" / "keep"
    sole = sole_figure(ms)
    out = []
    paras = _citation_paragraphs(ms)
    order = {q.id: k for k, q in enumerate(ms.paragraphs)}
    for k, p in enumerate(paras):
        t = p.text
        joined = _stray_citation(ms, paras[k - 1] if k else None, p, style, sole, order) \
            if square and style.get("figures.stray_citations") == "join" else None
        if joined:  # " (Table 3)." alone on a line: the end of the paragraph above it ("... the groups [Table 3].")
            out += joined
            continue
        if square:
            out += _supplementary_citations(p, style, lead_words)
        fmts = char_formats(p) if unbold else []
        # "table 2" -> "Table 2" (S: "Table citation | Tables 1–6; Table 1"), when figures.lowercase_labels is on
        for g in parse_groups(t, anycase=bool(style.get("figures.lowercase_labels", False))):
            if not _with_number(g, sole):
                continue
            new = render_group(g, style)
            mine: list[Change] = []
            s, e = g.start, g.end
            # Bracketed: the group is the whole content of (...), {...} or a mismatched [...).
            ls = len(t[:s]) - len(t[:s].rstrip(" "))
            rs = len(t[e:]) - len(t[e:].lstrip(" "))
            suffix = ""
            sm = _SUFFIX.match(t, e)
            if sm:  # "(Figure 1, top)" -> "[Figure 1, top]"
                suffix, rs = f", {sm.group(1)}", sm.end() - e
            elif t[e + rs:e + rs + 1] == "," and t[e + rs + 1:].lstrip(" ")[:1] in (")", "}"):
                rs += 1 + len(t[e + rs + 1:]) - len(t[e + rs + 1:].lstrip(" "))  # "(Figure 1,2,3,)"
            elif t[e + rs:e + rs + 1] == "." and t[e + rs + 1:].lstrip(" ")[:1] in _CLOSE:
                rs += 1 + len(t[e + rs + 1:]) - len(t[e + rs + 1:].lstrip(" "))  # "(Table 1.)": a stray period
            o, c = s - ls - 1, e + rs
            bracketed = o >= 0 and c < len(t) and t[o] in _OPEN and t[c] in _CLOSE
            opener = "["
            lw = _LEAD_WORDS.search(t, 0, s) if lead_words and not bracketed and c < len(t) and t[c] in _CLOSE \
                else None
            if lw:  # "(see Figure 1)" -> "[Figure 1]" (drop) or "[see Figure 1]" (keep)
                o, bracketed = lw.start(), True
                if lead_words == "keep":
                    opener = "[" + re.sub(r"\s+", " ", lw.group("w")).rstrip() + " "
            mixed = None if bracketed or not square or not style.get("figures.split_mixed_brackets") \
                or c >= len(t) or t[c] != ")" or suffix else _MIXED_OPEN.search(t, 0, s)
            if mixed and t.count("(", mixed.start(), s) == 1 and ")" not in t[mixed.start():s]:
                # S260 "citations ... within round brackets ... changed to square brackets", the bracket holding
                # other content too: "(P = 0.21; Table 2)" -> "(P = 0.21) [Table 2]"
                from .rules import _change
                out.append(_change(p, mixed.start("sep"), s, ") [", Layer.TABLES_FIGURES, "fig_citations"))
                out += _diff(p, s, t[s:c + 1], f"{new}]", "fig_citations")
                continue
            if bracketed and square:
                lead = ""
                if o > 0 and (t[o - 1].isalnum() or t[o - 1] == "]"):
                    lead = " "  # "71](Figure 1)" -> "71] [Figure 1]"
                old = t[o:c + 1]
                repl = f"{lead}{opener}{new}{suffix}]"
                stop = _STOP_BEFORE.search(t, 0, o) if before_stop else None
                # "fully immunized. (Table 1) The" -> "fully immunized [Table 1]. The"; with a second stop after it,
                # "presentation. [Fig 1a, 1b, 1c]. On" -> "presentation [Fig. 1A-C]. On"
                end = c + 1 if stop and re.match(r"\s*(?:$|[A-Z])", t[c + 1:]) else \
                    c + 2 if stop and re.match(r"\.(?:\s|$)", t[c + 1:]) else None
                if end is not None:
                    from .rules import _change
                    mine.append(_change(p, stop.start(1), s, " " + opener, Layer.TABLES_FIGURES, "fig_citations"))
                    mine += _diff(p, s, t[s:end], f"{new}{suffix}].", "fig_citations")
                elif repl != old:
                    from .rules import _change
                    if lead:
                        mine.append(_change(p, o, o, " ", Layer.TABLES_FIGURES, "fig_citations"))
                    if t[o:s] != opener:  # the opening bracket with any space inside it: "( Figure 1" -> "[Fig. 1"
                        mine.append(_change(p, o, s, opener, Layer.TABLES_FIGURES, "fig_citations"))
                    mine += _diff(p, s, t[s:c + 1], f"{new}{suffix}]", "fig_citations")
                lo, hi = o, c + 1
            else:
                if new != t[s:e]:
                    mine += _diff(p, s, t[s:e], new, "fig_citations")
                lo, hi = s, e
            if unbold:
                # the bold full stop right after a bold "(Fig. 1A)." goes roman with it (IJO_2510: "[Fig. 1A]**.**")
                while hi < len(t) and t[hi] in ".,;:" and len(fmts) == len(t) and fmts[hi].get("bold"):
                    hi += 1
                flat = _unbold(p, fmts, lo, hi)
                if flat:
                    for ch in mine:
                        if ch.new.strip():
                            ch.format = {"bold": False}
                    mine += flat
            out += mine
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
    sole = sole_figure(ms)
    for p in _citation_paragraphs(ms):
        for g in parse_groups(p.text, anycase=True):
            if not _with_number(g, sole):
                continue
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
    for p, check in image_queries(ms, style):
        qs.add(p.id, check, 0, len(p.text))
    for p, n in drawn_figures(ms):
        qs.add(p.id, "figures.drawn_shapes", 0, 0, n=n)
    for p, s0, e0, check, fmt in footnote_queries(ms, style):
        qs.add(p.id, check, s0, e0, **fmt)
    if style.get("figures.stray_citations") == "query":
        paras = _citation_paragraphs(ms)
        order = {q.id: k for k, q in enumerate(ms.paragraphs)}
        for k, p in enumerate(paras):
            if k and _stray_citation(ms, paras[k - 1], p, style, sole_figure(ms), order):
                qs.add(p.id, "figures.stray_citation", 0, len(p.text))
    cits = citations(ms)
    caps = [c for c in captions(ms)]
    lines = _line_captions(ms)  # "... text.\nTable 2: Means ..." just above its table (1764): provided, not a caption
    renumbered = renumber_plan(ms, style)
    for kind in (FIG, TABLE):
        kc = [c for c in cits if c.kind == kind]
        cap_nums = {c.number for c in caps if c.kind == kind}
        known = cap_nums | {n for k, n in lines if k == kind}
        # Order: each number's first citation must come after every smaller cited number's.
        seen: list[int] = []
        for c in kc:
            new = [n for n in c.numbers if n not in seen]
            if any(seen and n < max(seen) for n in new) and kind not in renumbered:  # renumbered: header.py
                qs.add(c.para_id, "figures.order", c.start, c.end, kind=kind)
                break
            seen += new
        cited = {n for c in kc for n in c.numbers}
        # Cited, but no caption / table in the file (only when the file carries captions of that kind).
        if cap_nums:
            missing_done: set[int] = set()
            for c in kc:
                for n in c.numbers:
                    if n not in known and n not in missing_done:
                        missing_done.add(n)
                        check = "figures.no_caption" if kind == FIG else "figures.no_table"
                        qs.add(c.para_id, check, c.start, c.end, n=n)
        elif kind == FIG and kc and not known and style.get("figures.query_no_captions", False):
            # G244: "In case citation(s) is present but caption is not present then please query using comments" --
            # no figure legend in the file at all (figures sent as files, 1486): one query at the first citation.
            nums = sorted(cited)
            figs = f"Figure {nums[0]}" if len(nums) == 1 else \
                f"Figures {nums[0]}\u2013{nums[-1]}" if nums == list(range(nums[0], nums[-1] + 1)) else \
                "Figures " + ", ".join(map(str, nums[:-1])) + f" and {nums[-1]}"
            qs.add(kc[0].para_id, "figures.no_captions", kc[0].start, kc[0].end, figs=figs)
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


def _line_captions(ms: Manuscript) -> list[tuple[str, int]]:
    """(kind, number) of captions typed as a later line of a body paragraph ("...as follows.\nTable 2: Means and
    standard deviations ..."), which `captions` does not take (the paragraph is running text)."""
    out = []
    for p in ms.paragraphs:
        if p.in_table or "\n" not in p.text:
            continue
        for line in p.text.split("\n")[1:]:
            m = _CAPTION.match(line)
            if m and re.search(r"[.:\-–—]", m.group("sep")) and m.group("num").isdigit():
                word = m.group("word").lower()
                out.append((TABLE if word.startswith("tab") else FIG, int(m.group("num"))))
    return out


# --- renumbering by first citation ----------------------------------------------------

# Every stylesheet: "All the reference, figure and table citations are to be cited in square bracket and in
# chronological order". Tables and figures first cited out of order are renumbered in the citations and the
# captions (and the tables / legends are put in the new order when the file is rendered: header.py), as the
# references are (pre-editing guide E.g. 4). Only when it is safe: every cited number has exactly one caption,
# every caption is cited, numbers run 1..n and every citation can be rewritten; otherwise the guide's query
# ("Figure/table citation not in order") goes instead.

_DIGITS = re.compile(r"\d{1,2}(?!\d)")


@dataclass
class Renumbering:
    kind: str
    mapping: dict[int, int]  # old number -> new number
    captions: dict[int, str]  # old number -> caption paragraph id

    @property
    def applies(self) -> bool:
        return any(k != v for k, v in self.mapping.items())


def _all_citation_groups(ms: Manuscript):
    """(paragraph, group) for every figure / table citation outside the reference list and outside the caption
    labels: the body, table cells and notes, legends."""
    from . import references

    refs = {r.para_id for r in references.extract(ms)}
    for r in references.extract(ms):
        refs.update(pid for pid, _ in r.continuation)
    for p in ms.paragraphs:
        if p.id in refs or not p.text.strip():
            continue
        m = caption_match(p)
        start = m.start("rest") if m else 0
        for g in parse_groups(p.text, anycase=True):
            if g.start >= start:
                yield p, g


def _rewrite_group(text: str, g: Group, kind: str, mapping: dict[int, int]) -> list[tuple[int, int, str]] | None:
    """(start, end, new number) for the number tokens of `kind` in the group; None when it cannot be rewritten
    safely (Roman numerals, a range that would no longer be a range)."""
    toks = list(_DIGITS.finditer(text, g.start, g.end))
    expected = sum((it.num is not None) + (it.to_num is not None) for seg in g.segments for it in seg.items)
    if len(toks) != expected:
        return None
    out: list[tuple[int, int, str]] = []
    k = 0
    for seg in g.segments:
        mine = []
        for it in seg.items:
            if it.num is None:
                continue
            a = toks[k]
            b = toks[k + 1] if it.to_num is not None else None
            k += 1 + (b is not None)
            mine.append((it, a, b))
        if seg.kind != kind or seg.supp:
            continue
        if any(it.num not in mapping or (it.to_num is not None and it.to_num not in mapping) for it, _, _ in mine):
            return None
        simple = all(not it.part and not it.to_part and it.to_num is None for it, _, _ in mine)
        if simple:  # "Tables 3 and 4" stays "Tables 3 and 4" when 3 and 4 swap
            new = sorted(mapping[it.num] for it, _, _ in mine)
            out += [(a.start(), a.end(), str(n)) for (_, a, _), n in zip(mine, new)]
            continue
        for it, a, b in mine:
            if b is None:
                out.append((a.start(), a.end(), str(mapping[it.num])))
            elif it.to_num == it.num:  # "1a to 1d"
                out += [(a.start(), a.end(), str(mapping[it.num])), (b.start(), b.end(), str(mapping[it.num]))]
            else:
                got = sorted(mapping[n] for n in range(it.num, it.to_num + 1))
                if got != list(range(got[0], got[-1] + 1)) or it.part or it.to_part:
                    return None
                out += [(a.start(), a.end(), str(got[0])), (b.start(), b.end(), str(got[-1]))]
    return [(s, e, n) for s, e, n in out if text[s:e] != n]


def renumber_plan(ms: Manuscript, style: JournalStyle) -> dict[str, Renumbering]:
    """kind -> the renumbering of tables / figures that applies (kinds that need none, or where it is not safe,
    are left out). Cached on the manuscript."""
    cached = getattr(ms, "_fig_renumber", None)
    if cached is not None:
        return cached
    out: dict[str, Renumbering] = {}
    ms._fig_renumber = out
    if not style.get("figures.renumber", True):
        return out
    cits = citations(ms)
    caps = captions(ms)
    groups = list(_all_citation_groups(ms))
    for kind in (TABLE, FIG):
        nums = [c.number for c in caps if c.kind == kind]
        if not nums or len(nums) != len(set(nums)) or sorted(nums) != list(range(1, len(nums) + 1)):
            continue
        if any(c.kind == kind and c.match.group("part") for c in caps):
            continue
        order: list[int] = []
        for c in cits:
            if c.kind == kind:
                order += [n for n in c.numbers if n not in order]
        if sorted(order) != sorted(nums):
            continue  # a caption never cited, or a citation with no caption: query, do not renumber
        mapping = {n: i for i, n in enumerate(order, 1)}
        if all(k == v for k, v in mapping.items()):
            continue
        if any(_rewrite_group(p.text, g, kind, mapping) is None for p, g in groups
               if any(s.kind == kind and not s.supp for s in g.segments)):
            continue
        out[kind] = Renumbering(kind, mapping, {c.number: c.para_id for c in caps if c.kind == kind})
    return out


def rule_renumber(ms: Manuscript, style: JournalStyle) -> list[Change]:
    """Renumbered table / figure citations and caption labels (registered before fig_citations / fig_captions:
    where they touch the same number, the renumbering wins)."""
    from . import provenance
    from .rules import _change

    plans = renumber_plan(ms, style)
    out: list[Change] = []
    if not plans:
        return out
    for p, g in _all_citation_groups(ms):
        for kind, pl in plans.items():
            for s, e, new in _rewrite_group(p.text, g, kind, pl.mapping) or []:
                c = _change(p, s, e, new, Layer.TABLES_FIGURES, "fig_renumber")
                c.provenance = provenance.make("renumber", f"{kind} {p.text[s:e]} → {new}",
                                               f"{kind.lower()}s renumbered in order of first citation in the text",
                                               figure_kind=kind, old=[int(p.text[s:e])], new=[int(new)])
                out.append(c)
    for cap in captions(ms):
        pl = plans.get(cap.kind)
        if pl is None:
            continue
        m = cap.match
        new = str(pl.mapping[cap.number])
        if m.group("num") != new:
            p = ms.by_id(cap.para_id)
            c = _change(p, m.start("num"), m.end("num"), new, Layer.TABLES_FIGURES, "fig_renumber")
            c.provenance = provenance.make("renumber", f"{cap.kind} {cap.number} → {new}",
                                           f"{cap.kind.lower()}s renumbered in order of first citation in the text",
                                           figure_kind=cap.kind, old=[cap.number], new=[int(new)], caption=True)
            out.append(c)
    return out


# --- legends from other submission material (figures_insert.py) ----------------------------------

def legend_texts(ms: Manuscript, style: JournalStyle) -> dict[int, str]:
    """Figure number -> its legend text after the "Figure N: " label, edited as the legend rules edit it (parts run
    on, part labels "(a)", end period): for legends supplied in a separate file of the submission."""
    out: dict[int, str] = {}
    for leg in legends(ms):
        if leg.number in out:
            continue
        p = leg.para
        changes = [c for c in _legend_changes(ms, leg, style, run_on=True) if c.para_id == p.id and not c.format_only]
        text = _apply_edits(p.text, [(c.start, c.end, c.new) for c in changes])
        text = re.sub(r"^\s*Figure\s+\d+\s*:\s*", "", re.sub(r"\s+", " ", text)).strip()
        if text:
            out[leg.number] = text
    return out


def legend_from_text(text: str, number: int, style: JournalStyle) -> str:
    """A legend given as plain text (the submission XML's <legend>): its "Figure N" label dropped, part labels "(a)",
    the end period as the journal has it."""
    t = re.sub(r"\s+", " ", text or "").strip()
    lm = _CAPTION_PARTS.match(t)
    if lm is None or not _more_ok(lm):
        lm = _CAPTION.match(t)
    head = ""
    if lm is not None and lm.group("part") and lm.group("num") == str(number) \
            and not lm.group("word").lower().startswith(("tab", "graph")):
        head, t = label_parts(lm, style)[2], lm.group("rest")  # "Fig 2a & 2b The goat's eye" -> "(a and b) The ..."
        last = label_parts(lm)[1].lower()
    else:
        m = re.match(r"(?:fig(?:ure)?s?\.?)\s*(?:no\.?\s*)?" + str(number) + r"(?!\d)\s*(?:[.:\-–—]+\s*)?", t, re.I)
        if m:
            t = t[m.end():]
    if not t:
        return ""
    if head:
        toks = [x for x in _select([_Token(0, -1, -1, "a", last, "", True, edit=False)] +
                                   _tokens(t, 0, number, style, 0, None)) if x.edit]
    else:
        toks = [x for x in _select(_tokens(t, 0, number, style, 0, 0)) if x.edit]
    t = _apply_edits(t, [(x.start, x.end, x.new) for x in toks]).strip()
    if head:
        t = f"{head} {t}"
    end_mode = style.get("figures.figure_caption_end_period")
    if end_mode is None:
        keep = style.get("figures.caption_end_period", False) or style.get("figures.caption_end_period_keep")
        end_mode = "keep" if keep else "delete"
    i = _end_period(t)
    if end_mode == "delete" and i is not None:
        t = t[:i].rstrip()
    elif end_mode == "add" and t and not t.endswith((".", "?", "!", ":")):
        t += "."  # as the legends in the text get it (figures.figure_caption_end_period)
    return t
