"""Claude spend per article (owner, 2026-10-29).

Every Claude call adds its tokens and cost to the ledger of the article being processed: `track()` opens a
ledger for the run (a context variable, so jobs running in parallel threads keep their own), and each caller
reports with `add(part, model, ...)`. The pipeline writes the ledger to the article's `spend.json`; the server
appends later steps (Autopilot's second check) to the same file and keeps a running log of all articles.

Parts: front_matter, references, reference_web_search, copy_edit, editing_agent, autopilot_check.
Cached replays cost nothing and are not reported.
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone

FILE = "spend.json"
WORDS_PER_PAGE = 300

_current: contextvars.ContextVar["Ledger | None"] = contextvars.ContextVar("edit_os_spend", default=None)


def _prices(model: str) -> tuple[float, float, float]:
    """$ per million input / output tokens and the cache-read factor (copyedit/claude_pass.PRICES); an unknown
    model is priced as the dearest, so the log never under-reports."""
    from .copyedit.claude_pass import PRICES

    base = model.removesuffix(" (batch)")
    return PRICES.get(base) or max(PRICES.values())


def usd(model: str, input_tokens=0, output_tokens=0, cache_write=0, cache_read=0, batch=False) -> float:
    pin, pout, read = _prices(model)
    total = (input_tokens * pin + cache_write * pin * 1.25 + cache_read * pin * read + output_tokens * pout) / 1e6
    return total * (0.5 if batch else 1)


class Ledger:
    def __init__(self):
        self.lock = threading.Lock()
        self.parts: dict[str, Counter] = {}
        self.models: Counter = Counter()

    def add(self, part: str, model: str, input_tokens=0, output_tokens=0, cache_write=0, cache_read=0,
            cost: float | None = None, batch=False, searches=0) -> None:
        cost = usd(model, input_tokens, output_tokens, cache_write, cache_read, batch) if cost is None else cost
        with self.lock:
            p = self.parts.setdefault(part, Counter())
            p["calls"] += 1
            p["input_tokens"] += input_tokens + cache_write + cache_read
            p["output_tokens"] += output_tokens
            p["searches"] += searches
            p["usd"] += cost
            self.models[model.removesuffix(" (batch)")] += cost

    def total(self) -> float:
        return sum(p["usd"] for p in self.parts.values())

    def to_dict(self) -> dict:
        return {"usd": round(self.total(), 4),
                "parts": {k: {**{n: v for n, v in p.items() if n != "usd" and v}, "usd": round(p["usd"], 4)}
                          for k, p in sorted(self.parts.items())},
                "models": {m: round(v, 4) for m, v in self.models.items()}}


@contextmanager
def track():
    """A ledger for everything Claude does inside the block (on this thread / context)."""
    led = Ledger()
    token = _current.set(led)
    try:
        yield led
    finally:
        _current.reset(token)


def add(part: str, model: str, **kw) -> None:
    """Report one Claude call to the current article's ledger (nothing when no article is being tracked)."""
    led = _current.get()
    if led is not None:
        led.add(part, model, **kw)


def add_usage(part: str, model: str, usage, batch=False) -> None:
    """`add` from an API response's `usage` (object or dict)."""
    g = (lambda k: usage.get(k, 0) or 0) if isinstance(usage, dict) else (lambda k: getattr(usage, k, 0) or 0)
    add(part, model, input_tokens=g("input_tokens"), output_tokens=g("output_tokens"),
        cache_write=g("cache_creation_input_tokens"), cache_read=g("cache_read_input_tokens"), batch=batch)


# --- the article's spend.json ---------------------------------------------------------------------

def load(outdir: str) -> dict:
    try:
        with open(os.path.join(outdir, FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def record(outdir: str, step: str, led: Ledger, words: int | None = None, **info) -> dict:
    """Append one step (edit, autopilot, ...) to OUTDIR/spend.json and return the whole record: the steps, the
    article's total, its words and the cost per 300-word page."""
    rec = load(outdir) or {"steps": []}
    rec["steps"].append({"step": step, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         **led.to_dict()})
    if words is not None:
        rec["words"] = words
    rec.update({k: v for k, v in info.items() if v is not None})
    rec["usd"] = round(sum(s["usd"] for s in rec["steps"]), 4)
    pages = (rec.get("words") or 0) / WORDS_PER_PAGE
    rec["pages"] = round(pages, 1)
    rec["usd_per_page"] = round(rec["usd"] / pages, 4) if pages else None
    with open(os.path.join(outdir, FILE), "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
    return rec
