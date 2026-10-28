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

from . import frontmatter, provenance, queries, references
from .changes import Layer, Source
from .docmodel import Manuscript
from .frontmatter import Author, NewParagraph
from .style import JournalStyle

REASON = "first_author"
ROLE = "first_author"

queries.TEXT.update({
    "first_author.missing": "Kindly provide the first author’s full name and affiliation.",
    "first_author.affiliation": "Kindly provide the first author’s affiliation.",
    # the IJO editors, on the block they add (37 of 37 articles that get one; "Kindly provide designation for first
    # author" where the submission has the photograph: IJO_1184)
    "first_author.photo": "Kindly provide photo and designation for the first author.",
    "first_author.designation": "Kindly provide designation for the first author.",
})

# A submission file that is the author's photograph ("First author photograph.jpg"), not a figure.
_PHOTO_FILE = re.compile(r"(?i)author.*(?:photo|image|picture|pic)|(?:photo|image|picture|pic).*author")

# A block the manuscript already has: "First author:", "First Author", "First author's details:", "First author: Name".
_EXISTING = re.compile(r"^\s*(?:<H\d>)?\s*first[\s-]+author(?:['’]s)?(?:\s+details?)?\s*(?:[:\-–]|$)", re.I)
@dataclass
class Plan:
    inserts: list[NewParagraph] = field(default_factory=list)
    queries: list[tuple[str, str]] = field(default_factory=list)  # (para_id, queries.TEXT key)
    insert_queries: list[queries.Query] = field(default_factory=list)  # on the inserted block itself


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
    """(anchor paragraph id, "before" | "after"): where the back of the article starts after the reference list
    (legends.back_slot): before the figure legends that follow it (their heading, or the picture over the first
    legend), else before the first table (G195: tables at the end, after the figure captions), else after the last
    paragraph of the document (outside tables)."""
    from . import legends

    return legends.back_slot(ms)


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
    if cfg.get("photo_query"):
        # on the block's label, where the editors anchor it ("First author:")
        label = out.inserts[-1]
        photo = any(_PHOTO_FILE.search(n) for n in (front.sources.file_names if front is not None and front.sources
                                                     else []))
        check = "first_author.designation" if photo else "first_author.photo"
        out.insert_queries.append(queries.Query(anchor, 0, len(label.text), queries.TEXT[check], check,
                                                insert_id=label.id, quote=label.text))
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
    text = frontmatter.affiliation_text(f, aff_id, drop_postal=bool(fcfg.get("delete_postal_codes"))) \
        if aff_id is not None else ""
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
