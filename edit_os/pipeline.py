"""End-to-end run: front matter + style rules + copy-edit checkers -> every output an editor needs.

    edit(input, journal, outdir)   run the stages, save the records, render the outputs
    render_outputs(outdir)         render again from the saved records and their current statuses

What `edit` writes into OUTDIR (STEM is the manuscript's file name):

    source/STEM.docx          the manuscript as received (the base every offset refers to)
    source/figures/           figure files from the zip proposed for insertion (figures_insert.py)
    STEM_changes.json         every proposed Change (status suggested / accepted / rejected)
    STEM_front_matter.json    front-matter paragraphs to insert (same statuses)
    STEM_queries.json         author queries (status suggested / accepted / rejected; rejected ones are dropped)
    STEM_tracked.docx         tracked changes + author queries as Word comments
    STEM_clean.docx           accepted changes only
    STEM_queries.xlsx         query sheet
    STEM_qc_report.json/.html what ran, counts by layer and status, QC checks passed / failed
    edit_manifest.json        journal, stages, what ran, warnings; `render_outputs` reads it

Statuses decide what is rendered. The clean copy (the deliverable) holds accepted changes only. The
tracked copy holds accepted changes and, unless `include_pending=False`, the suggestions nobody has
decided yet, as tracked revisions for review in Word; rejected ones never appear. QC runs on the text
as it would be with every non-rejected change applied, so the report says what the file will look like
once the pending suggestions are accepted; with nothing pending that is the clean copy itself.
"""

from __future__ import annotations

import html
import inspect
import json
import re
import os
import shutil
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import docmodel, figures_insert, first_author, frontmatter, header, qc, queries, rules, style as style_mod, submission, tracked
from .changes import Change, ChangeSet, Layer, Source, Status
from .style import JournalStyle

MANIFEST = "edit_manifest.json"
STAGES = ("style", "copyedit")
_DEFAULT = object()

# Output kinds -> file name pattern (STEM substituted).
FILES = {
    "changes": "{stem}_changes.json",
    "front_matter": "{stem}_front_matter.json",
    "queries_json": "{stem}_queries.json",
    "tracked": "{stem}_tracked.docx",
    "clean": "{stem}_clean.docx",
    "queries": "{stem}_queries.xlsx",
    "qc_json": "{stem}_qc_report.json",
    "qc_html": "{stem}_qc_report.html",
}


@dataclass
class Outputs:
    outdir: str
    stem: str
    files: dict[str, str] = field(default_factory=dict)  # kind -> path
    summary: dict = field(default_factory=dict)  # the QC report

    def path(self, kind: str) -> str:
        return self.files[kind]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def manuscript_member(z: zipfile.ZipFile) -> str | None:
    """The manuscript .docx inside a submission zip: the one named "manuscript", else the largest."""
    docs = [n for n in z.namelist() if n.lower().endswith(".docx") and not n.startswith("__MACOSX")
            and not frontmatter._FIRST_PAGE.search(os.path.basename(n))]
    if not docs:
        return None
    named = [n for n in docs if "manuscript" in os.path.basename(n).lower()]
    # "Figure of manuscript file.docx", "Table of manuscript.docx" sit next to "Manuscript.docx": the file
    # named for a part of the manuscript comes after the one that is the manuscript.
    named.sort(key=lambda n: bool(_PART_OF_MANUSCRIPT.search(os.path.basename(n))))
    return (named or sorted(docs, key=lambda n: -z.getinfo(n).file_size))[0]


_PART_OF_MANUSCRIPT = re.compile(r"(?i)\b(?:figures?|fig|tables?|supplement\w*|appendix|rebuttal|response|cover|"
                                 r"letter|revision\s+notes?)\b")


def manuscript_from_zip(path: str, outdir: str) -> str:
    """Write the manuscript .docx from a submission zip into OUTDIR (`manuscript_member`)."""
    z = zipfile.ZipFile(path)
    pick = manuscript_member(z)
    if pick is None:
        raise ValueError(f"{os.path.basename(path)}: no manuscript .docx in the zip")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, os.path.basename(pick))
    with open(out, "wb") as f:
        f.write(z.read(pick))
    return out


# --- records on disk ------------------------------------------------------------------------

def query_to_dict(q: queries.Query, key: str, status: str = Status.SUGGESTED.value) -> dict:
    return {**asdict(q), "key": key, "status": status}


