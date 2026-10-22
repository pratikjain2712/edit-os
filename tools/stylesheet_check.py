"""Three-way check: the tool vs the style editor vs the journal's stylesheet.

When the rules and the style editor disagree, the editor is not always right. For each article this
runs the style rules on the original (as tools/evaluate.py does), lists every disagreement at edit
level and asks Claude Haiku which side the journal's stylesheet and the pre-editing guide support:

  MISSED    an edit the editor made that the tool did not
  EXTRA     an edit the tool made that the editor did not
  CONFLICT  both changed the same words, differently

Body paragraphs are paired with the editor's tracked file by their original text (evaluate.py);
reference entries are paired by number (eval_refs.py). Identical patterns (same old -> new, same
punctuation to the left) are grouped per journal and judged once.

For each pattern, the stylesheet and guide lines are retrieved deterministically (BM25 over
stylesheet lines, with hint words from the kind of edit) and the top lines go to Claude with up to
three examples, the tool's rule name, the PubMed / CrossRef record the tool used (references) and
two corpus counts: how often the editor made this change elsewhere in the journal and how often it
left the same text alone. Classes:

  tool_gap             the editor follows a stylesheet rule the tool missed
  editor_missed        the tool follows a rule (or, for references, the PubMed / CrossRef record) the editor did not
  house_habit          the stylesheet is silent and the editor does it consistently
  editor_inconsistent  the editor does it both ways in the journal (only allowed when the counts show it)
  tool_error           the tool's edit is wrong and no rule supports it
  unclear

Every tool_gap / editor_missed verdict must quote the rule; code checks the quote occurs (whitespace,
quotes and case normalised) in the lines retrieved for that pattern (or in the reference record), or
the verdict becomes `unclear`. Answers are cached per pattern in SQLite under the edit-os cache dir.

The adjusted score counts what the tool got right against the editor-or-rules standard:
  raw      = matched / editor edits
  adjusted = (matched + editor misses the tool caught) / (editor edits + caught EXTRA edits)
(a CONFLICT or MISSED edit judged editor_missed -- the tool's version or its leaving the text alone follows
the rules -- counts as matched; it is already among the editor edits.)

Manuscripts, stylesheets and the guide are client material: they are read at runtime from the paths
given and the reports are written outside the repository (default ~/.cache/edit-os/stylesheet_check).

Usage:
  python tools/stylesheet_check.py DATA_DIR --journals ijo,ni [--stylesheets DIR] [--guide FILE]
         [--pair EDITOR.docx ORIGINAL.docx ...] [--out DIR] [--limit N] [--no-judge] [--model M]
  python tools/stylesheet_check.py --pair EDITOR.docx ORIGINAL.docx --journal ijo
Env: EDITOS_STYLESHEETS (directory of *.txt), EDITOS_PREEDIT_GUIDE (guide .txt), EDITOS_ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import dataclasses
import difflib
import glob
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evaluate import manuscript_from_zip, tracked_pairs  # noqa: E402
from eval_refs import editor_refs  # noqa: E402

from edit_os import ai_refs, docmodel, references, rules, style as style_mod, tracked  # noqa: E402
from edit_os.changes import Status  # noqa: E402
from edit_os.copyedit.cache import cache_dir  # noqa: E402
from edit_os.stylesheets import (Rule, RuleIndex, _canon, _words, find_stylesheet, load_rules,  # noqa: E402,F401
                                 quote_ok, stylesheet_codes)

try:
    from evaluate import apply_text  # noqa: E402
except ImportError:  # older / newer evaluate.py
    def apply_text(text: str, changes) -> str:
        out = text
        for c in sorted((c for c in changes if not getattr(c, "format_only", False)),
                        key=lambda c: (c.start, c.end), reverse=True):
            out = out[:c.start] + c.new + out[c.end:]
        return out

try:
    from edit_os.copyedit.claude_pass import PRICES  # noqa: E402
except ImportError:
    PRICES = {"claude-haiku-5-5": (0.10, 0.50, 0.10)}

PROMPT_VERSION = "ssc-5"
MODEL = "claude-haiku-5-5"
CLASSES = ["tool_gap", "editor_missed", "house_habit", "editor_inconsistent", "tool_error", "unclear"]
NEEDS_QUOTE = {"tool_gap", "editor_missed"}
# Classes that make sense for each kind of disagreement (anything else becomes `unclear`): the tool
# cannot be wrong where it made no edit, and a rule the tool missed needs an editor edit.
VALID = {
    "missed": {"tool_gap", "editor_missed", "house_habit", "editor_inconsistent", "unclear"},
    "extra": {"editor_missed", "tool_error", "editor_inconsistent", "unclear"},
    "conflict": set(CLASSES),
}
MIN_SHARE = 0.15  # editor_inconsistent: the less frequent way is at least this share of the cases
MAX_SHOW = 300  # characters of old / new shown per example


# --- edit-level disagreements ------------------------------------------------------------------

_TOK = re.compile(r"\w+|\s+|[^\w\s]")


def _tokens(s: str) -> list[str]:
    return _TOK.findall(s)


def _key(t: str) -> str:
    return " " if t.isspace() else t


def _edits(a: list[str], b: list[str]) -> list[tuple]:
    sm = difflib.SequenceMatcher(None, [_key(t) for t in a], [_key(t) for t in b], autojunk=False)
    return sm.get_opcodes()


def _map(ops, x: int, start: bool) -> int:
    """Token boundary x of the old side mapped to the new side: the widest mapping, so that edits
    touching the region's edges are inside it."""
    cands = []
    for op, i1, i2, j1, j2 in ops:
        if not (i1 <= x <= i2):
            continue
        if op == "equal":
            cands.append(j1 + x - i1)
        else:
            if x == i1:
                cands.append(j1)
            if x == i2:
                cands.append(j2)
            if i1 < x < i2:
                cands.append(j1 if start else j2)
    return (min if start else max)(cands)


