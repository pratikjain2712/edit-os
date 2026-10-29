"""Safe autopilot: decide the pending suggestions an editor would accept without a second look.

Run from the review screen ("Autopilot"), after the pipeline. It only touches suggestions nobody has
decided yet, and it only accepts; everything it is unsure of stays pending for the editor:
- rule suggestions (journal style, PubMed / CrossRef-verified references, NLM abbreviations, spelling,
  LanguageTool's allowlisted rules, front matter) are accepted unless their reason is on the
  journal's `autopilot.hold` list;
- Claude's copy edits (source AI) go to a second, independent Claude check (`judge`): accepted only
  when it agrees the edit fixes a plain error without changing meaning, otherwise left for review;
- author queries are never touched (they stay in the file).
Every accepted change is still a tracked change in the Word file, and the decisions are recorded
with editor "autopilot" so they can be undone in one step. QC runs after; the report says what was
accepted, what was left and why.

Settings: `autopilot` in the journal style.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from . import ai_refs, spend
from .changes import Change, Layer, Source, Status
from .copyedit.cache import cache_dir
from .style import JournalStyle

EDITOR = "autopilot"
PROMPT_VERSION = "ap-1"

CATEGORIES = {  # report label for each kind of suggestion
    "front_matter": "Front matter",
    "references_verified": "References (PubMed / CrossRef verified)",
    "references": "References",
    "style": "Journal style",
    "spelling": "Spelling",
    "languagetool": "Grammar checker",
    "abbreviations": "Abbreviations",
    "ai": "AI copy edits",
    "other": "Other",
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["accept", "review"]},
                    "why": {"type": "string"},
                },
                "required": ["id", "verdict", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

JUDGE_INSTRUCTIONS = """You are a senior copy editor at a medical journal (Wolters Kluwer-Medknow) checking edits a
junior copy editor suggested. Each edit replaces OLD with NEW inside the sentence shown (the edited
span is marked [[OLD => NEW]]).

Answer "accept" only when ALL of these hold:
- OLD contains a plain error in English (grammar, agreement, article, punctuation, typo, wrong word form)
  that any professional copy editor would fix, and NEW fixes it correctly;
- NEW does not change the meaning, a fact, a number, a name, a term or an abbreviation;
- the sentence reads correctly with NEW in place.
Answer "review" for anything else: a matter of taste or style, an edit that is unnecessary, one that
could change meaning, or when you are not sure. When in doubt, answer "review".

