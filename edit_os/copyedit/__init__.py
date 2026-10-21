"""Copy-edit checkers (B1-B3): LanguageTool, British/American spelling, abbreviations.

Each checker proposes `Change` records (layer LANGUAGE, source RULE) and, where only the
author can answer, `Query` records. `run_all` resolves overlaps in checker order, like
`rules.run_all`. Settings live under `copyedit:` in the journal style.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..changes import Change, ChangeSet
from ..docmodel import Manuscript
from ..queries import Query
from ..style import JournalStyle
from . import abbrev, langtool, spelling


@dataclass
class Result:
    changes: ChangeSet = field(default_factory=ChangeSet)
    queries: list[Query] = field(default_factory=list)
    warnings: list[langtool.Warning] = field(default_factory=list)


def _spelling(ms, style, **_):
    return spelling.check(ms, style), [], []


def _abbreviations(ms, style, **_):
    changes, queries = abbrev.collect(ms, style)
    return changes, queries, []


def _languagetool(ms, style, lt_client=None, **_):
    changes, warnings = langtool.collect(ms, style, lt_client)
    return changes, [], warnings


CHECKERS = {
    "spelling": _spelling,
    "abbreviations": _abbreviations,
    "languagetool": _languagetool,
}


def run_all(ms: Manuscript, style: JournalStyle, only: list[str] | None = None,
            lt_client: langtool.Client | None = None, existing: list[Change] = ()) -> Result:
    """Run the checkers in order; drop proposals overlapping one already kept (or `existing`)."""
    res = Result()
    kept: list[Change] = list(existing)
    for name, checker in CHECKERS.items():
        if only and name not in only:
            continue
        changes, queries, warnings = checker(ms, style, lt_client=lt_client)
        for c in changes:
            if any(k.para_id == c.para_id and (c.start < k.end and k.start < c.end or c.start == k.start)
                   for k in kept):
                continue
            kept.append(c)
            res.changes.add(c)
        res.queries += queries
        res.warnings += warnings
    return res
