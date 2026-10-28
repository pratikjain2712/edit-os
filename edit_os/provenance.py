"""Where a suggestion comes from: the `provenance` dict on Change and front-matter insert records.

Filled by the rules that know it, deterministically, when the suggestion is made; the review screen shows it
as a "Source" chip next to the rule. Shape (every key but `kind` and `label` optional):

    kind     title_page | xml | manuscript | contribution_form | cover_letter | submission | figure_file |
             pubmed | crossref | europepmc | openalex | semantic_scholar | doaj | unverified | journal_style | inferred | checker | ai | renumber | multiple
    label    short text for the chip ("Title page", "XML: article type", "PubMed 12345678")
    detail   one line more (the input line it was read from, a checker's message)
    url      a link out (PubMed, doi.org, the LanguageTool rule)
    ...      kind-specific fields: file, field, para_id, record, original, styled, rule_id, old, new, parts

Old records without it load unchanged (`provenance` is None).
"""

from __future__ import annotations

import re

from . import biblio

MAX_DETAIL = 240


def _clip(s: str, n: int = MAX_DETAIL) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def make(kind: str, label: str, detail: str = "", url: str = "", **extra) -> dict:
    d = {"kind": kind, "label": label}
    if detail:
        d["detail"] = _clip(detail)
    if url:
        d["url"] = url
    d.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    return d


def combine(parts: list[dict]) -> dict | None:
    parts = [p for p in parts if p]
    uniq = []
    for p in parts:
        if p not in uniq:
            uniq.append(p)
    if not uniq:
        return None
    if len(uniq) == 1:
        return uniq[0]
    return make("multiple", " + ".join(dict.fromkeys(p["label"] for p in uniq)), parts=uniq)


# --- references ---------------------------------------------------------------------------------

def record_fields(r: biblio.Record) -> dict:
    return {k: v for k, v in {
        "authors": ", ".join(f"{s} {i}".strip() for s, i in r.authors[:8]) + (", et al." if len(r.authors) > 8 else ""),
        "title": r.title, "journal": r.journal, "year": r.year, "volume": r.volume, "issue": r.issue,
        "pages": r.pages, "pmid": r.pmid, "doi": r.doi,
    }.items() if v}


def pubmed_url(pmid: str) -> str:
    return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"


def doi_url(doi: str) -> str:
    return f"https://doi.org/{doi}"


def reference(res, original: str) -> dict:
    """A reference entry: the record it was checked against (PubMed, CrossRef, or "Verified in OpenAlex" and the
    like for the other databases), or why it was not verified.
    `res` is a refengine.Resolution (or refs_other's); `original` the author's entry."""
    rec = getattr(res, "record", None)
    styled = getattr(res, "formatted", "") or ""
    common = {"original": original, "styled": styled if styled and styled != original else "",
              "status": getattr(res, "status", "")}
    if getattr(res, "styled", False):  # not verified: styled from the author's own fields (refstyle)
        why = getattr(res, "message", "") or "no strict PubMed / CrossRef match"
        from .refstyle import PROVENANCE_LABEL
        return make("unverified", PROVENANCE_LABEL, why, **common)
    if rec is not None and getattr(rec, "found_via", ""):  # found in another database, taken from PubMed / CrossRef
        common["found_via"] = biblio.SOURCE_NAMES.get(rec.found_via, rec.found_via)
    if rec is not None and getattr(rec, "source", "") == "pubmed" and rec.pmid:
        return make("pubmed", f"PubMed {rec.pmid}", url=pubmed_url(rec.pmid), record=record_fields(rec), **common)
    if rec is not None and rec.source == "crossref" and rec.doi:
        return make("crossref", f"CrossRef {rec.doi}", url=doi_url(rec.doi), record=record_fields(rec), **common)
    if rec is not None and rec.source in biblio.SOURCE_NAMES:  # Europe PMC, OpenAlex, Semantic Scholar, DOAJ
        name = biblio.SOURCE_NAMES[rec.source]
        url = pubmed_url(rec.pmid) if rec.pmid else doi_url(rec.doi) if rec.doi else ""
        return make(rec.source, f"Verified in {name}", rec.doi or (f"PMID {rec.pmid}" if rec.pmid else ""), url=url,
                    record=record_fields(rec), source_name=name, **common)
    if rec is not None and rec.doi:
        name = (rec.source or "Record").replace("_", " ").title()
        return make(rec.source or "record", f"{name} {rec.doi}", url=doi_url(rec.doi), record=record_fields(rec), **common)
    if rec is not None:
        return make(rec.source or "record", (rec.source or "record").replace("_", " ").title(),
                    record=record_fields(rec), **common)
    why = getattr(res, "message", "") or "no strict PubMed / CrossRef match"
    if common["status"] == "other_verified":  # refs_other: a book record or the live page agrees
        return make("checked_online", "Checked online", why, **common)
    return make("unverified", "Not verified", why, **common)
