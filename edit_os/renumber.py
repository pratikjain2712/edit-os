"""Duplicate references and renumbering by order of first citation (pre-editing guide, E.g. 3-5).

  E.g. 3  duplicate references are deleted and the list renumbered;
  E.g. 4  references first cited out of order are renumbered in the text and the list;
  E.g. 5  if any reference is never cited, nothing is renumbered: the uncited query goes instead.

The change model only edits text inside paragraphs, so the list is reordered in place: each
reference paragraph gets the entry that belongs at its position (with its label number), and the
paragraphs left over by deleted duplicates are emptied. A moved entry carries the formatting the
reference rules propose for it, since its new position's own reference edits no longer apply.

Citations in every form `rules.rule_citations` handles ([n], [2-5], (n), superscript) are
renumbered on top of that rule's proposal, then sorted and re-compressed ([4,5,6] -> [4-6]).
The rule is registered before "citations", so its proposals win where they overlap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import refengine, references
from .changes import Change, Layer
from .docmodel import Manuscript
from .style import JournalStyle

QUERY_TEXT = {
    "references.renumbered": "References are renumbered in text and list for citation sequence.",
    "references.duplicate_removed": "References ({a} and {b}) are identical, so duplicate reference has been "
                                    "removed and references are renumbered in text and list.",
    "references.duplicate": "References ({a} and {b}) are identical. Kindly check.",
}


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", refengine.fold(text))


def expand(inner: str) -> list[int] | None:
    out: list[int] = []
    for part in re.split(r"\s*,\s*", inner.strip()):
        r = re.fullmatch(r"(\d+)\s*[-–—]\s*(\d+)", part)
        if r:
            a, b = int(r.group(1)), int(r.group(2))
            if b <= a or b - a > 200:
                return None
            out += range(a, b + 1)
        elif part.isdigit():
            out.append(int(part))
        else:
            return None
    return out


def compress(nums: list[int], style: JournalStyle) -> str:
    """Sorted, unique numbers as a citation: runs of `compress_from`+ become ranges."""
    nums = sorted(set(nums))
    least = style.get("renumber.compress_from", 3)
    dash = style.get("citations.range_dash", "-")
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        if j - i + 1 >= least:
            parts.append(f"{nums[i]}{dash}{nums[j]}")
        else:
            parts += [str(n) for n in nums[i:j + 1]]
        i = j + 1
    return style.get("citations.multiple_delimiter", ",").join(parts)


@dataclass
class Occurrence:
    para_id: str
    start: int
    end: int
    old: str  # text in the paragraph
    new: str  # after the citation rule (bracketed, punctuation placed), before renumbering
    numbers: list[int]


@dataclass
class Plan:
    refs: list[references.Reference] = field(default_factory=list)
    occurrences: list[Occurrence] = field(default_factory=list)
    duplicates: list[tuple[int, int]] = field(default_factory=list)  # (kept, duplicate), old numbers
    uncited: list[int] = field(default_factory=list)
    mapping: dict[int, int] = field(default_factory=dict)  # old number -> new number
    apply: bool = False  # renumber (reorder and/or drop duplicates)

    @property
    def reordered(self) -> bool:
        return any(k != v for k, v in self.mapping.items())


# "Table [2]", "Eq. [3]" are labels; unlike round brackets, "in 1985[64]" is a citation.
_PREV_NOT_CITATION = re.compile(r"\b(table|tables|fig|figure|figures|ref|reference|no|eq|equation)\.?$", re.I)


def _formula_superscript(text: str, start: int, end: int) -> bool:
    """Z²P, d², η²H: a power in a formula, not a citation."""
    nxt = text[end:end + 1]
    if nxt.isalnum():
        return True
    if start >= 1 and text[start - 1].isalpha() and (start < 2 or not text[start - 2].isalpha()):
        return True
    return "=" in text[max(0, start - 15):start]


def occurrences(ms: Manuscript, style: JournalStyle, n_refs: int) -> list[Occurrence]:
    """Every numbered citation, in document order: the body forms the citation rule converts, plus
    [n] citations anywhere outside the reference list (tables, legends)."""
    from . import rules

    proposals: dict[str, list[Change]] = {}
    for c in rules.rule_citations(ms, style):
        proposals.setdefault(c.para_id, []).append(c)
    # Round brackets are ambiguous ("Klebsiella (21)", "six (6) weeks"): when the document cites in
    # square brackets or superscript, round-bracket numbers are counts, not citations.
    all_props = [c for cs in proposals.values() for c in cs if "[" in c.new]
    n_round = sum(c.old.lstrip().startswith("(") for c in all_props)
    n_square = sum(len(list(rules._SQUARE.finditer(p.text))) for p in ms.paragraphs) + len(all_props) - n_round
    skip_round = n_square >= n_round
    ref_paras = {r.para_id for r in references.extract(ms)}
    heading = references.find_heading(ms)
    out = []
    for idx, p in enumerate(ms.paragraphs):
        if p.id in ref_paras or idx == heading or not p.text.strip():
            continue
        found = []
        mine = proposals.get(p.id, [])
        for c in mine:
            if c.old.lstrip().startswith("("):
                prev = p.text[:c.start].rstrip()
                if skip_round or re.search(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                                           r"majority|minority|total|cases|patients|eyes|n)$", prev, re.I):
                    continue
            elif not c.old.lstrip().startswith("[") and _formula_superscript(p.text, c.start, c.end):
                continue
            m = re.search(r"\[([^\]]*)\]", c.new)
            if m and (nums := expand(m.group(1))):
                found.append(Occurrence(p.id, c.start, c.end, c.old, c.new, nums))
        for m in rules._SQUARE.finditer(p.text):
            if any(c.start < m.end() and m.start() < c.end for c in mine):
                continue
            prev = p.text[:m.start()].rstrip()
            if _PREV_NOT_CITATION.search(prev):
                continue
            nums = expand(m.group(2))
            if nums and max(nums) <= n_refs:
                found.append(Occurrence(p.id, m.start(), m.end(), m.group(0), m.group(0), nums))
        out += sorted(found, key=lambda o: o.start)
    return out


def duplicates(ms: Manuscript, style: JournalStyle, refs: list[references.Reference]) -> list[tuple[int, int]]:
    """(kept, duplicate) pairs: same text once normalised, or the same verified PMID / DOI."""
    from . import rules

    ids: dict[str, set[str]] = {}
    for ref, res in rules.lookup_references(ms, style):
        if res.record is not None and res.status in (refengine.MATCHED, refengine.FORMAT_FIXED):
            rec = res.record
            ids[ref.para_id] = ({f"pmid:{rec.pmid}"} if rec.pmid else set()) | \
                               ({f"doi:{rec.doi.lower()}"} if rec.doi else set())
    seen: dict[str, int] = {}
    out = []
    for r in refs:
        keys = {k for k in [_key(r.text)] if len(k) >= 25} | ids.get(r.para_id, set())
        first = next((seen[k] for k in keys if k in seen), None)
        if first is not None:
            out.append((first, r.number))
        else:
            for k in keys:
                seen[k] = r.number
    return out


def plan(ms: Manuscript, style: JournalStyle) -> Plan:
    cached = getattr(ms, "_renumber_plan", None)
    if cached is not None:
        return cached
    pl = Plan()
    ms._renumber_plan = pl
    if not style.get("renumber.enabled", True):
        return pl
    refs = references.extract(ms)
    if not refs or [r.number for r in refs] != list(range(1, len(refs) + 1)):
        return pl  # no list, author-year, or labels out of sequence: leave it to the editor
    pl.refs = refs
    n = len(refs)
    pl.occurrences = occurrences(ms, style, n)
    if not pl.occurrences:
        return pl
    pl.duplicates = duplicates(ms, style, refs) if style.get("renumber.remove_duplicates", True) else []
    canon = {r.number: r.number for r in refs}
    for kept, dup in pl.duplicates:
        canon[dup] = canon[kept]
    order: list[int] = []
    for o in pl.occurrences:
        for num in sorted(o.numbers):
            c = canon.get(num)
            if c is None:
                return pl  # cites a reference that is not in the list
            if c not in order:
                order.append(c)
    removed = {d for _, d in pl.duplicates}
    pl.uncited = [r.number for r in refs if r.number not in removed and r.number not in order]
    if pl.uncited:
        return pl  # E.g. 5: query the missing citations, do not renumber
    if not style.get("renumber.reorder", True):
        order = sorted(order)
    new = {c: i + 1 for i, c in enumerate(order)}
    pl.mapping = {r.number: new[canon[r.number]] for r in refs}
    pl.apply = bool(pl.duplicates) or pl.reordered
    return pl


def _formatted_entries(ms: Manuscript, style: JournalStyle) -> dict[str, str]:
    """Each reference paragraph's text with the reference rules' own proposals applied."""
    from . import rules

    cs = rules.run_all(ms, style, only=["ref_lookup", "references"])
    out = {}
    for r in references.extract(ms):
        text = ms.by_id(r.para_id).text
        for c in sorted((c for c in cs.changes if c.para_id == r.para_id), key=lambda c: c.start, reverse=True):
            text = text[:c.start] + c.new + text[c.end:]
        out[r.para_id] = text
    return out


def rule_renumber(ms: Manuscript, style: JournalStyle) -> list[Change]:
    from . import rules

    pl = plan(ms, style)
    if not pl.apply:
        return []
    out: list[Change] = []
    # Citations.
    for o in pl.occurrences:
        mapped = sorted({pl.mapping[n] for n in o.numbers})
        if mapped == sorted(set(o.numbers)) and not (len(set(o.numbers)) < len(o.numbers)):
            continue
        new = re.sub(r"\[[^\]]*\]", lambda m: f"[{compress(mapped, style)}]", o.new, count=1)
        if new != o.old:
            p = ms.by_id(o.para_id)
            out.append(rules._change(p, o.start, o.end, new, Layer.REFERENCES, "renumber"))
    # Reference list: position k gets the entry numbered k.
    by_new: dict[int, references.Reference] = {}
    removed = {d for _, d in pl.duplicates}
    for r in pl.refs:
        if r.number not in removed:
            by_new[pl.mapping[r.number]] = r
    formatted = None
    for k, pos in enumerate(pl.refs, 1):
        p = ms.by_id(pos.para_id)
        src = by_new.get(k)
        if src is None:  # left over after deleting duplicates
            out.append(rules._change(p, 0, len(p.text), "", Layer.REFERENCES, "renumber"))
            continue
        if pos.label:
            m = re.search(r"\d+", pos.label)
            if m and int(m.group(0)) != k:
                out.append(rules._change(p, m.start(), m.end(), str(k), Layer.REFERENCES, "renumber"))
        if src is not pos:
            if formatted is None:
                formatted = _formatted_entries(ms, style)
            src_p = ms.by_id(src.para_id)
            full = formatted[src.para_id]
            tail = len(src_p.text) - (src.offset + len(src.text))  # trailing whitespace kept as is
            entry = full[src.offset:len(full) - tail]
            out.append(rules._change(p, pos.offset, pos.offset + len(pos.text), entry, Layer.REFERENCES, "renumber"))
    return out


def add_queries(ms: Manuscript, style: JournalStyle, qs) -> None:
    from . import queries

    queries.TEXT.update({k: v for k, v in QUERY_TEXT.items() if k not in queries.TEXT})
    pl = plan(ms, style)
    if not pl.refs:
        return
    by_num = {r.number: r for r in pl.refs}
    for kept, dup in pl.duplicates:
        r = by_num[dup]
        check = "references.duplicate_removed" if pl.apply else "references.duplicate"
        qs.add(r.para_id, check, r.offset, r.offset + len(r.text), a=kept, b=dup)
    if pl.apply and pl.reordered and not pl.duplicates:
        heading = references.find_heading(ms)
        qs.add(ms.paragraphs[heading].id, "references.renumbered")
