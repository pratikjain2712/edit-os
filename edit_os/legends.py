"""Figure legends at the end of the article under a "Figure Legends" heading; table captions in the table's first row.

The guide: G265 "The caption(s) should be placed at end of document (before table if present)", G195 the tables "at
end of document after figure captions"; the stylesheets: "Image should be present first and below it the respective
caption" (a picture moves with its legend). The back of the article then reads: references, the First author block
(first_author.py), the "Figure Legends" heading and the legends, the tables.

  - Legends in the body (or after the tables) move there: one `BlockMove` record the editor accepts or rejects. In the
    tracked copy each legend is deleted where it was and inserted, as edited, at the end (header.move_blocks does the
    same for renumbered tables); in the clean copy it is moved. A heading over the legends moves with them; with none,
    the move adds one.
  - Legends already in place with no heading get the heading as an inserted paragraph (`NewParagraph`); an author's
    heading ("Legends:", "Legends to Figures", "Image description") is renamed by `rules_figures` (fig_captions).
  - Figures supplied only as files (figures_insert.py) go under the heading too.
  - Table captions go into a merged first row of their table (`figures.table_caption_row`): the IJO technical editors'
    form in every article with tables (8 of 8); the sheet is silent on where the caption sits.

Settings: `figures.legends_to_end`, `figures.legends_heading`, `figures.legends_heading_single`,
`figures.table_caption_row`.

    back_slot(ms)                       where the back blocks start (First author, legends, tables)
    plan(ms, style, figure_inserts)     -> list of records (NewParagraph / BlockMove) and notes
    render(ms, inserts, statuses, ...)  carry out the accepted (tracked copy: also pending) moves
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from . import provenance, references, rules_figures
from .changes import Layer, Source, Status
from .docmodel import Manuscript, has_picture, paragraph_text
from .frontmatter import NewParagraph
from .style import JournalStyle

MOVE_LEGENDS = "fig_legends_move"
MOVE_TABLE_CAPTION = "table_caption_row"
HEADING = "fig_legends_heading"


@dataclass
class BlockMove(NewParagraph):
    """Paragraphs to move (legends to the end, a table caption into its table). `anchor` / `where` say where they go
    for the review screen; `render` works out the place in the rendered document."""
    kind: str = "legends"  # legends | table_caption
    blocks: list[list[str]] = field(default_factory=list)  # paragraph ids, one list per block, in order
    heading: str = ""  # legends: a heading to put over them ("" = none, or the author's heading moves with them)
    refs_end: str = ""  # legends: the last paragraph of the reference list
    side: str = ""  # table_caption: "above" | "below" -- where the manuscript's table captions sit

    def to_dict(self) -> dict:
        return {**super().to_dict(), "move": self.kind, "blocks": self.blocks, "heading": self.heading,
                "refs_end": self.refs_end, "side": self.side}

    @classmethod
    def from_dict(cls, d: dict) -> "BlockMove":
        return cls(id=d["id"], anchor=d["anchor"], where=d.get("where", "before"),
                   runs=[(t, f) for t, f in d.get("runs") or []], role=d.get("role", "move"),
                   status=Status(d.get("status", "suggested")), source=Source(d.get("source", "rule")),
                   reason=d.get("reason", MOVE_LEGENDS), provenance=d.get("provenance") or None,
                   layer=Layer(d.get("layer", Layer.TABLES_FIGURES.value)), kind=d.get("move", "legends"),
                   blocks=[list(b) for b in d.get("blocks") or []], heading=d.get("heading", ""),
                   refs_end=d.get("refs_end", ""), side=d.get("side", ""))


def is_move(n) -> bool:
    return isinstance(n, BlockMove)


# --- where things are ------------------------------------------------------------------------

def refs_end(ms: Manuscript) -> int | None:
    """Index of the last paragraph of the reference list (its heading when it has no entries)."""
    head = references.find_heading(ms)
    if head is None:
        return None
    index = {p.id: i for i, p in enumerate(ms.paragraphs)}
    refs = references.extract(ms)
    ids = [r.para_id for r in refs] + [pid for r in refs for pid, _ in r.continuation]
    return max([head] + [index[i] for i in ids if i in index])


def _is_table_caption(p) -> bool:
    m = rules_figures.caption_match(p)
    return m is not None and m.group("word").lower().startswith("tab")


def legend_heading(ms: Manuscript, first: int) -> int | None:
    """Index of the heading right above the legend (or its picture) at index `first`, if it has one."""
    return rules_figures.heading_above(ms, first)


def _block_start(ms: Manuscript, i: int) -> int:
    """A legend's first paragraph: the picture right above it, if any (blank lines between)."""
    start = i
    for j in range(i - 1, max(-1, i - 4), -1):
        p = ms.paragraphs[j]
        if p.in_table:
            break
        if has_picture(p.element) and not p.text.strip():
            start = j
            continue
        if p.text.strip():
            break
    return start


