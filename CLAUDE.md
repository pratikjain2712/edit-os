# Edit OS

Python package `edit_os`. Tests: `.venv/bin/python -m pytest`.

- Never edit manuscript text in place. Produce `Change` records and render them with `tracked.render`.
- Offsets are into `Paragraph.text` (the text view from `docmodel.paragraph_text`); every change carries
  the `old` text it replaces so stale offsets fail loudly.
- AI suggests; editors decide. Nothing reaches a delivered file unless its status is `accepted`.
- QC checks are deterministic rules, not model calls.
- Never decide that a reference exists from model output alone; verify against PubMed / CrossRef.
- Manuscripts are client-confidential: keep real samples out of git (`samples/private/` is ignored).
- The journal's stylesheet (and the pre-editing guide) is the source of truth. Editors work by hand and miss
  things, so a stylesheet rule is applied even when editors often skip it; that counts as an editor miss, not a
  tool error. Use what editors consistently do only where the stylesheet is silent. When nearly all editors go
  against a stylesheet rule, keep the stylesheet and list the rule for the owner to confirm (it may be outdated).
