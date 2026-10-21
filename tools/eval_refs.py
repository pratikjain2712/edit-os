"""Score reference handling against a style editor's reference lists.

For each article, the manuscript runs through the full rule pipeline (PubMed / CrossRef lookup
plus reference rules), and each output reference is compared with the editor's version of the
same reference, paired by reference number.

Usage: python tools/eval_refs.py DATA_DIR JOURNAL [--show N]
"""

from __future__ import annotations

import collections
import glob
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evaluate import distance, manuscript_from_zip, tracked_pairs  # noqa: E402

from edit_os import ai_refs, docmodel, references, rules, style as style_mod, tracked  # noqa: E402
from edit_os.changes import Status  # noqa: E402

_HEAD = re.compile(r"^\s*(?:<H\d>)?\s*references?\s*:?\s*$", re.I)


def editor_refs(te_bytes: bytes) -> dict[int, str]:
    after = [a for _, a in tracked_pairs(te_bytes)]
    heads = [i for i, t in enumerate(after) if _HEAD.match(t)]
    out, n = {}, 0
    for t in after[heads[-1] + 1:] if heads else []:
        if not t.strip():
            continue
        if re.match(r"^\s*(?:<H\d>)?\s*(tables?|figures?|fig\.|legends?)\b", t, re.I):
            break
        m = re.match(r"^\s*(\[\d+\]|\(\d+\)|\d+[.)]|\d+(?=\s))\s*", t)
        n = int(re.sub(r"\D", "", m.group(1))) if m else n + 1
        out[n] = t[m.end():].strip() if m else t.strip()
    return out


def norm(s: str) -> str:
    return " ".join(s.replace("–", "-").split()).rstrip(".")


def main(argv):
    data, journal = argv[0], argv[1].lower()
    show = int(argv[argv.index("--show") + 1]) if "--show" in argv else 0
    st = style_mod.load(journal)
    if "--no-ai" in argv:
        st.settings["references"]["ai_parse"] = False
    tot = collections.Counter()
    shown = 0
    for d in sorted(glob.glob(os.path.join(data, "*"))):
        if not os.path.basename(d).lower().startswith(journal + "_"):
            continue
        src = manuscript_from_zip(os.path.join(d, "original.zip"))
        with open(os.path.join(d, "te.docx"), "rb") as f:
            ed = editor_refs(f.read())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.docx")
            with open(path, "wb") as f:
                f.write(src)
            ms = docmodel.load(path)
            before = {r.number: r.text for r in references.extract(ms) if r.number}
            status = {r.number: res.status for r, res in rules.lookup_references(ms, st)}
            cs = rules.run_all(ms, st)
            for c in cs.changes:
                c.status = Status.ACCEPTED
            out = os.path.join(tmp, "u.docx")
            tracked.render(docmodel.load(path), cs, tracked=False).save(out)
            after = {r.number: r.text for r in references.extract(docmodel.load(out)) if r.number}
        for n, orig in before.items():
            if n not in ed or n not in after:
                continue
            t, u = ed[n], after[n]
            tot["refs"] += 1
            tot["status:" + status.get(n, "-")] += 1
            tot["dOT"] += distance(orig, t)
            tot["dUT"] += distance(u, t)
            tot["exact_before"] += norm(orig) == norm(t)
            tot["exact_after"] += norm(u) == norm(t)
            if show and shown < show and norm(u) != norm(t):
                shown += 1
                print(f"[{status.get(n)}] {os.path.basename(d)} #{n}\n  orig:   {orig[:200]}\n  ours:   {u[:200]}\n  editor: {t[:200]}")
    r = tot["refs"] or 1
    print(f"references {tot['refs']}  identical to editor: before {tot['exact_before'] / r:.0%} -> after {tot['exact_after'] / r:.0%}"
          f"  closure {(tot['dOT'] - tot['dUT']) / (tot['dOT'] or 1):.1%}")
    print("  " + "  ".join(f"{k[7:]} {v}" for k, v in sorted(tot.items()) if k.startswith("status:")))
    print(f"  Claude: {'on' if st.get('references.ai_parse') and ai_refs.available() else 'off'}, usage this run {ai_refs.USAGE}")


if __name__ == "__main__":
    main(sys.argv[1:])
