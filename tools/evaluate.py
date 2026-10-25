"""Score the style-edit rules against a style editor's real output.

Expects a data directory with one folder per article holding `original.zip` (containing the
manuscript .docx) and `te.docx` (the style editor's tracked file). Each manuscript paragraph
is paired with the same paragraph in the tracked file (whose pre-edit text matches it):

  O = original paragraph, T = the editor's version (tracked changes accepted),
  U = the paragraph with our rule changes applied.

closure = (d(O,T) - d(U,T)) / d(O,T): the share of the editor's edits we reproduced, net of
any edits we made that the editor did not (those push it down). d is a token edit distance.
Changed paragraphs are also counted as helped (closer to the editor) or hurt (further away).

With --join-paragraphs, a paragraph whose paragraph mark the editor deleted (a part legend run on into its figure's
legend, a line broken by hand joined) is scored as joined to the next one, as Word shows it once the changes are
accepted; by default each paragraph is scored on its own (the numbers reported so far).

Usage: python tools/evaluate.py DATA_DIR JOURNAL [--per-rule] [--show-hurt] [--join-paragraphs]
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

from edit_os import docmodel, frontmatter, references, rules, style as style_mod, tracked  # noqa: E402
from edit_os.changes import Status  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def tracked_pairs(docx_bytes: bytes, join: bool = False) -> list[tuple[str, str]]:
    """(text before the editor's changes, text after) for each paragraph of a tracked file. `join`: a paragraph whose
    mark the editor deleted takes the next paragraph's text after it (that one's own text after is then empty)."""
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(docx_bytes)).read("word/document.xml"))
    pairs = []
    carry = None
    for p in root.iter(W + "p"):
        before, after = [], []
        for el in p.iter(W + "t", W + "delText", W + "tab"):
            anc = {a.tag for a in el.iterancestors()}
            txt = "\t" if el.tag == W + "tab" else (el.text or "")
            if W + "ins" not in anc:
                before.append(txt)
            if W + "del" not in anc and el.tag != W + "delText":
                after.append(txt)
        b, a = "".join(before), "".join(after)
        target = len(pairs)
        if carry is not None:
            pairs[carry] = (pairs[carry][0], pairs[carry][1] + a)
            a, target = "", carry
        pairs.append((b, a))
        carry = None
        if join and p.find(f"{W}pPr/{W}rPr/{W}del") is not None and pairs[target][1].strip() \
                and not any(x.tag == W + "tbl" for x in p.iterancestors()):
            carry = target
    return pairs


def manuscript_from_zip(path: str) -> bytes | None:
    from edit_os import pipeline  # the file the tool edits

    z = zipfile.ZipFile(path)
    pick = pipeline.manuscript_member(z)
    return z.read(pick) if pick else None


def tokens(s: str) -> list[str]:
    return [" " if t.isspace() else t for t in re.findall(r"\w+|\s+|[^\w\s]", s)]


def distance(a: str, b: str) -> int:
    return Levenshtein.distance(tokens(a), tokens(b))


def apply_text(text: str, changes) -> str:
    """The paragraph's text with its changes applied. When a change starts new paragraphs
    (tracked.PARAGRAPH_BREAK), the part holding most of the original text is this paragraph."""
    chars = [(ch, True) for ch in text]
    for c in sorted((c for c in changes if not c.format_only), key=lambda c: (c.start, c.end), reverse=True):
        chars[c.start:c.end] = [(ch, False) for ch in c.new]
    parts, cur = [], []
    for ch, orig in chars:
        if ch == tracked.PARAGRAPH_BREAK and not orig:
            parts.append(cur)
            cur = []
        else:
            cur.append((ch, orig))
    parts.append(cur)
    best = max(parts, key=lambda part: sum(orig for _, orig in part))
    return "".join(ch for ch, _ in best)


def moved_back(originals: list[str], ours: list[str], cs) -> list[str]:
    """A paragraph our changes delete and insert elsewhere (a section moved to its place: the consent statement
    before the financial support, the key words under the abstract) is scored as its inserted copy: pairing by
    position cannot see a move, the editor's own moves included."""
    lines = [ln for c in cs.changes if not c.format_only for ln in c.new.split(tracked.PARAGRAPH_BREAK)
             if len(ln.strip()) >= 20]
    out = list(ours)
    for i, (o, u) in enumerate(zip(originals, ours)):
        if u.strip() or len(o.strip()) < 20 or not lines:
            continue
        best = min(lines, key=lambda ln: distance(o, ln))
        if distance(o, best) <= len(tokens(o)) // 3:
            out[i] = best
    return out


def evaluate(article_dir: str, journal: str, only=None, show_hurt=False, join=False):
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
        ms.front_sources = frontmatter.sources_from_zip(os.path.join(article_dir, "original.zip"))
        originals = [p.text for p in ms.paragraphs]
        # Reference entries are scored by eval_refs.py, which pairs them by number: editors often
        # move or retype them, so pairing paragraphs by their original text would mislead.
        ref_ids = {x.para_id for x in references.extract(ms)}
        is_ref = [p.id in ref_ids for p in ms.paragraphs]
        cs = rules.run_all(ms, st, only=only)
        for c in cs.changes:
            c.status = Status.ACCEPTED
        tracked.render(docmodel.load(path), cs, tracked=False)  # refuses stale or overlapping changes
        ours = [apply_text(p.text, cs.for_paragraph(p.id)) for p in ms.paragraphs]
        ours = moved_back(originals, ours, cs)

    editor: dict[str, list[str]] = {}
    for before, after in tracked_pairs(te, join):
        editor.setdefault(before.strip(), []).append(after)
    r = dict(changes=len(cs.changes), dOT=0, dUT=0, matched=0, paragraphs=len(originals), helped=0, hurt=0)
    for o, u, ref in zip(originals, ours, is_ref):
        queue = editor.get(o.strip())
        if not o.strip() or not queue:
            continue
        if ref:
            queue.pop(0)
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
                  if os.path.basename(d).lower().startswith((journal + "_", journal + "-")))
    runs = [("all", None)] + ([(r, [r]) for r in rules.RULES] if "--per-rule" in argv else [])
    for label, only in runs:
        tot = dict(dOT=0, dUT=0, changes=0, helped=0, hurt=0, matched=0, paragraphs=0)
        for d in dirs:
            r = evaluate(d, journal, only, show_hurt="--show-hurt" in argv and label == "all",
                         join="--join-paragraphs" in argv)
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
