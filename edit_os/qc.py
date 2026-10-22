"""QC gate: deterministic checks of a manuscript against its journal style.

Errors block the file from moving on; warnings go on the editor's checklist.
Run it on the rendered (edited) file, not the original.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import references
from .docmodel import Manuscript
from .rules import _ROUND, _SQUARE, body_paragraphs
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


def _citations(ms: Manuscript) -> list[tuple[str, list[int]]]:
    """(para_id, numbers) for each square-bracket citation in the body and in tables, in citation
    order (a table's citations count at its first callout: renumber.citation_key)."""
    from .renumber import citation_key, citation_paragraphs

    found = []
    for p in citation_paragraphs(ms):
        for m in re.finditer(r"\[(\d[\d,\-–\s]*)\]", p.text):
            nums = []
            for part in re.split(r"\s*,\s*", m.group(1).strip()):
                r = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
                if r:
                    nums += list(range(int(r.group(1)), int(r.group(2)) + 1))
                elif part.isdigit():
                    nums.append(int(part))
            found.append((citation_key(ms, p.id, m.start()), p.id, nums))
    return [(pid, nums) for _, pid, nums in sorted(found, key=lambda x: x[0])]


_LIST_ITEM = re.compile(r"\((\d{1,2})\)\s+[A-Za-z]")
_STAT_WORDS = {"or", "rr", "hr", "ci", "sd", "se", "df", "auc", "icc", "ar", "aor"}


def _enumeration(text: str) -> bool:
    """Round-bracket numbers that run 1, 2, 3 ... each before a word: a numbered list inside the sentence."""
    nums = [int(m.group(1)) for m in _LIST_ITEM.finditer(text)]
    return len(nums) >= 2 and nums == list(range(1, len(nums) + 1))


def _unary_after_word(text: str, m: re.Match) -> bool:
    """A sign with a space before and none after, following an ordinary word ("showed >90", "aged <18"): a unary
    sign, which the guide writes closed up. Not after a variable or statistic ("p >0.05", "OR >2")."""
    if not (m.group(0).startswith(" ") and len(m.group(0)) == 2):
        return False
    word = re.search(r"([A-Za-z]+)\W*$", text[:m.start()])
    return bool(word) and len(word.group(1)) >= 2 and word.group(1).lower() not in _STAT_WORDS


def check(ms: Manuscript, style: JournalStyle) -> Report:
    issues: list[Issue] = []
    refs = references.extract(ms)
    headings = {_heading_key(p.text) for p in ms.paragraphs if len(p.text) < 80}

    if not refs:
        issues.append(Issue(ERROR, "references.present", "No reference list found"))

    # Citations: brackets, order, and every reference cited.
    for p in body_paragraphs(ms):
        if _enumeration(p.text):  # "aims were: (1) describing ... (2) assessing ..." is a list, not citations
            continue
        for m in _ROUND.finditer(p.text):
            prev = p.text[:m.start()].rstrip()
            if prev and not re.search(r"(\d|%|table|figure|fig\.)$", prev, re.I) and style.get("citations.brackets") == "square":
                issues.append(Issue(ERROR, "citations.brackets", f"Citation in round brackets: {m.group(0).strip()}", p.id))
        for m in _SQUARE.finditer(p.text):
            if re.search(r"\s", m.group(2)):
                issues.append(Issue(ERROR, "citations.format", f"Space inside citation: [{m.group(2)}]", p.id))
    cited, seen_max = set(), 0
    for para_id, nums in _citations(ms):
        for n in nums:
            if n not in cited and n > seen_max + 1:
                issues.append(Issue(ERROR, "citations.order",
                                    f"Reference {n} is cited before reference {seen_max + 1}", para_id))
            cited.add(n)
            seen_max = max(seen_max, n)
    numbers = [r.number for r in refs if r.number is not None]
    for n in numbers:
        if cited and n not in cited:
            issues.append(Issue(ERROR, "references.cited", f"Reference {n} is not cited (author query needed)"))
    for n in sorted(cited - set(numbers)) if numbers else []:
        issues.append(Issue(ERROR, "references.exists", f"Citation [{n}] has no entry in the reference list"))

    # Reference entries.
    max_authors = style.get("references.max_authors")
    for r in refs:
        authors = r.text.split(". ")[0]
        if max_authors and "et al" not in authors and authors.count(",") + 1 > max_authors:
            issues.append(Issue(ERROR, "references.authors", f"Reference {r.number}: more than {max_authors} authors", r.para_id))
        if re.search(r"\b[A-Z]\.,", authors):
            issues.append(Issue(WARNING, "references.initials", f"Reference {r.number}: periods after initials", r.para_id))

    # Keywords.
    for p in ms.paragraphs:
        m = re.match(r"^\s*(?:<H\d>)?\s*key\s*-?words?\s*[:.\-–]?\s*(.*)$", p.text, re.I)
        if not m:
            continue
        words = [w.strip() for w in re.split(r"[;,]", m.group(1)) if w.strip()]
        lo, hi = style.get("keywords.min", 3), style.get("keywords.max", 5)
        if not lo <= len(words) <= hi:
            issues.append(Issue(WARNING, "keywords.count", f"{len(words)} keywords; journal needs {lo}-{hi} (query)", p.id))
        if style.get("keywords.alphabetical") and [w.lower() for w in words] != sorted(w.lower() for w in words):
            issues.append(Issue(ERROR, "keywords.order", "Keywords are not in alphabetical order", p.id))
        break

    # Required back matter and expected sections.
    for sec in style.get("required_sections", []):
        if sec["heading"].lower() not in headings:
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

    # Statistics spacing.
    if style.get("statistics.space_around_operators"):
        for p in body_paragraphs(ms):
            for m in re.finditer(r"(?<=[A-Za-z0-9)])[=<>≤≥](?=\d|\.\d)|(?<=[A-Za-z0-9)])[=<>≤≥] (?=\d)|(?<=[A-Za-z0-9)]) [=<>≤≥](?=\d)", p.text):
                if _unary_after_word(p.text, m):  # "showed >90°": a sign before a number, no space after (guide)
                    continue
                if "http" not in p.text and not re.search(r"<\w+>", p.text[max(0, m.start() - 4):m.end() + 4]):
                    issues.append(Issue(ERROR, "statistics.spacing", f"Operator needs a space each side: '{p.text[max(0, m.start() - 6):m.end() + 6]}'", p.id))
    return Report(issues)