def back_slot(ms: Manuscript) -> tuple[str, str] | None:
    """(anchor paragraph id, "before" | "after"): where the back of the article starts after the reference list -- the
    legends' heading (or the picture over the first legend, or the legend) when legends follow the references before
    any table, else the first table caption (or the paragraph above the first table) -- with none of these, after the
    last paragraph with text or a picture. None for an empty document."""
    paras = ms.paragraphs
    if not paras:
        return None
    end = refs_end(ms)
    index = {p.id: i for i, p in enumerate(paras)}
    legs = {index[leg.para.id] for leg in rules_figures.legends(ms)}
    if end is not None:  # after the references, a legend in a heading style or written as a sentence counts too
        legs |= {i for i in range(end + 1, len(paras)) if not paras[i].in_table and _LEGEND_START.match(paras[i].text)}
    if end is not None:
        for i in range(end + 1, len(paras)):
            p = paras[i]
            if p.in_table:
                above = [j for j in range(end, i) if not paras[j].in_table and (paras[j].text.strip() or
                                                                                   has_picture(paras[j].element))]
                return paras[above[-1] if above else end].id, "after"
            if i in legs:
                h = legend_heading(ms, _block_start(ms, i))
                return paras[h if h is not None else _block_start(ms, i)].id, "before"
            if rules_figures.LEGENDS_HEADING.match(p.text) and any(j > i for j in legs):
                return p.id, "before"
            if _is_table_caption(p) or _TABLES_HEADING.match(p.text):
                return p.id, "before"
    body = [i for i, p in enumerate(paras) if not p.in_table]
    if not body:
        return None
    last = body[-1]
    filled = [i for i in body if paras[i].text.strip() or has_picture(paras[i].element)]
    if filled and not any(paras[j].in_table for j in range(filled[-1] + 1, last + 1)):
        last = filled[-1]  # trailing empty paragraphs stay below the block (a table after it does not)
    return paras[last].id, "after"


def _first_table_after(ms: Manuscript, end: int) -> int:
    """Index of the first table (or table caption) after the reference list; len(paragraphs) when there is none."""
    for i in range(end + 1, len(ms.paragraphs)):
        p = ms.paragraphs[i]
        if p.in_table or _is_table_caption(p) or _TABLES_HEADING.match(p.text):
            return i
    return len(ms.paragraphs)


# A heading over the tables at the end: "Tables", "TABLES:", "Table Legends".
_TABLES_HEADING = re.compile(r"^\s*(?:<H\d>)?\s*tables?(?:\s+(?:legends?|captions?))?\s*:?\s*$", re.I)
# "Figure 1", "Fig. 2:", "Figure 1 presents ...": a figure legend after the reference list, whatever its style.
_LEGEND_START = re.compile(r"^\s*(?:<H\d>)?\s*fig(?:ure)?s?\.?\s*\d{1,2}(?!\d)", re.I)


def heading_text(style: JournalStyle, n_figures: int) -> str:
    many = style.get("figures.legends_heading") or ""
    return (style.get("figures.legends_heading_single") or many) if n_figures == 1 else many


# --- planning --------------------------------------------------------------------------------

def plan(ms: Manuscript, style: JournalStyle, figure_inserts=()) -> tuple[list[NewParagraph], list[str]]:
    """Records for the legends (move / heading) and the table captions; notes for the editor."""
    out: list[NewParagraph] = []
    notes: list[str] = []
    if style.get("figures.legends_to_end"):
        out += _plan_legends(ms, style, figure_inserts, notes)
    if style.get("figures.table_caption_row"):
        out += _plan_table_captions(ms)
    return out, notes