why: a few words for the editor. Return JSON only, one verdict per edit id."""


@dataclass
class Plan:
    accept: list[str] = field(default_factory=list)
    left: list[dict] = field(default_factory=list)  # {id, category, why}
    categories: dict[str, str] = field(default_factory=dict)  # id -> category
    judge: dict = field(default_factory=lambda: {"used": False, "asked": 0, "agreed": 0})


def settings(style: JournalStyle) -> dict:
    cfg = dict(style.get("autopilot", {}) or {})
    if os.environ.get("EDITOS_AUTOPILOT_MODEL"):
        cfg["model"] = os.environ["EDITOS_AUTOPILOT_MODEL"]
    return cfg


def category(item) -> str:
    """Report category of a change or front-matter insert, from its source, layer and reason."""
    reason = item.reason or ""
    if item.source == Source.AI:
        return "ai"
    if reason.startswith("front_matter"):
        return "front_matter"
    if reason.startswith("ref_lookup"):
        return "references_verified"
    if reason.startswith("spelling"):
        return "spelling"
    if reason.startswith("languagetool"):
        return "languagetool"
    if "abbreviation" in reason:
        return "abbreviations"
    if getattr(item, "layer", None) == Layer.REFERENCES and not reason.startswith(("citations", "fig_")):
        return "references"
    if item.source == Source.RULE:
        return "style"
    return "other"


def _held(reason: str, hold) -> bool:
    return any(reason == h or reason.startswith((h + ":", h + ".", h + " ")) for h in hold or [])


def plan(changes: list[Change], inserts, style: JournalStyle, judge=None, texts: dict[str, str] | None = None) -> Plan:
    """What autopilot accepts among the still-pending `changes` and front-matter `inserts`.
    `judge(items) -> {id: (verdict, why)}` checks Claude's copy edits; default `judge_edits` (with
    `texts`, paragraph id -> text, for the sentence around each edit)."""
    cfg = settings(style)
    hold = cfg.get("hold") or []
    accept_sources = set(cfg.get("accept_sources") or ["rule"])
    out = Plan()
    ai: list[Change] = []
    for item in list(changes) + list(inserts):
        if item.status != Status.SUGGESTED:
            continue
        cat = out.categories[item.id] = category(item)
        if _held(item.reason or "", hold):
            out.left.append({"id": item.id, "category": cat, "why": "this journal keeps it for the editor"})
        elif item.source == Source.AI:
            ai.append(item)
        elif item.source.value in accept_sources:
            out.accept.append(item.id)
        else:
            out.left.append({"id": item.id, "category": cat, "why": "not an automatic suggestion"})
    _with_its_figures(inserts, out)
    if not ai:
        return out
    if not cfg.get("ai_judge", True):
        out.left += [{"id": c.id, "category": "ai", "why": "AI edits are left for the editor"} for c in ai]
        return out
    if judge is None:
        if not ai_refs.available():
            out.left += [{"id": c.id, "category": "ai", "why": "second check unavailable (no Claude key)"}
                         for c in ai]
            return out
        judge = lambda items: judge_edits(items, cfg, texts)  # noqa: E731
    verdicts = judge(ai)
    out.judge.update(used=True, asked=len(ai))
    for c in ai:
        verdict, why = verdicts.get(c.id, ("review", "no answer from the second check"))
        if verdict == "accept":
            out.accept.append(c.id)
            out.judge["agreed"] += 1
        else:
            out.left.append({"id": c.id, "category": "ai", "why": f"second check: {why}" if why else "second check"})
    return out


def _with_its_figures(inserts, out: Plan) -> None:
    """A "Figure Legends" heading added over the figures sent as files (their pictures and legends inserted at the
    same place) is one decision with them: while those inserts are left for the editor, so is the heading (else the
    clean copy ends with a heading over nothing: IJO_2938_25)."""
    from .figures_insert import REASON as FIGURE
    from .legends import HEADING

    accepted = set(out.accept)
    figs = [n for n in inserts if n.reason == FIGURE and getattr(n, "legend", "")]
    for h in inserts:
        if h.reason != HEADING or h.id not in accepted:
            continue
        under = [f for f in figs if f.anchor == h.anchor]
        if under and not any(f.status == Status.ACCEPTED or f.id in accepted for f in under):
            out.accept.remove(h.id)
            out.left.append({"id": h.id, "category": out.categories.get(h.id, "style"),
                             "why": "goes with the figure inserts it heads, left for the editor"})


# --- the judge: a second Claude check of Claude's copy edits ------------------------------------

def _sentence(text: str, start: int, end: int, width: int = 240) -> str:
    """The text around start..end with the edit marked, cut at sentence ends where possible."""
    a = max(0, start - width)
    b = min(len(text), end + width)
    left, right = text[a:start], text[end:b]
    if a > 0 and ". " in left:
        left = left[left.index(". ") + 2:]
    if b < len(text) and ". " in right:
        right = right[:right.rindex(". ") + 1]
    return left, right


def judge_payload(items: list[Change], texts: dict[str, str]) -> str:
    lines = []
    for c in items:
        left, right = _sentence(texts.get(c.para_id, ""), c.start, c.end)
        lines.append(f"[{c.id}] {left}[[{c.old} => {c.new}]]{right}\n    (suggested because: {c.reason})")
    return "\n\n".join(lines)


class _Cache:
    def __init__(self, path: str | None = None):
        self.con = sqlite3.connect(path or cache_dir("autopilot.sqlite"), timeout=60, check_same_thread=False)
        self.con.execute("CREATE TABLE IF NOT EXISTS judge (key TEXT PRIMARY KEY, body TEXT)")

    def get(self, key: str) -> dict | None:
        row = self.con.execute("SELECT body FROM judge WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, body: dict) -> None:
        self.con.execute("INSERT OR REPLACE INTO judge VALUES (?, ?)", (key, json.dumps(body)))
        self.con.commit()


_cache: _Cache | None = None
USAGE = Counter()  # this process, for cost reporting


def _call(model: str, payload: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(
        api_key=ai_refs._api_key(),
        base_url=os.environ.get("EDITOS_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    )
    params = dict(
        model=model,
        max_tokens=8000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        system=JUDGE_INSTRUCTIONS,
        messages=[{"role": "user", "content": payload}],
    )
    if model in ai_refs._FALLBACK_MODELS:
        # A declined request is re-run on Anthropic's recommended fallback model.
        response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
    else:
        response = client.messages.create(**params)
    USAGE["calls"] += 1
    USAGE["input_tokens"] += response.usage.input_tokens
    USAGE["output_tokens"] += response.usage.output_tokens
    spend.add_usage("autopilot_check", model, response.usage)
    if response.stop_reason in ("refusal", "max_tokens"):
        return {"verdicts": []}
    try:
        return json.loads(next((b.text for b in response.content if b.type == "text"), ""))
    except ValueError:
        return {"verdicts": []}


def judge_edits(items: list[Change], cfg: dict, texts: dict[str, str] | None = None, call=None) -> dict:
    """{change id: (verdict, why)} from one Claude request for all of `items` (cached by its text).
    Anything malformed or missing counts as "review"."""
    global _cache
    model = cfg.get("model") or "claude-haiku-5-5"
    payload = judge_payload(items, texts or {})
    key = hashlib.sha256(json.dumps([PROMPT_VERSION, model, JUDGE_INSTRUCTIONS, payload]).encode()).hexdigest()
    if _cache is None:
        _cache = _Cache()
    body = _cache.get(key)
    if body is None:
        try:
            body = (call or _call)(model, payload)
        except Exception as e:  # network or API error: nothing is accepted
            return {c.id: ("review", f"unavailable ({type(e).__name__})") for c in items}
        _cache.put(key, body)
    out = {}
    for v in body.get("verdicts") or []:
        if isinstance(v, dict) and isinstance(v.get("id"), str):
            out[v["id"]] = ("accept" if v.get("verdict") == "accept" else "review", str(v.get("why", ""))[:160])
    return out


# --- report -----------------------------------------------------------------------------------

def report(p: Plan, items: dict, summary: dict, ran_at: str) -> dict:
    """What autopilot did, for the review screen and `_autopilot_report.json`. `items` maps id to
    the change or insert; `summary` is the render summary (QC) after the decisions."""
    by_cat: dict[str, dict[str, int]] = {}
    for i in p.accept:
        by_cat.setdefault(p.categories[i], {"accepted": 0, "left": 0})["accepted"] += 1
    for d in p.left:
        by_cat.setdefault(d["category"], {"accepted": 0, "left": 0})["left"] += 1
    left = []
    for d in p.left:
        it = items[d["id"]]
        left.append({**d, "label": CATEGORIES[d["category"]], "old": getattr(it, "old", ""),
                     "new": getattr(it, "new", None) if hasattr(it, "new") else it.text})
    qc = summary["qc"]
    return {
        "ran_at": ran_at,
        "accepted": len(p.accept),
        "left": len(p.left),
        "by_category": [{"category": k, "label": CATEGORIES[k], **v} for k, v in
                        sorted(by_cat.items(), key=lambda kv: list(CATEGORIES).index(kv[0]))],
        "left_items": left,
        "judge": p.judge,
        "qc": {"passed": qc["passed"], "errors": qc["errors"], "warnings": qc["warnings"],
               "failed": [c for c in qc["checks"] if not c["passed"]]},
        "pending": summary["changes"]["pending"],
        "queries": summary["queries"]["total"],
        "render_conflicts": len(summary.get("render_conflicts", [])),
        "delivery_ready": summary["delivery_ready"],
    }
