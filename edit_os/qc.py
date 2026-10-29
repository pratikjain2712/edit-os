"""QC gate: deterministic checks of a manuscript against its journal style.

Errors block the file from moving on; warnings go on the editor's checklist.
Run it on the rendered (edited) file, not the original.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import preservation, references
from .docmodel import Manuscript
from .rules import _SQUARE, body_paragraphs, not_citation_before, round_citations
from .style import JournalStyle

ERROR, WARNING = "error", "warning"

# Every check `check` can report, with what it looks at (the QC report lists passed and failed).
CHECKS = {
    "references.present": "A reference list is present",
    "citations.brackets": "Citations use the journal's brackets",
    "citations.format": "No spaces inside citations",
    "citations.order": "References are cited in numerical order",
    "references.cited": "Every reference is cited",
    "references.exists": "Every citation has a reference entry",
    "references.authors": "Author lists within the journal's limit",
    "references.initials": "No periods after initials",
    "keywords.count": "Number of key words",
    "keywords.order": "Key words in alphabetical order (when required)",
    "sections.required": "Required back-matter sections present",
    "sections.expected": "Expected main headings present",
    "statistics.spacing": "Spaces around statistical operators",
    "signoff.name": "The technical editing sign-off carries the editor's name",
    "figures.order": "Tables and figures are cited in numerical order",
    **preservation.CHECKS,  # compared with the original manuscript (when `check` is given it)
}


@dataclass
class Issue:
    severity: str
    check: str
    message: str
    para_id: str | None = None


@dataclass
class Report:
    issues: list[Issue]

    @property
    def passed(self) -> bool:
        return not any(i.severity == ERROR for i in self.issues)


def _heading_key(text: str) -> str:
    return re.sub(r"^<H\d>|:$", "", text.strip()).strip().lower()


def _citations(ms: Manuscript, style: JournalStyle) -> list[tuple[str, list[int]]]:
    """(para_id, numbers) for each citation, in citation order: what renumbering reads as citations
    (renumber.occurrences: the citation rule's own decisions), so QC, the queries and renumbering agree
    (a table's citations count at its first callout: renumber.citation_key)."""
    from .renumber import occurrences

    return [(o.para_id, o.numbers) for o in occurrences(ms, style, 0)]


_STAT_WORDS = {"or", "rr", "hr", "ci", "sd", "se", "df", "auc", "icc", "ar", "aor"}


def _unary_after_word(text: str, m: re.Match) -> bool:
    """A sign with a space before and none after, following an ordinary word ("showed >90", "aged <18"): a unary
    sign, which the guide writes closed up. Not after a variable or statistic ("p >0.05", "OR >2")."""
    if not (m.group(0).startswith(" ") and len(m.group(0)) == 2):
        return False
    word = re.search(r"([A-Za-z]+)\W*$", text[:m.start()])
    if word and word.group(1) == "or":  # the conjunction ("P < 0.05 or <0.01"); the odds ratio is "OR"
        return True
    return bool(word) and len(word.group(1)) >= 2 and word.group(1).lower() not in _STAT_WORDS


def _keywords(listed: str) -> list[str]:
    """The key words of a "Keywords:" line: a list delimited by semicolons keeps the commas inside its terms
    ("Diabetes mellitus, type 2; education, professional"), otherwise commas and semicolons both delimit."""
    parts = re.split(r";(?![^()]*\))", listed) if ";" in listed else re.split(r"[;,](?![^()]*\))", listed)
    return [w.strip() for w in parts if w.strip()]


def _split_keywords(listed: str) -> list[str]:
    from .rules_sections import _split_keywords as split  # what the key-word rule sorted

    return split(listed)


def _sorted(words: list[str]) -> bool:
    return [w.lower() for w in words] == sorted(w.lower() for w in words)


def _author_count(authors: str) -> int:
    """Authors in the author part of a reference: a group author after ";" ("...; Ethics, Law, and Humanities
    Committee, a joint committee of ...") is one author whatever commas its name holds, and "Surname, I, Surname, I"
    (APA order) counts one author per surname-initials pair."""
    people, *groups = [g.strip() for g in authors.split(";")]
    n = sum(1 for g in groups if g)
    parts = [x.strip() for x in re.split(r",|\s&\s|\sand\s", people) if x.strip()]
    parts = [re.sub(r"^(?:&|and)\s+", "", x) for x in parts]
    initials = [x for x in parts if re.fullmatch(r"(?:[A-Z]\.?\s?-?){1,4}", x)]
    return n + len(parts) - len(initials)


def _queried_refs(queries) -> tuple[set[int], set[int]]:
    """The reference numbers the author is asked about: (uncited entries, citations without an entry), from the
    queries placed (queries.Query or their saved dicts; rejected ones excluded by the caller)."""
    uncited, missing = set(), set()
    for q in queries or ():
        chk, text = (q.get("check"), q.get("text", "")) if isinstance(q, dict) else (q.check, q.text)
        if chk == "references.uncited":  # one or several ("Reference nos. 19, 24 and 27–29 have not been cited")
            from .queries import uncited_numbers

            uncited.update(uncited_numbers(text))
            continue
        m = re.search(r"\d+", text or "")
        if m and chk == "references.missing_entry":
            missing.add(int(m.group(0)))
    return uncited, missing


_SUPP_HEAD = re.compile(r"^\s*(?:<H\d>)?\s*(?:supplementary|supplemental)\s+(?:materials?|information|data|files?)\s*:?\s*$",
                        re.I)


def supplementary_ids(ms: Manuscript) -> set[str]:
    """Ids of the paragraphs under a "Supplementary Material" heading, up to the next tagged heading: the supporting
    material the stylesheets keep as it is (S372 'retain the supplementary material as it is', G151), merged from
    the submission's own file (supplied.py) or the author's."""
    out: set[str] = set()
    inside = False
    for p in ms.paragraphs:
        if not p.in_table and _SUPP_HEAD.match(p.text):
            inside = True
            continue
        if inside and not p.in_table and re.match(r"\s*<H\d>", p.text):
            inside = False
        if inside:
            out.add(p.id)
    return out


def check(ms: Manuscript, style: JournalStyle, *, source: Manuscript | None = None, changes=None, inserts=None,
          statuses=None, queries=None) -> Report:
    """QC of `ms` (the rendered output) against the journal's rules. With `source` (the manuscript as received),
    also what the output kept of it (preservation.py): main sections, body text, cited references; `changes` and
    `inserts` (with the `statuses` rendered into `ms`) say which rule deleted what and what was only moved.
    `queries` (the author queries placed): a reference problem the author is asked about is a warning, not an
    error (`_queried_refs`)."""
    issues: list[Issue] = []
    if source is not None:
        from .changes import Status

        for f in preservation.check(ms, source, style, changes or (), inserts or (),
                                    statuses or (Status.SUGGESTED, Status.ACCEPTED)):
            issues.append(Issue(f.severity, f.check, f.message, f.para_id))
    refs = references.extract(ms)
    headings = {_heading_key(p.text) for p in ms.paragraphs if len(p.text) < 80}

    if not refs:
        issues.append(Issue(ERROR, "references.present", "No reference list found"))

    # Citations: brackets, order, and every reference cited. A round-bracket number is a citation when the
    # citation rule would convert it (counts, list markers, degrees of freedom and numbers beyond the list
    # are not), so the check and the rule never disagree.
    for p, m in round_citations(ms, style):
        issues.append(Issue(ERROR, "citations.brackets", f"Citation in round brackets: {m.group(0).strip()}", p.id))
    for p in body_paragraphs(ms):
        for m in _SQUARE.finditer(p.text):
            what = "Space" if re.search(r"\s", m.group(2)) else "“&”" if "&" in m.group(2) else None
            if what and not not_citation_before(p.text[:m.start()].rstrip(), False):
                issues.append(Issue(ERROR, "citations.format", f"{what} inside citation: [{m.group(2)}]", p.id))
    uncited_q, missing_q = _queried_refs(queries)
    blocked_q = any((q.get("check") if isinstance(q, dict) else q.check) == "references.renumber_blocked"
                    for q in queries or ())
    cited, seen_max = set(), 0
    for para_id, nums in _citations(ms, style):
        for n in nums:
            if n not in cited and n > seen_max + 1:
                # with an uncited entry or a citation without one queried, the list is not renumbered (guide E.g. 5,
                # renumber.plan) and the author's answer settles the order; likewise a renumbering queried as blocked
                asked = bool(uncited_q or missing_q or blocked_q)
                issues.append(Issue(WARNING if asked else ERROR, "citations.order",
                                    f"Reference {n} is cited before reference {seen_max + 1}"
                                    + (" (queried)" if asked else ""), para_id))
            cited.add(n)
            seen_max = max(seen_max, n)
    numbers = [r.number for r in refs if r.number is not None]
    for n in numbers:
        if cited and n not in cited:
            issues.append(Issue(WARNING, "references.cited", f"Reference {n} is not cited (queried)") if n in uncited_q
                          else Issue(ERROR, "references.cited", f"Reference {n} is not cited (author query needed)"))
    for n in sorted(cited - set(numbers)) if numbers else []:
        asked = n in missing_q
        issues.append(Issue(WARNING if asked else ERROR, "references.exists",
                            f"Citation [{n}] has no entry in the reference list" + (" (queried)" if asked else "")))

    # Reference entries.
    max_authors = style.get("references.max_authors")
    for r in refs:
        authors = r.text.split(". ")[0]
        if max_authors and "et al" not in authors and _author_count(authors) > max_authors:
            issues.append(Issue(ERROR, "references.authors", f"Reference {r.number}: more than {max_authors} authors", r.para_id))
        if re.search(r"\b[A-Z]\.,", authors):
            issues.append(Issue(WARNING, "references.initials", f"Reference {r.number}: periods after initials", r.para_id))

    # Keywords.
    for p in ms.paragraphs:
        m = re.match(r"^\s*(?:<H\d>)?\s*key\s*-?words?\s*[:.\-–]?\s*(.*)$", p.text, re.I)
        if not m:
            continue
        words = _keywords(m.group(1))
        lo, hi = style.get("keywords.min", 3), style.get("keywords.max", 5)
        if not lo <= len(words) <= hi:
            issues.append(Issue(WARNING, "keywords.count", f"{len(words)} keywords; journal needs {lo}-{hi} (query)", p.id))
        if style.get("keywords.alphabetical") and not _sorted(words) and not _sorted(_split_keywords(m.group(1))):
            issues.append(Issue(ERROR, "keywords.order", "Keywords are not in alphabetical order", p.id))
        break

    # Required back matter and expected sections.
    from .rules_sections import section_headings

    for sec in style.get("required_sections", []):
        if not section_headings(sec) & headings:  # the journal's heading, or a variant it keeps as written
            issues.append(Issue(ERROR, "sections.required", f"Missing section: {sec['heading']}"))
    for h in style.get("expected_h1", []):
        if h not in headings and f"{h}s" not in headings:
            issues.append(Issue(WARNING, "sections.expected", f"No '{h.title()}' heading (query the author)"))

    # The sign-off at the top (header.py): the placeholder stands until the editor is known.
    from . import header, rules_figures

    pid = header.has_placeholder(ms)
    if pid is not None:
        issues.append(Issue(ERROR, "signoff.name", "The technical editing sign-off has no editor name", pid))

    # Tables and figures in order of first citation (renumbered by the rules, or queried).
    for kind in (rules_figures.TABLE, rules_figures.FIG):
        seen: list[int] = []
        for c in rules_figures.citations(ms):
            if c.kind != kind:
                continue
            new = [n for n in c.numbers if n not in seen]
            if seen and any(n < max(seen) for n in new):
                issues.append(Issue(WARNING, "figures.order", f"{kind} {min(new)} is cited after {kind} {max(seen)}"
                                    " (renumber or query)", c.para_id))
                break
            seen += new

    # Statistics spacing (not in supplementary material: retained as it is, S372 / G151).
    if style.get("statistics.space_around_operators"):
        supp = supplementary_ids(ms)
        for p in body_paragraphs(ms):
            if p.id in supp:
                continue
            for m in re.finditer(r"(?<=[A-Za-z0-9)])[=<>≤≥](?=\d|\.\d)|(?<=[A-Za-z0-9)])[=<>≤≥] (?=\d)|(?<=[A-Za-z0-9)]) [=<>≤≥](?=\d)", p.text):
                if _unary_after_word(p.text, m):  # "showed >90°": a sign before a number, no space after (guide)
                    continue
                if "http" not in p.text and not re.search(r"<\w+>", p.text[max(0, m.start() - 4):m.end() + 4]):
                    issues.append(Issue(ERROR, "statistics.spacing", f"Operator needs a space each side: '{p.text[max(0, m.start() - 6):m.end() + 6]}'", p.id))
    return Report(issues)
