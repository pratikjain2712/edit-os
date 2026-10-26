"""British / American spelling convention from VarCon (SCOWL's variant lists, open source).

Only variant conversion is done: a word is changed only when VarCon lists it as the preferred
spelling of one convention and gives the other convention's preferred form. Nothing is flagged
as a misspelling, so medical vocabulary that is not in a general dictionary is never touched.

Style key `copyedit.spelling.convention`:
  british        American forms -> British (-ise by default; `british_z: true` keeps Oxford -ize)
  american       British forms  -> American
  follow_author  the minority convention in the manuscript is made consistent with the majority
  none           off
`copyedit.spelling.journals` maps journal codes to a convention and wins over the default.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from .. import provenance
from ..changes import Change, Layer
from ..docmodel import Manuscript
from ..style import JournalStyle
from .cache import cache_dir, download
from .common import change, overlaps, prose_paragraphs, protected_spans

VARCON_URL = "https://raw.githubusercontent.com/en-wl/wordlist/master/varcon/varcon.txt"

_HEAD = re.compile(r"^#.*\(level (\d+)\)")


@dataclass
class Variants:
    to_american: dict[str, str] = field(default_factory=dict)
    to_british: dict[str, str] = field(default_factory=dict)   # -ise
    to_oxford: dict[str, str] = field(default_factory=dict)    # British with -ize (VarCon "Z")
    american: set[str] = field(default_factory=set)  # American-only preferred forms
    british: set[str] = field(default_factory=set)   # British-only preferred forms


def _tags(tags: str) -> set[str]:
    """VarCon tags of one spelling: "A", "B." (equal variant) count as preferred; "Av", "BV",
    "B-", "Bx" (variants) do not."""
    return {t[0] for t in tags.split() if len(t) == 1 or t[1:] == "."}


def parse(lines, max_level: int = 70, min_length: int = 4) -> Variants:
    """Parse VarCon's varcon.txt. Clusters above `max_level` (rare words, odd forms such as
    "colourectal"), proper nouns, short words and sense-specific lines ("prise | otherwise")
    give no conversions. A word that is a preferred form of the target convention anywhere
    ("analyses", plural of analysis) is never converted."""
    v = Variants()
    ok = {"A": set(), "B": set(), "Z": set()}
    pending = []
    level = 0
    for line in lines:
        line = line.strip()
        if line.startswith("#"):
            m = _HEAD.match(line)
            level = int(m.group(1)) if m else 100
            continue
        if not line:
            continue
        sense_specific = "|" in line
        line = line.split("|", 1)[0].strip()
        forms: dict[str, set[str]] = {}
        for group in line.split(" / "):
            tags, _, word = group.partition(": ")
            if word and " " not in word:
                forms.setdefault(word.strip(), set()).update(_tags(tags))
        for w, tags in forms.items():
            for t in tags & ok.keys():
                ok[t].add(w.lower())
        if not sense_specific and level <= max_level:
            pending.append(forms)
    for forms in pending:
        pref = {}
        for w, tags in forms.items():
            for t in ("A", "B", "Z"):
                if t in tags:
                    pref.setdefault(t, w)
        a, b = pref.get("A"), pref.get("B")
        z = pref.get("Z") or b  # no Z tag: Oxford spelling is the British one
        if not a or not b or a == b or not a.islower() or not b.islower():
            continue
        for w, tags in forms.items():
            lw = w.lower()
            if len(lw) < min_length or not w.replace("'", "").islower():
                continue
            if lw not in ok["A"] and ("B" in tags or "Z" in tags):
                v.to_american.setdefault(lw, a)
            if lw not in ok["B"] and "A" in tags:
                v.to_british.setdefault(lw, b)
            if lw not in ok["Z"] and lw != z.lower() and ("A" in tags or "B" in tags):
                v.to_oxford.setdefault(lw, z)
            if "A" in tags and lw not in ok["B"]:
                v.american.add(lw)
            if "B" in tags and lw not in ok["A"]:
                v.british.add(lw)
    return v


def varcon_path() -> str:
    return download(VARCON_URL, cache_dir("varcon", "varcon.txt"))


@lru_cache(maxsize=4)
def load(path: str | None = None, max_level: int = 70) -> Variants:
    path = path or varcon_path()
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse(f, max_level)


# A word, also as part of a hyphenated compound ("greyish-white"); not inside a path or code ("a/b").
_WORD = re.compile(r"(?<![\w/])[A-Za-z]+(?:'s)?(?![\w/])")


def _match_case(src: str, new: str) -> str:
    if src.isupper() and len(src) > 1:
        return new.upper()
    if src[0].isupper():
        return new[0].upper() + new[1:]
    return new


def _candidates(ms: Manuscript, skip: set[str]):
    """(paragraph, match) for every word worth checking: not inside protected spans, not an
    all-caps acronym, and not part of a capitalised name ("World Health Organization")."""
    for p in prose_paragraphs(ms):
        prot = protected_spans(p.text)
        words = list(_WORD.finditer(p.text))
        for i, m in enumerate(words):
            w = m.group(0)
            if w.lower() in skip or overlaps(m.start(), m.end(), prot) or (w.isupper() and len(w) > 1):
                continue
            start = m.start()
            compound = re.match(r"[A-Za-z]+-$", p.text[max(0, start - 30):start].split()[-1]) \
                if start and p.text[start - 1] == "-" and p.text[max(0, start - 30):start].split() else None
            if compound and compound.group(0)[0].isupper() and not w[0].isupper():
                # the tail of a capitalised compound ("Risk Of Bias In Non-randomised Studies"): judged as the
                # compound is, so a name keeps the spelling it was given
                w0 = compound.group(0)
                start -= len(w0)
            else:
                w0 = w
            if w0[0].isupper():
                before = p.text[:start].rstrip()
                sentence_start = not before or before[-1] in ".?!:" or before.endswith(">")
                nxt = words[i + 1].group(0) if i + 1 < len(words) else ""
                gap = p.text[m.end():words[i + 1].start()] if nxt else ""
                if not sentence_start or (nxt[:1].isupper() and gap.strip() in ("", "of", "for", "and", "&")):
                    continue  # proper noun / institution name
            yield p, m


def majority(ms: Manuscript, v: Variants, skip: set[str] = frozenset()) -> str | None:
    n = Counter()
    for _, m in _candidates(ms, set(skip)):
        w = m.group(0).lower()
        if w in v.american:
            n["american"] += 1
        elif w in v.british:
            n["british"] += 1
    if n["american"] == n["british"]:
        return None
    return "american" if n["american"] > n["british"] else "british"


def convention(style: JournalStyle, ms: Manuscript | None = None, variants: Variants | None = None) -> str | None:
    """The journal's convention ("british" / "american"), resolving follow_author on `ms`."""
    by_journal = {k.upper(): v for k, v in (style.get("copyedit.spelling.journals", {}) or {}).items()}
    conv = by_journal.get(style.code.upper()) or style.get("copyedit.spelling.convention", "follow_author")
    if conv == "follow_author":
        if ms is None:
            return None
        skip = {w.lower() for w in style.get("copyedit.spelling.skip", []) or []}
        v = variants or load(max_level=style.get("copyedit.spelling.max_level", 70))
        return majority(ms, v, skip)
    return conv if conv in ("british", "american") else None