@dataclass
class Region:
    start: int  # character offsets in the original text
    end: int
    old: str
    editor: str
    tool: str
    left: str  # the non-space token just before the region (context for grouping)

    @property
    def kind(self) -> str | None:
        e, t = self.editor != self.old, self.tool != self.old
        if e and t:
            return None if _norm_ws(self.editor) == _norm_ws(self.tool) else "conflict"
        return "missed" if e else "extra" if t else None


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def _shape(s: str) -> str:
    """Grouping form of an edit's text: whitespace runs as one space, digit runs as '#'."""
    return re.sub(r"\d+", "#", _norm_ws(s))


def regions(o: str, t: str, u: str, agreed: bool = False) -> list[Region]:
    """Every place where the editor (o -> t) or the tool (o -> u) changed the original, with what each
    made of it; overlapping or touching edits from either side form one region."""
    a, bt, bu = _tokens(o), _tokens(t), _tokens(u)
    ot, ou = _edits(a, bt), _edits(a, bu)
    spans = sorted([(i1, i2) for op, i1, i2, _, _ in ot + ou if op != "equal"])
    merged: list[list[int]] = []
    for i1, i2 in spans:
        if merged and i1 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], i2)
        else:
            merged.append([i1, i2])
    starts = [0]
    for tok in a:
        starts.append(starts[-1] + len(tok))
    out = []
    for i1, i2 in merged:
        ja, jb = _map(ot, i1, True), _map(ot, i2, False)
        ka, kb = _map(ou, i1, True), _map(ou, i2, False)
        left = next((a[k] for k in range(i1 - 1, -1, -1) if not a[k].isspace()), "")
        r = Region(starts[i1], starts[i2], "".join(a[i1:i2]), "".join(bt[ja:jb]), "".join(bu[ka:kb]), left)
        if r.kind or agreed and r.editor != r.old:
            out.append(r)
    return out


@dataclass
class Item:
    article: str
    section: str  # "text" or "ref"
    kind: str  # missed / extra / conflict
    where: str  # paragraph id or reference number
    old: str
    editor: str
    tool: str
    left: str
    context: str
    tool_rules: list[str] = field(default_factory=list)
    record: str = ""  # the PubMed / CrossRef record the tool used (references)
    entry: tuple = ()  # references: (original, editor's, tool's) whole entry
    moved: bool = False  # half of a move: the editor deleted this text in one place and inserted it in another

    def pattern(self) -> tuple:
        left = self.left if self.left and not self.left[0].isalnum() else ""
        return (self.section, self.kind + (" (move)" if self.moved else ""), left, _shape(self.old),
                _shape(self.editor), _shape(self.tool))


def _clip(s: str, n: int = MAX_SHOW) -> str:
    return s if len(s) <= n else s[:n] + "..."


def context(text: str, r: Region, width: int = 110) -> str:
    a, b = max(0, r.start - width), min(len(text), r.end + width)
    if r.kind == "missed":
        mark = f"[[{_clip(r.old)} => editor: {_clip(r.editor)}]]"
    elif r.kind == "extra":
        mark = f"[[{_clip(r.old)} => tool: {_clip(r.tool)}]]"
    else:
        mark = f"[[{_clip(r.old)} => editor: {_clip(r.editor)} | tool: {_clip(r.tool)}]]"
    return ("..." if a else "") + text[a:r.start] + mark + text[r.end:b] + ("..." if b < len(text) else "")


def _reasons(changes, lo: int, hi: int) -> list[str]:
    out = []
    for c in changes:
        if c.start < hi and lo < c.end or c.start == c.end and lo <= c.start <= hi:
            reason = (c.reason or "").split(" ")[0]
            if reason and reason not in out:
                out.append(reason)
    return out


def record_text(rec) -> str:
    if rec is None:
        return ""
    authors = ", ".join(f"{s} {i}".strip() for s, i in (rec.authors or [])[:8])
    ident = f"PMID {rec.pmid}" if rec.pmid else f"DOI {rec.doi}" if rec.doi else ""
    issue = f"({rec.issue})" if rec.issue else ""
    return (f"{rec.source} {ident}: {authors}. {rec.title}. {rec.journal} {rec.year};{rec.volume}{issue}:{rec.pages}"
            + (f". doi:{rec.doi}" if rec.doi and rec.pmid else ""))


_LABEL = re.compile(r"^\s*(\[\d+\]|\(\d+\)|\d+[.)]|\d+(?=\s))\s*")


