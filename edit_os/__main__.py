"""Command line: python -m edit_os <command> ...

  paragraphs IN.docx                       list paragraph IDs and text
  refs IN.docx                             list the reference entries found
  render IN.docx CHANGES.json OUTDIR       write tracked and clean copies from accepted changes
  style IN.docx JOURNAL OUTDIR             run the journal's rules; write tracked (with author queries as
        [--first-page FP.docx] [--xml M.xml]  comments) and clean copies, the query sheet and QC; the title-page
  style original.zip JOURNAL OUTDIR        file and XML feed the front matter (a zip supplies all three)
  journals update                          download NLM's journal list into the local database
  journals find NAME                       look up a journal by title, abbreviation or ISSN
"""

from __future__ import annotations

import json
import os
import sys

from . import docmodel, frontmatter, journals, qc, queries, references, rules, style, tracked
from .changes import ChangeSet, Status


def _manuscript_from_zip(path: str, outdir: str) -> str:
    """Write the manuscript .docx from a submission zip into OUTDIR; the one named "manuscript", else the largest."""
    import zipfile

    z = zipfile.ZipFile(path)
    docs = [n for n in z.namelist() if n.lower().endswith(".docx") and not frontmatter._FIRST_PAGE.search(os.path.basename(n))]
    named = [n for n in docs if "manuscript" in os.path.basename(n).lower()]
    pick = (named or sorted(docs, key=lambda n: -z.getinfo(n).file_size))[0]
    out = os.path.join(outdir, os.path.basename(pick))
    with open(out, "wb") as f:
        f.write(z.read(pick))
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, path = argv[0], argv[1]
    if cmd == "journals" and path == "update":
        print(f"{journals.update()} journals saved to {journals.DEFAULT_DB}")
    elif cmd == "journals" and path == "find" and len(argv) == 3:
        for j in journals.Directory().find(argv[2]):
            print(f"{j.med_abbr}\t{j.title}\tISSN {j.issn_print or '-'} / {j.issn_online or '-'}\tNLM {j.nlm_id}")
    elif cmd == "paragraphs":
        for p in docmodel.load(path).paragraphs:
            if p.text.strip():
                print(f"{p.id}\t{p.style or '-'}\t{p.text[:100]}")
    elif cmd == "refs":
        for r in references.extract(docmodel.load(path)):
            print(f"{r.para_id}\t{r.number if r.number is not None else '-'}\t{r.text}")
    elif cmd == "render" and len(argv) == 4:
        changes = ChangeSet.load(argv[2])
        os.makedirs(argv[3], exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        for kind, is_tracked in (("tracked", True), ("clean", False)):
            out = os.path.join(argv[3], f"{stem}_{kind}.docx")
            tracked.render(docmodel.load(path), changes, tracked=is_tracked).save(out)
            print(out)
    elif cmd == "style" and len(argv) >= 4 and len(argv) % 2 == 0:
        st = style.load(argv[2])
        os.makedirs(argv[3], exist_ok=True)
        opts = dict(zip(argv[4::2], argv[5::2]))
        src = frontmatter.sources_from_files(opts.get("--first-page"), opts.get("--xml"))
        if path.lower().endswith(".zip"):
            src = frontmatter.sources_from_zip(path)
            path = _manuscript_from_zip(path, argv[3])
        stem = os.path.splitext(os.path.basename(path))[0]
        ms0 = docmodel.load(path)
        ms0.front_sources = src
        changes = rules.run_all(ms0, st)
        front = frontmatter.plan(ms0, st)
        for c in changes.changes:
            c.status = Status.ACCEPTED  # preview: render every rule suggestion; the editor screen decides later
        for np_ in front.inserts:
            np_.status = Status.ACCEPTED
        changes.dump(os.path.join(argv[3], f"{stem}_changes.json"))
        with open(os.path.join(argv[3], f"{stem}_front_matter.json"), "w", encoding="utf-8") as f:
            json.dump([n.to_dict() for n in front.inserts], f, indent=2, ensure_ascii=False)
        ms_q = docmodel.load(path)
        qs = front.add_queries(queries.collect(ms_q, st), ms_q)
        for kind, is_tracked in (("tracked", True), ("clean", False)):
            out = os.path.join(argv[3], f"{stem}_{kind}.docx")
            ms = docmodel.load(path)
            if is_tracked:  # anchors from the original text
                queries.write_comments(ms, front.add_queries(queries.collect(ms, st), ms))
            tracked.render(ms, changes, tracked=is_tracked)
            frontmatter.render_inserts(ms, front.inserts, tracked=is_tracked)
            ms.save(out)
        queries.write_sheet(docmodel.load(path), qs, os.path.join(argv[3], f"{stem}_queries.xlsx"), stem)
        report = qc.check(docmodel.load(out), st)
        print(f"{len(changes.changes)} changes; {len(front.inserts)} front-matter paragraphs; "
              f"{len(qs.queries)} author queries; QC {'PASSED' if report.passed else 'BLOCKED'}")
        for q in qs.queries:
            print(f"  {q.id:5s} {q.text}")
        for note in front.notes:
            print(f"  note    {note}")
        for i in report.issues:
            print(f"  {i.severity:7s} {i.check:22s} {i.message}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
