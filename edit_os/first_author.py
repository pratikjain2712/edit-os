"""The "First author" block at the end of the article: label, the first author's full name, their affiliation.

    First author:            (bold)
    Nikhil S Gokhale         (bold)
    Gokhale Eye Hospital, Mumbai, Maharashtra, India

Placed after the reference list, before the figure legends ("Figure Legends" heading or the first legend);
with no legends after the references, at the end of the document. Off by default
(`sections.back_matter.first_author`); journals whose editors add the block switch it on (IJO: the stylesheet is
silent and the technical editors add it in 21 of 22 articles).

The name and affiliation come from the front-matter result (`frontmatter.plan`: the title page split, checked
against the sources, and the submission XML): the first author's name with given names in full (the XML's fuller
form when the title page shortens it), and their first affiliation printed complete, as the author group's
affiliation line prints it. Nothing is invented: when the first author or their affiliation is not known, the
label (and the name, if known) is inserted and an author query asks for the rest.

The paragraphs are `NewParagraph` records (tracked insertions, rendered by `frontmatter.render_inserts`) with the
reason `first_author`, which is the review screen's rule key (`rule_sources.RULES`). A manuscript that already has
the block gets none.

    plan(ms, style, front) -> Plan(inserts, queries)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import count

from . import frontmatter, provenance, queries, references, rules_figures
from .changes import Layer, Source
from .docmodel import Manuscript, has_picture
from .frontmatter import Author, NewParagraph
from .style import JournalStyle

REASON = "first_author"
ROLE = "first_author"

queries.TEXT.update({
    "first_author.missing": "Kindly provide the first author’s full name and affiliation.",
    "first_author.affiliation": "Kindly provide the first author’s affiliation.",
})

# A block the manuscript already has: "First author:", "First Author", "First author's details:", "First author: Name".
_EXISTING = re.compile(r"^\s*(?:<H\d>)?\s*first[\s-]+author(?:['’]s)?(?:\s+details?)?\s*(?:[:\-–]|$)", re.I)
# A heading over the figure legends.
_LEGENDS_HEADING = re.compile(
    r"^\s*(?:<H\d>)?\s*(?:(?:figures?|fig\.?)\s+)?legends?(?:\s+(?:to|for|of)\s+(?:the\s+)?(?:figures?|images?))?"
    r"(?:\s+(?:of|to|for)\s+(?:the\s+)?figures?)?\s*:?\s*$|^\s*(?:<H\d>)?\s*figures?\s*:?\s*$", re.I)


# "Figure 1", "Fig. 2:", "Figure 1 presents ...": a figure legend after the reference list, whatever its style.
_LEGEND_START = re.compile(r"^\s*(?:<H\d>)?\s*fig(?:ure)?s?\.?\s*\d{1,2}(?!\d)", re.I)


@dataclass
class Plan:
    inserts: list[NewParagraph] = field(default_factory=list)
    queries: list[tuple[str, str]] = field(default_factory=list)  # (para_id, queries.TEXT key)


def settings(style: JournalStyle) -> dict:
    return style.get("sections.back_matter.first_author") or {}


def existing(ms: Manuscript) -> str | None:
    """Id of a "First author" paragraph the manuscript already has after its reference list (anywhere past the
    title page when it has no reference heading)."""
    ref = references.find_heading(ms)
    start = ref + 1 if ref is not None else len(frontmatter.leading_paragraphs(ms))
    for p in ms.paragraphs[start:]:
        if not p.in_table and _EXISTING.match(p.text):
            return p.id
    return None


def slot(ms: Manuscript) -> tuple[str, str] | None:
    """(anchor paragraph id, "before" | "after"): before the figure legends that follow the reference list (their
    heading when they have one), else after the last paragraph of the document (outside tables)."""
    paras = ms.paragraphs
    if not paras:
        return None
    ref = references.find_heading(ms)
    start = ref + 1 if ref is not None else 0
    index = {p.id: i for i, p in enumerate(paras)}
    legends = {i for c in rules_figures.captions(ms) if c.kind == rules_figures.FIG
               and (i := index[c.para_id]) >= start and not paras[i].in_table}
    if ref is not None:  # after the references, a legend in a heading style or written as a sentence counts too
        legends |= {i for i in range(start, len(paras)) if not paras[i].in_table and _LEGEND_START.match(paras[i].text)}
    legends = sorted(legends)
    heads = [i for i in range(start, len(paras)) if not paras[i].in_table and _LEGENDS_HEADING.match(paras[i].text)
             and (not legends or i < legends[0])]
    if ref is not None and (heads or legends):
        return paras[heads[0] if heads else legends[0]].id, "before"
    if ref is None and legends:
        return paras[legends[0]].id, "before"
    body = [i for i, p in enumerate(paras) if not p.in_table]
    if not body:
        return None
    last = body[-1]
    filled = [i for i in body if paras[i].text.strip() or has_picture(paras[i].element)]
    if filled and not any(paras[j].in_table for j in range(filled[-1] + 1, last + 1)):
        last = filled[-1]  # trailing empty paragraphs stay below the block (a table after it does not)
    return paras[last].id, "after"


def full_name(a: Author, f: frontmatter.Fields, src: frontmatter.Sources | None, cfg: dict) -> str:
    """The author's name with given names in full; the XML's fuller form of the same name when there is one."""
    names = [Author(x.get("given", ""), x.get("surname", "")) for x in (src.meta.authors if src else [])]
    fuller = frontmatter._expand(Author(a.given, a.surname), names)
    if fuller is None and any(len(t.strip(".")) == 1 for t in frontmatter._name_tokens(a)):
        fuller = frontmatter._expand_from(a, names)
    if fuller is not None and frontmatter._norm(fuller.surname) == frontmatter._norm(a.surname) \
            and not re.search(r"[\d@/]", fuller.given):  # "Meena 9382113646": a phone number in the XML's name field
        a = Author(fuller.given, a.surname)
    return frontmatter.format_name(a, cfg, abbreviate=False)[0]


def plan(ms: Manuscript, style: JournalStyle, front: frontmatter.Plan | None) -> Plan:
    """The block's paragraphs, and the queries for what is not known."""
    cfg = settings(style)
    out = Plan()
    if not cfg.get("enabled") or existing(ms) is not None:
        return out
    atype = next((b.text for b in front.blocks if b.role == "article_type"), "") if front is not None else ""
    if atype.strip().lower() in {t.lower() for t in cfg.get("skip_types", [])}:
        return out
    where = slot(ms)
    if where is None:
        return out
    anchor, side = where
    f = front.fields if front is not None else None
    src = front.sources if front is not None else None
    fcfg = frontmatter._settings(style)
    lead = frontmatter.leading_paragraphs(ms)
    ids = count(1)
    rule = provenance.make("journal_style", "Journal practice", "First author block after the references: label, "
                           "the first author's name and affiliation from the front matter", rule=REASON)

    def add(runs, prov):
        out.inserts.append(NewParagraph(f"fa{next(ids)}", anchor, side, runs, ROLE, source=Source.RULE,
                                        reason=REASON, provenance=prov, layer=Layer.STRUCTURE))

    add([(cfg.get("label", "First author:"), {"bold": True})], rule)
    author = f.authors[0] if f and f.authors else None
    if author is None:
        out.queries.append((anchor, "first_author.missing"))
        return out
    name = full_name(author, f, src, fcfg)
    raw = f"{author.given} {author.surname}".strip()
    name_prov = (frontmatter.locate(raw, src, lead, what="first author") or
                 frontmatter.locate(author.surname, src, lead, what="first author")) if src else None
    add([(name, {"bold": True} if cfg.get("name_bold", True) else {})],
        name_prov or provenance.make("inferred", "Front matter", f"first author of the author group: {raw}"))
    aff_id = next((i for i in author.affs if i in f.affiliations), None)
    if aff_id is None and len(f.affiliations) == 1:
        aff_id = next(iter(f.affiliations))
    text = frontmatter.affiliation_text(f, aff_id) if aff_id is not None else ""
    if not text:
        out.queries.append((anchor, "first_author.affiliation"))
        return out
    aff = f.affiliations[aff_id]
    aff_prov = None
    if src:
        for key in ("institution", "department", "city"):
            aff_prov = frontmatter.locate(aff.get(key, ""), src, lead, what="first author's affiliation")
            if aff_prov:
                break
    add([(text, {})], aff_prov or provenance.make("inferred", "Front matter", "the first author's affiliation in "
                                                  "the author group"))
    return out