def _wordset(s: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", s.lower()))


def similar(a: str, b: str) -> float:
    """Share of words two reference entries have in common (Jaccard)."""
    x, y = _wordset(a), _wordset(b)
    return len(x & y) / len(x | y) if x | y else float(a.strip() == b.strip())


def pair_refs(orig: list[tuple[int | None, str]], ed: dict[int, str], min_overlap: float = 0.35) -> dict[int, int]:
    """Original reference number -> the editor's number for the same entry: the same number when the
    texts agree, otherwise the unpaired editor entry sharing most words (editors renumber and
    retype entries)."""
    out, used = {}, set()
    for n, text in orig:
        if n and n in ed and similar(text, ed[n]) >= 0.5:
            out[n] = n
            used.add(n)
    cands = []
    for n, text in orig:
        if not n or n in out:
            continue
        for m, t in ed.items():
            if m in used:
                continue
            j = similar(text, t)
            if j >= min_overlap:
                cands.append((j, n, m))
    for j, n, m in sorted(cands, reverse=True):
        if n not in out and m not in used:
            out[n] = m
            used.add(m)
    return out


def run_article(name: str, original: bytes, editor: bytes, journal: str, zip_path: str | None = None) -> dict:
    """The tool's run on one original and every disagreement with the editor's tracked file."""
    st = style_mod.load(journal)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "m.docx")
        with open(path, "wb") as f:
            f.write(original)
        ms = docmodel.load(path)
        if zip_path:
            try:
                from edit_os import frontmatter
                ms.front_sources = frontmatter.sources_from_zip(zip_path)
            except Exception:  # noqa: BLE001  (front matter sources are optional)
                pass
        originals = [(p.id, p.text) for p in ms.paragraphs]
        refs = [r for r in references.extract(ms)]
        ref_by_para = {r.para_id: r for r in refs}
        try:
            records = {r.number: res for r, res in rules.lookup_references(ms, st) if r.number}
        except Exception:  # noqa: BLE001
            records = {}
        cs = rules.run_all(ms, st)
        for c in cs.changes:
            c.status = Status.ACCEPTED
        by_para = collections.defaultdict(list)
        for c in cs.changes:
            by_para[c.para_id].append(c)
        tracked.render(docmodel.load(path), cs, tracked=False)  # refuses stale or overlapping changes
        ours = {pid: apply_text(text, by_para.get(pid, [])) for pid, text in originals}

    items: list[Item] = []
    stats = collections.Counter(tool_changes=len(cs.changes))
    texts = {"text": [], "ref": []}  # original text the editor saw, for the corpus counts
    editor_regions: list[tuple[str, str, str, str]] = []  # (section, left, old, new) the editor made

    def add(section: str, where: str, o: str, t: str, u: str, changes, offset: int = 0, record: str = ""):
        entry = (o, t, u) if section == "ref" else ()
        texts[section].append(o)
        rs = regions(o, t, u, agreed=True)
        deleted = {r.old.strip() for r in rs if r.old.strip() and not r.editor.strip()}
        moves = {r.editor.strip() for r in rs if r.editor.strip() and not r.old.strip()} & deleted
        for r in rs:
            if not (r.old + r.editor + r.tool).strip():
                stats[f"{section}:whitespace_only"] += 1  # spacing, tabs: not judged, not scored
                continue
            if r.editor != r.old:
                left = r.left if r.left and not r.left[0].isalnum() else ""
                editor_regions.append((section, left, _shape(r.old), _shape(r.editor)))
                stats[f"{section}:editor_edits"] += 1  # matched + missed + conflict
            if r.kind is None:
                stats[f"{section}:matched"] += 1
                continue
            stats[f"{section}:{r.kind}"] += 1
            moved = r.kind == "missed" and (r.editor.strip() or r.old.strip()) in moves
            items.append(Item(name, section, r.kind, where, r.old, r.editor, r.tool, r.left, context(o, r),
                              _reasons(changes, r.start + offset, r.end + offset), record, entry, moved))

    queue: dict[str, list[str]] = {}
    for before, after in tracked_pairs(editor):
        queue.setdefault(before.strip(), []).append(after)
    for pid, o in originals:
        q = queue.get(o.strip())
        if not o.strip() or not q:
            continue
        t = q.pop(0)
        if pid in ref_by_para or not t.strip():
            continue
        stats["paragraphs_paired"] += 1
        add("text", pid, o, t, ours[pid], by_para.get(pid, []))
    stats["paragraphs"] = len(originals)

    ed = editor_refs(editor)
    pairs = pair_refs([(r.number, r.text) for r in refs], ed)
    stats["refs_not_paired"] = sum(1 for r in refs if r.number not in pairs)
    def entry(r) -> str:
        u_para = ours.get(r.para_id, "")
        m = _LABEL.match(u_para) if r.label else None
        return u_para[m.end():].strip() if m else u_para.strip() if r.label else u_para[r.offset:].strip()

    # The tool's version of an entry: renumbering rewrites the list slot by slot, so after a reorder
    # an entry's text lands in another reference paragraph. Pair each original entry with the tool
    # entry sharing most words (its own slot when nothing moved).
    tool_entry, taken = {}, set()
    cands = sorted(((similar(r.text, entry(t)), i, j) for i, r in enumerate(refs) for j, t in enumerate(refs)),
                   key=lambda c: (-c[0], c[1] != c[2]))
    for score, i, j in cands:
        if i not in tool_entry and j not in taken and (score >= 0.35 or i == j):
            tool_entry[i] = j
            taken.add(j)
    for i, r in enumerate(refs):
        if r.number not in pairs:
            continue
        n = pairs[r.number]
        landed = refs[tool_entry.get(i, i)]
        u = entry(landed)
        stats["refs_paired"] += 1
        res = records.get(r.number)
        rec = record_text(getattr(res, "record", None)) if res else ""
        if res is not None:
            rec = f"[tool lookup: {res.status}] {rec}".strip()
        add("ref", f"ref {n}", r.text, ed[n], u, by_para.get(landed.para_id, []), r.offset, rec)
    return {"article": name, "journal": journal, "items": items, "stats": stats, "texts": texts,
            "editor_regions": editor_regions}


# --- corpus counts: does the editor do it both ways? -------------------------------------------

def _countable(old: str) -> bool:
    return bool(re.search(r"[A-Za-z]{2,}", old)) and len(old.strip()) >= 3


def corpus_counts(articles: list[dict]) -> tuple[collections.Counter, dict]:
    """(editor changes by (section, left, old, new), all original texts by section)."""
    made = collections.Counter()
    texts = collections.defaultdict(list)
    for a in articles:
        made.update(a["editor_regions"])
        for k, v in a["texts"].items():
            texts[k] += v
    return made, texts


def _both_ways(did: int, kept: int) -> bool:
    return did > 0 and kept > 0 and min(did, kept) / (did + kept) >= MIN_SHARE


def consistency(pattern: tuple, n: int, made: collections.Counter, texts: dict, cache: dict) -> dict:
    """How often the editor made this pattern's change elsewhere in the journal and how often it
    left the same original text alone; `inconsistent` when both happen."""
    section, kind, left, old, ed_new, tool_new = pattern
    if kind.startswith("extra"):
        did = sum(v for (s, lf, o, nw), v in made.items()
                  if s == section and o == old and nw == tool_new and (not left or lf == left))
        kept = n
        return {"editor_made_elsewhere": did, "editor_left_unchanged": kept, "inconsistent": _both_ways(did, kept),
                "change": f"{old!r} -> {tool_new!r}"}
    did = sum(v for (s, lf, o, nw), v in made.items()
              if s == section and o == old and nw == ed_new and (not left or lf == left))
    if not _countable(old):
        return {"editor_made_elsewhere": did, "editor_left_unchanged": None, "inconsistent": False,
                "change": f"{old!r} -> {ed_new!r}"}
    key = (section, left, old)
    if key not in cache:
        body = re.escape(old).replace(r"\#", r"\d+")
        rx = re.compile((re.escape(left) + r"\s*" if left else r"(?<!\w)") + body + r"(?!\w)")
        cache[key] = sum(len(rx.findall(t)) for t in texts.get(section, []))
    changed_any = sum(v for (s, lf, o, _), v in made.items() if s == section and o == old and (not left or lf == left))
    kept = max(0, cache[key] - changed_any)
    return {"editor_made_elsewhere": did, "editor_left_unchanged": kept, "inconsistent": _both_ways(did, kept),
            "change": f"{old!r} -> {ed_new!r}"}


