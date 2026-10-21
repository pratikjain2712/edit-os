# Edit OS

Copy editing and style editing for journal manuscripts. A user uploads a `.docx`; the system
suggests copy edits and style edits, checks and styles the references, runs the journal's QC
rules, and returns a tracked file, a clean copy, a query sheet and a QC report. An editor
accepts or rejects every suggestion.

## How it works

1. **Document model** (`edit_os/docmodel.py`): the `.docx` is read into paragraphs with stable
   IDs (`p0001`, ...) and a plain-text view. The original XML is kept, so formatting survives.
2. **Change records** (`edit_os/changes.py`): every edit, from rules, AI or an editor, is a
   record with paragraph, offsets, old and new text, layer, source, reason and status. The file
   itself is never edited directly.
3. **Rendering** (`edit_os/tracked.py`): accepted changes are written back as Word tracked
   changes (`w:ins` / `w:del`) or as a clean copy. Runs are split at change boundaries so
   inserted text keeps the formatting around it. A change whose `old` text no longer matches
   is refused.
4. **References** (`edit_os/references.py`): finds the reference list heading and splits it
   into entries, one per paragraph, with numbering labels (`1.`, `[1]`, `(1)`, `1)`, Word
   auto-numbering) kept separate.
5. **Journal styles** (`edit_os/styles/*.yaml`, `edit_os/style.py`): a shared Medknow base plus
   per-journal overrides, written from each journal's stylesheet and tuned against real edits.
6. **Rules** (`edit_os/rules.py`): deterministic style edits proposed as change records:
   heading tags, citation brackets and placement, statistics spacing, and reference cleanup
   (initials, et al., year/volume, issue, page ranges, capital after colon). Section rules
   (`edit_os/rules_sections.py`): heading renames and numbering, the abstract heading and
   sub-headings, key words (label, order, case), and the standard back-matter sections.
7. **QC gate** (`edit_os/qc.py`): checks the edited file; errors block it, warnings go to the
   editor's checklist.
8. **References engine** (`edit_os/refengine.py`, `biblio.py`, `journals.py`, `ai_refs.py`): Claude Haiku
   splits entries into fields, PubMed / CrossRef verify them, NLM's list gives journal abbreviations;
   only strict matches are rebuilt, everything else becomes a query.
9. **Author queries** (`edit_os/queries.py`): the pre-editing guide's wording, numbered AQ1..., written as
   Word comments in the tracked file and listed on a query sheet (`_queries.xlsx`).
10. **Evaluation** (`tools/evaluate.py`, `tools/eval_refs.py`): runs the rules on original manuscripts and scores them
   against a style editor's tracked files (`closure` = share of the editor's edits reproduced).

## Run

    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
    .venv/bin/python -m pytest
    .venv/bin/python -m edit_os paragraphs manuscript.docx
    .venv/bin/python -m edit_os refs manuscript.docx
    .venv/bin/python -m edit_os render manuscript.docx changes.json out/
    .venv/bin/python -m edit_os style manuscript.docx ijo out/     # tracked + clean + query sheet + QC
    .venv/bin/python tools/evaluate.py DATA_DIR ijo --per-rule
    .venv/bin/python -m edit_os style original.zip ijo out/          # with front matter from the zip
    .venv/bin/python tools/eval_frontmatter.py DATA_DIR all --by-journal
    .venv/bin/python -m edit_os edit original.zip ijo out/           # full run: style + copy edit, QC report
    .venv/bin/python -m edit_os render out/                          # re-render out/ from its records' statuses

## Service and review screen

`edit_os/pipeline.py` is the end-to-end run (`edit`): front matter, style rules and copy-edit checkers
propose changes and author queries; it writes `STEM_changes.json`, `_front_matter.json`, `_queries.json`,
a tracked copy (queries as Word comments), a clean copy (accepted changes only), the query sheet and a QC
report (`_qc_report.json` / `.html`: what ran, counts by layer and status, QC checks passed / failed).
Proposals start as `suggested`; `render_outputs` re-renders with the editor's decisions.

    .venv/bin/pip install -e '.[dev]'                  # adds fastapi, uvicorn, boto3
    (cd web && npm ci && npm run build)                # review screen -> web/dist, served by the API
    EDITOS_API_KEYS=dev-key .venv/bin/python -m server --port 8000    # http://127.0.0.1:8000
    docker compose up --build                          # API + LanguageTool (cp deploy/env.example .env)

