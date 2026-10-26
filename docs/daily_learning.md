# Daily learning

`tools/daily_learn.py` runs every afternoon in a scheduled Claude Code cloud session (a fresh container with this
repository checked out). It evaluates the articles the editors completed, compares the tool with their files and
writes a short report with what to change. It changes nothing on its own except, through `apply-safe`, which rules
Autopilot leaves for the editor.

## Usage

    python tools/daily_learn.py run [--journals ijo,jehp] [--max-new 30] [--backfill] [--dry-run]
    python tools/daily_learn.py apply-safe            # then commit / push / deploy only if it exits 0

Other `run` options: `--local-dir DIR` (articles from `DIR/<id>/` instead of the dashboard), `--prefix` (S3 prefix,
default `learning/`; use a test prefix for trials), `--workers N`, `--heldout-size N` (default 5),
`--reeval-heldout`, `--timeout SEC` (per article), `--no-cache-sync`, `--no-languagetool`, `--out DIR` (also write
the report locally). `--dry-run` reads S3 but writes nothing there.

Suggested schedule prompt (one run a day):

    cd <repo> && python tools/daily_learn.py run --max-new 30
    python tools/daily_learn.py apply-safe && git commit -am "Autopilot holds from daily learning" && <push/deploy>

The first run with `--backfill` stores every past completed article in S3 and queues it; each run then evaluates up
to `--max-new` of the queue (journals in the order of `tools/daily_learn.yaml`, newest first).

## Where things are

Credentials come from AWS Secrets Manager `edit-os/env` (region ap-south-1): `EDITINK_DASHBOARD_USER`,
`EDITINK_DASHBOARD_PASSWORD`, `EDITOS_ANTHROPIC_API_KEY`, `EDITOS_S3_BUCKET` (environment variables of the same
names are the fallback). They are never printed or written anywhere.

Everything else lives in S3 under `s3://$EDITOS_S3_BUCKET/learning/`, server-side encrypted, never in git:

| Key | What |
|---|---|
| `articles/<id>/{original.zip,te.docx,ce.docx,meta.json}` | the article as the dashboard has it |
| `state.json` | every article seen: queued / done / held-out / baseline, the commit it was evaluated with |
| `heldout.json` | the fixed held-out set per journal (picked once, never used for learning) |
| `results/<id>.json` | the article's metrics (numbers and masked snippets only) |
| `reports/<date>.md`, `.json`, `reports/latest.json` | the daily report; the JSON holds the safe actions |
| `history/<journal>.json` | the rolling per-journal metrics history (held-out, new, learning window) |
| `cache/*.sqlite.gz` | the pipeline's Claude / PubMed / CrossRef / LanguageTool caches, restored in a fresh container |

## What is measured

Per article, with the repository's own evaluation tools (imported, not shelled out): style closure against the style
editor's file (`tools/evaluate.py`, joined paragraphs), rule precision and bigram closure of text, references and front
matter against the final copy-edited file (`tools/eval_final.py`), references (`tools/eval_refs.py`), front-matter
fields (`tools/eval_frontmatter.py`), the editors' formatting (`tools/eval_format.py`) and the QC report (`qc.check`,
content-preservation checks included). The held-out set is evaluated again whenever the commit changes; that is the
trend line.

## What is learnt (tools/learn.py, deterministic)

- **Autopilot hold / release.** Over the last 30 evaluated articles of a journal, a rule whose suggestions are in the
  final file less than 80% of the time (at least 20 judged edits) is proposed for `autopilot.hold`; a hold this tool
  added is released once the rule is back above 90%. Holding never switches a rule off: it is still suggested and the
  editor decides. Holds the owner set are never released automatically (the report lists them when they recover), and
  rules the owner decided on are pinned in `tools/daily_learn.yaml`. The holds this tool added are named in a
  `# daily_learn holds` comment in the journal's style file.
- **New rule proposals.** Edits the editors make and the tool does not (small text edits, formatting changes, word
  pairs of the final file), with how many articles they appear in and how consistently the editors make them. Never
  applied: the stylesheet is the source of truth, and what editors do is only used where it is silent.
- **Owner questions.** Stylesheet rules that nearly all editors go against (final text keeps the author's wording in
  more than 80% of at least 20 cases). The tool keeps following the stylesheet; only the owner changes it.
- **Regressions.** A held-out metric dropping more than one point since the previous run on the same held-out set,
  more QC-blocked articles, and crashed steps.

## Confidentiality

Reports hold numbers, rule names and masked snippets of at most eight words (numbers, names, e-mails and links
masked); no manuscript text is printed or committed. Article files stay in the private bucket.

## Run time and cost

With warm caches an article takes about 15-30 s (one process); a cold one 30-100 s (PubMed / CrossRef lookups).
Claude (Haiku) costs well under a cent per article. With `--max-new 30` and 4 workers a daily run is about 10-15
minutes, plus the cache restore and LanguageTool start.
