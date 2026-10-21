"""What the review screen needs to show the manuscript as a page, and the editor's own edits.

    document(docx_path)        paragraphs in order: kind (heading, front matter, list, table cell,
                               caption, reference, body), runs with basic formatting
    plan_edit(...)             turn the editor's rewrite of one paragraph into word-level Change records

Direct edits never touch the manuscript: they are `Change` records (source editor, status accepted)
anchored with the exact source text they replace, rendered by `tracked.render` like any other.
They may not overlap an accepted suggestion; pending suggestions they overlap are set aside.
"""

from __future__ import annotations

import difflib
import re
import uuid
from dataclasses import dataclass

from docx.oxml.ns import qn

from . import docmodel, references
from .changes import Change, Layer, Source, Status

DIRECT_EDIT = "editor_edit"  # Change.reason of the editor's own edits

_SECTION = re.compile(
    r"^\s*(?:<H\d>)?\s*(abstract|summary|key\s*words?|keywords|introduction|background|methods?|"
    r"materials? and methods|patients and methods|case (?:reports?|presentation|description|history)|"
    r"results|discussion|conclusions?|references|bibliography|acknowledge?ments?|figure legends?|"
    r"legends?|funding|financial support|conflicts? of interest|declaration of interest|"
    r"author contributions?|ethics?(?: statement)?|data availability)\b[^.]{0,60}$", re.I)
_FRONT_END = re.compile(r"^\s*(?:<H\d>)?\s*(abstract|summary|introduction|background|key\s*words?|keywords)\b", re.I)
_TAG = re.compile(r"^\s*<H(\d)>")
_CAPTION = re.compile(r"^\s*(fig(?:ure)?s?\.?|table|supplementary (?:figure|table))\s*\d+", re.I)
_CAPTION_PUNCT = re.compile(r"^\s*(fig(?:ure)?s?\.?|table)\s*\d+[A-Za-z]?\s*[.:|\u2013\u2014-]", re.I)
_HEADING_STYLE = re.compile(r"^heading\s*(\d)$", re.I)


def _on(rpr, tag: str) -> bool:
    el = rpr.find(qn(tag)) if rpr is not None else None
    return el is not None and el.get(qn("w:val")) not in ("0", "false", "off", "none")


def runs(p_el) -> list[dict]:
    """Text runs with bold / italic / superscript / subscript, adjacent equal runs merged.
    Their texts join to exactly the paragraph's text view."""
    out: list[dict] = []
    for r in docmodel.text_runs(p_el):
        t = docmodel.run_text(r)
        if not t:
            continue
        rpr = r.find(qn("w:rPr"))
        va = rpr.find(qn("w:vertAlign")) if rpr is not None else None
        va = va.get(qn("w:val")) if va is not None else ""
        fmt = {k: True for k, on in (("bold", _on(rpr, "w:b")), ("italic", _on(rpr, "w:i")),
                                      ("superscript", va == "superscript"), ("subscript", va == "subscript"),
                                      ("small_caps", _on(rpr, "w:smallCaps"))) if on}
        if out and out[-1]["format"] == fmt:
            out[-1]["text"] += t
        else:
            out.append({"text": t, "format": fmt})
    return out


def _cell(p_el, tables: dict) -> dict | None:
    tc = next((a for a in p_el.iterancestors() if a.tag == qn("w:tc")), None)
    if tc is None:
        return None
    tr = tc.getparent()
    tbl = tr.getparent()
    while tbl is not None and tbl.tag != qn("w:tbl"):
        tbl = tbl.getparent()
    if tbl not in tables:  # (the dict keeps the element proxies alive, so identity holds)
        tables[tbl] = f"t{len(tables) + 1}"
    rows = [c for c in tbl if c.tag == qn("w:tr")]
    cells = [c for c in tr if c.tag == qn("w:tc")]
    return {"table": tables[tbl], "row": rows.index(tr) if tr in rows else 0,
            "col": cells.index(tc) if tc in cells else 0}


def document(docx_path: str) -> dict:
    return {"paragraphs": classify(docmodel.load(docx_path))}


