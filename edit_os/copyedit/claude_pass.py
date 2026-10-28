"""Claude copy-edit pass (B4): grammar, punctuation, articles, agreement and word choice.

The manuscript's prose paragraphs go to Claude in chunks (about `chunk_words` words, paragraphs
numbered P1, P2, ... within the chunk). Claude answers in a fixed JSON schema with edits
{paragraph_id, old, new, kind, reason, confidence} and author queries {paragraph_id, anchor, text}.
Nothing Claude says is applied as is: `verify` keeps an edit only when Claude marks it certain, its
`old` text occurs exactly once in that paragraph, the changed span is small, and digits, brackets,
symbols, acronyms and capitalised names are untouched. The kept edit is trimmed to the words that actually change and
becomes a `Change` (layer LANGUAGE, source AI, reason "claude.<kind> (<why>)"). Overlaps with rule
and checker changes are resolved by `copyedit.run_all`, which runs this pass last (rules win).

Prompt layout, for prompt caching: instructions, then few-shot examples (both stable for a
journal, cached), then the chunk. The examples are real copy-editor edits read at runtime from a
private changes.jsonl (`examples_path` or EDITOS_CE_EXAMPLES), only from articles in the training
split (`is_test_article` is False), so evaluation on test articles stays honest. Without that
file a few built-in synthetic examples are used. Results are cached in SQLite by model, effort,
prompt and chunk text. `batch_prefetch` sends uncached chunks through the Message Batches API
(half price) for runs that can wait.

Model choice (tools/eval_ce.py, 16 held-out articles, 10 journals): Haiku 5.5 at effort medium had
the best precision (70%) at $0.0025 per article; Sonnet 5.5 61% at $0.026, Opus 5.5 65% at $0.063
with twice as many edits.

Settings: `copyedit.claude` in the journal style. Needs EDITOS_ANTHROPIC_API_KEY; off without it.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass
from itertools import count

from .. import ai_refs, provenance
from ..changes import Change, Layer, Source
from ..docmodel import Manuscript, Paragraph
from ..queries import Query
from ..style import JournalStyle
from .cache import cache_dir
from .common import hidden_offsets, near_hidden, overlaps, prose_paragraphs, protected_spans

PROMPT_VERSION = "b4-4"
KINDS = ["punctuation", "article", "agreement", "word_choice", "grammar", "capitalisation", "hyphenation",
         "spelling", "other"]
CONFIDENCE = ["certain", "likely"]  # kept edits: copyedit.claude.min_confidence

# $ per million tokens: input, output. Cache writes 1.25x input, cache reads 0.1x (0.05x on Opus 5.5).
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.10),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-opus-5-5": (4.00, 20.00, 0.05),
}

SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "paragraph_id": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "kind": {"type": "string", "enum": KINDS},
                    "reason": {"type": "string"},
                    "confidence": {"type": "string", "enum": CONFIDENCE},
                },
                "required": ["paragraph_id", "old", "new", "kind", "reason", "confidence"],
                "additionalProperties": False,
            },
        },
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "paragraph_id": {"type": "string"},
                    "anchor": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["paragraph_id", "anchor", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["edits", "queries"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You are an experienced copy editor for medical journals published by Wolters Kluwer-Medknow.
The text below has already been style-edited (headings, citations, numbers, units, references and
abbreviations are done). Your job is the copy-editing checklist only:
- subject-verb agreement; singular/plural nouns; verb tense consistency
- articles: insert a missing "the"/"a"/"an", remove a wrong one
- punctuation: missing commas after introductory phrases or around non-restrictive clauses,
  comma before "respectively", serial commas in lists, misplaced or doubled punctuation, missing
  full stops
- standard misuses: which/that (restrictive clauses), since/because (causal), while/although or whereas
  (contrast, not time)
- word choice only where a word is clearly wrong English (wrong preposition, wrong word form), and
  small missing words ("and", "of", "to") that leave a sentence ungrammatical. Do not swap correct
  words for synonyms or preferred phrasings, and do not change word order.
- capitalisation errors in running text (a common noun capitalised mid-sentence: "the Patient")
{hyphenation}- contractions written out (didn't -> did not); stray commas such as "et al., reported" -> "et al. reported"
- obvious misspellings (typos). Do NOT switch between British and American spelling.
- eliminate sexist language (he/she -> they or plural, etc.)

Hard rules:
- Only list real changes: never return an edit whose "new" equals "old", and never list text that
  is already correct.
- Never change meaning, facts, numbers (including number words), units, statistics, drug names, gene names, people's names,
  abbreviations/acronyms, citations ([1], (1), superscripts), table/figure labels, or URLs.
- Do not rewrite for style. Do not reorder sentences. Do not shorten. If a sentence is awkward or
  confusing but not clearly wrong, leave it; if its meaning is truly unclear, add a QUERY instead
  of rewriting (at most one query per paragraph, only for real ambiguity).
- Be conservative: an editor must accept or reject every suggestion, so suggest only edits a
  professional copy editor would certainly make. When in doubt, do not edit.
- Each edit fixes one problem. "old" must be copied exactly (character for character) from the
  paragraph and must be unique in that paragraph: include one or two neighbouring words so it is.
  Keep "old" short (normally 1-6 words). "new" is the corrected replacement for "old".
- kind is one of: punctuation, article, agreement, word_choice, grammar, capitalisation, hyphenation,
  spelling, other.
  reason: a few words for the editor.
- confidence: "certain" when any professional copy editor would make this edit (a plain error);
  "likely" when it is an improvement some editors would leave alone.
- Queries: paragraph_id, anchor (a short exact phrase from the paragraph the query is about) and
  text written to the author politely in the journal's style, e.g. "Please check the meaning of
  this sentence." or "Kindly clarify what 'it' refers to here." Queries are rare: most chunks
  need none.

The input is a list of paragraphs, each starting with its id in square brackets ([P1], [P2], ...).
Return JSON only, matching the schema. If nothing needs changing, return empty lists."""

