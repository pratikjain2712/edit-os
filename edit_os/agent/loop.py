"""The tool-use loop: one conversation per manuscript, priced call by call, cached on disk."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass, field

from .. import ai_refs
from ..changes import Change, ChangeSet
from ..copyedit import claude_pass
from ..copyedit.cache import cache_dir
from ..docmodel import Manuscript
from ..queries import Query
from ..style import JournalStyle
from . import context
from .tools import TOOLS, Workspace

DEFAULTS = {"enabled": False, "model": "claude-opus-5-5", "effort": "medium", "budget_usd": 2.0, "max_turns": 16,
            "max_words": 20, "max_chars": 160, "min_confidence": "certain", "max_queries": 25, "examples": 60,
            "max_tokens": 16000}


def settings(style: JournalStyle) -> dict:
    """`copyedit.agent` from the journal style over DEFAULTS; EDITOS_AGENT_MODEL / _EFFORT / _BUDGET and
    EDITOS_CE_EXAMPLES override it."""
    cfg = {**DEFAULTS, **(style.get("copyedit.agent", {}) or {})}
    for env, key, conv in (("EDITOS_AGENT_MODEL", "model", str), ("EDITOS_AGENT_EFFORT", "effort", str),
                           ("EDITOS_AGENT_BUDGET", "budget_usd", float), ("EDITOS_CE_EXAMPLES", "examples_path", str),
                           ("EDITOS_AGENT_MIN_CONFIDENCE", "min_confidence", str)):
        if os.environ.get(env):
            cfg[key] = conv(os.environ[env])
    return cfg


def enabled(style: JournalStyle) -> bool:
    return bool(settings(style).get("enabled")) and ai_refs.available()


def price(model: str, usage: dict) -> float:
    pin, pout, read = claude_pass.PRICES.get(model, claude_pass.PRICES["claude-opus-5-5"])
    return (usage.get("input_tokens", 0) * pin + usage.get("cache_creation_input_tokens", 0) * pin * 1.25
            + usage.get("cache_read_input_tokens", 0) * pin * read + usage.get("output_tokens", 0) * pout) / 1e6


@dataclass
class AgentResult:
    changes: ChangeSet = field(default_factory=ChangeSet)
    queries: list[Query] = field(default_factory=list)
    usage: Counter = field(default_factory=Counter)
    cost: float = 0.0  # what the calls cost when they were made (cached replays included)
    spent: float = 0.0  # what this run paid (0 for a cached replay)
    turns: int = 0
    stop: str = ""
    log: list[dict] = field(default_factory=list)
    drops: Counter = field(default_factory=Counter)


class _Cache:
    def __init__(self, path: str | None = None):
        self.con = sqlite3.connect(path or cache_dir("agent.sqlite"), timeout=60, check_same_thread=False)
        self.lock = threading.Lock()
        self.con.execute("CREATE TABLE IF NOT EXISTS calls (key TEXT PRIMARY KEY, body TEXT)")

    @staticmethod
    def key(params: dict) -> str:
        return hashlib.sha256(json.dumps([context.PROMPT_VERSION, params], sort_keys=True,
                                         ensure_ascii=False).encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        with self.lock:
            row = self.con.execute("SELECT body FROM calls WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, body: dict) -> None:
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO calls VALUES (?, ?)", (key, json.dumps(body)))
            self.con.commit()


_cache: _Cache | None = None


def _cache_db() -> _Cache:
    global _cache
    if _cache is None:
        _cache = _Cache()
    return _cache


def _api_call(params: dict) -> dict:
    """One Messages API call; the response as plain data (content blocks as dicts, usage, stop reason)."""
    import anthropic

    client = anthropic.Anthropic(api_key=ai_refs._api_key(),
                                 base_url=os.environ.get("EDITOS_ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    extra = {}
    if params["model"] in ai_refs._FALLBACK_MODELS:  # a declined request is re-run on the recommended fallback
        extra = dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    response = client.beta.messages.create(**params, **extra)
    usage = {k: getattr(response.usage, k, 0) or 0 for k in
             ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
    return {"content": [b.model_dump(exclude_none=True) for b in response.content],
            "stop_reason": response.stop_reason, "usage": usage, "model": response.model}


def editable_paragraphs(ms: Manuscript) -> set[str]:
    return {p.id for p in claude_pass.paragraphs(ms)}


def run(ms: Manuscript, style: JournalStyle, call=None, article: str = "", resolver=None) -> AgentResult:
    """Run the agent on a manuscript. `call(params) -> response dict` replaces the API (tests)."""
    cfg = settings(style)
    model = cfg["model"]
    editable = editable_paragraphs(ms)
    text, ids = context.manuscript_text(ms, editable)
    ws = Workspace(ms, style, ids, editable, cfg, resolver=resolver)
    rules = context.rules_text(style, cfg.get("examples_path"), int(cfg.get("examples", 60)),
                               exclude={article} if article else set())
    system = [{"type": "text", "text": context.INSTRUCTIONS},
              {"type": "text", "text": rules, "cache_control": {"type": "ephemeral"}}]
    messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "# Manuscript\n" + text}, {
        "type": "text", "text": "Copy-edit this manuscript with the tools, then reply with one short line."}]}]
    res = AgentResult()
    budget = float(cfg["budget_usd"])
    warned = False
    for turn in range(int(cfg["max_turns"])):
        params = dict(model=model, max_tokens=int(cfg["max_tokens"]), system=system, tools=TOOLS,
                      messages=messages, thinking={"type": "adaptive"}, output_config={"effort": cfg["effort"]},
                      cache_control={"type": "ephemeral"})
        key = _Cache.key(params)
        got = _cache_db().get(key)
        if got is None:
            got = (call or _api_call)(params)
            _cache_db().put(key, got)
            res.spent += price(model, got["usage"])
        res.turns += 1
        res.usage["calls"] += 1
        for k, v in got["usage"].items():
            res.usage[k] += v
        res.cost += price(model, got["usage"])
        messages.append({"role": "assistant", "content": got["content"]})
        uses = [b for b in got["content"] if b.get("type") == "tool_use"]
        if got["stop_reason"] in ("refusal", "max_tokens") and not uses:
            res.stop = got["stop_reason"]
            break
        if not uses:
            res.stop = "done"
            break
        results = [{"type": "tool_result", "tool_use_id": b["id"],
                    "content": json.dumps(ws.call(b["name"], b.get("input") or {}), ensure_ascii=False)}
                   for b in uses]
        if res.cost >= budget:
            res.stop = "budget"
            break
        if turn + 1 >= int(cfg["max_turns"]):
            res.stop = "max_turns"
            break
        if not warned and (res.cost >= 0.7 * budget or turn + 2 >= int(cfg["max_turns"])):
            warned = True
            results.append({"type": "text", "text": "Budget nearly used: send your last edits and queries now, "
                                                    "in this turn, then stop."})
        messages.append({"role": "user", "content": results})
    # Held back after the loop, so the conversation (and its cache) is the same whatever the setting: on the tuning
    # articles "likely" edits agreed with the copy editor 39% of the time against 68% for "certain".
    for c in ws.changes:
        if cfg.get("min_confidence") == "certain" and c.provenance.get("confidence") != "certain":
            ws.drops["held back: likely"] += 1
            continue
        res.changes.add(c)
    res.queries = ws.queries
    res.log = ws.log
    res.drops = ws.drops
    return res


def collect(ms: Manuscript, style: JournalStyle, call=None) -> tuple[list[Change], list[Query]]:
    """copyedit.run_all's checker interface: nothing unless copyedit.agent.enabled and a key (or `call`)."""
    if call is None and not enabled(style):
        return [], []
    res = run(ms, style, call=call)
    return res.changes.changes, res.queries