def classify(ms: docmodel.Manuscript) -> list[dict]:
    """Each paragraph of a loaded manuscript with its kind, level, table cell and runs."""
    refs = {r.para_id for r in references.extract(ms)}
    ref_heading = references.find_heading(ms)
    # Front matter: what comes before the abstract / introduction (if the manuscript has one).
    front_end = next((i for i, p in enumerate(ms.paragraphs[:80]) if not p.in_table and _FRONT_END.match(p.text)), 0)
    tables: dict = {t: f"t{i}" for i, t in enumerate(ms.document.element.body.iter(qn("w:tbl")), 1)}
    out = []
    for i, p in enumerate(ms.paragraphs):
        rs = runs(p.element)
        text = p.text
        d = {"id": p.id, "style": p.style, "text": text, "runs": rs, "kind": "body", "level": None, "cell": None}
        bold = bool(rs) and all(r["format"].get("bold") for r in rs if r["text"].strip())
        sm = _HEADING_STYLE.match(p.style or "")
        tag = _TAG.match(text)
        cell = _cell(p.element, tables) if p.in_table else None
        if cell:
            d["kind"], d["cell"] = "table", cell
        elif not text.strip():
            d["kind"] = "empty"
        elif p.id in refs:
            d["kind"] = "reference"
        elif (p.style or "").lower() == "title":
            d["kind"], d["level"] = "heading", 0
        elif sm:
            d["kind"], d["level"] = "heading", int(sm.group(1))
        elif tag:
            d["kind"], d["level"] = "heading", int(tag.group(1))
        elif i == ref_heading or (_SECTION.match(text) and len(text) < 90):
            d["kind"], d["level"] = "heading", 1
        elif (p.style or "").lower() == "caption" or _CAPTION_PUNCT.match(text) or \
                (_CAPTION.match(text) and ref_heading is not None and i > ref_heading):
            d["kind"] = "caption"
        elif i < front_end:
            d["kind"] = "front"
        elif bold and len(text) < 120 and not text.rstrip().endswith("."):
            d["kind"], d["level"] = "heading", 2
        elif p.numbered:
            d["kind"] = "list"
        d["front"] = i < front_end
        out.append(d)
    return out


# --- the editor's own edits -------------------------------------------------------------------

class EditConflict(ValueError):
    """The edit cannot be expressed without touching an accepted suggestion, or the text is stale."""


class EditInvalid(ValueError):
    """The edit is not a plain-text change within one paragraph."""


@dataclass
class _Seg:
    src: tuple[int, int]  # span in the source text view
    cur: tuple[int, int]  # span in the text with the changes applied
    change: Change | None  # None: unchanged source text


def text_changes(changes) -> list[Change]:
    """Text changes (not formatting-only) in order, dropping any that overlap an earlier one,
    the same way the review screen applies them."""
    out, last_end = [], -1
    for c in sorted((c for c in changes if not c.format_only), key=lambda c: (c.start, c.end)):
        if c.start < last_end:
            continue
        out.append(c)
        last_end = c.end
    return out


def apply(text: str, changes) -> tuple[str, list[_Seg]]:
    """`text` with the changes applied, and how its spans map back to the source."""
    segs: list[_Seg] = []
    parts: list[str] = []
    pos = cur = 0

    def add(s, e, new, change):
        nonlocal cur
        segs.append(_Seg((s, e), (cur, cur + len(new)), change))
        parts.append(new)
        cur += len(new)

    for c in text_changes(changes):
        if text[c.start:c.end] != c.old:
            raise EditConflict(f"{c.id} no longer matches the paragraph text")
        if c.start > pos:
            add(pos, c.start, text[pos:c.start], None)
        add(c.start, c.end, c.new, c)
        pos = c.end
    if pos < len(text) or not segs:
        add(pos, len(text), text[pos:], None)
    return "".join(parts), segs


def overlaps(a: int, b: int, c: int, d: int) -> bool:
    """Whether span a..b and c..d collide the way `tracked.render` refuses (a pure insertion only
    collides with a span it falls strictly inside)."""
    if a == b and c == d:
        return False
    if a == b:
        return c < a < d
    if c == d:
        return a < c < b
    return a < d and c < b


def _ops(old: str, new: str, words: bool) -> list[tuple[int, int, str]]:
    tok = re.compile(r"\s+|\w+|[^\w\s]") if words else re.compile(r"(?s).")
    a, b = tok.findall(old), tok.findall(new)
    starts, pos = [], 0
    for t in a:
        starts.append(pos)
        pos += len(t)
    starts.append(pos)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            out.append((starts[i1], starts[i2], "".join(b[j1:j2])))
    return out


