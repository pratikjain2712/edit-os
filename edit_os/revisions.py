"""The author's own tracked changes and comments in the manuscript as received.

Authors often send the revised manuscript with their revisions still tracked ("Manuscript-track changes.docx").
Editors accept those revisions before editing (IJO_1130: the editor's file holds none of the author's w:ins /
w:del), so the source is normalised first: `accept_revisions` rewrites the .docx with every pending revision
accepted (insertions kept, deletions removed, formatting changes applied). It runs before `docmodel.load`, so
every paragraph id and offset the pipeline produces refers to the accepted text.

Comments in the manuscript as received (the editorial office's "Provided" notes, the author's remarks) stay in the
tracked copy, as editors leave them there; `strip_comments` removes them from the clean copy, which must carry no
comment of any kind.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from collections import Counter

from docx.oxml.ns import qn
from lxml import etree

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_UNWRAP = {_W + "ins", _W + "moveTo"}  # inline revision wrappers whose content is kept
_DROP = {_W + "del", _W + "moveFrom"}  # inline revision wrappers whose content goes
_PROP_CHANGES = {_W + t for t in ("rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "trPrChange",
                                  "tcPrChange", "tblGridChange", "numberingChange", "tblPrExChange")}
_RANGES = {_W + t for t in ("moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd")}
_COUNTED = {"ins": "insertions", "moveTo": "insertions", "del": "deletions", "moveFrom": "deletions"}


def _accept(root) -> Counter:
    """Accept every revision under ROOT in place; counts by kind (insertions, deletions, formatting)."""
    n: Counter = Counter()
    # Paragraph marks: <w:pPr><w:rPr><w:ins/></w:rPr> is an inserted mark (keep the paragraph, drop the marker);
    # <w:del/> there is a deleted mark: the paragraph joins the next one (its text goes to the start of that one,
    # which keeps its own properties, as in Word).
    for rpr in list(root.iter(_W + "rPr")):
        ppr = rpr.getparent()
        if ppr is None or ppr.tag != _W + "pPr":
            continue
        for mark in rpr.findall(_W + "ins") + rpr.findall(_W + "moveTo"):
            rpr.remove(mark)
            n["insertions"] += 1
        for mark in rpr.findall(_W + "del") + rpr.findall(_W + "moveFrom"):
            rpr.remove(mark)
            p = ppr.getparent()
            nxt = p.getnext() if p is not None else None
            if p is None or nxt is None or nxt.tag != _W + "p":
                continue  # the last paragraph of its container: the mark stays
            content = [c for c in p if c.tag != _W + "pPr"]
            at = 1 if nxt.find(_W + "pPr") is not None else 0
            for k, c in enumerate(content):
                nxt.insert(at + k, c)
            p.getparent().remove(p)
            n["deletions"] += 1
    # table rows inserted / deleted
    for trpr in list(root.iter(_W + "trPr")):
        for mark in trpr.findall(_W + "ins"):
            trpr.remove(mark)
            n["insertions"] += 1
        if trpr.find(_W + "del") is not None:
            tr = trpr.getparent()
            if tr is not None and tr.getparent() is not None:
                tr.getparent().remove(tr)
                n["deletions"] += 1
    for el in list(root.iter(*_DROP)):
        if el.getparent() is not None:
            el.getparent().remove(el)
            n[_COUNTED[etree.QName(el).localname]] += 1
    for el in list(root.iter(*_UNWRAP)):
        parent = el.getparent()
        if parent is None:
            continue
        i = parent.index(el)
        for k, c in enumerate(list(el)):
            parent.insert(i + k, c)
        parent.remove(el)
        n[_COUNTED[etree.QName(el).localname]] += 1
    for el in list(root.iter(*_PROP_CHANGES)):
        el.getparent().remove(el)
        n["formatting"] += 1
    for el in list(root.iter(*_RANGES)):
        el.getparent().remove(el)
    for tag in ("cellIns", "cellDel", "cellMerge"):
        for el in list(root.iter(_W + tag)):
            el.getparent().remove(el)
    return n


def _word_xml(name: str) -> bool:
    return name.startswith("word/") and name.endswith(".xml") and "/_rels/" not in name and \
        not name.startswith("word/media/")


def accept_revisions(path: str) -> dict:
    """Rewrite the .docx at PATH with every tracked revision accepted (document, headers, footers, foot- and
    endnotes, styles, numbering). Returns {"insertions": n, "deletions": n, "formatting": n}, all 0 (and the file
    untouched) when it had none."""
    total: Counter = Counter()
    new: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not _word_xml(name):
                continue
            data = z.read(name)
            if not any(m in data for m in (b"<w:ins ", b"<w:del ", b"<w:moveFrom ", b"<w:moveTo ", b"Change ",
                                           b"Change>", b"<w:ins>", b"<w:del>", b"<w:cellIns", b"<w:cellDel")):
                continue
            root = etree.fromstring(data)
            n = _accept(root)
            if sum(n.values()):
                total += n
                new[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        if new:
            fd, tmp = tempfile.mkstemp(suffix=".docx", dir=os.path.dirname(os.path.abspath(path)))
            os.close(fd)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
                for info in z.infolist():
                    out.writestr(info, new.get(info.filename, None) or z.read(info.filename))
    if new:
        shutil.move(tmp, path)
    return {k: total.get(k, 0) for k in ("insertions", "deletions", "formatting")}


def note(counts: dict) -> str:
    """The QC report / manifest note for what `accept_revisions` did ('' when nothing)."""
    if not sum(counts.values()):
        return ""
    return (f"The author's tracked changes in the manuscript were accepted before editing "
            f"({counts['insertions']} insertions, {counts['deletions']} deletions, "
            f"{counts['formatting']} formatting changes).")


_COMMENT_MARKS = (qn("w:commentRangeStart"), qn("w:commentRangeEnd"))
_COMMENT_RELS = ("/comments", "/commentsExtended", "/commentsIds", "/commentsExtensible")


def strip_comments(ms) -> int:
    """Remove every comment from a rendered manuscript (the clean copy): range marks, reference runs and the
    comments parts. Returns how many comment references were removed."""
    body = ms.document.element
    n = 0
    for el in list(body.iter(*_COMMENT_MARKS)):
        el.getparent().remove(el)
    for ref in list(body.iter(qn("w:commentReference"))):
        run = ref.getparent()
        n += 1
        if run is not None and run.tag == qn("w:r") and \
                all(c.tag in (qn("w:rPr"), qn("w:commentReference")) for c in run):
            run.getparent().remove(run)
        else:
            run.remove(ref)
    rels = ms.document.part.rels
    for rid, rel in list(rels.items()):
        if rel.reltype.endswith(_COMMENT_RELS):
            del rels[rid]
    return n
