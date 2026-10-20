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
   (initials, et al., year/volume, issue, page ranges, capital after colon).
7. **QC gate** (`edit_os/qc.py`): checks the edited file; errors block it, warnings go to the
   editor's checklist.
8. **Evaluation** (`tools/evaluate.py`): runs the rules on original manuscripts and scores them
   against a style editor's tracked files (`closure` = share of the editor's edits reproduced).

## Run

    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
    .venv/bin/python -m pytest
    .venv/bin/python -m edit_os paragraphs manuscript.docx
    .venv/bin/python -m edit_os refs manuscript.docx
    .venv/bin/python -m edit_os render manuscript.docx changes.json out/
    .venv/bin/python -m edit_os style manuscript.docx ijo out/     # rules -> tracked + clean + QC
    .venv/bin/python tools/evaluate.py DATA_DIR ijo --per-rule

## Roadmap

- [x] 1. Read and write `.docx` with tracked changes and formatting intact; find references
- [x] 2. Journal stylesheet as rules; QC gate (IJO first: 5.2% closure on 19 articles, 210 paragraphs
      improved, 31 worse; remaining gap is 88% references, which step 3 covers)
- [ ] 3. Reference engine: DOI / PubMed ID lookup first, then strict search, CSL styling,
      statuses Matched / Format fixed / Query / Added by author
- [ ] 4. Copy edit and style edit suggestions from Claude (structured, anchored to exact text)
- [ ] 5. Query sheet and QC report outputs
- [ ] 6. Editor screen: accept or reject each suggestion
- [ ] 7. Test set from past edited manuscripts to measure accuracy
