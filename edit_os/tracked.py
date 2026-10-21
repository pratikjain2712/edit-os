"""Render change records into the .docx as Word tracked changes, or as a clean copy.

Formatting is kept by splitting the existing runs at change boundaries: deleted
text stays in its original run (wrapped in <w:del>), and inserted text copies the
formatting of the run it replaces or follows.

A paragraph break in inserted text (`PARAGRAPH_BREAK`, U+2029) starts a new paragraph there: the
text after it, and the rest of the original paragraph, move into new <w:p> elements. In tracked
mode the new paragraph marks are tracked insertions, so rejecting them joins the text back.

Formatting changes (`Change.format`) are applied first, while offsets still match the
original text: each run in the span gets the new run properties and, in tracked mode, a
<w:rPrChange> holding its previous properties. Text changes are applied afterwards.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from docx.oxml import OxmlElement
from lxml import etree
from docx.oxml.ns import qn

from .changes import Change, ChangeSet
from .docmodel import Manuscript, explode_runs, paragraph_text, run_text, text_runs


PARAGRAPH_BREAK = "\u2029"  # in Change.new: a new paragraph starts here


class ChangeConflict(ValueError):
    """A change no longer matches the text it was made against, or overlaps another."""


class _Ids:
    def __init__(self, start: int = 9000):
        self.n = start

    def next(self) -> str:
        self.n += 1
        return str(self.n)


def _split_run(run, offset: int):
    """Split a single-w:t run at `offset`; returns the right-hand run."""
    t = run.find(qn("w:t"))
    text = t.text or ""
    right = copy.deepcopy(run)
    t.text = text[:offset]
    right.find(qn("w:t")).text = text[offset:]
    for el in (t, right.find(qn("w:t"))):
        el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    run.addnext(right)
    return right


def _spans(p_el):
    """(run, start, end) for each text run of an exploded paragraph."""
    out, pos = [], 0
    for r in text_runs(p_el):
        n = len(run_text(r))
        out.append((r, pos, pos + n))
        pos += n
    return out


def _cut(p_el, offset: int) -> None:
    """Make sure a run boundary falls exactly at `offset`."""
    for r, s, e in _spans(p_el):
        if s < offset < e:
            if r.find(qn("w:t")) is None:
                raise ChangeConflict(f"cannot split non-text content at offset {offset}")
            _split_run(r, offset - s)
            return


# Child order inside <w:rPr> (CT_RPr); Word rejects out-of-order properties.
_RPR_ORDER = [qn(f"w:{t}") for t in (
    "rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint noProof "
    "snapToGrid vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd "
    "fitText vertAlign rtl cs em lang eastAsianLayout specVanish oMath rPrChange").split()]
_TOGGLES = {"italic": "w:i", "bold": "w:b", "small_caps": "w:smallCaps"}
_VERT = {"superscript": "superscript", "subscript": "subscript"}


def _rpr_insert(rpr, el) -> None:
    rank = _RPR_ORDER.index(el.tag)
    for child in rpr:
        if child.tag in _RPR_ORDER and _RPR_ORDER.index(child.tag) > rank:
            child.addprevious(el)
            return
    rpr.append(el)


def apply_format(rpr, fmt: dict) -> None:
    """Set (True) or clear (False) run properties on a <w:rPr>, keeping schema order."""
    for prop, on in fmt.items():
        if prop in _TOGGLES:
            tag = qn(_TOGGLES[prop])
            for el in rpr.findall(tag):
                rpr.remove(el)
            if on:
                _rpr_insert(rpr, OxmlElement(_TOGGLES[prop]))
        elif prop in _VERT:
            va = rpr.find(qn("w:vertAlign"))
            if on:
                if va is None:
                    va = OxmlElement("w:vertAlign")
                    _rpr_insert(rpr, va)
                va.set(qn("w:val"), _VERT[prop])
            elif va is not None and va.get(qn("w:val")) == _VERT[prop]:
                rpr.remove(va)
        else:
            raise ValueError(f"unknown format property {prop!r}")


def _rpr_of(run):
    rpr = run.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        run.insert(0, rpr)
    return rpr


def _format_run(run, fmt: dict, tracked: bool, ids: "_Ids", author: str, date: str) -> None:
    rpr = _rpr_of(run)
    before = copy.deepcopy(rpr)
    for el in before.findall(qn("w:rPrChange")):
        before.remove(el)
    apply_format(rpr, fmt)
    after = copy.deepcopy(rpr)
    for el in after.findall(qn("w:rPrChange")):
        after.remove(el)
    if etree.tostring(after) == etree.tostring(before):
        if not len(rpr):
            run.remove(rpr)
        return
    if tracked and rpr.find(qn("w:rPrChange")) is None:  # an earlier revision keeps its baseline
        ch = OxmlElement("w:rPrChange")
        ch.set(qn("w:id"), ids.next())
        ch.set(qn("w:author"), author)
        ch.set(qn("w:date"), date)
        ch.append(before)
        rpr.append(ch)


def _new_run(template, text: str, fmt: dict | None = None):
    r = OxmlElement("w:r")
    if template is not None and template.find(qn("w:rPr")) is not None:
        rpr = copy.deepcopy(template.find(qn("w:rPr")))
        for el in rpr.findall(qn("w:rPrChange")):  # inserted text carries no format revision
            rpr.remove(el)
        r.append(rpr)
    if fmt:
        apply_format(_rpr_of(r), fmt)
    t = OxmlElement("w:t")
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    r.append(t)
    return r


def _wrap(tag: str, run, ids: _Ids, author: str, date: str):
    w = OxmlElement(tag)
    w.set(qn("w:id"), ids.next())
    w.set(qn("w:author"), author)
    w.set(qn("w:date"), date)
    run.addprevious(w)
    w.append(run)
    return w


def _to_deleted(run) -> None:
    for t in run.findall(qn("w:t")):
        t.tag = qn("w:delText")


def _apply_paragraph(p_el, changes: list[Change], tracked: bool, ids: _Ids, date: str) -> None:
    text = paragraph_text(p_el)
    formats = [c for c in changes if c.format_only]
    changes = [c for c in changes if not c.format_only]
    # Text changes must not overlap each other, nor formatting changes each other; a
    # formatting change may cover text that a text change replaces.
    for group in (changes, formats):
        last_end = -1
        for c in group:
            if text[c.start:c.end] != c.old:
                raise ChangeConflict(
                    f"{c.id}: expected {c.old!r} at {c.start}-{c.end} in {c.para_id}, found {text[c.start:c.end]!r}")
            if c.start < last_end:
                raise ChangeConflict(f"{c.id}: overlaps an earlier change in {c.para_id}")
            last_end = c.end

    explode_runs(p_el)
    for c in formats + changes:
        _cut(p_el, c.start)
        _cut(p_el, c.end)

    # Formatting first: it does not move any offsets.
    for c in formats:
        for r, s, e in _spans(p_el):
            if s >= c.start and e <= c.end and e > s:
                _format_run(r, c.format, tracked, ids, c.author, date)

    # Work right to left so earlier offsets stay valid; at a shared start, the
    # replacement goes before a pure insertion so the insertion lands in front of it.
    for c in sorted(changes, key=lambda c: (c.start, c.end), reverse=True):
        spans = _spans(p_el)
        doomed = [r for r, s, e in spans if s >= c.start and e <= c.end and e > s]
        before = [r for r, s, e in spans if e <= c.start]
        after = [r for r, s, e in spans if s >= c.end]
        template = doomed[0] if doomed else (before[-1] if before else (after[0] if after else None))

        anchor = None  # element the inserted run goes after
        for r in doomed:
            if tracked:
                _to_deleted(r)
                anchor = _wrap("w:del", r, ids, c.author, date)
            else:
                anchor = r.getprevious()
                r.getparent().remove(r)

        if not c.new:
            continue
        first, *rest = c.new.split(PARAGRAPH_BREAK)
        ins = _new_run(template, first, c.format)
        if doomed and tracked:
            anchor.addnext(ins)
        elif after:
            after[0].addprevious(ins)
        elif before:
            before[-1].addnext(ins)
        else:
            p_el.append(ins)
        if tracked:
            _wrap("w:ins", ins, ids, c.author, date)
        if rest:
            _break_paragraph(p_el, ins, after, rest, template, c, tracked, ids, date)
        if not first:  # only there to mark the position of the break
            top = ins.getparent() if tracked else ins
            top.getparent().remove(top)


_PPR = qn("w:pPr")
# Child order inside <w:pPr> (CT_PPr); the paragraph mark's <w:rPr> goes before sectPr / pPrChange.
_PPR_TAIL = (qn("w:rPr"), qn("w:sectPr"), qn("w:pPrChange"))


def _top(p_el, el):
    while el.getparent() is not p_el:
        el = el.getparent()
    return el


def _mark_inserted(p_el, ids: _Ids, author: str, date: str) -> None:
    """Record the paragraph mark of `p_el` as a tracked insertion."""
    ppr = p_el.find(_PPR)
    if ppr is None:
        ppr = OxmlElement("w:pPr")
        p_el.insert(0, ppr)
    rpr = ppr.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        tail = [c for c in ppr if c.tag in _PPR_TAIL[1:]]
        if tail:
            tail[0].addprevious(rpr)
        else:
            ppr.append(rpr)
    if rpr.find(qn("w:ins")) is not None:  # already a new paragraph
        return
    mark = OxmlElement("w:ins")
    mark.set(qn("w:id"), ids.next())
    mark.set(qn("w:author"), author)
    mark.set(qn("w:date"), date)
    rpr.insert(0, mark)


def _break_paragraph(p_el, ins, after, segments, template, c: Change, tracked, ids, date) -> None:
    """Split `p_el` after the inserted run `ins`: each segment becomes a new paragraph, and
    whatever followed the insertion point moves into the last one."""
    tail = []
    if after:
        start = _top(p_el, after[0])
        if start is _top(p_el, ins):
            raise ChangeConflict(f"{c.id}: cannot start a paragraph inside a hyperlink or field")
        tail = [start] + [s for s in start.itersiblings()]
    ppr = p_el.find(_PPR)
    new_paras = []
    for i, seg in enumerate(segments):
        p = OxmlElement("w:p")
        last = i == len(segments) - 1
        if ppr is not None:
            pp = copy.deepcopy(ppr)
            for tag in ("w:pPrChange",) + (() if last else ("w:sectPr",)):
                for el in pp.findall(qn(tag)):
                    pp.remove(el)
            if not (last and tail):  # new text is not a list item, whatever the paragraph was
                for el in pp.findall(qn("w:numPr")):
                    pp.remove(el)
            p.append(pp)
        if seg:
            run = _new_run(template, seg, c.format)
            p.append(run)
            if tracked:
                _wrap("w:ins", run, ids, c.author, date)
        if last:
            for el in tail:
                p.append(el)
        new_paras.append(p)
    # The original paragraph mark now ends the last part: the first part's mark, and every
    # mark but the last, are new.
    if ppr is not None:
        for el in ppr.findall(qn("w:sectPr")):
            ppr.remove(el)
    if tracked:
        _mark_inserted(p_el, ids, c.author, date)
        for p in new_paras[:-1]:
            _mark_inserted(p, ids, c.author, date)
    anchor = p_el
    for p in new_paras:
        anchor.addnext(p)
        anchor = p


def render(ms: Manuscript, changes: ChangeSet, tracked: bool = True, statuses=None) -> Manuscript:
    """Apply changes to the manuscript in place. Use a freshly loaded Manuscript per output."""
    from .changes import Status
    statuses = statuses or (Status.ACCEPTED,)
    ids = _Ids()
    date = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for p in ms.paragraphs:
        cs = changes.for_paragraph(p.id, statuses)
        if cs:
            _apply_paragraph(p.element, cs, tracked, ids, date)
    return ms