def _plan_legends(ms: Manuscript, style: JournalStyle, figure_inserts, notes: list[str]) -> list[NewParagraph]:
    paras = ms.paragraphs
    index = {p.id: i for i, p in enumerate(paras)}
    legs = rules_figures.legends(ms)
    end = refs_end(ms)
    no_legend = [f for f in figure_inserts if getattr(f, "legend", "")]
    numbers = {leg.number for leg in legs} | {f.figure for f in no_legend}
    head = heading_text(style, len(numbers))
    if end is None or not (legs or no_legend):
        return []
    if any(q.in_table for leg in legs for q in leg.paragraphs):
        notes.append("Figure legends in a table: left where they are")
        return []
    stop = _first_table_after(ms, end)
    in_place = all(end < index[leg.para.id] < stop for leg in legs)
    starts = [_block_start(ms, index[leg.para.id]) for leg in legs]
    h = legend_heading(ms, min(starts)) if legs else None
    prov = provenance.make("guide", "Pre-editing guide", "G265: the captions at the end of the document, before the "
                           "tables; G195: the tables after the figure captions", rule=MOVE_LEGENDS)
    if legs and not in_place:
        heads = sorted({x for x in (legend_heading(ms, s) for s in starts) if x is not None})
        blocks = [[paras[j].id for j in heads]] if heads else []
        blocks += [[leg.para.id] for leg in legs]
        slot = back_slot(ms)
        anchor, where = slot if slot else (paras[-1].id, "after")
        what = ", ".join(f"Figure {leg.number}" for leg in legs)
        return [BlockMove(id="legends_move", anchor=anchor, where=where,
                          runs=[(f"[Figure legends moved to the end, after the references: {what}]", {})],
                          role="move", reason=MOVE_LEGENDS, provenance=prov, layer=Layer.TABLES_FIGURES,
                          kind="legends", blocks=blocks, heading="" if heads else head,
                          refs_end=paras[end].id)]
    if h is not None or not head:
        return []
    # In place (or only supplied figures) with no heading: add one.
    if legs:
        anchor, where = paras[min(starts)].id, "before"
    else:
        slot = back_slot(ms)
        if slot is None:
            return []
        anchor, where = slot
    hp = provenance.make("journal_style", "Journal practice", f"\"{head}\" over the figure legends (the editors' "
                         "heading; the sheet is silent)", rule=HEADING)
    return [NewParagraph("legends_heading", anchor, where, [(head, {"bold": True})], "legends_heading",
                         source=Source.RULE, reason=HEADING, provenance=hp, layer=Layer.TABLES_FIGURES)]


def _plan_table_captions(ms: Manuscript) -> list[NewParagraph]:
    """A table caption right above its table (or right below it, notes aside) goes into a merged first row."""
    out = []
    body = ms.document.element.body
    side = _side(ms)
    if side is None:
        return out
    claimed: list = []  # (elements, not id(): lxml proxies are not stable)
    for cap in rules_figures.captions(ms):
        if cap.kind != rules_figures.TABLE:
            continue
        p = ms.by_id(cap.para_id)
        if p.in_table or p.element.getparent() is not body or has_picture(p.element):
            continue
        here = side
        tbl = _caption_table(p.element, side)
        if tbl is None or any(t is tbl for t in claimed):  # this caption sits on the other side of its table
            here = "below" if side == "above" else "above"
            tbl = _caption_table(p.element, here)
            if tbl is not None and _captioned(tbl, side):
                tbl = None
        if tbl is None or any(t is tbl for t in claimed):
            continue
        claimed.append(tbl)
        prov = provenance.make("journal_style", "Journal practice", "Table caption in a merged first row of the table "
                               "(the editors' form; the sheet is silent)", rule=MOVE_TABLE_CAPTION)
        out.append(BlockMove(id=f"tcap{len(out) + 1}", anchor=p.id, where="before",
                             runs=[(f"[Table {cap.number} caption moved into the table's first row]", {})],
                             role="move", reason=MOVE_TABLE_CAPTION, provenance=prov, layer=Layer.TABLES_FIGURES,
                             kind="table_caption", blocks=[[p.id]], side=here))
    return out