def query_from_dict(d: dict) -> tuple[queries.Query, str, str]:
    q = queries.Query(d["para_id"], d["start"], d["end"], d["text"], d["check"], d.get("id", ""))
    return q, d["key"], d.get("status", Status.SUGGESTED.value)


def insert_from_dict(d: dict) -> frontmatter.NewParagraph:
    if "images" in d:  # a figure supplied as a separate file (figures_insert.py)
        return figures_insert.FigureInsert.from_dict(d)
    return frontmatter.NewParagraph(
        id=d["id"], anchor=d["anchor"], where=d["where"], runs=[(t, f) for t, f in d["runs"]], role=d["role"],
        status=Status(d.get("status", "suggested")), source=Source(d.get("source", "rule")),
        reason=d.get("reason", "front_matter"), provenance=d.get("provenance") or None,
        layer=Layer(d.get("layer", Layer.STRUCTURE.value)))


def load_manifest(outdir: str) -> dict:
    with open(os.path.join(outdir, MANIFEST), encoding="utf-8") as f:
        return json.load(f)


def file_paths(outdir: str, stem: str) -> dict[str, str]:
    return {k: os.path.join(outdir, v.format(stem=stem)) for k, v in FILES.items()}


def load_records(outdir: str) -> tuple[dict, ChangeSet, list[frontmatter.NewParagraph], list[dict]]:
    """(manifest, changes, front-matter inserts, query dicts) as saved in OUTDIR."""
    man = load_manifest(outdir)
    paths = file_paths(outdir, man["stem"])
    changes = ChangeSet.load(paths["changes"])
    with open(paths["front_matter"], encoding="utf-8") as f:
        inserts = [insert_from_dict(d) for d in json.load(f)]
    with open(paths["queries_json"], encoding="utf-8") as f:
        qdicts = json.load(f)
    return man, changes, inserts, qdicts


def save_records(outdir: str, changes: ChangeSet | None = None, inserts=None, qdicts=None) -> None:
    paths = file_paths(outdir, load_manifest(outdir)["stem"])
    if changes is not None:
        changes.dump(paths["changes"])
    if inserts is not None:
        with open(paths["front_matter"], "w", encoding="utf-8") as f:
            json.dump([n.to_dict() for n in inserts], f, indent=2, ensure_ascii=False)
    if qdicts is not None:
        with open(paths["queries_json"], "w", encoding="utf-8") as f:
            json.dump(qdicts, f, indent=2, ensure_ascii=False)


def source_path(outdir: str) -> str:
    return os.path.join(outdir, load_manifest(outdir)["source"])


def paragraph_texts(docx_path: str) -> dict[str, str]:
    """para_id -> text view, for showing changes in context."""
    return {p.id: p.text for p in docmodel.load(docx_path).paragraphs}


# --- running the stages --------------------------------------------------------------------

def _run_copyedit(ms, st, existing: list[Change], lt_client, only):
    """copyedit.run_all, passing only the options its signature takes (it is developed separately)."""
    from . import copyedit

    if lt_client is _DEFAULT:
        from .copyedit import langtool
        lt_client = langtool.default_client()
    params = inspect.signature(copyedit.run_all).parameters
    kw = {"lt_client": lt_client, "existing": existing, "only": only}
    return copyedit.run_all(ms, st, **{k: v for k, v in kw.items() if k in params})


def _unique_ids(changes: list[Change]) -> None:
    seen: set[str] = set()
    for c in changes:
        base, n = c.id, 1
        while c.id in seen:
            n += 1
            c.id = f"{base}_{n}"
        seen.add(c.id)


