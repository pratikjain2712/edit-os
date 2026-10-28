"""What the agent is given: instructions, the journal's rules and examples, and the manuscript with paragraph ids."""

from __future__ import annotations

import json
import os
import random
import re

from .. import references, stylesheets
from ..copyedit import claude_pass
from ..docmodel import Manuscript, Paragraph
from ..style import JournalStyle

PROMPT_VERSION = "agent-5"

INSTRUCTIONS = """You are a senior copy editor for medical journals published by Wolters Kluwer-Medknow, working
on one manuscript at a time. The manuscript has already been style-edited and passed through deterministic
checkers (citations, numbers, units, reference list, headings, abbreviation expansion, British/American spelling).
Your job is what those cannot do: the copy editor's language edit and the author queries that need judgement.

What to edit (running text only):
- grammar: subject-verb agreement, singular/plural, tense consistency, missing or wrong articles,
  prepositions, word forms, dangling or broken constructions, missing small words
- punctuation: commas after introductory phrases and around non-restrictive clauses, serial commas,
  comma before "respectively", run-on sentences joined with a comma, doubled or misplaced punctuation
- standard misuses: which/that, since/because (cause), while/whereas (contrast), "in concordance to",
  "compared to/with", "on the other hand" without a first hand, etc.
- awkward or non-idiomatic wording that a professional copy editor would recast: you may reword a phrase
  or a clause (up to about 20 words) when it is clearly wrong or unclear English, keeping the meaning
  exactly. Never rewrite a paragraph or join/split many sentences; never shorten content.
- consistency within the article (the same term written the same way), sexist language, contractions.
- capitalisation of common nouns mid-sentence, obvious typos.

Never change: facts, meaning, numbers (including number words), units, statistics, P values, drug and gene
names, people's names, abbreviations/acronyms, citations ([1], (1), superscripts), table/figure labels, URLs,
headings, the reference list, or British/American spelling. Code checks every edit and refuses edits that touch
these; a refused edit is wasted budget.

Author queries (add_queries): write what an experienced copy editor would ask the author, politely and briefly,
in the journal's style ("Please check the meaning of this sentence.", "Kindly clarify whether ... or ...",
"Please check the placement of this text.", "Are you referring to ref. 3 here?"). Ask only where the author
must answer: genuinely ambiguous or contradictory statements, text that seems misplaced or duplicated, a figure
or table mentioned that does not exist, an author named with a citation number whose reference entry has a
different first author (call check_reference first; a query naming a reference is refused until you have), and
inconsistencies between sections (e.g. numbers in the abstract vs results, which you may NOT fix yourself).
Check every in-text "Author et al." (or "Author and Author") that comes with a citation number or year:
call check_reference for that number and query when the entry's first author or year differs ("Please check:
the author name does not match reference 12." / "Kindly check the year does not match with the reference
citation."). Query a figure or table that is never cited in the text, at its legend ("Please cite Figure 2 in
the text."). Do not ask for abbreviation expansions (a separate checker does) and do not query style matters you can fix.
A reference you have not verified with check_reference must never be called missing, wrong or fake.

How to work:
- The whole manuscript is in the first message: [P<n>] ids, with tags (heading), (table), (reference) and
  (read-only) for paragraphs you may query but not edit.
- Go through it section by section. Send many edits in one propose_edits call (all edits for a section or
  more), and many queries in one add_queries call; each item is answered with kept/refused and why. Fix a
  refused edit at most once, otherwise drop it.
- "old" is copied character for character from the paragraph and must be unique in it (add a neighbouring
  word if needed); "new" is its replacement. Keep "old" as short as the fix allows. One problem per edit.
- Be thorough on plain errors. These manuscripts are often written by authors whose first language is not
  English, and the journal's copy editors fix most errors of tense (what was done and found: past tense, in
  the Methods and Results above all), number (singular/plural), articles, prepositions, word forms and missing
  small words. Go sentence by sentence through every editable paragraph; long articles need many edits.
- confidence: "certain" when any professional copy editor would make the edit (a plain error); "likely" for an
  improvement most of this journal's copy editors would make, though the text is not wrong. Do not recast a
  correct sentence for style, do not change the author's capitalisation of a term for consistency unless the
  stylesheet asks for it, and do not change tense where the present is right (general truths, what a table shows).
- Do not swap correct words for synonyms or preferred phrasings (utilize/use, carried out/performed, while/
  although where both read correctly). Reword a clause only when it is ungrammatical or unclear as written.
- Do not add a full stop to list items, run-in subheadings, short labels or lines without a verb, and leave
  spacing and line breaks alone (the rules handle them).
- The work has a fixed budget. Aim to finish in about 6 tool rounds. When done, reply with one short line
  (no tool call)."""


def _sheet_text(style: JournalStyle) -> str:
    path = stylesheets.find_stylesheet(style.code, os.environ.get("EDITOS_STYLESHEETS"))
    if not path:
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()