def _side(ms: Manuscript) -> str | None:
    """Whether the manuscript's table captions sit above or below their tables, read from its first table that has a
    caption next to it (None: no table caption next to a table)."""
    body = ms.document.element.body
    for tbl in body.iterchildren(qn("w:tbl")):
        for side, step in (("above", "getprevious"), ("below", "getnext")):
            el = getattr(tbl, step)()
            while el is not None and _empty(el):
                el = getattr(el, step)()
            if el is not None and el.tag == qn("w:p") and _CAPTION_START.match(paragraph_text(el)):
                return side
    return None


_CAPTION_START = re.compile(r"\s*tables?\s*[\dIVX]{1,4}(?![\w])", re.I)


def _empty(el) -> bool:
    """A blank paragraph (no text, no picture, text box or object)."""
    return el.tag == qn("w:p") and not paragraph_text(el).strip() and not has_picture(el) and \
        el.find(f".//{qn('w:drawing')}") is None and el.find(f".//{qn('w:pict')}") is None and \
        el.find(f".//{qn('w:object')}") is None


def _captioned(tbl, side: str) -> bool:
    """Whether a table has a caption paragraph on that side ("above" | "below")."""
    step = "getprevious" if side == "above" else "getnext"
    el = getattr(tbl, step)()
    while el is not None and _empty(el):
        el = getattr(el, step)()
    return el is not None and el.tag == qn("w:p") and bool(_CAPTION_START.match(paragraph_text(el)))


def _caption_table(p_el, side: str):
    """The table a caption paragraph belongs to: the table right below it (captions above their tables), or right
    above it (captions below), blank lines aside."""
    step = "getnext" if side == "above" else "getprevious"
    el = getattr(p_el, step)()
    while el is not None and _empty(el):
        el = getattr(el, step)()
    return el if el is not None and el.tag == qn("w:tbl") else None


# --- rendering -------------------------------------------------------------------------------

def render(ms: Manuscript, inserts, statuses, *, tracked: bool, author: str, date: str) -> list[str]:
    """Carry out the moves whose status is in `statuses` (call after tracked.render and the inserts). Returns notes."""
    from .header import _Marks

    notes: list[str] = []
    mk = _Marks(ms, author, date, tracked)
    for n in inserts:
        if not is_move(n) or n.status not in statuses:
            continue
        try:
            if n.kind == "legends":
                notes += _move_legends(ms, n, mk)
            elif n.kind == "table_caption":
                notes += _caption_row(ms, n, mk)
        except KeyError:
            notes.append(f"{n.text}: not done (a paragraph is gone)")
    return notes


def _top(el, body):
    while el is not None and el.getparent() is not body:
        el = el.getparent()
    return el


def _is_deleted(el) -> bool:
    return el.tag == qn("w:p") and el.find(f"{qn('w:pPr')}/{qn('w:rPr')}/{qn('w:del')}") is not None


def _is_inserted(el) -> bool:
    return el.tag == qn("w:p") and el.find(f"{qn('w:pPr')}/{qn('w:rPr')}/{qn('w:ins')}") is not None


def _detach(els: list, mk) -> list:
    """The elements to place at the new position; in the tracked copy the originals stay, deleted, and copies with
    the edits accepted go in as insertions (a paragraph that is itself an insertion just moves)."""
    from .header import _accept, _mark_block, _reject

    if not mk.tracked:
        return els
    out = []
    for e in els:
        if _is_inserted(e):
            out.append(e)
            continue
        c = copy.deepcopy(e)
        _accept(c)
        _mark_block(c, mk, "w:ins")
        _reject(e)
        _mark_block(e, mk, "w:del")
        out.append(c)
    return out