def edit(input_path: str, journal: str | JournalStyle, outdir: str, stages=STAGES, *,
         first_page: str | None = None, xml: str | None = None, lt_client=_DEFAULT,
         copyedit_only: list[str] | None = None, rules_only: list[str] | None = None,
         accept_all: bool = False, progress: Callable[[str], None] | None = None,
         counts: Callable[[dict], None] | None = None, editor: str | None = None) -> Outputs:
    """Run the stages on a manuscript .docx or a submission .zip and write every output into OUTDIR.

    All proposals are saved as `suggested` (the editor decides) unless `accept_all` (a preview run,
    like the `style` command). `progress(message)` is called as each step starts, and
    `counts({"changes": n, "queries": n, "inserts": n})` as proposals accumulate (for live progress).
    `editor` signs the technical editing (header.py); without it the sign-off has a placeholder QC flags.
    """
    say = progress or (lambda msg: None)
    tally = counts or (lambda d: None)
    unknown = set(stages) - set(STAGES)
    if unknown:
        raise ValueError(f"unknown stage(s) {sorted(unknown)}; choose from {STAGES}")
    st = journal if isinstance(journal, JournalStyle) else style_mod.load(journal)
    os.makedirs(outdir, exist_ok=True)
    srcdir = os.path.join(outdir, "source")
    os.makedirs(srcdir, exist_ok=True)

    say("reading manuscript")
    front_src = frontmatter.sources_from_files(first_page, xml)
    zip_path = input_path if input_path.lower().endswith(".zip") else None
    if zip_path:
        front_src = frontmatter.sources_from_zip(input_path)
        src = manuscript_from_zip(input_path, srcdir)
    elif input_path.lower().endswith(".docx"):
        src = os.path.join(srcdir, os.path.basename(input_path))
        if os.path.abspath(src) != os.path.abspath(input_path):
            shutil.copyfile(input_path, src)
    else:
        raise ValueError("input must be a .docx manuscript or a submission .zip")
    stem = os.path.splitext(os.path.basename(src))[0]

    ms = docmodel.load(src)
    ms.front_sources = front_src
    changes = ChangeSet()
    inserts: list[frontmatter.NewParagraph] = []
    qs = queries.QuerySet()
    stage_info: dict[str, dict] = {}
    warnings: list[dict] = []
    notes: list[str] = []

    if "style" in stages:
        say("style rules and front matter")
        ran = [n for n in rules.RULES if not rules_only or n in rules_only]
        style_changes = rules.run_all(ms, st, only=rules_only)
        front = frontmatter.plan(ms, st) if "front_matter" in ran else frontmatter.Plan()
        inserts = list(front.inserts)
        notes += front.notes
        say("author queries")
        style_q = front.add_queries(queries.collect(ms, st), ms) if "front_matter" in ran else queries.collect(ms, st)
        changes.changes += style_changes.changes
        qs.queries += style_q.queries
        warnings += submission.warnings(ms, st)  # submission material not certain enough to delete
        figs = figures_insert.plan(ms, st, zip_path, outdir)  # figures sent as separate files
        inserts += figs.inserts
        warnings += figs.warnings
        for pid, key, fmt in figs.queries:  # a supplied figure with no legend in the manuscript
            qs.add(pid, key, **fmt)
        if "front_matter" in ran:  # the "First author" block after the references, from the front matter
            fa = first_author.plan(ms, st, front)
            inserts += fa.inserts
            for pid, key in fa.queries:
                qs.add(pid, key)
        tally({"changes": len(changes.changes), "queries": len(qs.queries), "inserts": len(inserts)})
        stage_info["style"] = {
            "ran": ran, "changes": len(style_changes.changes), "inserts": len(inserts), "figures": len(figs.inserts),
            "first_author": sum(n.reason == first_author.REASON for n in inserts),
            "queries": len(style_q.queries), "by_rule": dict(Counter(c.reason for c in style_changes.changes)),
        }

    if "copyedit" in stages:
        say("copy-edit checkers")
        from . import copyedit

        res = _run_copyedit(ms, st, list(changes.changes), lt_client, copyedit_only)
        ce_ran = [n for n in copyedit.CHECKERS if not copyedit_only or n in copyedit_only]
        changes.changes += res.changes.changes
        qs.queries += res.queries
        tally({"changes": len(changes.changes), "queries": len(qs.queries), "inserts": len(inserts)})
        warnings += [asdict(w) if hasattr(w, "__dataclass_fields__") else dict(w) for w in res.warnings]
        stage_info["copyedit"] = {
            "ran": ce_ran, "changes": len(res.changes.changes), "queries": len(res.queries),
            "warnings": len(res.warnings), "by_rule": dict(Counter(c.reason for c in res.changes.changes)),
        }

    head_info = header.plan(ms, st) if "style" in stages else {"renumbering": [], "after": None}

    # a cover letter or reply to the reviewers that is deleted gets no heading tags or copy edits
    changes.changes = submission.drop_inside_deleted(changes.changes)
    _unique_ids(changes.changes)
    qs.number(ms)
    if accept_all:
        for c in changes.changes:
            c.status = Status.ACCEPTED
        for n in inserts:
            n.status = Status.ACCEPTED

    manifest = {
        "version": 1, "created_at": _now(), "input_name": os.path.basename(input_path),
        "source": os.path.relpath(src, outdir), "stem": stem, "journal": st.code, "journal_name": st.name,
        "stages": list(stages), "stage_info": stage_info, "warnings": warnings, "notes": notes,
        "header": head_info, "editor": editor or "",
    }
    with open(os.path.join(outdir, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    save_records(outdir, changes, inserts,
                 [query_to_dict(q, f"q{i}", Status.ACCEPTED.value if accept_all else Status.SUGGESTED.value)
                  for i, q in enumerate(qs.queries, 1)])
    say("rendering outputs")
    return render_outputs(outdir, style=st)


# --- rendering ------------------------------------------------------------------------------

def render_outputs(outdir: str, include_pending: bool = True, style: JournalStyle | None = None,
                   editor: str | None = None, when: datetime | None = None) -> Outputs:
    """Render tracked and clean copies, the query sheet and the QC report from the saved records
    and their current statuses. Changes whose text no longer matches are skipped and reported.

    `editor` (else the one saved with the job) signs the technical editing at the top of both copies, dated
    `when` (default: now, in the journal's sign-off time zone); see header.py."""
    man, changes, inserts, qdicts = load_records(outdir)
    st = style or style_mod.load(man["journal"])
    src = os.path.join(outdir, man["source"])
    paths = file_paths(outdir, man["stem"])
    pending = (Status.SUGGESTED, Status.ACCEPTED)
    tracked_statuses = pending if include_pending else (Status.ACCEPTED,)

    # Queries: drop rejected ones, number the rest in reading order (AQ1, AQ2, ...).
    base = docmodel.load(src)
    live = [(query_from_dict(d)[0], d) for d in qdicts if d.get("status") != Status.REJECTED.value]
    qs = queries.QuerySet([q for q, _ in live]).number(base)
    for q, d in live:
        d["id"] = q.id
    for d in qdicts:
        if d.get("status") == Status.REJECTED.value:
            d["id"] = ""
    save_records(outdir, qdicts=qdicts)

    conflicts: list[dict] = []
    editor = (editor or man.get("editor") or "").strip()
    head_info = man.get("header") or {"renumbering": [], "after": None}
    if when is None:
        from zoneinfo import ZoneInfo
        when = datetime.now(ZoneInfo((st.get("header.signoff", {}) or {}).get("timezone", "Asia/Kolkata")))
    rev_date = _now()
    head_notes: list[str] = []

    def conflict(kind):
        return lambda para_id, err: conflicts.append({"output": kind, "para_id": para_id, "error": str(err)})

    def render(kind: str, is_tracked: bool, statuses, comments: bool) -> docmodel.Manuscript:
        ms = docmodel.load(src)
        if comments:
            queries.write_comments(ms, qs)
        tracked.render(ms, changes, tracked=is_tracked, statuses=statuses, on_conflict=conflict(kind))
        frontmatter.render_inserts(ms, [n for n in inserts if not figures_insert.is_figure_insert(n)],
                                   tracked=is_tracked, statuses=statuses)
        figures_insert.render(ms, inserts, outdir, tracked=is_tracked, statuses=statuses,
                              align=st.get("figures.insert.align", "center"))
        author = editor or header.AUTHOR
        notes = header.move_blocks(ms, head_info, changes, statuses, tracked=is_tracked, author=author, date=rev_date)
        if kind == "clean":
            head_notes.extend(notes)
        header.render(ms, header.lines(st, head_info, changes, statuses, stages=man["stages"], editor=editor, when=when),
                      tracked=is_tracked, author=author, date=rev_date, after=head_info.get("after"))
        if kind == "clean":
            tracked.strip_highlighting(ms)  # the author's revision highlights never reach the deliverable
        return ms

    render("tracked", True, tracked_statuses, True).save(paths["tracked"])
    render("clean", False, (Status.ACCEPTED,), False).save(paths["clean"])
    queries.write_sheet(docmodel.load(src), qs, paths["queries"], man["stem"])

    n_pending = sum(c.status == Status.SUGGESTED for c in changes.changes) + \
        sum(n.status == Status.SUGGESTED for n in inserts)
    if n_pending:
        projected = os.path.join(outdir, ".qc_projected.docx")
        render("projected", False, pending, False).save(projected)
        qc_doc = docmodel.load(projected)
        os.remove(projected)
    else:
        qc_doc = docmodel.load(paths["clean"])
    report = qc.check(qc_doc, st)

    summary = build_report(dict(man, notes=man.get("notes", []) + head_notes), changes, inserts, qdicts, report,
                           conflicts, n_pending)
    with open(paths["qc_json"], "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    with open(paths["qc_html"], "w", encoding="utf-8") as f:
        f.write(report_html(summary))
    return Outputs(outdir, man["stem"], paths, summary)


def build_report(man: dict, changes: ChangeSet, inserts, qdicts: list[dict], report: qc.Report,
                 conflicts: list[dict], n_pending: int) -> dict:
    by_layer: dict[str, dict[str, int]] = {}
    for c in changes.changes:
        row = by_layer.setdefault(c.layer.value, {"total": 0, "suggested": 0, "accepted": 0, "rejected": 0})
        row["total"] += 1
        row[c.status.value] += 1
    failed = Counter(i.check for i in report.issues)
    severity = {}
    for i in report.issues:
        severity[i.check] = qc.ERROR if qc.ERROR in (severity.get(i.check), i.severity) else i.severity
    checks = [{"check": k, "description": d, "passed": k not in failed, "issues": failed.get(k, 0),
               "severity": severity.get(k)} for k, d in qc.CHECKS.items()]
    checks += [{"check": k, "description": k, "passed": False, "issues": n, "severity": severity[k]}
               for k, n in failed.items() if k not in qc.CHECKS]
    live_q = [d for d in qdicts if d.get("status") != Status.REJECTED.value]
    return {
        "generated_at": _now(),
        "article": man["stem"], "input_name": man.get("input_name"), "journal": man["journal"],
        "journal_name": man.get("journal_name"), "stages": man["stages"], "stage_info": man.get("stage_info", {}),
        "changes": {
            "total": len(changes.changes), "by_layer": by_layer,
            "by_source": dict(Counter(c.source.value for c in changes.changes)),
            "by_status": dict(Counter(c.status.value for c in changes.changes)),
            "pending": n_pending,
        },
        "front_matter_inserts": {"total": len(inserts), "by_status": dict(Counter(n.status.value for n in inserts))},
        "figure_inserts": {"total": sum(map(figures_insert.is_figure_insert, inserts)),
                           "by_status": dict(Counter(n.status.value for n in inserts
                                                     if figures_insert.is_figure_insert(n)))},
        "queries": {"total": len(live_q), "rejected": len(qdicts) - len(live_q),
                    "by_check": dict(Counter(d["check"] for d in live_q))},
        "qc": {
            "passed": report.passed,
            "basis": "accepted + pending suggestions" if n_pending else "clean copy (all decided)",
            "errors": sum(i.severity == qc.ERROR for i in report.issues),
            "warnings": sum(i.severity == qc.WARNING for i in report.issues),
            "checks": checks,
            "issues": [asdict(i) for i in report.issues],
        },
        "editor_warnings": man.get("warnings", []),
        "notes": man.get("notes", []),
        "render_conflicts": conflicts,
        "delivery_ready": report.passed and n_pending == 0 and not conflicts,
    }


def report_html(s: dict) -> str:
    e = html.escape
    rows_layer = "".join(
        f"<tr><td>{e(k)}</td><td>{v['total']}</td><td>{v['accepted']}</td><td>{v['rejected']}</td><td>{v['suggested']}</td></tr>"
        for k, v in sorted(s["changes"]["by_layer"].items()))
    rows_checks = "".join(
        f"<tr class='{'ok' if c['passed'] else c['severity'] or 'error'}'><td>{'PASS' if c['passed'] else e((c['severity'] or 'error').upper())}</td>"
        f"<td>{e(c['check'])}</td><td>{e(c['description'])}</td><td>{c['issues'] or ''}</td></tr>"
        for c in s["qc"]["checks"])
    rows_issues = "".join(
        f"<tr class='{e(i['severity'])}'><td>{e(i['severity'])}</td><td>{e(i['check'])}</td><td>{e(i['para_id'] or '')}</td><td>{e(i['message'])}</td></tr>"
        for i in s["qc"]["issues"])
    stages = "".join(
        f"<li><b>{e(name)}</b>: {info.get('changes', 0)} changes, {info.get('queries', 0)} queries"
        f"{', %d front-matter paragraphs' % info['inserts'] if info.get('inserts') else ''}"
        f"{', %d warnings' % info['warnings'] if info.get('warnings') else ''}"
        f"<br><span class='muted'>ran: {e(', '.join(info.get('ran', [])))}</span>"
        + ("<br><span class='muted'>by rule: " + e(", ".join(f"{k} {v}" for k, v in sorted(info.get("by_rule", {}).items(), key=lambda kv: -kv[1]))) + "</span>" if info.get("by_rule") else "")
        + "</li>" for name, info in s["stage_info"].items())
    q_rows = "".join(f"<tr><td>{e(k)}</td><td>{v}</td></tr>" for k, v in sorted(s["queries"]["by_check"].items()))
    warn = "".join(f"<li>{e(w.get('para_id', ''))} {e(w.get('rule', ''))}: {e(w.get('message', ''))}</li>"
                   for w in s["editor_warnings"][:200])
    conflicts = "".join(f"<li>{e(c['output'])} {e(c['para_id'])}: {e(c['error'])}</li>" for c in s["render_conflicts"])
    verdict = "PASSED" if s["qc"]["passed"] else "BLOCKED"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>QC report – {e(s['article'])}</title>
<style>
body{{font:14px/1.45 system-ui,sans-serif;margin:24px auto;max-width:960px;padding:0 16px;color:#1d2433;background:#fff}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 8px;border-bottom:1px solid #dde2ea;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%}} td,th{{text-align:left;padding:5px 8px;border-bottom:1px solid #eef1f5;vertical-align:top}}
th{{font-weight:600;background:#f6f8fb}} .muted{{color:#667085;font-size:12px}}
.badge{{display:inline-block;padding:3px 10px;border-radius:12px;font-weight:600}}
.pass{{background:#e5f6ec;color:#146c3b}} .fail{{background:#fde8e8;color:#9b1c1c}}
tr.error td:first-child{{color:#9b1c1c;font-weight:600}} tr.warning td:first-child{{color:#9a6700;font-weight:600}}
tr.ok td:first-child{{color:#146c3b}}
</style></head><body>
<h1>QC report: {e(s['article'])}</h1>
<div class="muted">{e(s.get('journal_name') or '')} ({e(s['journal'])}) · input {e(s.get('input_name') or '')} · generated {e(s['generated_at'])}</div>
<p><span class="badge {'pass' if s['qc']['passed'] else 'fail'}">QC {verdict}</span>
 {s['qc']['errors']} errors, {s['qc']['warnings']} warnings · checked on: {e(s['qc']['basis'])} ·
 {s['changes']['pending']} suggestions pending · delivery ready: {'yes' if s['delivery_ready'] else 'no'}</p>
<h2>Stages run</h2><ul>{stages}</ul>
<h2>Changes by layer</h2>
<table><tr><th>Layer</th><th>Total</th><th>Accepted</th><th>Rejected</th><th>Pending</th></tr>{rows_layer}</table>
<p class="muted">By source: {e(', '.join(f'{k} {v}' for k, v in s['changes']['by_source'].items()))} ·
front-matter paragraphs: {s['front_matter_inserts']['total']}</p>
<h2>QC checks</h2>
<table><tr><th>Result</th><th>Check</th><th>What</th><th>Issues</th></tr>{rows_checks}</table>
{'<h2>QC issues</h2><table><tr><th>Severity</th><th>Check</th><th>Paragraph</th><th>Message</th></tr>' + rows_issues + '</table>' if rows_issues else ''}
<h2>Author queries ({s['queries']['total']})</h2>
<table><tr><th>Kind</th><th>Count</th></tr>{q_rows}</table>
{'<h2>Editor warnings</h2><ul>' + warn + '</ul>' if warn else ''}
{'<h2>Changes that could not be rendered</h2><ul>' + conflicts + '</ul>' if conflicts else ''}
</body></html>
"""
