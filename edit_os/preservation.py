"""Content preservation: the edited file keeps the article the author sent.

QC (qc.py) checks the output against the journal's rules; these checks compare it with the ORIGINAL manuscript, so
a rule that deletes too much (a reply to the reviewers that took the Discussion, Limitations, Conclusion and
declarations with it, JEHP) blocks the file instead of reaching the editor as one more accepted deletion.

    content.sections     a main section heading of the original is missing from the output (renames such as
                         Result -> Results or Material and Methods -> Materials and Methods are the same section)
    content.deleted      body prose deleted outside what the tool is meant to remove (submission material,
                         the title page the front matter rebuilds, reference entries rebuilt, blocks moved)
    content.references   references cited in the original and no longer cited in the output

All three are deterministic: headings by a normalised key, deletions from the Change records themselves (who
deleted what, so the issue names the rule), citations as renumbering reads them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import references
from .changes import Change, Status
from .docmodel import Manuscript, Paragraph
from .tracked import PARAGRAPH_BREAK, _emptying

ERROR, WARNING = "error", "warning"

CHECKS = {
    "content.sections": "Every main section of the original manuscript is still in the output",
    "content.deleted": "No body text deleted beyond what the tool is meant to remove",
    "content.references": "References cited in the original are still cited",
}

# --- section headings ------------------------------------------------------------------------

# key -> (label, the names that are that section). One key per section, so renames and variants compare equal.
_SECTIONS = {
    "introduction": ("Introduction", r"introduction|background|introduction\s+and\s+background|background\s+and\s+introduction"),
    "methods": ("Methods", r"(?:(?:materials?|patients?|subjects?|participants?)\s+and\s+)?methods?(?:\s+and\s+materials?)?|"
                           r"methodology|research\s+methodology|study\s+methods?"),
    "results": ("Results", r"results?|findings|observations?"),
    "discussion": ("Discussion", r"discussions?"),
    "conclusion": ("Conclusion", r"conclusions?|concluding\s+remarks|summary\s+and\s+conclusions?"),
    "limitations": ("Limitations", r"(?:study\s+)?limitations?(?:\s+of\s+(?:the|this|our)\s+(?:study|research))?|"
                                   r"strengths?\s+and\s+limitations?|limitations?\s+and\s+strengths?|"
                                   r"limitations?\s+and\s+(?:future\s+)?(?:directions?|recommendations?|scope)"),
    "case": ("Case report", r"case\s+(?:reports?|presentations?|descriptions?|history|summary|details|series)|"
                            r"clinical\s+(?:presentation|case)"),
    "declarations": ("Declarations", r"declarations?"),
    "acknowledgements": ("Acknowledgement", r"acknowledge?ments?"),
    "funding": ("Funding", r"funding(?:\s+(?:information|sources?|statement|details))?|sources?\s+of\s+(?:support|funding)|"
                           r"financial\s+(?:support|disclosures?)(?:\s+and\s+sponsorship)?"),
    "conflicts": ("Conflicts of interest", r"conflicts?\s+of\s+interests?(?:\s+statement)?|competing\s+interests?|"
                                           r"declarations?\s+of\s+(?:competing\s+)?interests?"),
    "references": ("References", r"references|bibliography|literature\s+cited|reference\s+list|references\s+cited"),
}
_FULL = {k: re.compile(rf"(?:{pat})") for k, (_, pat) in _SECTIONS.items()}
# The body of the article starts at the first of these (what comes before is the title page and the abstract).
_BODY_START = ("introduction", "case", "methods", "results", "discussion")
_TAGS = re.compile(r"<H\d>", re.I)
_NUMBERING = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[ivx]+\.|[a-h][.)])\s+", re.I)
_RUN_IN = re.compile(r"^(?P<head>[A-Za-z][A-Za-z '’&]{2,60}?)\s*[:.\-–—]\s*\S")


def _clean(text: str) -> str:
    t = _TAGS.sub("", text).replace(" ", " ").strip()
    t = _NUMBERING.sub("", t)
    t = re.sub(r"[\s:.\-–—]+$", "", t)
    return re.sub(r"\s+", " ", t).strip().lower().replace("’", "'")


def _keys(name: str) -> set[str]:
    """The section keys a heading names: one, several ("Results and discussion"), or none."""
    for k, rx in _FULL.items():
        if rx.fullmatch(name):
            return {k}
    parts = [x for x in re.split(r"\s*(?:,|&|\band\b)\s*", name) if x]
    if len(parts) > 1:
        out = set()
        for part in parts:
            ks = {k for k, rx in _FULL.items() if rx.fullmatch(part)}
            if not ks:
                return set()
            out |= ks
        return out
    return set()


def heading_keys(text: str) -> set[str]:
    """Section keys of a paragraph that is a heading on its own ("<H1>2. Materials and Methods:")."""
    if len(text) > 90:
        return set()
    return _keys(_clean(text))


def section_keys(text: str) -> set[str]:
    """Section keys of a heading on its own or run in at the start of a paragraph ("Funding: Nil.")."""
    keys = heading_keys(text)
    if keys:
        return keys
    m = _RUN_IN.match(_NUMBERING.sub("", _TAGS.sub("", text).strip()))
    return _keys(_clean(m.group("head"))) if m else set()


def _label(key: str) -> str:
    return _SECTIONS[key][0]


# --- what the changes do ---------------------------------------------------------------------

_WORD = re.compile(r"[^\W\d_]+")  # words of prose: numbers (citations, values restyled) are not counted


def _words(s: str) -> list[str]:
    return _WORD.findall(s.casefold())


def _norm(s: str) -> str:
    """Text as a comparable string (to find deleted text put back elsewhere)."""
    return " ".join(re.findall(r"\w+", s.casefold()))


# Reasons whose deletions are what the tool is meant to remove: the title page the front matter rebuilds,
# reference entries rebuilt (they sit outside the body anyway). Submission material (submission_*) is meant to go
# too, but only up to the article's next section: a part that takes a section heading went too far.
_EXPECTED = ("front_matter", "references", "ref_", "first_author")
_SUBMISSION = "submission_"
BIG_CHANGE = 25  # words one change removes before it counts as deleting prose (not trimming a phrase)
MAX_WORDS = 150  # words of body prose one rule may delete...
MAX_SHARE = 0.03  # ... or this share of the body, whichever is larger
MAX_RUN = 3  # consecutive body paragraphs deleted by one rule...
MIN_RUN_WORDS = 50  # ... holding at least this many words (not a row of "Not applicable." statements)


def _expected(reason: str) -> bool:
    return reason.startswith(_EXPECTED)


@dataclass
class _Loss:
    para: Paragraph
    index: int  # position among the body paragraphs
    reason: str
    words: int
    emptied: bool


def _body_range(ms: Manuscript) -> tuple[int, int]:
    """[start, end) paragraph indexes of the article body: from its first main section to the reference list."""
    end = references.find_heading(ms)
    end = len(ms.paragraphs) if end is None else end
    for i, p in enumerate(ms.paragraphs[:end]):
        if not p.in_table and heading_keys(p.text) & set(_BODY_START):
            return i, end
    return 0, end


def _inserted_text(changes: list[Change], inserts, statuses) -> str:
    """Everything the run puts into the document (new text of changes, inserted and moved paragraphs), as one
    normalised string: deleted text found in it was moved, not lost."""
    out = [c.new for c in changes if c.status in statuses and c.new.strip()]
    for n in inserts or []:
        if getattr(n, "status", None) in statuses:
            out.append(getattr(n, "text", "") or "")
    return " " + _norm(" ".join(t.replace(PARAGRAPH_BREAK, " ") for t in out)) + " "


def _losses(source: Manuscript, changes: list[Change], inserts, statuses) -> tuple[list[_Loss], int, list[Paragraph]]:
    start, end = _body_range(source)
    body = [p for p in source.paragraphs[start:end] if not p.in_table and p.text.strip()]
    pos = {p.id: i for i, p in enumerate(body)}
    by_para: dict[str, list[Change]] = {}
    for c in changes:
        if c.status in statuses and c.para_id in pos:
            by_para.setdefault(c.para_id, []).append(c)
    moved = _inserted_text(changes, inserts, statuses)
    losses: list[_Loss] = []
    for pid, cs in by_para.items():
        p = body[pos[pid]]
        _, emptied = _emptying(p.text, cs)
        if emptied:
            gone = [c for c in cs if not c.format_only and c.end > c.start]
            reason = max(gone, key=lambda c: c.end - c.start).reason
            text = _norm(p.text)
            if len(text.split()) >= 4 and f" {text} " in moved:
                continue  # the paragraph is put back elsewhere
            losses.append(_Loss(p, pos[pid], reason, len(_words(p.text)), True))
            continue
        for c in cs:
            lost = len(_words(c.old)) - len(_words(c.new.replace(PARAGRAPH_BREAK, " ")))
            if lost >= BIG_CHANGE and f" {_norm(c.old)} " not in moved:
                losses.append(_Loss(p, pos[pid], c.reason, lost, False))
    body_words = sum(len(_words(p.text)) for p in body)
    return sorted(losses, key=lambda x: x.index), body_words, body


def _where(p: Paragraph) -> str:
    t = re.sub(r"\s+", " ", _TAGS.sub("", p.text)).strip()
    return f"'{t[:60]}{'…' if len(t) > 60 else ''}'"


def _runs(losses: list[_Loss]) -> list[list[_Loss]]:
    """Consecutive emptied body paragraphs deleted by one rule."""
    runs: list[list[_Loss]] = []
    for x in (x for x in losses if x.emptied):
        if runs and runs[-1][-1].reason == x.reason and runs[-1][-1].index == x.index - 1:
            runs[-1].append(x)
        else:
            runs.append([x])
    return runs


# --- the checks ------------------------------------------------------------------------------

@dataclass
class Finding:
    severity: str
    check: str
    message: str
    para_id: str | None = None


_BACK = {"acknowledgements", "funding", "conflicts", "declarations"}
NIL_WORDS = 30  # a back-matter section this short ("Acknowledgement" / "Nil.") is a statement the journal restyles


def _dropped_by_style(style) -> set[str]:
    """Sections whose heading the journal's own sheet removes: `sections.headings.drop` (IJO: Introduction), the
    "Declarations" container heading (`sections.back_matter.delete_declarations_heading`)."""
    if style is None:
        return set()
    out: set[str] = set()
    for h in style.get("sections.headings.drop", []) or []:
        out |= _keys(_clean(str(h)))
    if style.get("sections.back_matter.delete_declarations_heading", False):
        out.add("declarations")
    return out


def _section_words(source: Manuscript, p: Paragraph) -> int:
    """Words under a heading up to the next heading (or label line)."""
    paras = [x for x in source.paragraphs if not x.in_table and x.text.strip()]
    i = next(i for i, x in enumerate(paras) if x is p)
    n = 0
    for x in paras[i + 1:i + 8]:
        if section_keys(x.text) or len(x.text.split()) <= 6 and not x.text.rstrip().endswith("."):
            break
        n += len(_words(x.text))
    return n


def _output_keys(output: Manuscript) -> dict[str, int]:
    """How many headings of each section the output has: headings on their own, and back-matter statements run in
    ("Funding: Nil.") -- not "Conclusion:" run in at the head of an abstract paragraph."""
    have: dict[str, int] = {}
    for p in output.paragraphs:
        if p.in_table:
            continue
        keys = heading_keys(p.text) or section_keys(p.text) & _BACK
        for k in keys:
            have[k] = have.get(k, 0) + 1
    return have


def check_sections(source: Manuscript, output: Manuscript, changes: list[Change] = (), statuses=(),
                   style=None) -> list[Finding]:
    """Main section headings of the original that the output no longer has. Compared by key and count, so renames
    are not losses and the abstract's "Conclusion" does not stand in for the article's. Not a heading the journal's
    sheet removes, nor a nil back-matter statement its section rule restyles."""
    have = _output_keys(output)
    by_para: dict[str, list[Change]] = {}
    for c in changes:
        if c.status in statuses:
            by_para.setdefault(c.para_id, []).append(c)
    seen: dict[str, list[Paragraph]] = {}
    for p in source.paragraphs:
        if p.in_table or any(c.reason.startswith("front_matter") for c in by_para.get(p.id, ())):
            continue  # the title page the front matter rebuilds ("Case Report", the article type) is no section
        for k in heading_keys(p.text):
            seen.setdefault(k, []).append(p)
    dropped = _dropped_by_style(style)
    out = []
    for k, ps in seen.items():
        deficit = len(ps) - have.get(k, 0)
        if deficit <= 0:
            continue
        gone = [p for p in ps if p.id in by_para and _emptying(p.text, by_para[p.id])[1]] or ps[-deficit:]
        for p in gone[:deficit]:
            who = sorted({c.reason for c in by_para.get(p.id, ()) if c.end > c.start and not c.format_only})
            by_sections = all(r.startswith("sections_") for r in who)  # the journal's own heading rules
            if k in dropped and by_sections or k in _BACK and who and by_sections \
                    and _section_words(source, p) <= NIL_WORDS:
                continue
            if who == ["template_leftovers"] and any(
                    q is not p and q not in gone and _norm(q.text) == _norm(p.text) for q in ps):
                continue  # a heading typed twice (leftovers.py): its other copy stays
            by = f" (deleted by {', '.join(who)})" if who else ""
            out.append(Finding(ERROR, "content.sections",
                               f"Section '{_label(k)}' of the original manuscript is missing from the output{by}: "
                               f"heading {_where(p)}", p.id))
    return out


def check_deleted(source: Manuscript, changes: list[Change], inserts=(), statuses=()) -> list[Finding]:
    """Body prose deleted by a rule beyond what it is meant to remove."""
    losses, body_words, body = _losses(source, changes, inserts, statuses)
    out: list[Finding] = []
    flagged: set[str] = set()
    # submission material: meant to go, but not past the article's next section heading
    for run in _runs([x for x in losses if x.reason.startswith(_SUBMISSION)]):
        at = next((i for i, x in enumerate(run) if heading_keys(x.para.text) - {"references"}), None)
        if at is None:
            continue
        rest = run[at:]
        words = sum(x.words for x in rest)
        out.append(Finding(ERROR, "content.deleted",
                           f"{run[0].reason} deletes {len(rest)} paragraphs ({words} words) of the article from the "
                           f"section {_where(rest[0].para)} to {_where(rest[-1].para)}", rest[0].para.id))
        flagged.add(run[0].reason)
    limit = max(MAX_WORDS, MAX_SHARE * body_words)
    other = [x for x in losses if not _expected(x.reason) and not x.reason.startswith(_SUBMISSION)]
    for run in _runs(other):
        if len(run) >= MAX_RUN and sum(x.words for x in run) >= MIN_RUN_WORDS:
            words = sum(x.words for x in run)
            out.append(Finding(ERROR, "content.deleted",
                               f"{run[0].reason} deletes {len(run)} consecutive body paragraphs ({words} words), "
                               f"{_where(run[0].para)} to {_where(run[-1].para)}", run[0].para.id))
            flagged.add(run[0].reason)
    totals: dict[str, list[_Loss]] = {}
    for x in other:
        totals.setdefault(x.reason, []).append(x)
    for reason, xs in totals.items():
        words = sum(x.words for x in xs)
        if reason not in flagged and words > limit:
            out.append(Finding(ERROR, "content.deleted",
                               f"{reason} deletes {words} words of body text ({100 * words / max(body_words, 1):.1f}% "
                               f"of the body) in {len(xs)} places, first {_where(xs[0].para)}", xs[0].para.id))
    return out


def _lost_spans(source: Manuscript, changes: list[Change], inserts, statuses) -> dict[str, list[tuple[int, int, str]]]:
    """para_id -> (start, end, reason) of the text the changes delete for good: emptied paragraphs not put back
    elsewhere, and changes that drop most of the words they replace (a citation restyled keeps its numbers)."""
    by_para: dict[str, list[Change]] = {}
    for c in changes:
        if c.status in statuses:
            by_para.setdefault(c.para_id, []).append(c)
    moved = _inserted_text(changes, inserts, statuses)
    out: dict[str, list[tuple[int, int, str]]] = {}
    for p in source.paragraphs:
        cs = by_para.get(p.id)
        if not cs:
            continue
        _, emptied = _emptying(p.text, cs)
        if emptied:
            if f" {_norm(p.text)} " in moved:
                continue
            gone = [c for c in cs if not c.format_only and c.end > c.start]
            out[p.id] = [(0, len(p.text), max(gone, key=lambda c: c.end - c.start).reason)]
            continue
        for c in cs:
            old, new = len(_words(c.old)), len(_words(c.new.replace(PARAGRAPH_BREAK, " ")))
            if old - new >= max(3, old / 2) and f" {_norm(c.old)} " not in moved:
                out.setdefault(p.id, []).append((c.start, c.end, c.reason))
    return out


def check_references(source: Manuscript, style, changes: list[Change], inserts=(), statuses=()) -> list[Finding]:
    """References the original cites whose every citation sits in text the changes delete (the citing text was
    lost, so the output no longer cites them). Read on the original, as renumbering reads citations, so restyled
    or renumbered entries and merged duplicates do not count as losses."""
    from .renumber import occurrences

    numbers = {r.number for r in references.extract(source) if r.number is not None}
    if not numbers:
        return []  # author-year lists: nothing numbered to compare
    spans = _lost_spans(source, changes, inserts, statuses)
    kept: set[int] = set()
    lost: dict[int, tuple[str, str]] = {}  # number -> (para_id, reason) of its first lost citation
    for o in occurrences(source, style, 0):
        hit = next((r for s, e, r in spans.get(o.para_id, ()) if s <= o.start and o.end <= e), None)
        for n in o.numbers:
            if hit is None:
                kept.add(n)
            else:
                lost.setdefault(n, (o.para_id, hit))
    gone = sorted(n for n in lost if n not in kept and n in numbers)
    if not gone:
        return []
    severity = ERROR if len(gone) >= 3 else WARNING
    out = []
    for reason in sorted({lost[n][1] for n in gone}):
        ns = [n for n in gone if lost[n][1] == reason]
        p = source.by_id(lost[ns[0]][0])
        out.append(Finding(severity, "content.references",
                           f"{reason} deletes the only citation(s) of reference(s) {', '.join(map(str, ns))} of the "
                           f"original: they are no longer cited (first in {_where(p)})", p.id))
    return out


def check(output: Manuscript, source: Manuscript, style, changes: list[Change] = (), inserts=(),
          statuses=(Status.SUGGESTED, Status.ACCEPTED)) -> list[Finding]:
    statuses = tuple(statuses)
    changes = list(getattr(changes, "changes", changes) or [])
    out = check_sections(source, output, changes, statuses, style)
    if changes:
        out += check_deleted(source, changes, list(inserts or []), statuses)
    if changes:
        out += check_references(source, style, changes, list(inserts or []), statuses)
    return out
