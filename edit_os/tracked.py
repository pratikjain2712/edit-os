"""Render change records into the .docx as Word tracked changes, or as a clean copy.

Formatting is kept by splitting the existing runs at change boundaries: deleted
text stays in its original run (wrapped in <w:del>), and inserted text copies the
formatting of the run it replaces or follows.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from .changes import Change, ChangeSet
from .docmodel import Manuscript, explode_runs, paragraph_text, run_text, text_runs


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


def _new_run(template, text: str):
    r = OxmlElement("w:r")
    if template is not None and template.find(qn("w:rPr")) is not None:
        r.append(copy.deepcopy(template.find(qn("w:rPr"))))
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
    last_end = -1
    for c in changes:
        if text[c.start:c.end] != c.old:
            raise ChangeConflict(
                f"{c.id}: expected {c.old!r} at {c.start}-{c.end} in {c.para_id}, found {text[c.start:c.end]!r}")
        if c.start < last_end:
            raise ChangeConflict(f"{c.id}: overlaps an earlier change in {c.para_id}")
        last_end = c.end

    explode_runs(p_el)
    for c in changes:
        _cut(p_el, c.start)
        _cut(p_el, c.end)

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
        ins = _new_run(template, c.new)
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
