"""Clean up and split references into fields with Claude, before the PubMed / CrossRef lookup.

Claude's output is used only to build better searches and to classify the source type. The
reference text that goes into the manuscript always comes from the matched database record,
or stays as the author wrote it; it is never taken from the model.

Needs EDITOS_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY). Without it, `available()` is False and the
rule-based parser is used. Requests go to EDITOS_ANTHROPIC_BASE_URL, default api.anthropic.com, so a
base URL set for some other tool in the environment is not picked up.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3

from .journals import DEFAULT_DB

MODEL = os.environ.get("EDITOS_MODEL", "claude-haiku-5-5")  # same IJO score as Opus 5.5 at ~1/40 the cost
# Models that accept the server-side refusal fallback ("default" form); Haiku has none.
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")
CACHE_DB = os.path.join(os.path.dirname(DEFAULT_DB), "ai_cache.sqlite")
KINDS = ["journal_article", "book", "book_chapter", "website", "report", "thesis", "conference", "other"]

_FIELD = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "kind": {"type": "string", "enum": KINDS},
                    "authors": {"type": "array", "items": _FIELD},
                    "title": _FIELD, "year": _FIELD, "doi": _FIELD, "pmid": _FIELD,
                },
                # Only what the search needs; journal, volume and pages come from the matched record.
                "required": ["index", "kind", "authors", "title", "year", "doi", "pmid"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["entries"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You split bibliography entries from medical journal manuscripts into fields.
For each numbered entry, return its fields exactly as written in the entry, with obvious typing
slips cleaned up (stray spaces, doubled punctuation, broken line wraps). Do not add, guess or
correct any fact that is not in the entry: if a field is missing, return an empty string.
- authors: one string per author as "Surname Initials" (e.g. "Shields CL"); keep group authors whole;
  leave out "et al".
- title: the full article or chapter title, including any part after an inner full stop.
- year: four digits. doi and pmid: only if present in the entry.
- kind: journal_article, book, book_chapter, website, report, thesis, conference or other."""


def _api_key() -> str:
    return os.environ.get("EDITOS_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")


def available() -> bool:
    return bool(_api_key())


class _Cache:
    def __init__(self, path: str = CACHE_DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.execute("CREATE TABLE IF NOT EXISTS parsed (key TEXT PRIMARY KEY, body TEXT)")

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(f"{MODEL}\n{INSTRUCTIONS}\n{text}".encode()).hexdigest()

    def get(self, text: str) -> dict | None:
        row = self.con.execute("SELECT body FROM parsed WHERE key = ?", (self.key(text),)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, text: str, fields: dict) -> None:
        self.con.execute("INSERT OR REPLACE INTO parsed VALUES (?, ?)", (self.key(text), json.dumps(fields)))
        self.con.commit()


_cache: _Cache | None = None
USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0}  # this process, for cost reporting


def _call(numbered: str) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic(
        api_key=_api_key(),
        base_url=os.environ.get("EDITOS_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    )
    request = dict(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=INSTRUCTIONS,
        messages=[{"role": "user", "content": numbered}],
    )
    if MODEL in _FALLBACK_MODELS:
        # A declined request is re-run on Anthropic's recommended fallback model.
        response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request)
    else:
        response = client.messages.create(**request)
    USAGE["calls"] += 1
    USAGE["input_tokens"] += response.usage.input_tokens
    USAGE["output_tokens"] += response.usage.output_tokens
    if response.stop_reason in ("refusal", "max_tokens"):
        return []
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)["entries"]
    except (ValueError, KeyError):
        return []


def parse_batch(texts: list[str], batch_size: int = 40) -> list[dict | None]:
    """Fields for each reference text (None where Claude gave nothing). Cached by text."""
    global _cache
    if _cache is None:
        _cache = _Cache()
    out: list[dict | None] = [_cache.get(t) for t in texts]
    todo = [i for i, f in enumerate(out) if f is None]
    for start in range(0, len(todo), batch_size):
        chunk = todo[start:start + batch_size]
        numbered = "\n".join(f"{k + 1}. {texts[i]}" for k, i in enumerate(chunk))
        for entry in _call(numbered):
            k = entry.get("index", 0) - 1
            if 0 <= k < len(chunk):
                out[chunk[k]] = entry
                _cache.put(texts[chunk[k]], entry)
    return out
