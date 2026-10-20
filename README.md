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

## Run

    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
    .venv/bin/python -m pytest
    .venv/bin/python -m edit_os paragraphs manuscript.docx
    .venv/bin/python -m edit_os refs manuscript.docx
    .venv/bin/python -m edit_os render manuscript.docx changes.json out/

## Roadmap

- [x] 1. Read and write `.docx` with tracked changes and formatting intact; find references
- [ ] 2. Journal stylesheet as rules; QC gates that block a file until it passes
- [ ] 3. Reference engine: DOI / PubMed ID lookup first, then strict search, CSL styling,
      statuses Matched / Format fixed / Query / Added by author
- [ ] 4. Copy edit and style edit suggestions from Claude (structured, anchored to exact text)
- [ ] 5. Query sheet and QC report outputs
- [ ] 6. Editor screen: accept or reject each suggestion
- [ ] 7. Test set from past edited manuscripts to measure accuracy
