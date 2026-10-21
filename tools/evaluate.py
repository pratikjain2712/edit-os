"""Score the style-edit rules against a style editor's real output.

Expects a data directory with one folder per article holding `original.zip` (containing the
manuscript .docx) and `te.docx` (the style editor's tracked file). Each manuscript paragraph
is paired with the same paragraph in the tracked file (whose pre-edit text matches it):

  O = original paragraph, T = the editor's version (tracked changes accepted),
  U = the paragraph with our rule changes applied.

closure = (d(O,T) - d(U,T)) / d(O,T): the share of the editor's edits we reproduced, net of
any edits we made that the editor did not (those push it down). d is a token edit distance.
Changed paragraphs are also counted as helped (closer to the editor) or hurt (further away).

Usage: python tools/evaluate.py DATA_DIR JOURNAL [--per-rule] [--show-hurt]
"""

from __future__ import annotations

import glob
import io
import os
import re
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lxml import etree  # noqa: E402
from rapidfuzz.distance import Levenshtein  # noqa: E402

from edit_os import docmodel, rules, style as style_mod, tracked  # noqa: E402
from edit_os.changes import Status  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def tracked_pairs(docx_bytes: bytes) -> list[tuple[str, str]]:
    """(text before the editor's changes, text after) for each paragraph of a tracked file."""
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(docx_bytes)).read("word/document.xml"))
    pairs = []
    for p in root.iter(W + "p"):
        before, after = [], []
        for el in p.iter(W + "t", W + "delText", W + "tab"):
            anc = {a.tag for a in el.iterancestors()}
            txt = "\t" if el.tag == W + "tab" else (el.text or "")
            if W + "ins" not in anc:
                before.append(txt)
            if W + "del" not in anc and el.tag != W + "delText":
                after.append(txt)
        pairs.append(("".join(before), "".join(after)))
    return pairs


def manuscript_from_zip(path: str) -> bytes | None:
    z = zipfile.ZipFile(path)
    docs = [n for n in z.namelist() if n.lower().endswith(".docx")]
    named = [n for n in docs if "manuscript" in os.path.basename(n).lower()]
    pick = named or sorted(docs, key=lambda n: -z.getinfo(n).file_size)
    return z.read(pick[0]) if pick else None


def tokens(s: str) -> list[str]:
    return [" " if t.isspace() else t for t in re.findall(r"\w+|\s+|[^\w\s]", s)]


def distance(a: str, b: str) -> int:
    return Levenshtein.distance(tokens(a), tokens(b))


def evaluate(article_dir: str, journal: str, only=None, show_hurt=False):
    src = manuscript_from_zip(os.path.join(article_dir, "original.zip"))
    if src is None:
        return None
    with open(os.path.join(article_dir, "te.docx"), "rb") as f:
        te = f.read()
    st = style_mod.load(journal)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "m.docx")
        with open(path, "wb") as f:
            f.write(src)
        ms = docmodel.load(path)
        originals = [p.text for p in ms.paragraphs]
        cs = rules.run_all(ms, st, only=only)
        for c in cs.changes:
            c.status = Status.ACCEPTED
        out = os.path.join(tmp, "u.docx")
        tracked.render(docmodel.load(path), cs, tracked=False).save(out)
        ours = [p.text for p in docmodel.load(out).paragraphs]

    editor: dict[str, list[str]] = {}
    for before, after in tracked_pairs(te):
        editor.setdefault(before.strip(), []).append(after)
    r = dict(changes=len(cs.changes), dOT=0, dUT=0, matched=0, paragraphs=len(originals), helped=0, hurt=0)
    for o, u in zip(originals, ours):
        queue = editor.get(o.strip())
        if not o.strip() or not queue:
            continue
        t = queue.pop(0)
        if not t.strip():
            continue  # the editor deleted the paragraph (often to retype it); eval_refs scores those
        r["matched"] += 1
        d_o, d_u = distance(o, t), distance(u, t)
        r["dOT"] += d_o
        r["dUT"] += d_u
        if u != o:
            if d_u < d_o:
                r["helped"] += 1
            elif d_u > d_o:
                r["hurt"] += 1
                if show_hurt:
                    print(f"  HURT\n    ours:   {u[:160]!r}\n    editor: {t[:160]!r}")
    return r


def main(argv):
    data, journal = argv[0], argv[1].lower()
    dirs = sorted(d for d in glob.glob(os.path.join(data, "*"))
                  if os.path.basename(d).lower().startswith(journal + "_"))
    runs = [("all", None)] + ([(r, [r]) for r in rules.RULES] if "--per-rule" in argv else [])
    for label, only in runs:
        tot = dict(dOT=0, dUT=0, changes=0, helped=0, hurt=0, matched=0, paragraphs=0)
        for d in dirs:
            r = evaluate(d, journal, only, show_hurt="--show-hurt" in argv and label == "all")
            if not r:
                continue
            for k in tot:
                tot[k] += r[k]
            if label == "all":
                print(f"{os.path.basename(d):20s} edits {r['changes']:4d}  distance {r['dOT']:5d} -> {r['dUT']:5d}"
                      f"  helped {r['helped']:3d} hurt {r['hurt']:3d}  paragraphs matched {r['matched']}/{r['paragraphs']}")
        closure = (tot["dOT"] - tot["dUT"]) / tot["dOT"] if tot["dOT"] else 0
        print(f"[{label:12s}] articles {len(dirs)}  edits {tot['changes']:4d}  closure {closure:6.1%}"
              f"  paragraphs helped {tot['helped']} hurt {tot['hurt']}"
              f"  (matched {tot['matched']}/{tot['paragraphs']})")


if __name__ == "__main__":
    main(sys.argv[1:])