def _dest(ms: Manuscript, refs_end_id: str):
    """The element the legends go before: the first table or table caption after the reference list; None: the end."""
    body = ms.document.element.body
    el = _top(ms.by_id(refs_end_id).element, body)
    nxt = el.getnext()
    while nxt is not None:
        if nxt.tag == qn("w:tbl"):
            return nxt
        if nxt.tag == qn("w:p") and (_CAPTION_START.match(paragraph_text(nxt)) or
                                     _TABLES_HEADING.match(paragraph_text(nxt))):
            return nxt
        if nxt.tag == qn("w:sectPr"):
            return nxt
        nxt = nxt.getnext()
    return None


def _picture_above(el) -> list:
    """Picture-only paragraphs right above a legend (blank lines between), top-down."""
    out, prev, steps = [], el.getprevious(), 0
    while prev is not None and prev.tag == qn("w:p") and steps < 3:
        if has_picture(prev) and not paragraph_text(prev).strip():
            out.insert(0, prev)
        elif paragraph_text(prev).strip():
            break
        prev = prev.getprevious()
        steps += 1
    return out


def _move_legends(ms: Manuscript, n: BlockMove, mk) -> list[str]:
    body = ms.document.element.body
    groups = []
    for block in n.blocks:
        els = []
        for pid in block:
            el = ms.by_id(pid).element
            if el.getparent() is not body:
                return [f"{n.text}: not done (a legend is not a paragraph of its own)"]
            els += _picture_above(el) + [el]
        groups.append(els)
    dest = _dest(ms, n.refs_end)
    place = []
    if n.heading:
        place.append(_heading(n.heading, mk))
    seen = set()
    for els in sorted(groups, key=lambda g: list(body).index(g[-1])):
        for e in els:
            if id(e) not in seen:
                seen.add(id(e))
                place.append(e)
    if any(_is_deleted(e) for e in place):
        return [f"{n.text}: not done (the legends were moved with a renumbering)"]
    moved = _detach(place, mk)
    for e in moved:
        if dest is not None:
            dest.addprevious(e)
        else:
            body.append(e)
    return []


def _heading(text: str, mk):
    p = OxmlElement("w:p")
    ppr = OxmlElement("w:pPr")
    if mk.tracked:
        rpr = OxmlElement("w:rPr")
        rpr.append(mk.mark("w:ins"))
        ppr.append(rpr)
    p.append(ppr)
    r = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    rpr.append(OxmlElement("w:b"))
    r.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    r.append(t)
    if mk.tracked:
        w = mk.mark("w:ins")
        w.append(r)
        p.append(w)
    else:
        p.append(r)
    return p


def _grid_columns(tbl) -> int:
    grid = tbl.find(qn("w:tblGrid"))
    n = len(grid.findall(qn("w:gridCol"))) if grid is not None else 0
    if n:
        return n
    tr = tbl.find(qn("w:tr"))
    total = 0
    for tc in (tr.findall(qn("w:tc")) if tr is not None else []):
        span = tc.find(f"{qn('w:tcPr')}/{qn('w:gridSpan')}")
        total += int(span.get(qn("w:val"))) if span is not None else 1
    return max(total, 1)


def _caption_row(ms: Manuscript, n: BlockMove, mk) -> list[str]:
    body = ms.document.element.body
    p_el = ms.by_id(n.blocks[0][0]).element
    if p_el.getparent() is not body:
        return [f"{n.text}: not done (the caption is not a paragraph of its own)"]
    if _is_deleted(p_el):
        return [f"{n.text}: not done (the caption was moved with its table)"]
    tbl = _caption_table(p_el, n.side or "above")
    if tbl is None:
        return [f"{n.text}: not done (no table next to the caption)"]
    cols = _grid_columns(tbl)
    tr = OxmlElement("w:tr")
    trpr = OxmlElement("w:trPr")
    if mk.tracked:
        trpr.append(mk.mark("w:ins"))
    tr.append(trpr)
    tc = OxmlElement("w:tc")
    tcpr = OxmlElement("w:tcPr")
    if cols > 1:
        span = OxmlElement("w:gridSpan")
        span.set(qn("w:val"), str(cols))
        tcpr.append(span)
    tc.append(tcpr)
    tr.append(tc)
    (moved,) = _detach([p_el], mk)
    tc.append(moved)
    first = tbl.find(qn("w:tr"))
    if first is not None:
        first.addprevious(tr)
    else:
        tbl.append(tr)
    return []