def check(ms: Manuscript, style: JournalStyle, variants: Variants | None = None) -> list[Change]:
    if not style.get("copyedit.spelling.enabled", True):
        return []
    v = variants or load(max_level=style.get("copyedit.spelling.max_level", 70))
    conv = convention(style, ms, v)
    if conv is None:
        return []
    skip = {w.lower() for w in style.get("copyedit.spelling.skip", []) or []}
    if conv == "american":
        table = v.to_american
    elif style.get("copyedit.spelling.british_z"):
        table = v.to_oxford
    else:
        table = v.to_british
    extra = {k.lower(): v for k, v in (style.get(f"copyedit.spelling.extra.{conv}") or {}).items()}
    out = []
    for p, m in _candidates(ms, skip):
        w = m.group(0)
        new = table.get(w.lower()) or extra.get(w.lower())
        if new and w.lower() in extra and re.match(r"\s+scales?\b", p.text[m.end():], re.I):
            continue  # "visual analogue scale": the instrument's name is kept as written
        if new and new.lower() != w.lower():
            c = change(p, m.start(), m.end(), _match_case(w, new), "spelling", reason=f"spelling ({conv})")
            c.provenance = provenance.make("checker", f"Spelling: {conv.capitalize()} convention",
                                           f"{w} → {c.new} (VarCon word list)", checker="spelling", convention=conv)
            out.append(c)
    return out
