"""Copy-edit checkers (B1-B4): LanguageTool, British/American spelling, abbreviations, Claude pass.

Each checker proposes `Change` records (layer LANGUAGE, source RULE) and, where only the
author can answer, `Query` records. `run_all` resolves overlaps in checker order, like
`rules.run_all`; the Claude pass (source AI) runs last, so rules and the deterministic checkers win. Settings live under `copyedit:` in the journal style.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import provenance
from ..changes import Change, ChangeSet
from ..docmodel import Manuscript
from ..queries import Query
from ..style import JournalStyle
from . import abbrev, claude_pass, langtool, spelling


@dataclass
class Result:
    changes: ChangeSet = field(default_factory=ChangeSet)
    queries: list[Query] = field(default_factory=list)
    warnings: list[langtool.Warning] = field(default_factory=list)


def _spelling(ms, style, **_):
    return spelling.check(ms, style), [], []


def _text(ms, style, **_):
    from .. import rules_text
    return rules_text.collect(ms, style), [], []


def _abbreviations(ms, style, **_):
    changes, queries = abbrev.collect(ms, style)
    return changes, queries, []


def _languagetool(ms, style, lt_client=None, **_):
    changes, warnings = langtool.collect(ms, style, lt_client)
    return changes, [], warnings


def _claude(ms, style, claude_call=None, **_):
    changes, queries = claude_pass.collect(ms, style, call=claude_call)
    return changes, queries, []


CHECKERS = {
    "spelling": _spelling,
    "text": _text,  # edit_os/rules_text.py: prefixes, number words, ranges, ... (settings under `text:`)
    "abbreviations": _abbreviations,
    "languagetool": _languagetool,
    "claude": _claude,  # needs EDITOS_ANTHROPIC_API_KEY and copyedit.claude.enabled; last, so it never wins an overlap
}

# Checkers whose suggestions carry no provenance of their own: the chip names the checker.
_CHECKER_SOURCE = {"spelling": "Spelling checker", "text": "Copy-edit text rule", "abbreviations": "Abbreviation checker",
                   "languagetool": "LanguageTool"}


def run_all(ms: Manuscript, style: JournalStyle, only: list[str] | None = None,
            lt_client: langtool.Client | None = None, existing: list[Change] = (),
            claude_call=None) -> Result:
    """Run the checkers in order; drop proposals overlapping one already kept (or `existing`).
    Formatting-only changes (e.g. colour removed over a whole paragraph) do not block text edits."""
    res = Result()
    kept: list[Change] = [c for c in existing if not c.format_only]
    for name, checker in CHECKERS.items():
        if only and name not in only:
            continue
        changes, queries, warnings = checker(ms, style, lt_client=lt_client, claude_call=claude_call)
        for c in changes:
            if any(k.para_id == c.para_id and (c.start < k.end and k.start < c.end or c.start == k.start)
                   for k in kept):
                continue
            if c.provenance is None and name in _CHECKER_SOURCE:
                c.provenance = provenance.make("checker", _CHECKER_SOURCE[name], c.reason, checker=name)
            kept.append(c)
            res.changes.add(c)
        res.queries += queries
        res.warnings += warnings
    return res
