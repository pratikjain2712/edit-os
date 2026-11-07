"""Claude spend per article (owner, 2026-10-29).

Every Claude call adds its tokens and cost to the ledger of the article being processed: `track()` opens a
ledger for the run (a context variable, so jobs running in parallel threads keep their own), and each caller
reports with `add(part, model, ...)`. The pipeline writes the ledger to the article's `spend.json`; the server
appends later steps (Autopilot's second check) to the same file and keeps a running log of all articles.

Parts: front_matter, references, reference_web_search, copy_edit, editing_agent, autopilot_check.

Re-runs count as new runs (owner, 2026-11-06): a cached answer costs nothing to replay, but the article is priced
as if Claude had been asked again. Each live call's cost is remembered under its cache key (`keyed`), and a
replay (`replay`) adds that cost to the run's full price, `usd_full`; an answer cached before this was kept is
priced from its text (about 4 characters a token). `usd` stays what Anthropic actually billed.
"""

from __future__ import annotations

import contextvars
import json
import os
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone

FILE = "spend.json"
WORDS_PER_PAGE = 300


def inr_rate() -> float:
    """Rupees per dollar for showing the spend (EDITOS_USD_INR; default 100: owner, 2026-10-30). The log itself stays in dollars, as
    Anthropic bills."""
    try:
        return float(os.environ.get("EDITOS_USD_INR") or 100)
    except ValueError:
        return 100.0

_current: contextvars.ContextVar["Ledger | None"] = contextvars.ContextVar("edit_os_spend", default=None)
_key: contextvars.ContextVar["str | None"] = contextvars.ContextVar("edit_os_spend_key", default=None)
CHARS_PER_TOKEN = 4


def price_inr_per_page() -> float:
    """What a customer pays per 300-word page, in rupees (EDITOS_PRICE_INR_PER_PAGE; default 10.24: owner,
    2026-11-07). Every upload is priced as a new article."""
    try:
        return float(os.environ.get("EDITOS_PRICE_INR_PER_PAGE") or 10.24)
    except ValueError:
        return 10.24


def price_inr(words: int | None) -> float | None:
    """The article's price: its 300-word pages at the per-page rate."""
    return round(words / WORDS_PER_PAGE * price_inr_per_page(), 2) if words else None


