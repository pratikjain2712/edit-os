"""Command line: python -m edit_os <command> ...

  paragraphs IN.docx                       list paragraph IDs and text
  refs IN.docx                             list the reference entries found
  render IN.docx CHANGES.json OUTDIR       write tracked and clean copies from accepted changes; with
        [--queries Q.json] [--front-matter F.json]  author queries as comments and front-matter paragraphs
  render OUTDIR [--final] [--editor NAME]  re-render an `edit` output folder from its records' statuses
                                           (NAME signs the technical editing at the top of the file)
  edit IN.docx|IN.zip JOURNAL OUTDIR       full run: front matter + style rules + copy-edit checkers ->
        [--stages style,copyedit]          changes.json, tracked (queries as comments) and clean copies,
        [--accept-all]                     query sheet, QC report (.json + .html); proposals stay
        [--first-page FP.docx] [--xml M.xml]  "suggested" unless --accept-all; --editor NAME signs it
        [--editor NAME]
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

from . import docmodel, frontmatter, journals, pipeline, qc, queries, references, rules, style, tracked
from .changes import ChangeSet, Status

_manuscript_from_zip = pipeline.manuscript_from_zip


def _options(argv: list[str], flags=()) -> tuple[list[str], dict[str, str], set[str]]:
    """Split ARGV into positionals, --key value options and bare --flags."""
    pos, opts, seen = [], {}, set()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in flags:
            seen.add(a)
        elif a.startswith("--") and i + 1 < len(argv):
            opts[a] = argv[i + 1]
            i += 1
        else:
            pos.append(a)
        i += 1
    return pos, opts, seen


def _print_summary(out: "pipeline.Outputs") -> None:
    s = out.summary
    print(f"{s['changes']['total']} changes ({s['changes']['pending']} pending); "
          f"{s['front_matter_inserts']['total']} front-matter paragraphs; {s['queries']['total']} author queries; "
          f"QC {'PASSED' if s['qc']['passed'] else 'BLOCKED'} ({s['qc']['basis']})")
    for layer, row in sorted(s["changes"]["by_layer"].items()):
        print(f"  {layer:15s} {row['total']:4d}  accepted {row['accepted']}  rejected {row['rejected']}")
    for c in s["qc"]["checks"]:
        if not c["passed"]:
            print(f"  {c['severity'] or 'error':7s} {c['check']:22s} {c['issues']} issue(s)")
    for c in s["render_conflicts"]:
        print(f"  skipped {c['para_id']} in {c['output']}: {c['error']}")
    for kind in ("tracked", "clean", "queries", "qc_html"):
        print(f"  {out.files[kind]}")


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
    elif cmd == "render" and os.path.isdir(path):
        pos, opts, flags = _options(argv[1:], ("--final",))
        _print_summary(pipeline.render_outputs(pos[0], include_pending="--final" not in flags,
                                               editor=opts.get("--editor")))
    elif cmd == "render" and len(argv) >= 4:
        pos, opts, _ = _options(argv[1:])
        path, changes_path, outdir = pos[:3]
        changes = ChangeSet.load(changes_path)
        qs = queries.QuerySet()
        if "--queries" in opts:
            with open(opts["--queries"], encoding="utf-8") as f:
                qs.queries = [pipeline.query_from_dict({"key": "", **d})[0] for d in json.load(f)
                              if d.get("status") != Status.REJECTED.value]
            qs.number(docmodel.load(path))
        inserts = []
        if "--front-matter" in opts:
            with open(opts["--front-matter"], encoding="utf-8") as f:
                inserts = [pipeline.insert_from_dict(d) for d in json.load(f)]
        os.makedirs(outdir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        for kind, is_tracked in (("tracked", True), ("clean", False)):
            out = os.path.join(outdir, f"{stem}_{kind}.docx")
            ms = docmodel.load(path)
            if is_tracked and qs.queries:
                queries.write_comments(ms, qs)
            tracked.render(ms, changes, tracked=is_tracked)
            frontmatter.render_inserts(ms, [n for n in inserts if type(n) is frontmatter.NewParagraph],
                                       tracked=is_tracked)
            ms.save(out)
            print(out)
        if qs.queries:
            out = os.path.join(outdir, f"{stem}_queries.xlsx")
            queries.write_sheet(docmodel.load(path), qs, out, stem)
            print(out)
    elif cmd == "edit" and len(argv) >= 4:
        pos, opts, flags = _options(argv[1:], ("--accept-all",))
        path, journal, outdir = pos[:3]
        stages = tuple(opts.get("--stages", ",".join(pipeline.STAGES)).split(","))
        out = pipeline.edit(path, journal, outdir, stages, first_page=opts.get("--first-page"),
                            xml=opts.get("--xml"), accept_all="--accept-all" in flags,
                            progress=lambda m: print(f"... {m}", file=sys.stderr), editor=opts.get("--editor"))
        _print_summary(out)
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
