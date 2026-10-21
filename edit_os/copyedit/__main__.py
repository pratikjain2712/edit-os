"""python -m edit_os.copyedit IN.docx JOURNAL OUTDIR [--only spelling,abbreviations,languagetool]
   python -m edit_os.copyedit languagetool install|start [PORT]|status

Run the copy-edit checkers; write the change records (JSON) and a tracked preview with the author
queries as comments, and list the queries and LanguageTool warnings. LanguageTool is used when
its server answers (python -m edit_os.copyedit languagetool start).
"""

from __future__ import annotations

import os
import sys

from .. import docmodel, queries, style, tracked
from ..changes import Status
from . import langtool, run_all


def main(argv: list[str]) -> int:
    if argv[:1] == ["languagetool"]:
        return langtool.main(argv[1:])
    if len(argv) < 3:
        print(__doc__)
        return 2
    path, journal, outdir = argv[:3]
    only = argv[argv.index("--only") + 1].split(",") if "--only" in argv else None
    st = style.load(journal)
    client = langtool.default_client()
    if client is None:
        print("LanguageTool server not running; skipping it.")
    res = run_all(docmodel.load(path), st, only=only, lt_client=client)
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    res.changes.dump(os.path.join(outdir, f"{stem}_copyedit_changes.json"))
    for c in res.changes.changes:
        c.status = Status.ACCEPTED  # preview only; the editor decides
    ms = docmodel.load(path)
    qs = queries.QuerySet(list(res.queries)).number(ms)
    queries.write_comments(ms, qs)
    out = os.path.join(outdir, f"{stem}_copyedit_tracked.docx")
    tracked.render(ms, res.changes, tracked=True).save(out)
    print(f"{len(res.changes.changes)} changes; {len(qs.queries)} author queries; {len(res.warnings)} warnings -> {out}")
    for q in qs.queries:
        print(f"  {q.id:5s} {q.text}")
    for w in res.warnings:
        print(f"  warning {w.para_id} {w.rule}: {w.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
