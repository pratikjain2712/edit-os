# Edit OS

Python package `edit_os`. Tests: `.venv/bin/python -m pytest`.

- Never edit manuscript text in place. Produce `Change` records and render them with `tracked.render`.
- Offsets are into `Paragraph.text` (the text view from `docmodel.paragraph_text`); every change carries
  the `old` text it replaces so stale offsets fail loudly.
- AI suggests; editors decide. Nothing reaches a delivered file unless its status is `accepted`.
- QC checks are deterministic rules, not model calls.
- Never decide that a reference exists from model output alone; verify against PubMed / CrossRef.
- Manuscripts are client-confidential: keep real samples out of git (`samples/private/` is ignored).
