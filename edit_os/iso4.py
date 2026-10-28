"""ISO 4 journal title abbreviations from the ISSN Centre's List of Title Word Abbreviations (LTWA).

A journal NLM does not list has no NLM abbreviation; the editors abbreviate its title by ISO 4 all the same
("Interactive Technology and Smart Education" -> "Interact Technol Smart Educ", "Journal of the Learning
Sciences" -> "J Learn Sci"; owner, 2026-10-28). The rules, as ISO 4 and NLM write them:

- articles, conjunctions and prepositions go ("of", "the", "and");
- each other word is abbreviated by the LTWA: a whole-word entry ("journal" -> "j."), else the longest stem
  ("technolog-" -> "technol."); a word the list does not abbreviate ("n.a.", or no entry) stays in full;
- a title of one word stays in full; acronyms ("MIS", "BMC") and words with digits stay as written;
- Vancouver style: no periods, each word capitalised ("J Learn Sci").

The list is downloaded with the NLM list (`python -m edit_os journals update`); abbreviations come only from
it, never from a model.
"""

from __future__ import annotations

import csv
import io
import os
import re
import unicodedata
import urllib.request

LTWA_URL = "https://www.issn.org/wp-content/uploads/2021/07/ltwa_20210702.csv"
DEFAULT_PATH = os.path.join(os.environ.get("EDIT_OS_DATA", os.path.expanduser("~/.cache/edit-os")), "ltwa.csv")

_LANGS = {"eng", "mul", "und", "lat"}
_DROP = {"a", "an", "the", "and", "or", "of", "for", "in", "on", "at", "to", "by", "with", "from", "into", "&",
         "+", "upon", "via"}


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()


class LTWA:
    def __init__(self, text: str):
        self.words: dict[str, str | None] = {}    # "journal" -> "j."; None: not abbreviated
        self.stems: dict[str, str | None] = {}    # "technolog" -> "technol."
        self.phrases: dict[tuple[str, ...], str | None] = {}
        for row in csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"):
            if len(row) < 3 or row[0] == "WORDS":
                continue
            key, abbr = row[0].strip(), row[1].strip()
            langs = {x.strip() for x in row[2].split(",")}
            if not langs & _LANGS or not key or key.startswith("-"):
                continue
            key = re.sub(r"\s*\([^)]*\)", "", key)  # "band (book)": a note on the sense
            value = None if abbr.lower() == "n.a." else abbr.rstrip("-")
            k = _fold(key)
            if " " in k:
                self.phrases.setdefault(tuple(k.split()), value)
            elif k.endswith("-"):
                self.stems.setdefault(k.rstrip("-"), value)
            else:
                self.words.setdefault(k, value)

    def word(self, w: str) -> str:
        """One title word, abbreviated with its own capitalisation; as written when the list does not abbreviate it."""
        k = _fold(w)
        if k in self.words:
            return _shape(self.words[k], w)
        if k.endswith("s") and k[:-1] in self.words:  # plurals take the singular's abbreviation (ISO 4)
            return _shape(self.words[k[:-1]], w)
        for n in range(len(k), 1, -1):
            if k[:n] in self.stems:
                return _shape(self.stems[k[:n]], w)
        return w


def _shape(abbr: str | None, w: str) -> str:
    if not abbr:
        return w
    a = abbr.replace(".", "")
    if w.isascii():
        a = _fold(a)
    return a[:1].upper() + a[1:] if w[:1].isupper() else a


def abbreviate(title: str, ltwa: LTWA | None = None) -> str | None:
    """The title's ISO 4 abbreviation (no periods), or None without the list."""
    ltwa = ltwa or table()
    if ltwa is None:
        return None
    words = [w for w in re.split(r"\s+", title.strip().rstrip(".")) if w]
    kept = [w for i, w in enumerate(words) if _fold(w.strip(",:;")) not in _DROP or i == 0 and len(words) == 1]
    kept = [w.strip(",:;") for w in kept if w.strip(",:;")]
    if len(kept) <= 1:
        return " ".join(words)  # a one-word title stays in full
    out, i = [], 0
    while i < len(kept):
        for n in (3, 2):
            key = tuple(_fold(w) for w in kept[i:i + n])
            if len(key) == n and key in ltwa.phrases:
                v = ltwa.phrases[key]
                out.append(" ".join(kept[i:i + n]) if v is None else
                           " ".join(_shape(p, kept[i]) for p in v.split()))
                i += n
                break
        else:
            w = kept[i]
            keep = (w.isupper() and len(w) > 1) or any(c.isdigit() for c in w) or "(" in w
            out.append(w if keep else "-".join(ltwa.word(p) if p else p for p in w.split("-")))
            i += 1
    cap = lambda w: "-".join(p[:1].upper() + p[1:] for p in w.split("-"))  # noqa: E731
    return " ".join(cap(w) for w in out)  # "Journal of caring sciences" -> "J Caring Sci", "J E-Learn Knowl Soc"


def update(path: str = DEFAULT_PATH, url: str = LTWA_URL) -> int:
    with urllib.request.urlopen(url, timeout=120) as r:
        text = r.read().decode("utf-8", errors="replace")
    n = len(LTWA(text).words)
    if n < 1000:
        raise ValueError(f"LTWA download from {url} looks wrong ({n} words)")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".tmp", path)
    global _table
    _table = None
    return n


_table: LTWA | None = None


def table() -> LTWA | None:
    """Shared LTWA, or None when the list has not been downloaded."""
    global _table
    if _table is None and os.path.exists(DEFAULT_PATH):
        with open(DEFAULT_PATH, encoding="utf-8") as f:
            _table = LTWA(f.read())
    return _table