HYPHENATION = {
    True: """- hyphenation per the journal stylesheet: compounds with prefixes are closed up (post-operative ->
  postoperative, pre-operatively -> preoperatively, non-closure -> nonclosure, co-existent ->
  coexistent, re-approximation -> reapproximation, cut-off -> cutoff), except when the same vowel
  meets (anti-inflammatory, pre-empt), a capital or numeral follows (anti-French, post-1990s), or
  the base is already hyphenated; the noun "follow-up" and compound adjectives before a noun take a hyphen
""",
    False: """- hyphenation: the noun "follow-up" and compound adjectives before a noun take a hyphen. Leave
  prefixed words (non-, post-, pre-, co-, re-) as the author wrote them.
""",
}

# Fallback few-shot examples (synthetic) when no private examples file is available.
_BUILTIN = [
    ("The patients was followed up", "patients was", "patients were", "agreement"),
    ("was observed in majority of cases", "in majority", "in the majority", "article"),
    ("However the difference was not significant", "However the", "However, the", "punctuation"),
    ("Since the sample size was small, the results", "Since the", "As the", "word_choice"),
    ("the study which was conducted in 2 centres", "study which was", "study that was", "word_choice"),
    ("These findings are in concordance to earlier", "concordance to", "concordance with", "word_choice"),
    ("Each surgeon recorded his findings", "his findings", "their findings", "other"),
    ("The diagnosis were confirmed by biopsy", "diagnosis were", "diagnosis was", "agreement"),
    ("and the Patient was discharged", "the Patient", "the patient", "capitalisation"),
]


def is_test_article(article: str) -> bool:
    """Held-out split for evaluation: about one article in five, fixed by the article's name.
    Few-shot examples never come from these articles."""
    return int(hashlib.sha1(article.encode()).hexdigest(), 16) % 5 == 0


def _journal(article: str) -> str:
    return re.split(r"[_-]", article)[0].lower()


