"""Author-year articles numbered (S320, S326; owner, 2026-10-27).

S320 (JEHP): 'If citations are present in "surname, year" format and not in numbers then change them to respective
reference numbers'. When the whole article cites by name and year and its reference list is unnumbered (an APA list
in alphabetical order, jehp_997), the owner's decision (2026-10-27) is to number the references in order of first
citation and to turn every citation into its number:

  "(Al-Ali & Shattnawi, 2018; Li, 2023)."  -> ".[1,2]"           (a bracketed group: the numbers, after punctuation)
  "Hoekstra et al. (2023) found"           -> "Hoekstra et al.[3] found"   (a narrative mention keeps the name)

Nothing is guessed: a name and year resolve only to the one entry whose first author (and second author for "A & B")
and year agree; initials before the name ("J. S. Hong et al.") must agree with the first author's. A group item that
resolves to no entry, or to more than one, stays in the text as written and is queried; so is a narrative mention.
References never cited go after the cited ones, in their list order, and are queried as uncited.

The list is renumbered in place, as `renumber` does for a numbered list: each reference paragraph gets the entry that
belongs at its position (with the reference rules' own restyling: Vancouver from PubMed / CrossRef or from the
author's text), and the paragraphs are numbered (Word's numbering, or a typed "1. " label).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import provenance, refengine, references, renumber
from .changes import Change, Layer
from .docmodel import Manuscript, Paragraph
from .style import JournalStyle

QUERY_TEXT = {
    "references.author_year_numbered": "The name–year citations have been changed to numbered citations and the "
                                       "references numbered in order of first citation (as per journal style). "
                                       "Kindly check.",
    "references.author_year_unmatched": "The citation “{cite}” could not be matched to one reference in the list. "
                                        "Kindly provide the reference or correct the citation.",
}

_YEAR = r"(?:19|20)\d{2}[a-z]?"
_YEARS = rf"{_YEAR}(?:\s*,\s*{_YEAR})*"
_NAME = r"[A-ZÀ-ÖØ-ÞĀ-Ž][\w’'\-]+(?:\s+[A-ZÀ-ÖØ-ÞĀ-Ž][\w’'\-]+){0,2}"
_INITIALS = r"(?:[A-Z]\.\s*(?:-\s*)?){1,3}"
# One item of a bracketed group: "J. S. Hong et al., 2024", "Ttofi & Farrington, 2011", "Li, 2023", "e.g., Li, 2023",
# "De Jong et al., 2022a, 2022b".
_ITEM = re.compile(rf"^\s*(?:(?:e\.g\.|i\.e\.|see(?:\s+also)?|cf\.)\s*,?\s*)?(?P<ini>{_INITIALS})?(?P<a>{_NAME})"
                   rf"(?:\s+(?P<etal>et\s+al\.?)|\s*(?:&|\band\b)\s*(?P<b>{_NAME}))?\s*,?\s+(?P<years>{_YEARS})\s*$")
_GROUP = re.compile(r"(?P<lead>[  ]*)\((?P<inner>[^()]{4,600}?)\)(?P<punct>[.,;:]?)")
# A narrative mention: "Hoekstra et al. (2023)", "Ttofi and Farrington (2011)", "Salmivalli (2023)".
_NARRATIVE = re.compile(rf"(?P<ini>{_INITIALS})?(?P<a>{_NAME})(?:\s+(?P<etal>et\s+al\.?)|\s+(?:&|and)\s+(?P<b>{_NAME}))?"
                        rf"(?P<lead>[  ]*)\((?P<years>{_YEARS})\)")
_ENTRY_YEAR = re.compile(r"\((?P<year>(?:19|20)\d{2})(?P<suffix>[a-z]?)(?:,[^)]*)?\)")


def _key(s: str) -> str:
    return re.sub(r"[^a-z]", "", refengine.fold(s))


@dataclass
class Entry:
    ref: references.Reference
    position: int  # 1-based place in the list as written
    first: str  # surname key of the first author
    second: str  # ... of the second ("" when one author)
    authors: int
    initials: str  # the first author's initials, letters only ("JS")
    year: str
    suffix: str


def _entry(ref: references.Reference, position: int) -> Entry | None:
    text = ref.text
    head = refengine.APA_HEAD.match(text)
    y = _ENTRY_YEAR.search(text)
    if not head or not y:
        return None
    block = head.group("authors")
    names = refengine.apa_authors(block)
    if names:
        people = [n.rsplit(" ", 1) if " " in n else [n, ""] for n in names]
        count = len(people) + (1 if re.search(r"\bet al\b", block) else 0)
    else:  # "Karyanti, Atmoko, A., & Hitipieuw, I.": surnames read off the commas
        parts = [x.strip() for x in re.split(r",|&|\band\b", block) if x.strip() and not re.fullmatch(r"(?:[A-Z]\.\s*-?\s*)+", x.strip())]
        if not parts:
            return None
        people = [[x, ""] for x in parts]
        count = len(people)
    first, ini = people[0]
    second = people[1][0] if len(people) > 1 else ""
    return Entry(ref, position, _key(first), _key(second), count, re.sub(r"[^A-Z]", "", ini),
                 y.group("year"), y.group("suffix"))


def entries(ms: Manuscript) -> list[Entry] | None:
    """The reference list read as an author-year list, or None when it is not one (numbered, or entries without an
    author and a year in brackets)."""
    refs = references.extract(ms)
    if len(refs) < 3 or any(r.number is not None for r in refs):
        return None
    out = [_entry(r, i) for i, r in enumerate(refs, 1)]
    if sum(e is not None for e in out) * 10 < len(out) * 8:
        return None  # most entries "Surname, I. (2020)": an APA list
    # an entry that cannot be read keeps its place; nothing cites it by name, so it ends up uncited and queried
    return [e if e is not None else Entry(r, i, "", "", 0, "", "", "") for i, (e, r) in enumerate(zip(out, refs), 1)]


@dataclass
class Item:
    text: str  # as written: "Li, 2023"
    entries: list[int] = field(default_factory=list)  # positions it resolves to (one per year), [] when it does not


@dataclass
class Cite:
    para_id: str
    start: int  # the span replaced: the space before the bracket, the bracket, the punctuation after it
    end: int
    kind: str  # "group" | "narrative"
    items: list[Item]
    punct: str = ""
    name_end: int = 0  # narrative: where the name ends ("Hoekstra et al." | " (2023)")


def _match(es: list[Entry], ini: str, a: str, b: str | None, etal: bool, year: str) -> int | None:
    words = a.split()
    keys = [_key(" ".join(words[i:])) for i in range(len(words))]
    ys, suffix = year[:4], year[4:]
    found = [e for e in es if e.first in keys and e.year == ys]
    if b:
        found = [e for e in found if e.second == _key(b)]
    if ini:
        letters = re.sub(r"[^A-Z]", "", ini)
        found = [e for e in found if e.initials.startswith(letters)]
    if len(found) > 1 and suffix:
        found = [e for e in found if e.suffix == suffix] or found
    if len(found) > 1:
        want = (lambda e: e.authors >= 3) if etal else (lambda e: e.authors == 2) if b else (lambda e: e.authors == 1)
        found = [e for e in found if want(e)] or found
    return found[0].position if len(found) == 1 else None


def _resolve(es: list[Entry], ini: str | None, a: str, b: str | None, etal: bool, years: str) -> list[int]:
    out = []
    for y in re.split(r"\s*,\s*", years):
        pos = _match(es, ini or "", a, b, etal, y)
        if pos is None:
            return []
        out.append(pos)
    return out


def _paragraphs(ms: Manuscript) -> list[Paragraph]:
    from . import rules

    front = rules.front_matter_ids(ms)
    return [p for p in renumber.citation_paragraphs(ms) if p.id not in front]


def cites(ms: Manuscript, es: list[Entry]) -> list[Cite]:
    out: list[Cite] = []
    for p in _paragraphs(ms):
        text = p.text
        taken: list[tuple[int, int]] = []
        for m in _GROUP.finditer(text):
            parts = [x for x in m.group("inner").split(";")]
            items = []
            for part in parts:
                im = _ITEM.match(part)
                if not im:
                    items = []
                    break
                items.append(Item(part.strip(), _resolve(es, im.group("ini"), im.group("a"), im.group("b"),
                                                         bool(im.group("etal")), im.group("years"))))
            if not items:
                continue
            out.append(Cite(p.id, m.start(), m.end(), "group", items, m.group("punct")))
            taken.append((m.start("lead") + len(m.group("lead")), m.end()))
        for m in _NARRATIVE.finditer(text):
            if any(a <= m.start("lead") < b for a, b in taken):
                continue
            if re.search(r"\(\s*$", text[:m.start()]):
                continue  # "(Hoekstra et al. (2023)": inside a bracket of its own
            nums = _resolve(es, m.group("ini"), m.group("a"), m.group("b"), bool(m.group("etal")), m.group("years"))
            if not nums and not (m.group("etal") or m.group("b")):
                continue  # "Salmivalli (2023)" with no entry: a capitalised word before a year, not surely a name
            item = Item(text[m.start():m.end()].strip(), nums)
            out.append(Cite(p.id, m.start("lead"), m.end(), "narrative", [item], name_end=m.start("lead")))
    return sorted(out, key=lambda c: renumber.citation_key(ms, c.para_id, c.start))


@dataclass
class Plan:
    entries: list[Entry] = field(default_factory=list)
    cites: list[Cite] = field(default_factory=list)
    numbers: dict[int, int] = field(default_factory=dict)  # list position -> new number
    uncited: list[int] = field(default_factory=list)  # positions never cited
    unmatched: list[tuple[Cite, Item]] = field(default_factory=list)

    @property
    def apply(self) -> bool:
        return bool(self.numbers)


def plan(ms: Manuscript, style: JournalStyle) -> Plan:
    cached = getattr(ms, "_author_year_plan", None)
    if cached is not None and cached[0] is style:
        return cached[1]
    pl = Plan()
    ms._author_year_plan = (style, pl)
    if not style.get("citations.author_year", False) or not style.get("citations.author_year_number_list", True):
        return pl
    es = entries(ms)
    if not es:
        return pl
    cs = cites(ms, es)
    order: list[int] = []
    for c in cs:
        for it in c.items:
            for pos in it.entries:
                if pos not in order:
                    order.append(pos)
            if not it.entries:
                pl.unmatched.append((c, it))
    if not order:
        return pl
    pl.uncited = [e.position for e in es if e.position not in order]
    order += pl.uncited
    pl.entries, pl.cites = es, cs
    pl.numbers = {pos: i for i, pos in enumerate(order, 1)}
    return pl


def _citation_changes(ms: Manuscript, style: JournalStyle, pl: Plan) -> list[Change]:
    from . import rules, rules_format

    sup = rules_format.citation_format(style)
    after_punct = style.get("citations.after_punctuation", True)
    out = []
    for c in pl.cites:
        p = ms.by_id(c.para_id)
        nums = [pl.numbers[pos] for it in c.items for pos in it.entries]
        if not nums:
            continue
        bracket = f"[{renumber.compress(nums, style)}]"
        if c.kind == "narrative":  # "Hoekstra et al. (2023)" -> "Hoekstra et al.[3]"
            ch = rules._change(p, c.start, c.end, bracket, Layer.REFERENCES, "author_year",
                               {**(sup or {}), "italic": False})
        else:
            left = [it.text for it in c.items if not it.entries]
            keep = f" ({'; '.join(left)})" if left else ""  # what could not be matched stays, queried
            before = p.text[:c.start].rstrip()
            punct = c.punct
            if punct and before.endswith(punct):
                punct = ""  # "older. (Li, 2023)." : the stop is there already
            if after_punct and punct:
                plain, new = keep + punct, keep + punct + bracket
            else:
                plain, new = keep, keep + bracket + punct
            if not before:  # a table cell holding only the citation
                plain, new = plain.lstrip(), new.lstrip()
            ch = rules._change(p, c.start, c.end, new, Layer.REFERENCES, "author_year", sup)
            if sup:
                pieces = [[len(plain), {"superscript": False}]] if plain else []
                if not after_punct and punct:
                    pieces += [[len(bracket), None], [len(punct), {"superscript": False}]]
                ch.segment_format = [pieces] if pieces else None
        ch.provenance = provenance.make("renumber", f"Citation {', '.join(it.text for it in c.items)} → {bracket}",
                                        "name–year citations numbered in order of first citation (S320)")
        out.append(ch)
    return out


def _entry_format(entry: str, style: JournalStyle) -> list | None:
    """Pieces for a moved entry: roman throughout ("et al" in italics where the journal italicises it)."""
    from . import rules

    if not rules.et_al_italic(style):
        return None
    pieces, at = [], 0
    for m in re.finditer(r"\bet al\b", entry):
        if m.start() > at:
            pieces.append([m.start() - at, None])
        pieces.append([m.end() - m.start(), {"italic": True}])
        at = m.end()
    return [pieces] if pieces else None


def _list_changes(ms: Manuscript, style: JournalStyle, pl: Plan) -> list[Change]:
    from . import rules

    out: list[Change] = []
    by_new = {pl.numbers[e.position]: e for e in pl.entries}
    formatted = None
    auto = style.get("references.label_auto_number")
    for e in pl.entries:  # e: the entry at this position as written
        k = e.position
        p = ms.by_id(e.ref.para_id)
        src = by_new[k]
        prov = provenance.make("renumber", f"Reference {k}", "references numbered in order of first citation (S320)")
        if src is not e:
            if formatted is None:
                formatted = renumber._formatted_entries(ms, style)
            src_p = ms.by_id(src.ref.para_id)
            full = formatted[src.ref.para_id]
            tail = len(src_p.text) - (src.ref.offset + len(src.ref.text))
            entry = full[src.ref.offset:len(full) - tail]
            c = rules._change(p, e.ref.offset, e.ref.offset + len(e.ref.text), entry, Layer.REFERENCES, "renumber",
                              {"italic": False, "bold": False})
            c.segment_format = _entry_format(entry, style)
            c.provenance = prov
            out.append(c)
        if auto:
            if not p.numbered:
                c = rules._change(p, 0, 0, "", Layer.REFERENCES, "ref_label", {"list_number": True})
                c.provenance = prov
                out.append(c)
        else:
            c = rules._change(p, 0, 0, f"{k}. ", Layer.REFERENCES, "ref_label")
            c.provenance = prov
            out.append(c)
    return out


def rule(ms: Manuscript, style: JournalStyle) -> list[Change]:
    pl = plan(ms, style)
    if not pl.apply:
        return []
    if any(e.ref.continuation for e in pl.entries):
        return _citation_changes(ms, style, pl)  # (an entry over several paragraphs: the list is not reordered)
    return _citation_changes(ms, style, pl) + _list_changes(ms, style, pl)


def add_queries(ms: Manuscript, style: JournalStyle, qs) -> None:
    from . import queries

    queries.TEXT.update({k: v for k, v in QUERY_TEXT.items() if k not in queries.TEXT})
    pl = plan(ms, style)
    if not pl.apply:
        return
    heading = references.find_heading(ms)
    qs.add(ms.paragraphs[heading].id, "references.author_year_numbered")
    seen = set()
    for c, it in pl.unmatched:
        key = _key(re.sub(r"\b(?:and|et al)\b", "", it.text))  # "He et al. (2024)" and "He et al., 2024": one query
        if key in seen:
            continue
        seen.add(key)
        text = ms.by_id(c.para_id).text
        i = text.find(it.text, c.start if c.kind == "group" else max(0, c.start - len(it.text) - 2))
        s, e = (i, i + len(it.text)) if i >= 0 else (c.start, c.end)
        qs.add(c.para_id, "references.author_year_unmatched", s, e, cite=it.text)
    by_pos = {e.position: e for e in pl.entries}
    for pos in pl.uncited:
        r = by_pos[pos].ref
        qs.add(r.para_id, "references.uncited", r.offset, r.offset + len(r.text), n=pl.numbers[pos])