def style_summary(style: JournalStyle) -> str:
    """The journal style settings that matter for a copy edit, in a few lines."""
    s = style.settings
    ce = s.get("copyedit") or {}
    cit = s.get("citations") or {}
    close = bool((ce.get("claude") or {}).get("close_prefixes", False))
    lines = [
        f"Journal: {s.get('name', style.code)} ({style.code.upper()})",
        f"Spelling convention: {(ce.get('spelling') or {}).get('convention', 'american')} (handled by the spelling "
        "checker; do not change spelling variants)",
        f"Citations: {'author-year' if cit.get('author_year') else 'numbered'}, {cit.get('brackets', 'square')} "
        "brackets (handled by the citation rules; never edit them)",
        "Prefixes (post-, pre-, non-, co-, re-): " + (
            "closed up per the stylesheet (postoperative, nonsignificant) unless the same vowel meets, a capital or "
            "numeral follows, or the base is hyphenated" if close else "leave them as the author wrote them"),
        "Compound adjectives before a noun take a hyphen; the noun 'follow-up' is hyphenated.",
    ]
    return "\n".join("- " + x for x in lines)


def _whole(word_before: str, old: str, new: str, word_after: str) -> tuple[str, str]:
    return word_before + old + word_after, word_before + new + word_after


def load_examples(path: str | None, journal: str, n: int = 60, exclude: set[str] = frozenset(),
                  seed: int = 0) -> list[dict]:
    """Real copy-editor edits (stage copy_edit, outside the references) from training-split articles only
    (claude_pass.is_test_article False, and never the article being edited), same journal first. Unlike the
    chunked pass's examples, word changes up to a clause are kept: the agent may reword a clause."""
    if not path or not os.path.exists(path):
        return []
    pool: dict[str, list[dict]] = {"same": [], "other": []}
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("stage") != "copy_edit" or r.get("in_refs") or claude_pass.is_test_article(r["article"]) \
                    or r["article"] in exclude:
                continue
            old, new, before, after = r["old"], r["new"], r.get("before", ""), r.get("after", "")
            if old == new or len(before) < 25 or len(after) < 8 or "Copyedit" in old + new:
                continue
            wb = re.search(r"\w*$", before).group()
            wa = re.match(r"\w*", after).group()
            fo, fn = _whole(wb, old, new, wa)
            if re.search(r"\d", fo + fn) or re.search(r"[A-Z]{2,}", fo + fn) or re.search(r"[()\[\]]", fo + fn):
                continue  # numbers, abbreviations, brackets: the rules' work
            if claude_pass._spelling_variant(fo, fn) or len(fo) + len(fn) > 160:
                continue
            words = len(re.findall(r"\w+", fo + " " + fn))
            ex = dict(before=before[:len(before) - len(wb)][-70:], old=fo, new=fn, after=after[len(wa):][:40],
                      size="clause" if words > 6 else "word")
            pool["same" if claude_pass._journal(r["article"]) == journal else "other"].append(ex)
    rng = random.Random(f"{journal}:{seed}")
    out: list[dict] = []
    for key in ("same", "other"):
        items = pool[key]
        rng.shuffle(items)
        clause = [e for e in items if e["size"] == "clause"]
        word = [e for e in items if e["size"] == "word"]
        want = n - len(out)
        take = clause[:want // 3] + word[:want - min(len(clause), want // 3)]
        rng.shuffle(take)
        out += take[:want]
        if len(out) >= n:
            break
    return out


def examples_text(examples: list[dict]) -> str:
    if not examples:
        return ""
    lines = [f"- ...{e['before']}[{e['old']} -> {e['new']}]{e['after']}..." for e in examples]
    return ("Real edits this journal's copy editors made (context, then [old -> new]). They show the kind and size "
            "of edit expected:\n" + "\n".join(lines))


def rules_text(style: JournalStyle, examples_path: str | None, n_examples: int = 60,
               exclude: set[str] = frozenset()) -> str:
    parts = ["# Journal style (summary)\n" + style_summary(style)]
    sheet = _sheet_text(style)
    if sheet:
        parts.append("# The journal's stylesheet (source of truth; struck-through text in it is still a live rule)\n"
                     + sheet)
    ex = examples_text(load_examples(examples_path, style.code.lower(), n_examples, exclude))
    if ex:
        parts.append("# Examples\n" + ex)
    return "\n\n".join(parts)


# --- the manuscript ------------------------------------------------------------------------------

def label(i: int) -> str:
    return f"P{i}"


def manuscript_text(ms: Manuscript, editable: set[str]) -> tuple[str, dict[str, Paragraph]]:
    """The manuscript as "[P<n>] (tag) text" lines, and the id -> paragraph map."""
    ref_start = references.find_heading(ms)
    ids: dict[str, Paragraph] = {}
    lines = []
    for i, p in enumerate(ms.paragraphs):
        if not p.text.strip():
            continue
        pid = label(i)
        ids[pid] = p
        if p.id in editable:
            tag = ""
        elif ref_start is not None and i > ref_start and not p.in_table:
            tag = "(reference) "
        elif p.in_table:
            tag = "(table) "
        elif claude_pass._is_heading(p):
            tag = "(heading) "
        else:
            tag = "(read-only) "
        lines.append(f"[{pid}] {tag}{p.text}")
    return "\n".join(lines), ids