_SPELL_VARIANT = [("z", "s"), ("our", "or"), ("tre", "ter"), ("ae", "e"), ("oe", "e"), ("ll", "l")]


def _spelling_variant(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    if a == b:
        return False
    for x, y in _SPELL_VARIANT:
        a, b = a.replace(x, y), b.replace(x, y)
    return a == b


def _example_kind(old: str, new: str, before: str = "", after: str = "") -> str | None:
    """Category of a real copy edit, or None for edits we do not want Claude to imitate."""
    both = old + new
    if re.search(r"\d", both) or not both.strip() or len(old) + len(new) > 40 or "Copyedit" in both:
        return None
    # Judge the whole words the edit touches ("multicent[r -> ]e" is a spelling change).
    wb = re.search(r"\w*$", before).group()
    wa = re.match(r"\w*", after).group()
    full_old, full_new = wb + old + wa, wb + new + wa
    if re.search(r"[A-Z]{2,}", full_old + full_new) or re.search(r"[‐-―−()\[\]]", both):
        return None  # abbreviations, dashes, brackets: other checkers' work
    if _spelling_variant(full_old, full_new) or old.strip() == new.strip():
        return None
    if full_old.lower() == full_new.lower():
        return "capitalisation"
    if (wb or wa) and re.fullmatch(r"\w*", old) and re.fullmatch(r"\w*", new):
        return None  # letters inside a word: spelling, which the spelling checker owns
    if re.fullmatch(r"[\s-]*", old) and re.fullmatch(r"[\s-]*", new):
        return "hyphenation"
    if not re.search(r"\w", both):
        return "punctuation"
    words = {w.lower() for w in re.findall(r"\w+", both)}
    if words <= {"the", "a", "an"}:
        return "article"
    if len(re.findall(r"\w+", old)) > 4 or len(re.findall(r"\w+", new)) > 4:
        return None
    return "word_choice"


def load_examples(path: str | None, journal: str, n: int = 40, seed: int = 0) -> list[dict]:
    """Real copy-editor edits for few-shot examples: stage copy_edit, outside the reference list,
    training-split articles only, same journal first. Deterministic for a journal."""
    if not path or not os.path.exists(path):
        return []
    pool: dict[str, list[dict]] = {"same": [], "other": []}
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("stage") != "copy_edit" or r.get("in_refs") or is_test_article(r["article"]):
                continue
            kind = _example_kind(r["old"], r["new"], r.get("before", ""), r.get("after", ""))
            if kind is None or len(r.get("before", "")) < 25 or len(r.get("after", "")) < 10:
                continue
            ex = dict(before=r["before"][-60:], old=r["old"], new=r["new"], after=r["after"][:40], kind=kind)
            pool["same" if _journal(r["article"]) == journal else "other"].append(ex)
    rng = random.Random(f"{journal}:{seed}")
    out: list[dict] = []
    for key in ("same", "other"):
        items = pool[key]
        rng.shuffle(items)
        # Keep the copy editors' own mix of kinds, but at least a few of each.
        by_kind: dict[str, list[dict]] = {}
        for ex in items:
            by_kind.setdefault(ex["kind"], []).append(ex)
        want = n - len(out)
        if want <= 0:
            break
        take = []
        for k, lst in by_kind.items():
            take += lst[:max(2, round(want * len(lst) / max(1, len(items))))]
        rng.shuffle(take)
        out += take[:want]
    return out


def examples_text(examples: list[dict]) -> str:
    if not examples:
        lines = [f"- {ctx.replace(old, f'[{old} -> {new}]', 1)}  ({kind})" for ctx, old, new, kind in _BUILTIN]
    else:
        lines = [f"- ...{e['before']}[{e['old']} -> {e['new']}]{e['after']}...  ({e['kind']})" for e in examples]
    return ("Examples of edits this journal's copy editors made (context, then [old -> new]). They show the "
            "kind and size of edit expected; most sentences need no edit at all:\n" + "\n".join(lines))


# --- chunks ------------------------------------------------------------------------------------

def _is_heading(p: Paragraph) -> bool:
    t = p.text.strip()
    return bool(re.match(r"<H\d>", t)) or (len(t.split()) < 10 and not re.search(r"[.?!:]$", t))


# The keywords line: MeSH terms keep their own commas ("Diabetes Mellitus, Type 2; Burnout, Psychological"), which
# Claude took for list separators (JEHP 1580).
_KEYWORDS = re.compile(r"^\s*(?:<H\d>\s*)?(?:key\s*-?\s*words?|index\s+terms|mesh\s+terms?)\b", re.I)


def paragraphs(ms: Manuscript) -> list[Paragraph]:
    return [p for p in prose_paragraphs(ms) if not _is_heading(p) and not _KEYWORDS.match(p.text)]


def chunks(paras: list[Paragraph], max_words: int = 1500) -> list[list[Paragraph]]:
    out, cur, words = [], [], 0
    for p in paras:
        n = len(p.text.split())
        if cur and words + n > max_words:
            out.append(cur)
            cur, words = [], 0
        cur.append(p)
        words += n
    if cur:
        out.append(cur)
    return out


def chunk_text(paras: list[Paragraph]) -> str:
    return "\n\n".join(f"[P{i}] {p.text}" for i, p in enumerate(paras, 1))


# --- API ---------------------------------------------------------------------------------------

USAGE: dict[str, Counter] = {}  # model (or "model (batch)") -> calls, input/output/cache tokens, this process


def cost(model: str | None = None) -> float:
    total = 0.0
    for m, u in USAGE.items():
        base = m.removesuffix(" (batch)")
        if model and base != model:
            continue
        pin, pout, read = PRICES.get(base, PRICES["claude-opus-5-5"])
        total += (u["input_tokens"] * pin + u["cache_creation_input_tokens"] * pin * 1.25
                  + u["cache_read_input_tokens"] * pin * read + u["output_tokens"] * pout) / 1e6 \
            * (0.5 if m.endswith(" (batch)") else 1)
    return total


class _Cache:
    def __init__(self, path: str | None = None):
        # shared by the service's worker threads (serialized SQLite; writes under the lock)
        self.con = sqlite3.connect(path or cache_dir("claude_copyedit.sqlite"), timeout=60, check_same_thread=False)
        self.lock = threading.Lock()
        self.con.execute("CREATE TABLE IF NOT EXISTS ce (key TEXT PRIMARY KEY, body TEXT)")

    @staticmethod
    def key(model: str, effort: str, system: str, text: str) -> str:
        return hashlib.sha256(json.dumps([PROMPT_VERSION, model, effort, system, text]).encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        with self.lock:
            row = self.con.execute("SELECT body FROM ce WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, body: dict) -> None:
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO ce VALUES (?, ?)", (key, json.dumps(body)))
            self.con.commit()


_cache: _Cache | None = None


def request(system: str, examples: str, text: str, model: str, effort: str) -> dict:
    """Message parameters: instructions and examples first (cached), the chunk last."""
    return dict(
        model=model,
        max_tokens=16000,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        system=[{"type": "text", "text": system},
                {"type": "text", "text": examples, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": text}],
    )


def _call(params: dict) -> dict:
    import anthropic

    client = anthropic.Anthropic(
        api_key=ai_refs._api_key(),
        base_url=os.environ.get("EDITOS_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    )
    if params["model"] in ai_refs._FALLBACK_MODELS:
        # A declined request is re-run on Anthropic's recommended fallback model.
        response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
    else:
        response = client.messages.create(**params)
    u = USAGE.setdefault(params["model"], Counter())
    u["calls"] += 1
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        u[k] += getattr(response.usage, k, 0) or 0
    if response.stop_reason in ("refusal", "max_tokens"):
        u[response.stop_reason] += 1
        return {"edits": [], "queries": []}
    return parse(next((b.text for b in response.content if b.type == "text"), ""))


def parse(text: str) -> dict:
    """The structured output, tolerating anything malformed (it is dropped, not guessed at)."""
    try:
        data = json.loads(text)
    except ValueError:
        return {"edits": [], "queries": []}
    if not isinstance(data, dict):
        return {"edits": [], "queries": []}
    edits = [e for e in data.get("edits") or [] if isinstance(e, dict)
             and all(isinstance(e.get(k), str) for k in ("paragraph_id", "old", "new"))]
    queries = [q for q in data.get("queries") or [] if isinstance(q, dict)
               and all(isinstance(q.get(k), str) for k in ("paragraph_id", "anchor", "text"))]
    return {"edits": edits, "queries": queries}


# --- verification ------------------------------------------------------------------------------

_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")
_SYMBOLS = set("[](){}<>=%±°×≤≥μ/+*#@&§†‡")
DROPS: Counter = Counter()  # why edits were dropped (this process)
_ids = count(1)


@dataclass
class Edit:
    start: int
    end: int
    old: str
    new: str


def trim(text: str, start: int, old: str, new: str) -> Edit:
    """Shrink a replacement to the whole tokens that change (common leading/trailing tokens dropped)."""
    a, b = _TOKEN.findall(old), _TOKEN.findall(new)
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    j = 0
    while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    pre = "".join(a[:i])
    old_core = "".join(a[i:len(a) - j])
    new_core = "".join(b[i:len(b) - j])
    s = start + len(pre)
    return Edit(s, s + len(old_core), old_core, new_core)


def _names(text: str, start: int, end: int) -> Counter:
    """Capitalised words in text[start:end] that do not start a sentence (names), lower-cased."""
    out = Counter()
    for m in re.finditer(r"\b[A-Z][a-z]+\b", text[start:end]):
        before = text[:start + m.start()].rstrip()
        if before and before[-1] not in ".?!:":
            out[m.group().lower()] += 1
    return out


def unsafe(old: str, new: str, max_words: int, max_chars: int, names: Counter | None = None) -> str | None:
    """Why the (trimmed) replacement old -> new may not be suggested, or None if it may. `names`:
    capitalised words inside `old` that are not sentence-initial; each must survive into `new`."""
    if old == new:
        return "no_change"
    if len(re.findall(r"\w+", old)) > max_words or len(re.findall(r"\w+", new)) > max_words + 2:
        return "too_long"
    if len(old) > max_chars or len(new) > max_chars:
        return "too_long"
    if re.findall(r"\d+", old) != re.findall(r"\d+", new):
        return "digits"
    if sorted(c for c in old if c in _SYMBOLS) != sorted(c for c in new if c in _SYMBOLS):
        return "symbols"
    if sorted(re.findall(r"\b\w*[A-Z]\w*[A-Z]\w*\b", old)) != sorted(re.findall(r"\b\w*[A-Z]\w*[A-Z]\w*\b", new)):
        return "acronym"
    if (names if names is not None else _names(old, 0, len(old))) - Counter(w.lower() for w in re.findall(r"\w+", new)):
        return "name"
    if _spelling_variant(old.strip(), new.strip()):
        return "spelling_variant"  # the spelling checker owns the convention
    return None


def verify(p: Paragraph, e: dict, cfg: dict, prot=None, hidden=None) -> tuple[Change | None, str]:
    """A Change for Claude's edit `e` on paragraph `p`, or (None, why it was dropped)."""
    text, old, new = p.text, e["old"], e["new"]
    if not old:
        return None, "empty_old"
    n = text.count(old)
    if n == 0:
        return None, "not_found"
    if n > 1:
        return None, "not_unique"
    t = trim(text, text.index(old), old, new)
    why = unsafe(t.old, t.new, cfg.get("max_words", 6), cfg.get("max_chars", 60), _names(text, t.start, t.end))
    if why is None:
        # Whole-word edit: a replacement may not start or end inside a word.
        if t.start > 0 and t.old and text[t.start - 1].isalnum() and text[t.start].isalnum():
            why = "partial_word"
        elif t.end < len(text) and t.old and text[t.end - 1].isalnum() and text[t.end].isalnum():
            why = "partial_word"
    if why is None and overlaps(t.start, t.end, prot if prot is not None else protected_spans(text)):
        why = "protected"
    if why is None and near_hidden(t.start, t.end, hidden if hidden is not None else hidden_offsets(p)):
        why = "hidden"
    if why:
        return None, why
    if e.get("confidence") == "likely" and cfg.get("min_confidence", "certain") == "certain":
        return None, "not_certain"
    if e.get("kind") in (cfg.get("skip_kinds") or []):
        return None, "skipped_kind"
    kind = e.get("kind") if e.get("kind") in KINDS else "other"
    reason = f"claude.{kind}"
    if e.get("reason"):
        reason += f" ({e['reason'].strip()[:120]})"
    c = Change(id=f"ai{next(_ids)}", para_id=p.id, start=t.start, end=t.end, old=t.old, new=t.new,
               layer=Layer.LANGUAGE, source=Source.AI, reason=reason, author="Edit OS (AI)",
               provenance=provenance.make("ai", "Claude copy edit", (e.get("reason") or "").strip(), checker="claude",
                                          edit_kind=kind, confidence=e.get("confidence", ""),
                                          model=cfg.get("model", "claude-haiku-5-5")))
    return c, ""


def query(p: Paragraph, q: dict) -> Query | None:
    text = q["text"].strip()
    if not text:
        return None
    anchor = q["anchor"]
    s = e = 0
    if anchor and p.text.count(anchor) == 1:
        s = p.text.index(anchor)
        e = s + len(anchor)
    return Query(p.id, s, e, text, "claude.query")


# --- the pass ----------------------------------------------------------------------------------

def settings(style: JournalStyle) -> dict:
    """`copyedit.claude` from the journal style; EDITOS_CE_MODEL (else EDITOS_MODEL), EDITOS_CE_EFFORT,
    EDITOS_CE_EXAMPLES, EDITOS_CE_MAX_WORDS, EDITOS_CE_MAX_CHARS and EDITOS_CE_MIN_CONFIDENCE override it."""
    cfg = dict(style.get("copyedit.claude", {}) or {})
    for env, key in (("EDITOS_MODEL", "model"), ("EDITOS_CE_MODEL", "model"), ("EDITOS_CE_EFFORT", "effort"),
                     ("EDITOS_CE_EXAMPLES", "examples_path")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    for env, key in (("EDITOS_CE_MAX_WORDS", "max_words"), ("EDITOS_CE_MAX_CHARS", "max_chars")):
        if os.environ.get(env):
            cfg[key] = int(os.environ[env])
    if os.environ.get("EDITOS_CE_MIN_CONFIDENCE"):
        cfg["min_confidence"] = os.environ["EDITOS_CE_MIN_CONFIDENCE"]
    return cfg


def enabled(style: JournalStyle) -> bool:
    return bool(settings(style).get("enabled", True)) and ai_refs.available()


def prompt(style: JournalStyle) -> tuple[str, str]:
    """(instructions, examples) for the journal: the stable, cached part of every request."""
    cfg = settings(style)
    close = bool(cfg.get("close_prefixes", False))
    examples = load_examples(cfg.get("examples_path"), style.code.lower(), cfg.get("examples", 40))
    if not close:  # the other journals' prefix closing would teach the wrong habit
        examples = [e for e in examples if not _closes_prefix(e)]
    return INSTRUCTIONS.replace("{hyphenation}", HYPHENATION[close]), examples_text(examples)


def _closes_prefix(e: dict) -> bool:
    return e["kind"] == "hyphenation" and e["new"] == "" and e["old"] in ("-", "–")


def _cache_db() -> _Cache:
    global _cache
    if _cache is None:
        _cache = _Cache()
    return _cache


def _keyed_request(chunk: list[Paragraph], style: JournalStyle) -> tuple[str, dict]:
    cfg = settings(style)
    model, effort = cfg.get("model", "claude-haiku-5-5"), cfg.get("effort", "medium")
    system, examples = prompt(style)
    text = chunk_text(chunk)
    return _Cache.key(model, effort, system + "\n" + examples, text), request(system, examples, text, model, effort)


def suggest(chunk: list[Paragraph], style: JournalStyle, call=None) -> dict:
    """Claude's raw answer for one chunk (cached by model, effort, prompt and chunk text)."""
    key, params = _keyed_request(chunk, style)
    got = _cache_db().get(key)
    if got is None:
        got = (call or _call)(params)
        _cache_db().put(key, got)
    return got


def batch_prefetch(jobs: list[tuple[Manuscript, JournalStyle]], client=None, poll: float = 30.0,
                   wait: bool = True) -> str | None:
    """Message Batches API (half price, results within 24 h, usually under 1 h) for runs that are
    not urgent: send every uncached chunk of `jobs` as one batch, wait, and store the answers in
    the cache, so a following `collect` / `run_all` makes no further calls. Returns the batch id
    (None when everything was cached). The server-side refusal fallback is not available on
    batches; a refused chunk is cached as empty."""
    import time

    todo: dict[str, dict] = {}
    for ms, style in jobs:
        cfg = settings(style)
        for chunk in chunks(paragraphs(ms), cfg.get("chunk_words", 1500)):
            key, params = _keyed_request(chunk, style)
            if _cache_db().get(key) is None:
                todo[key] = params  # sha256 hex: a valid 64-character custom_id
    if not todo:
        return None
    if client is None:
        import anthropic

        client = anthropic.Anthropic(
            api_key=ai_refs._api_key(),
            base_url=os.environ.get("EDITOS_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        )
    batch = client.messages.batches.create(requests=[{"custom_id": k, "params": p} for k, p in todo.items()])
    if not wait:
        return batch.id
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        time.sleep(poll)
    for res in client.messages.batches.results(batch.id):
        if res.result.type != "succeeded" or res.custom_id not in todo:
            continue  # errored or expired: the chunk is sent again, synchronously, by `collect`
        msg = res.result.message
        u = USAGE.setdefault(todo[res.custom_id]["model"] + " (batch)", Counter())
        u["calls"] += 1
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            u[k] += getattr(msg.usage, k, 0) or 0
        got = {"edits": [], "queries": []} if msg.stop_reason in ("refusal", "max_tokens") else \
            parse(next((b.text for b in msg.content if b.type == "text"), ""))
        _cache_db().put(res.custom_id, got)
    return batch.id


def collect(ms: Manuscript, style: JournalStyle, call=None) -> tuple[list[Change], list[Query]]:
    """Verified copy-edit suggestions and author queries for the whole manuscript."""
    if call is None and not enabled(style):
        return [], []
    cfg = settings(style)
    changes: list[Change] = []
    queries: list[Query] = []
    for chunk in chunks(paragraphs(ms), cfg.get("chunk_words", 1500)):
        got = suggest(chunk, style, call)
        local = {f"P{i}": p for i, p in enumerate(chunk, 1)}
        for e in got.get("edits", []):
            p = local.get(e["paragraph_id"].strip("[] "))
            if p is None:
                DROPS["bad_paragraph"] += 1
                continue
            c, why = verify(p, e, cfg)
            if c is None:
                DROPS[why] += 1
                continue
            changes.append(c)
        per_para: Counter = Counter()
        for q in got.get("queries", [])[:cfg.get("max_queries_per_chunk", 1)]:
            p = local.get(q["paragraph_id"].strip("[] "))
            if p is None or per_para[p.id]:
                continue
            made = query(p, q)
            if made:
                per_para[p.id] += 1
                queries.append(made)
    return changes, queries