def server_usd_month() -> float:
    """What the server (AWS EC2 t3.medium, its 20 GB disk, S3 and traffic) costs a month, in dollars
    (EDITOS_SERVER_USD_MONTH; default 35). Fixed, whatever the number of articles."""
    try:
        return float(os.environ.get("EDITOS_SERVER_USD_MONTH") or 35)
    except ValueError:
        return 35.0


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
            p["usd_full"] += cost
            self.models[model.removesuffix(" (batch)")] += cost

    def add_replay(self, part: str, cost: float) -> None:
        with self.lock:
            p = self.parts.setdefault(part, Counter())
            p["replayed"] += 1
            p["usd_full"] += cost

    def total(self) -> float:
        return sum(p["usd"] for p in self.parts.values())

    def total_full(self) -> float:
        return sum(p["usd_full"] for p in self.parts.values())

    def to_dict(self) -> dict:
        def part(p):
            return {**{n: v for n, v in p.items() if n not in ("usd", "usd_full") and v},
                    "usd": round(p["usd"], 4), "usd_full": round(p["usd_full"], 4)}
        return {"usd": round(self.total(), 4), "usd_full": round(self.total_full(), 4),
                "parts": {k: part(p) for k, p in sorted(self.parts.items())},
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
    """Report one Claude call to the current article's ledger (nothing when no article is being tracked); inside
    `keyed(key)` its cost is also remembered under that cache key, for pricing later replays."""
    led = _current.get()
    if led is not None:
        led.add(part, model, **kw)
    key = _key.get()
    if key:
        cost = kw.get("cost")
        if cost is None:
            cost = usd(model, kw.get("input_tokens", 0), kw.get("output_tokens", 0), kw.get("cache_write", 0),
                       kw.get("cache_read", 0), kw.get("batch", False))
        _memo().add(key, cost)


@contextmanager
def keyed(key: str):
    """The live call(s) inside the block answer the cache entry KEY: their cost is remembered under it."""
    token = _key.set(key)
    try:
        yield
    finally:
        _key.reset(token)


def estimate(model: str, prompt, answer, searches: int = 0) -> float:
    """Dollars for a call from its text alone (about 4 characters a token, all input priced as new)."""
    def chars(x):
        return len(x) if isinstance(x, str) else len(json.dumps(x, ensure_ascii=False))
    return usd(model, chars(prompt) // CHARS_PER_TOKEN, chars(answer) // CHARS_PER_TOKEN) + searches * 0.01


def replay(part: str, key: str, model: str, prompt, answer) -> None:
    """A cached answer was used for this article: its full price is what the live call cost (remembered under
    KEY), else an estimate from the text. Nothing is billed."""
    led = _current.get()
    if led is None:
        return
    cost = _memo().get(key) if key else None
    led.add_replay(part, estimate(model, prompt, answer) if cost is None else cost)


def replay_cost(part: str, cost: float) -> None:
    """A cached answer whose cost is known (kept with the answer)."""
    led = _current.get()
    if led is not None:
        led.add_replay(part, cost)


class _Memo:
    """Cache key -> what its live call cost (spend_memo.sqlite next to the answer caches)."""

    def __init__(self, path: str | None = None):
        from . import shared_db
        if path is None:
            from . import ai_refs
            path = os.path.join(os.path.dirname(ai_refs.CACHE_DB), "spend_memo.sqlite")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.con = shared_db.connect(path)
        self.con.execute("CREATE TABLE IF NOT EXISTS memo (key TEXT PRIMARY KEY, usd REAL)")
        self.con.commit()

    def get(self, key: str) -> float | None:
        row = self.con.execute("SELECT usd FROM memo WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def add(self, key: str, cost: float) -> None:
        self.con.execute("INSERT INTO memo VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET usd = usd + excluded.usd",
                         (key, cost))
        self.con.commit()


_memo_db: _Memo | None = None
_memo_lock = threading.Lock()


def _memo() -> _Memo:
    global _memo_db
    with _memo_lock:
        if _memo_db is None:
            try:
                _memo_db = _Memo()
            except (OSError, sqlite3.Error):
                _memo_db = _Memo(":memory:")
        return _memo_db


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
    rec["usd_full"] = round(sum(s.get("usd_full", s["usd"]) for s in rec["steps"]), 4)
    pages = (rec.get("words") or 0) / WORDS_PER_PAGE
    rec["pages"] = round(pages, 1)
    rec["usd_per_page"] = round(rec["usd"] / pages, 4) if pages else None
    rec["usd_full_per_page"] = round(rec["usd_full"] / pages, 4) if pages else None
    with open(os.path.join(outdir, FILE), "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
    return rec


def ensure_words(outdir: str) -> dict:
    """OUTDIR/spend.json with the article's word count. A job edited before the spend log was kept (2026-10-29)
    has none: its words are counted from the edited source, as `pipeline.edit` does, and saved with no Claude
    spend recorded (`untracked`), so it is still priced."""
    rec = load(outdir)
    if rec.get("words"):
        return rec
    try:
        import re
        from .pipeline import paragraph_texts, source_path
        words = sum(len(re.findall(r"\w+", t)) for t in paragraph_texts(source_path(outdir)).values())
    except Exception:  # noqa: BLE001 - no source to count from: left unpriced
        return rec
    if not words:
        return rec
    rec = rec or {"steps": [], "usd": 0, "untracked": True}
    rec["words"] = words
    rec["pages"] = round(words / WORDS_PER_PAGE, 1)
    try:
        with open(os.path.join(outdir, FILE), "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
    except OSError:
        pass
    return rec
