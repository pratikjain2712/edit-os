"""A journal's stylesheet and the pre-editing guide as searchable rule lines.

Both are client material: they are never in the repository. They are read at runtime from the directories
EDITOS_STYLESHEETS (one .txt per journal, matched by the journal code in the file name) and EDITOS_PREEDIT_GUIDE
(the guide's .txt). Used by tools/stylesheet_check.py (retrieval for the judge) and edit_os/rule_sources.py
(the stylesheet line behind each style suggestion, for the review screen).
"""

from __future__ import annotations

import collections
import glob
import math
import os
import re
from dataclasses import dataclass

from . import style as style_mod

_STOP = set("""a an and are as at be by for from if in is it its of on or should the then this to with be
been not no any all only into than that these those which when where while will can may please kindly
case present given e g eg""".split())


def _words(s: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", s.lower()):
        if w in _STOP or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


@dataclass
class Rule:
    id: str
    text: str
    line: int = 0  # 1-based line in the file it was read from


def _is_heading(line: str) -> bool:
    words = line.split()
    return 0 < len(words) <= 6 and "|" not in line and not line.rstrip().endswith((".", ":", ";")) and (
        line.isupper() or len(words) <= 3 and not re.search(r"[:\[\]\d]", line))


def load_rules(path: str, prefix: str) -> list[Rule]:
    """One rule per stylesheet line, split at ' / ' into sub-rules; each keeps its section label
    (the text before ' | ', or the last heading line above it)."""
    out: list[Rule] = []
    section = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [" ".join(x.split()) for x in f]
    for n, line in enumerate(lines, 1):
        if not line:
            continue
        if _is_heading(line):
            section = line
            continue
        if " | " in line:
            label, body = line.split(" | ", 1)
            section = label
        else:
            label, body = section, line
        parts = [p.strip() for p in body.split(" / ") if p.strip()] if len(body) > 220 else [body]
        for p in parts:
            if len((label + " " + p).split()) < 2:
                continue
            out.append(Rule(f"{prefix}{len(out) + 1}", f"{label} | {p}" if label else p, n))
    return out


class RuleIndex:
    """Okapi BM25 over rule lines."""

    def __init__(self, rules_: list[Rule], k1: float = 1.4, b: float = 0.75):
        self.rules = rules_
        self.docs = [_words(r.text) for r in rules_]
        self.k1, self.b = k1, b
        self.avg = sum(map(len, self.docs)) / (len(self.docs) or 1)
        df = collections.Counter(w for d in self.docs for w in set(d))
        n = len(self.docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.tf = [collections.Counter(d) for d in self.docs]

    def examples(self, k: int = 2) -> list[Rule]:
        """The stylesheet's example journal references ("Journals | Doe J, ... J Abbr 2020;5:10-7."): they
        show the house format (punctuation, dashes, issue numbers) that no rule line spells out."""
        return [r for r in self.rules if re.match(r"journals?\s*\|", r.text, re.I)][:k]

    def search(self, terms: list[str], k: int) -> list[Rule]:
        q = collections.Counter(terms)
        scored = []
        for i, (tf, d) in enumerate(zip(self.tf, self.docs)):
            s = 0.0
            for w, qn in q.items():
                if w in tf:
                    f = tf[w]
                    s += qn * self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / self.avg))
            if s > 0:
                scored.append((s, i))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [self.rules[i] for _, i in scored[:k]]


def _canon(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("…", "...")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s.strip(" .\"'")


def quote_ok(quote: str, sources: list[str]) -> bool:
    """The quote occurs (normalised) in one of the source texts. Ellipses split it into parts that must
    all occur, in order, in the same source."""
    parts = [_canon(p) for p in re.split(r"\.\.\.|…", quote or "") if _canon(p)]
    if not parts or sum(len(p) for p in parts) < 8:
        return False
    for src in sources:
        s, pos = _canon(src), 0
        for p in parts:
            i = s.find(p, pos)
            if i < 0:
                break
            pos = i + len(p)
        else:
            return True
    return False


# --- stylesheets per journal --------------------------------------------------------------------

_SHEET_STOP = {"stylesheet", "sample", "new", "updated", "update", "updates", "journal", "rg", "comments", "txt"}


def stylesheet_codes(filename: str) -> list[str]:
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r"\[.*?\]", " ", stem)
    toks = [t for t in re.split(r"[\s_&\-]+", stem) if t and t.lower() not in _SHEET_STOP and not re.search(r"\d", t)]
    return [style_mod.resolve(t) for t in toks]


def find_stylesheet(journal: str, directory: str | None) -> str | None:
    if not directory or not os.path.isdir(directory):
        return None
    code = style_mod.resolve(journal)
    for p in sorted(glob.glob(os.path.join(directory, "*.txt"))):
        if code in stylesheet_codes(p):
            return p
    return None