# --- stylesheet and guide retrieval ------------------------------------------------------------

_SPELL_PAIRS = [("our", "or"), ("ise", "ize"), ("yse", "yze"), ("ll", "l"), ("tre", "ter"), ("ae", "e"), ("oe", "e"),
                ("ogue", "og"), ("ence", "ense")]


def hints(item: Item) -> list[str]:
    """Words that point retrieval at the kind of rule an edit is about."""
    old, ed, tl = item.old, item.editor, item.tool
    new = ed if item.kind != "extra" else tl
    both = old + " " + ed + " " + tl
    h = []
    if item.section == "ref":
        h += "reference journal author et al volume page number pubmed year title".split()
    if old.lower() == new.lower() and old != new or (old.strip() and new.strip() and old.strip()[0].lower() == new.strip()[0].lower()
                                                      and old.strip()[0] != new.strip()[0]):
        h += "case capital upper lower sentence title caps".split()
    if ":" in item.left + both:
        h += ["colon"]
    if re.search(r"[–—-]", both):
        h += "dash hyphen en range".split()
    if "," in both:
        h += ["comma", "serial"]
    if ";" in both:
        h += ["semicolon"]
    if re.search(r"\d", both):
        h += ["number", "numeral"] + (["unit", "value", "space"] if item.section == "text" else ["page", "volume"])
    if "%" in both:
        h += ["percent"]
    if re.search(r"[\[\]()]", both):
        h += "citation bracket square parenthese".split()
    if "<H" in both or "<h" in both:
        h += "heading h1 h2 h3".split()
    if re.search(r"[‘’“”\"']", both):
        h += ["quote"]
    if "." in both:
        h += ["period"]
    ow, nw = re.findall(r"[A-Za-z]+", old), re.findall(r"[A-Za-z]+", new)
    if len(ow) == len(nw) == 1 and ow[0].lower() != nw[0].lower() and \
            difflib.SequenceMatcher(None, ow[0].lower(), nw[0].lower()).ratio() > 0.75:
        h += "spelling american british".split()
    if "et al" in both:
        h += ["et", "al"]
    for r in item.tool_rules:
        h += re.split(r"[_.:\s]+", r)
    ctx = item.context.lower()
    for w in ("abstract", "keyword", "table", "figure", "acknowledg", "correspondence", "affiliation", "financial",
              "conflict", "running"):
        if w in ctx[:200]:
            h.append(w)
    return [w for w in _words(" ".join(h))]


_REF_LIKE = re.compile(r"\b(?:19|20)\d\d\s*;\s*\d+")  # "2017;9:" -- a reference outside the list


def retrieve(item_or_items, sheet: RuleIndex | None, guide: RuleIndex | None, k_sheet: int = 6,
             k_guide: int = 3) -> list[Rule]:
    items = item_or_items if isinstance(item_or_items, list) else [item_or_items]
    terms = []
    for it in items[:1]:
        terms += hints(it) * 2
    for it in items:
        terms += _words(" ".join([it.old, it.editor, it.tool]))
    out = []
    if sheet:
        out += sheet.search(terms, k_sheet)
        if any(it.section == "ref" or _REF_LIKE.search(it.context) for it in items):
            out += [r for r in sheet.examples() if r not in out]
    if guide:
        out += guide.search(terms, k_guide)
    return out


# --- the judge ----------------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": CLASSES},
                    "rule_id": {"type": "string"},
                    "rule_quote": {"type": "string"},
                    "record_quote": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["id", "verdict", "rule_id", "rule_quote", "record_quote", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You audit disagreements between an automatic style-editing tool and a human style editor at a
medical journal (Wolters Kluwer-Medknow). Neither side is assumed right: the journal's stylesheet and the
pre-editing guide decide. Each item is either a PATTERN (one difference seen in one or more places, with
examples) or a REFERENCE ENTRY (one reference with all its differences). Differences are numbered d1, d2, ...
For each difference you get:
- the disagreement: MISSED (the editor changed OLD, the tool left it), EXTRA (the tool changed OLD, the editor
  left it) or CONFLICT (both changed it, differently), with up to three examples in context, marked [[...]];
- how often the editor made this same change elsewhere in the journal and how often it left the same text alone;
- for reference entries, the PubMed / CrossRef record the tool looked up, when there is one;
- the stylesheet (S...) and guide (G...) lines retrieved for it. Only these lines count as rules. Lines labelled
  "Journals" are the stylesheet's example references: their form (punctuation, hyphen or en dash in page ranges,
  issue number or not, abbreviated journal name) is the journal's rule for reference entries, and may be quoted.

Classify each difference as exactly one of:
- tool_gap: the editor's change follows one of the given lines and the tool did not apply it.
- editor_missed: the tool's change follows one of the given lines, or (references) the record shows the tool's
  text is factually right (author initials, volume, issue, pages, year, published issue instead of early online)
  and the editor did not make it. For a MISSED item: the editor's change goes against a given line (e.g. British
  spelling where the journal asks for American) and the tool was right to leave the text.
- house_habit: the editor made the change but no given line asks for it, and the editor is consistent.
- editor_inconsistent: no given line settles it and the counts show the editor does it both ways.
- tool_error: the tool's change is wrong (introduces an error or goes against a given line) and nothing supports it.
  Only for EXTRA and CONFLICT items: where the tool made no change it cannot be in error.