API (`X-API-Key`, or a job's review token for that job): `POST /api/jobs` (multipart file + journal),
`POST /api/jobs/from-s3`, `GET /api/jobs[/{id}]`, `GET /api/jobs/{id}/changes` (with paragraph context),
`GET /api/jobs/{id}/queries`, `POST /api/jobs/{id}/decisions` (ids or a whole layer), `POST
/api/jobs/{id}/render`, `GET /api/jobs/{id}/qc`, `GET /api/jobs/{id}/files/{tracked|clean|queries|qc_report|qc_json|changes}`.
Also `GET /api/jobs/{id}/document`
(paragraphs in order with kind -- heading level, front matter, list, table cell, caption, reference -- and
bold / italic / superscript / subscript runs, for the page view) and `POST /api/jobs/{id}/changes` (the editor's own
correction of one paragraph, `{para_id, base, text}`: diffed word by word by `edit_os/docview.py` into accepted
`Change` records, source `editor`, saved in the job's records so every render includes them; 409 when it touches an
accepted suggestion or the paragraph changed since it was shown; overlapping pending suggestions are set aside).

The review screen (`web/`, React + Vite) opens on the manuscript as a page with every suggestion inline as a tracked
change: click one for Accept / Reject / Edit, click into any paragraph to type a correction, author queries sit in
the margin. Keys: `j`/`k` next / previous, `a` accept, `r` reject, `u` undo, `Esc`. A List toggle keeps the card
list for bulk work.

**Autopilot** (button on the review screen; `POST /api/jobs/{id}/autopilot`, `edit_os/autopilot.py`): accepts the
undecided suggestions it is sure of -- rule suggestions (journal style, verified references, spelling, LanguageTool, front
matter) unless the journal's `autopilot.hold` lists them, and Claude's copy edits only when a second Claude check agrees --
leaves the rest pending, keeps every author query and earlier decision, re-renders and runs QC. The report
(`_autopilot_report.json`, `GET .../autopilot`) lists what was accepted and why the rest was left;
`POST .../autopilot/undo` puts its acceptances back to pending.

Docs at `/docs`. Dashboard hook: `integrations/te_back/`. AWS: `docs/deploy-aws.md`.

## Roadmap

- [x] 1. Read and write `.docx` with tracked changes and formatting intact; find references
- [x] 2. Journal stylesheet as rules; QC gate (IJO first: 5.2% closure on 19 articles, 210 paragraphs
      improved, 31 worse; remaining gap is 88% references, which step 3 covers)
- [x] 3. Reference engine for journal articles (IJO: 40% of references identical to the editor's)
- [x] A1. Author queries as Word comments + query sheet; superscript citations (IJO overall 11.0%)
- [x] A2. Search-and-replace table from the pre-editing guide (`rules_guide.py`, `guide_replace:` in
      base.yaml; defaults set from how often editors make each edit)
- [x] A4. Headings / abstract / key words / back matter (`edit_os/rules_sections.py`, settings under
      `sections:`); inserted text can start new paragraphs (`tracked.PARAGRAPH_BREAK`) (IJO 11.6%)
- [x] A3. Tracked formatting changes (`Change.format`, `edit_os/rules_format.py`)
- [x] A5. Figure and table citations, captions, duplicate references, renumbering (`rules_figures.py`, `renumber.py`)
- [x] A6. Books, links and conference references (`edit_os/refs_other.py`)
- [x] A7. Front matter (`edit_os/frontmatter.py`): running head, type, title, authors, affiliations,
      correspondence, history dates, cite line, rebuilt from the title-page file, the submission XML and the
      manuscript; Claude Haiku only splits fields, code writes the text (`tools/eval_frontmatter.py`: 85% field
      similarity to the style editors on 160 articles, IJO 91%)
- [ ] B. Copy edit: LanguageTool (self-hosted), VarCon spelling, abbreviation checker, Claude pass
  - [x] B4. Claude pass (`edit_os/copyedit/claude_pass.py`, `copyedit.claude:` in base.yaml): chunks of
        ~1,500 words, structured edits + queries, each verified in code (exact unique `old`, size limits,
        digits / brackets / symbols / acronyms / names untouched) and trimmed to the changed words; source AI,
        runs last so rules and checkers win overlaps. Few-shot examples are real copy edits from training
        articles only (`EDITOS_CE_EXAMPLES`, never committed); prompt cached; answers cached by chunk text;
        `--batch` uses the Message Batches API. Default Claude Haiku 5.5 at effort medium: on 16 held-out
        articles 70% precision, 4.8 edits and $0.0025 per article (Sonnet 5.5 61%, $0.026; Opus 5.5 65%,
        $0.063, twice the edits). `tools/eval_ce.py DATA all --only claude --split test --model M`.
  - [x] B5. Copy-edit text rules (`edit_os/rules_text.py`, `text:` in base.yaml, a copy-edit checker): prefixes
        closed up, number words with time units, ranges after "between" / "from", statistics semicolons, capitalised
        common terms (off: editors split 1:2); per journal from the copy editors' text (IJO copy-edit closure 1.7% -> 11.9%)
- [ ] 4. Copy edit and style edit suggestions from Claude (structured, anchored to exact text)
- [x] 5. QC report output (`pipeline.py`: `_qc_report.json` / `.html`)
- [x] 6. Editor screen: accept or reject each suggestion (`server/` API, `web/` review screen)
- [ ] 7. Test set from past edited manuscripts to measure accuracy