def _to_source(segs: list[_Seg], a: int, b: int) -> tuple[int, int] | None:
    """Map a span of the applied text to the source, or None when it touches an applied change."""
    plain = [s for s in segs if s.change is None]
    for s in plain:
        if s.cur[0] <= a and b <= s.cur[1]:
            return s.src[0] + a - s.cur[0], s.src[0] + b - s.cur[0]
    if a == b:  # an insertion right next to a change
        for s in segs:
            if s.cur[1] == a:
                return s.src[1], s.src[1]
            if s.cur[0] == a:
                return s.src[0], s.src[0]
    return None


def _blocking(segs: list[_Seg], a: int, b: int) -> Change | None:
    for s in segs:
        if s.change is not None and (overlaps(a, b, *s.cur) or (a < b and s.cur[0] == s.cur[1] and a < s.cur[0] < b)):
            return s.change
    return None


def _trim(old: str, a: int, b: int, new: str) -> tuple[int, int, str]:
    seg = old[a:b]
    p = 0
    while p < min(len(seg), len(new)) and seg[p] == new[p]:
        p += 1
    q = 0
    while q < min(len(seg), len(new)) - p and seg[len(seg) - 1 - q] == new[len(new) - 1 - q]:
        q += 1
    return a + p, b - q, new[p:len(new) - q]


@dataclass
class EditPlan:
    changes: list[Change]  # new direct-edit changes (accepted)
    replaced: list[Change]  # earlier direct edits of this paragraph they replace
    superseded: list[Change]  # pending suggestions the edit overlaps (to be set aside)


def plan_edit(para_id: str, source: str, all_changes, base: str, new_text: str, editor: str = "") -> EditPlan:
    """The editor rewrote paragraph `para_id` from `base` (its text with accepted changes applied,
    as the screen showed it) to `new_text`.

    Earlier direct edits of the paragraph are replaced by one fresh set computed against the text
    with the accepted suggestions applied, so editing the same paragraph twice works.
    """
    mine = [c for c in all_changes if c.para_id == para_id]
    accepted = [c for c in mine if c.status == Status.ACCEPTED]
    own = [c for c in accepted if c.reason == DIRECT_EDIT]
    others = [c for c in accepted if c.reason != DIRECT_EDIT]
    current, _ = apply(source, accepted)
    if base != current:
        raise EditConflict("This paragraph changed since you started editing it; reload and try again.")
    if " " in new_text or "\r" in new_text:
        raise EditInvalid("Edits stay within one paragraph (no paragraph breaks).")
    applied, segs = apply(source, others)

    out: list[Change] = []
    for a, b, rep in _ops(applied, new_text, words=True):
        if any(ch in rep for ch in "\n\t") and rep.count("\n") + rep.count("\t") > \
                applied[a:b].count("\n") + applied[a:b].count("\t"):
            raise EditInvalid("Line breaks and tabs cannot be added here; edit the text only.")
        span = _to_source(segs, a, b)
        if span is None:  # a word-level op that brushes an accepted change: try the minimal characters
            a2, b2, rep2 = _trim(applied, a, b, rep)
            span = _to_source(segs, a2, b2)
            if span is not None:
                a, b, rep = a2, b2, rep2
        if span is None:
            hit = _blocking(segs, a, b) or _blocking(segs, *_trim(applied, a, b, rep)[:2])
            what = f"“{hit.old}” → “{hit.new}”" if hit else "an accepted suggestion"
            raise EditConflict(f"Your edit touches the accepted suggestion {what}. Undo that suggestion or "
                               "change its wording from the suggestion itself.")
        s, e = span
        out.append(Change(id=f"ed{uuid.uuid4().hex[:10]}", para_id=para_id, start=s, end=e, old=source[s:e],
                          new=rep, layer=Layer.LANGUAGE, source=Source.EDITOR, reason=DIRECT_EDIT,
                          status=Status.ACCEPTED, author=editor or "Editor"))
    for c in out:  # belt and braces: never collide with an accepted suggestion
        for o in text_changes(others):
            if overlaps(c.start, c.end, o.start, o.end):
                raise EditConflict(f"Your edit overlaps the accepted suggestion “{o.old}” → “{o.new}”.")
    superseded = [p for p in mine if p.status == Status.SUGGESTED and not p.format_only and
                  any(overlaps(c.start, c.end, p.start, p.end) or (c.start == c.end == p.start == p.end) for c in out)]
    return EditPlan(out, own, superseded)