- unclear: you cannot tell.
rule_id / rule_quote: for tool_gap and editor_missed (and tool_error when a line contradicts the tool), the id of
the line and a verbatim span of 4 to 30 words copied from it, character for character. Otherwise "".
record_quote: for references decided on the record, a verbatim span copied from the record; otherwise "".
The quoted line must be about this kind of edit in this part of the paper; a nearby line does not count:
- a rule for ranges inside citation brackets does not decide page ranges, years or hyphens in compound words;
- rules for the article title, running head or headings do not decide reference titles or body text;
- a rule about round brackets around citations does not decide issue numbers or other brackets;
- general checklist lines (fix spelling, fix capitalisation inconsistencies, apply the journal's style, check grammar
  and punctuation) are too general to decide any specific edit: treat them as no rule.
When no given line is about the edit, the stylesheet is silent: use house_habit, editor_inconsistent, tool_error
or unclear, never tool_gap or editor_missed (except editor_missed on a reference record's facts).
why: one short sentence. Return JSON only, one verdict per difference, with id "<item>.<difference>", e.g. "i3.d2"."""


def _kind(g: dict) -> str:
    return g["pattern"][1].split(" ")[0]


def _diff_line(g: dict, label: str, with_counts: bool = True) -> str:
    kind = _kind(g)
    ex = g["examples"][0]
    parts = [f"{label}: {kind.upper()}  OLD {_clip(ex.old)!r}"]
    if kind != "extra":
        parts.append(f"EDITOR {_clip(ex.editor)!r}")
    if kind != "missed":
        parts.append(f"TOOL {_clip(ex.tool)!r}" + (f" (tool rule: {', '.join(ex.tool_rules)})" if ex.tool_rules else ""))
    line = "  ".join(parts)
    if ex.moved:
        line += ("\n  Part of a MOVE: in the same paragraph the editor deleted this text in one place and inserted it in "
                 "another; judge the move as a whole (both halves get the same verdict).")
    if with_counts:
        c = g["consistency"]
        left = "unknown" if c["editor_left_unchanged"] is None else c["editor_left_unchanged"]
        line += (f"\n  {g['n']} time(s) in {len(g['articles'])} article(s). In this journal the editor made "
                 f"{c['change']} {c['editor_made_elsewhere']} time(s) and left the original {left} time(s).")
    return line


def unit_payload(unit: dict) -> str:
    """The text for one unit (also its cache key, with the prompt and model): a pattern with examples,
    or one reference entry with all its differences."""
    gs = unit["groups"]
    if unit["kind"] == "entry":
        ex = gs[0]["examples"][0]
        o, t, u = ex.entry
        lines = [f"REFERENCE ENTRY ({ex.where})", f"Original: {_clip(o, 600)}", f"Editor:   {_clip(t, 600)}",
                 f"Tool:     {_clip(u, 600)}", f"Record:   {ex.record or 'none (the tool found no record)'}"]
        lines += [_diff_line(g, f"d{k}") for k, g in enumerate(gs, 1)]
    else:
        g = gs[0]
        lines = [f"PATTERN in {'reference entries' if g['pattern'][0] == 'ref' else 'body text'}", _diff_line(g, "d1")]
        for i, e in enumerate(g["examples"], 1):
            lines.append(f"Example {i} ({e.where}): {e.context}")
            if e.record:
                lines.append(f"  Record: {e.record}")
    lines.append("Rules:")
    lines += [f"  {r.id}: {r.text}" for r in unit["rules"]]
    return "\n".join(lines)


def build_units(groups: list[dict], max_diffs: int = 10) -> list[dict]:
    """One unit per pattern, except reference differences seen once: those are judged per entry,
    together, with the entry and its record shown once."""
    units, entries = [], collections.OrderedDict()
    for g in groups:
        if g["pattern"][0] == "ref" and g["n"] == 1:
            e = g["examples"][0]
            entries.setdefault((e.article, e.where), []).append(g)
        else:
            units.append({"kind": "pattern", "groups": [g]})
    for gs in entries.values():
        gs.sort(key=lambda g: g["examples"][0].context)
        for i in range(0, len(gs), max_diffs):
            units.append({"kind": "entry", "groups": gs[i:i + max_diffs]})
    return units


class _Cache:
    def __init__(self, path: str | None = None):
        self.con = sqlite3.connect(path or cache_dir("stylesheet_check.sqlite"), timeout=60, check_same_thread=False)
        self.con.execute("CREATE TABLE IF NOT EXISTS verdicts (key TEXT PRIMARY KEY, body TEXT)")

    def get(self, key: str) -> dict | None:
        row = self.con.execute("SELECT body FROM verdicts WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, body: dict) -> None:
        self.con.execute("INSERT OR REPLACE INTO verdicts VALUES (?, ?)", (key, json.dumps(body)))
        self.con.commit()


USAGE = collections.Counter()  # this process: calls, input / output / cache tokens


def cost(model: str = MODEL) -> float:
    pin, pout, read = PRICES.get(model, PRICES.get(MODEL, (0.10, 0.50, 0.10)))
    u = USAGE
    return (u["input_tokens"] * pin + u["cache_creation_input_tokens"] * pin * 1.25
            + u["cache_read_input_tokens"] * pin * read + u["output_tokens"] * pout) / 1e6


def _call(model: str, payload: str, effort: str = "high") -> dict:
    import anthropic

    client = anthropic.Anthropic(
        api_key=ai_refs._api_key(),
        base_url=os.environ.get("EDITOS_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    )
    params = dict(
        model=model,
        max_tokens=16000,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        system=INSTRUCTIONS,
        messages=[{"role": "user", "content": payload}],
    )
    if model in getattr(ai_refs, "_FALLBACK_MODELS", ()):
        # A declined request is re-run on Anthropic's recommended fallback model.
        response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
    else:
        response = client.messages.create(**params)
    USAGE["calls"] += 1
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        USAGE[k] += getattr(response.usage, k, 0) or 0
    if response.stop_reason in ("refusal", "max_tokens"):
        USAGE[response.stop_reason] += 1
        return {"verdicts": []}
    try:
        return json.loads(next((b.text for b in response.content if b.type == "text"), ""))
    except ValueError:
        return {"verdicts": []}


def check_verdict(v: dict, group: dict, rules_: list[Rule]) -> dict:
    """The verdict after code checks: tool_gap / editor_missed need a quote found in the retrieved lines
    (or the reference record); editor_inconsistent needs the counts to show it."""
    verdict = v.get("verdict") if v.get("verdict") in CLASSES else "unclear"
    quote, rec_quote = str(v.get("rule_quote") or ""), str(v.get("record_quote") or "")
    by_id = {r.id: r.text for r in rules_}
    rule_ok = quote_ok(quote, [by_id[v["rule_id"]]] if v.get("rule_id") in by_id else [r.text for r in rules_]) \
        or quote_ok(quote, [r.text for r in rules_])
    records = [e.record for e in group["examples"] if e.record]
    rec_ok = bool(records) and quote_ok(rec_quote, records)
    note = ""
    kind = _kind(group)
    if verdict not in VALID[kind]:
        note = f"downgraded from {verdict}: not possible for a {kind} edit"
        verdict = "unclear"
    elif verdict in NEEDS_QUOTE and not rule_ok and not (verdict == "editor_missed" and group["pattern"][0] == "ref" and rec_ok):
        note = f"downgraded from {verdict}: quote not found in the retrieved rules"
        verdict = "unclear"
    elif verdict == "editor_inconsistent" and not group["consistency"]["inconsistent"]:
        note = "downgraded from editor_inconsistent: counts do not show it"
        verdict = "unclear"
    rule_id = v.get("rule_id") if rule_ok and v.get("rule_id") in by_id else ""
    if rule_ok and not rule_id:
        rule_id = next((r.id for r in rules_ if quote_ok(quote, [r.text])), "")
    return {"verdict": verdict, "model_verdict": v.get("verdict"), "rule_id": rule_id,
            "rule_quote": quote if rule_ok else "", "rule_text": by_id.get(rule_id, ""),
            "record_quote": rec_quote if rec_ok else "", "why": str(v.get("why") or "")[:300], "note": note}


def _unjudged(note: str) -> dict:
    return {"verdict": "unclear", "model_verdict": None, "rule_id": "", "rule_quote": "", "rule_text": "",
            "record_quote": "", "why": "", "note": note}


def judge(groups: list[dict], sheet: RuleIndex | None, guide: RuleIndex | None, model: str = MODEL,
          batch_size: int = 12, call=None, cache: _Cache | None = None, workers: int = 6,
          effort: str = "high", log=print) -> dict:
    """Fill group["judgement"] for every group: cached answers first, the rest in batched requests
    (about `batch_size` differences each). Returns counts of units and requests."""
    cache = cache or _Cache()
    units = build_units(groups)
    todo = []
    for u in units:
        items = [e for g in u["groups"] for e in g["examples"]]
        u["rules"] = retrieve(items, sheet, guide, k_sheet=8 if u["kind"] == "entry" else 6)
        for g in u["groups"]:
            g["rules"] = u["rules"]
        u["payload"] = unit_payload(u)
        u["key"] = hashlib.sha256(json.dumps([PROMPT_VERSION, model, effort, INSTRUCTIONS, u["payload"]]).encode()).hexdigest()
        got = cache.get(u["key"])
        if got is not None:
            for k, g in enumerate(u["groups"], 1):
                v = got.get(f"d{k}")
                g["judgement"] = check_verdict(v, g, u["rules"]) if v else _unjudged("no answer")
        else:
            todo.append(u)
    batches, cur, size = [], [], 0
    for u in todo:
        if cur and size + len(u["groups"]) > batch_size:
            batches.append(cur)
            cur, size = [], 0
        cur.append(u)
        size += len(u["groups"])
    if cur:
        batches.append(cur)
    if batches:
        log(f"  judging {sum(len(u['groups']) for u in todo)} differences in {len(todo)} units, "
            f"{len(batches)} requests ({len(units) - len(todo)} units cached)")

    def run(batch):
        payload = "\n\n".join(f"### Item i{k}\n{u['payload']}" for k, u in enumerate(batch, 1))
        try:
            return batch, (call or _call)(model, payload, effort), None
        except Exception as e:  # noqa: BLE001  (network / API error: those patterns stay unjudged)
            return batch, None, e

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for batch, body, err in pool.map(run, batches):
            got = {} if err else {str(v.get("id")).strip(): v for v in (body or {}).get("verdicts") or []
                                  if isinstance(v, dict)}
            for k, u in enumerate(batch, 1):
                answers = {}
                for j, g in enumerate(u["groups"], 1):
                    v = got.get(f"i{k}.d{j}") or (got.get(f"i{k}") if len(u["groups"]) == 1 else None)
                    if v is None:
                        g["judgement"] = _unjudged(f"not judged ({type(err).__name__})" if err else "no answer")
                        continue
                    answers[f"d{j}"] = v
                    g["judgement"] = check_verdict(v, g, u["rules"])
                if len(answers) == len(u["groups"]):
                    cache.put(u["key"], answers)
    return {"units": len(units), "requests": len(batches)}


# --- grouping and the report -------------------------------------------------------------------

def group_items(articles: list[dict], max_examples: int = 3) -> list[dict]:
    made, texts = corpus_counts(articles)
    groups: dict[tuple, dict] = {}
    for a in articles:
        for it in a["items"]:
            g = groups.setdefault(it.pattern(), {"pattern": it.pattern(), "n": 0, "articles": [], "items": []})
            g["n"] += 1
            g["items"].append(it)
            if it.article not in g["articles"]:
                g["articles"].append(it.article)
    cache: dict = {}
    out = []
    for g in groups.values():
        seen, ex = set(), []
        for it in g["items"]:  # examples from different articles first
            if it.article not in seen:
                ex.append(it)
                seen.add(it.article)
        ex += [it for it in g["items"] if it not in ex]
        g["examples"] = ex[:max_examples]
        g["consistency"] = consistency(g["pattern"], g["n"], made, texts, cache)
        out.append(g)
    out.sort(key=lambda g: (-g["n"], g["pattern"]))
    return out


def summarise(journal: str, articles: list[dict], groups: list[dict]) -> dict:
    st = collections.Counter()
    for a in articles:
        st.update(a["stats"])
    occ = collections.Counter()  # (kind, class) -> occurrences
    pats = collections.Counter()
    for g in groups:
        cls = g.get("judgement", {}).get("verdict", "not_judged")
        occ[(_kind(g), cls)] += g["n"]
        pats[(_kind(g), cls)] += 1
    matched = st["text:matched"] + st["ref:matched"]
    editor_edits = st["text:editor_edits"] + st["ref:editor_edits"]
    caught_extra = occ[("extra", "editor_missed")]
    caught_other = occ[("conflict", "editor_missed")] + occ[("missed", "editor_missed")]  # already editor edits
    raw = matched / editor_edits if editor_edits else 0.0
    adjusted = (matched + caught_extra + caught_other) / (editor_edits + caught_extra) if editor_edits else 0.0
    classes = {c: {k: occ[(k, c)] for k in ("missed", "extra", "conflict")} for c in CLASSES + ["not_judged"]}
    patterns = {c: {k: pats[(k, c)] for k in ("missed", "extra", "conflict")} for c in CLASSES + ["not_judged"]}
    fix = [g for g in groups if g.get("judgement", {}).get("verdict") == "tool_gap"]
    errors = [g for g in groups if g.get("judgement", {}).get("verdict") == "tool_error"]
    caught = [g for g in groups if g.get("judgement", {}).get("verdict") == "editor_missed"]
    return {
        "journal": journal,
        "articles": [a["article"] for a in articles],
        "stats": dict(st),
        "editor_edits": editor_edits,
        "matched": matched,
        "missed": st["text:missed"] + st["ref:missed"],
        "extra": st["text:extra"] + st["ref:extra"],
        "conflict": st["text:conflict"] + st["ref:conflict"],
        "caught": caught_extra + caught_other,
        "raw_score": round(raw, 4),
        "adjusted_score": round(adjusted, 4),
        "classes_by_occurrence": classes,
        "classes_by_pattern": patterns,
        "fix_list": [_group_json(g) for g in fix],
        "tool_errors": [_group_json(g) for g in errors],
        "editor_misses": [_group_json(g) for g in caught],
    }


def _group_json(g: dict) -> dict:
    section, kind, left, old, ed, tl = g["pattern"]
    return {"section": section, "kind": kind, "left": left, "old": old, "editor": ed, "tool": tl, "n": g["n"],
            "articles": g["articles"], "consistency": g["consistency"], "judgement": g.get("judgement"),
            "rules": [dataclasses.asdict(r) for r in g.get("rules", [])],
            "examples": [{"article": e.article, "where": e.where, "context": e.context, "tool_rules": e.tool_rules,
                          "record": e.record} for e in g["examples"]]}


def _short(s: str, n: int = 60) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def markdown(summary: dict, groups: list[dict], usage: dict) -> str:
    s = summary
    out = [f"# Stylesheet check: {s['journal'].upper()}", "",
           f"Articles: {len(s['articles'])}. Editor edits {s['editor_edits']}, matched {s['matched']}, "
           f"missed {s['missed']}, extra {s['extra']}, conflict {s['conflict']}.", "",
           f"**Raw score** (editor edits matched): {s['raw_score']:.1%}  ",
           f"**Adjusted score** (matched + editor misses caught): {s['adjusted_score']:.1%} "
           f"({s['caught']} caught)", "",
           f"Judge: {usage.get('calls', 0)} requests, {usage.get('input_tokens', 0)} in / "
           f"{usage.get('output_tokens', 0)} out tokens, ${usage.get('cost', 0):.4f}", "",
           "## Verdicts (occurrences; patterns in brackets)", "",
           "| class | missed | extra | conflict |", "|---|---:|---:|---:|"]
    for c in CLASSES + ["not_judged"]:
        o, p = s["classes_by_occurrence"][c], s["classes_by_pattern"][c]
        if sum(o.values()):
            out.append(f"| {c} | " + " | ".join(f"{o[k]} ({p[k]})" for k in ("missed", "extra", "conflict")) + " |")
    for title, key in (("Fix list: tool gaps", "fix_list"), ("Tool errors", "tool_errors"),
                       ("Editor misses the tool caught", "editor_misses")):
        out += ["", f"## {title}", "", "| # | n | where | change | rule | example |", "|---:|---:|---|---|---|---|"]
        for i, g in enumerate(s[key], 1):
            new = g["editor"] if g["kind"] != "extra" else g["tool"]
            j = g["judgement"] or {}
            quote = j.get("rule_quote") or j.get("record_quote") or j.get("why", "")
            ex = g["examples"][0]
            out.append(f"| {i} | {g['n']} | {g['section']} {g['kind']} | `{_short(g['left'] + ' ' + g['old'], 40)}` → "
                       f"`{_short(new, 40)}` | {j.get('rule_id', '')} “{_short(quote, 140)}” | "
                       f"{ex['article']} {ex['where']}: {_short(ex['context'], 140).replace('|', '/')} |")
    return "\n".join(out) + "\n"


def html_report(summary: dict, md_text: str) -> str:
    rows = []
    for line in md_text.splitlines():
        e = html.escape(line)
        if line.startswith("# "):
            rows.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            rows.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("|---"):
            continue
        elif line.startswith("|"):
            cells = [html.escape(c.strip()) for c in line.strip("|").split("|")]
            tag = "th" if cells and cells[0] in ("class", "#") else "td"
            rows.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
        elif line.strip():
            rows.append(f"<p>{e.replace('**', '')}</p>")
    body, table = [], []
    for r in rows:
        if r.startswith("<tr>"):
            table.append(r)
            continue
        if table:
            body.append("<table>" + "".join(table) + "</table>")
            table = []
        body.append(r)
    if table:
        body.append("<table>" + "".join(table) + "</table>")
    return ("<!doctype html><meta charset=utf-8><title>Stylesheet check " + html.escape(summary["journal"].upper())
            + "</title><style>body{font:14px system-ui;margin:24px;max-width:1200px}table{border-collapse:collapse;"
            "margin:8px 0}td,th{border:1px solid #ccc;padding:4px 6px;vertical-align:top}th{background:#f3f3f3}"
            "</style>" + "".join(body))


# --- main ---------------------------------------------------------------------------------------

def journal_of(folder: str) -> str:
    return style_mod.resolve(re.split(r"[-_]", os.path.basename(folder))[0])


def article_dirs(data: str, journal: str) -> list[str]:
    code = style_mod.resolve(journal)
    return sorted(d for d in glob.glob(os.path.join(data, "*"))
                  if os.path.isdir(d) and journal_of(d) == code
                  and os.path.exists(os.path.join(d, "original.zip")) and os.path.exists(os.path.join(d, "te.docx")))


def check_journal(journal: str, inputs: list[tuple], args, log=print) -> dict | None:
    articles = []
    for name, orig, editor, zip_path in inputs:
        try:
            articles.append(run_article(name, orig, editor, journal, zip_path))
        except Exception as e:  # noqa: BLE001  (one bad article does not stop the run)
            log(f"  {name}: skipped ({type(e).__name__}: {e})")
    if not articles:
        return None
    groups = group_items(articles)
    sheet_path = find_stylesheet(journal, args.stylesheets)
    sheet = RuleIndex(load_rules(sheet_path, "S")) if sheet_path else None
    guide = RuleIndex(load_rules(args.guide, "G")) if args.guide and os.path.exists(args.guide) else None
    log(f"{journal.upper()}: {len(articles)} articles, {sum(len(a['items']) for a in articles)} disagreements, "
        f"{len(groups)} patterns; stylesheet {os.path.basename(sheet_path) if sheet_path else 'NOT FOUND'}")
    before = dict(USAGE)
    if not args.no_judge:
        if not ai_refs.available():
            log("  no EDITOS_ANTHROPIC_API_KEY: patterns are left unjudged")
        else:
            judge(groups, sheet, guide, model=args.model, batch_size=args.batch_size, effort=args.effort, log=log)
    usage = {k: USAGE[k] - before.get(k, 0) for k in USAGE}
    pin, pout, read = PRICES.get(args.model, (0.10, 0.50, 0.10))
    usage["cost"] = round((usage.get("input_tokens", 0) * pin + usage.get("output_tokens", 0) * pout
                           + usage.get("cache_creation_input_tokens", 0) * pin * 1.25
                           + usage.get("cache_read_input_tokens", 0) * pin * read) / 1e6, 5)
    usage["cost_per_article"] = round(usage["cost"] / len(articles), 5)
    summary = summarise(journal, articles, groups)
    summary["usage"] = usage
    summary["stylesheet"] = os.path.basename(sheet_path) if sheet_path else None
    os.makedirs(args.out, exist_ok=True)
    base = os.path.join(args.out, f"stylesheet_check_{journal}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "patterns": [_group_json(g) for g in groups]}, f, indent=1, ensure_ascii=False)
    md = markdown(summary, groups, usage)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(md)
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(html_report(summary, md))
    log(f"  raw {summary['raw_score']:.1%} -> adjusted {summary['adjusted_score']:.1%}; "
        f"${usage['cost']:.4f} (${usage['cost_per_article']:.4f}/article); wrote {base}.md/.json/.html")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("data", nargs="?", help="folder of article folders (original.zip + te.docx)")
    ap.add_argument("--journals", default="", help="comma-separated journal codes (default: --journal)")
    ap.add_argument("--journal", default="", help="journal of --pair files (default: file-name prefix)")
    ap.add_argument("--pair", nargs=2, action="append", default=[], metavar=("EDITOR", "ORIGINAL"),
                    help="an editor's tracked .docx and the original .docx (repeatable)")
    ap.add_argument("--stylesheets", default=os.environ.get("EDITOS_STYLESHEETS", ""))
    ap.add_argument("--guide", default=os.environ.get("EDITOS_PREEDIT_GUIDE", ""))
    ap.add_argument("--out", default=os.environ.get("EDITOS_REPORT_DIR") or cache_dir("stylesheet_check", ""))
    ap.add_argument("--model", default=os.environ.get("EDITOS_JUDGE_MODEL", MODEL))
    ap.add_argument("--effort", default="high", help="high: fewer loosely-related rule quotes than medium, ~2x cost")
    ap.add_argument("--batch-size", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0, help="articles per journal")
    ap.add_argument("--no-judge", action="store_true", help="list disagreements only (no Claude)")
    args = ap.parse_args(argv)

    by_journal: dict[str, list[tuple]] = collections.defaultdict(list)
    codes = [c.strip().lower() for c in (args.journals or args.journal).split(",") if c.strip()]
    if args.data:
        for code in codes:
            dirs = article_dirs(args.data, code)
            for d in dirs[:args.limit or None]:
                src = manuscript_from_zip(os.path.join(d, "original.zip"))
                if src is None:
                    continue
                with open(os.path.join(d, "te.docx"), "rb") as f:
                    by_journal[code].append((os.path.basename(d), src, f.read(), os.path.join(d, "original.zip")))
    for editor_path, orig_path in args.pair:
        code = (args.journal or journal_of(orig_path)).lower()
        name = re.sub(r"_(original|editor)$", "", os.path.splitext(os.path.basename(orig_path))[0]) + "_pair"
        with open(orig_path, "rb") as f1, open(editor_path, "rb") as f2:
            by_journal[code].append((name, f1.read(), f2.read(), None))
    t0 = time.time()
    summaries = [s for code, inputs in by_journal.items() if (s := check_journal(code, inputs, args))]
    n = sum(len(s["articles"]) for s in summaries)
    print(f"done in {time.time() - t0:.0f}s: {n} articles, judge {USAGE['calls']} requests, "
          f"{USAGE['input_tokens']} in / {USAGE['output_tokens']} out tokens, ${cost(args.model):.4f}"
          + (f" (${cost(args.model) / n:.4f}/article)" if n else "")
          + f"; tool's reference parsing: {getattr(ai_refs, 'USAGE', {})}")
    return summaries


if __name__ == "__main__":
    main()
