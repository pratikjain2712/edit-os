"""Submission-only parts inside a manuscript: what the author sent the journal that is not the article.

A plain .docx upload, or a manuscript file the author built from the submission system's pieces, can hold
the copyright notice the portal adds on top, a cover letter, the reply to the reviewers (usually the
portal's table), and title-page lines that only the submission needs ("Number of Figures: 2", phone
numbers, a second "Title - ..." line above the abstract). The style editors delete them; what they do with
each kind was measured on their tracked files (160 articles, `submission:` in base.yaml has the counts).

Detection is deterministic and conservative: a part is recognised from its heading or opening words, its
position and its structure (a closing formula for a letter, the reply table, the abstract that follows a
title repeat). A part is proposed for deletion only when it is complete (start and end found); anything
less certain becomes an editor warning. Nothing is removed in place: each paragraph of a part gets one
`Change` deleting its text (tracked), so the editor can reject it.

    detect(ms, style)         the parts found (cached on the manuscript)
    skip_ids(ms, style)       paragraphs that are not the article (front matter and back matter skip them)
    rule_submission(ms, st)   the deletions (rules.RULES entry, runs before front matter)
    warnings(ms, st)          what was not certain enough to delete, for the editor
    texts_without_parts(t)    a list of paragraph texts (e.g. the title-page file) without letter and reply
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from docx.oxml.ns import qn

from . import provenance
from .changes import Change, Layer, Source
from .docmodel import Manuscript, Paragraph
from .style import JournalStyle, _merge

DELETE, AQ, WARN = "delete", "aq", "warn"

# --- patterns ---------------------------------------------------------------------------------

_COPYRIGHT = re.compile(r"(?is)^\s*(?:AQ\s*:\s*)?the\s+authors?(?:\(s\))?\b.{0,600}?\bha(?:s|ve)(?:\s*/\s*have)?\s+not\s+"
                        r"agreed\s+to\s+the\s+copyright")
_COVER_HEAD = re.compile(r"(?i)^\W*(?:cover(?:ing)?\s+letter|letter\s+of\s+submission|submission\s+letter)\W*$")
_TO = re.compile(r"(?i)^\W*to\s*,?\s*(?:$|\n|the\b)")
_EDITOR = re.compile(r"(?i)\b(?:the\s+)?(?:chief\s+)?editor(?:[\s-]+in[\s-]+chief)?\b")
_SUBMIT_CUE = re.compile(r"(?i)\bsubmi(?:t|ts|tting|tted|ssion)\b|\bfor\s+(?:your\s+)?(?:kind\s+)?consideration\b|"
                         r"\bfor\s+(?:possible\s+)?publication\b|\bintend\s+to\s+publish\b|\b(?:manuscript|article|paper)\s+"
                         r"(?:entitled|titled)\b")
_CLOSING = re.compile(r"(?i)^\W*(?:yours\s+(?:sincerely|faithfully|truly)|sincerely|(?:with\s+)?(?:best|kind|warm)(?:est)?\s+"
                      r"regards|regards|thanking\s+you|respectfully(?:\s+yours)?)\b")
_SIGNATURE = re.compile(r"(?i)^\W*(?:dr|prof|professor|mr|mrs|ms)\b\.?|\b(?:mbbs|md|mds|ms|dnb|phd|frcs|frcp|dm|mch|dnb|fico)\b|@|"
                        r"\b(?:e-?mail|mail\s*id|ph(?:one)?|mob(?:ile)?|tel(?:ephone)?|contact)\b|\b(?:department|dept|"
                        r"hospital|college|university|institute|centre|center|clinic|school)\b|\b(?:resident|professor|"
                        r"consultant|lecturer|head|director|fellow|surgeon|registrar)\b|on\s+behalf|corresponding\s+author")
_REPLY_HEAD = re.compile(
    r"(?i)^\W*(?:(?:the\s+)?authors?['’]?s?\s+)?(?:(?:reply|replies|response|responses|answers?|rebuttal)\s+(?:letter\s+)?(?:to|for)\s+"
    r"(?:the\s+)?(?:(?:reviewers?|referees?|editors?)(?:['’]s|s['’])?|comments|queries)|point[- ]by[- ]point\s+(?:reply|response|"
    r"answer)s?|reviewers?['’]?s?\s+comments?|comments?\s+(?:of|from|by)\s+(?:the\s+)?(?:reviewers?|referees?)|"
    r"reviewer\s*(?:#|no\.?|number)?\s*\d+\s*:?\s*$)")
_REPLY_TABLE = re.compile(r"(?i)^\W*(?:reviewer\s*(?:number|no\.?|#)|original\s+comments?\s+(?:of|by|from)\s+(?:the\s+)?reviewer|"
                          r"(?:reply|response)\s+(?:of|by|from)\s+(?:the\s+)?authors?|changes\s+done\s+on\s+page|location\s+of\s+"
                          r"change)")
_REVIEWER_LABEL = re.compile(r"(?i)^\W*reviewer\s*(?:#|no\.?|number)?\s*\d+\b")
_ARTICLE_TYPE = re.compile(
    r"(?i)^\W*(?:original\s+(?:research\s+)?article|case\s+reports?|case\s+series|review\s+article|systematic\s+review|"
    r"short\s+communication|brief\s+(?:communication|report)|research\s+letter|letter\s+to\s+(?:the\s+)?editor|editorial|"
    r"commentary|perspective|viewpoint|photo\s+essay|surgical\s+technique|images?\s+in\s+\w+(?:\s+\w+)?|ophthalmic\s+images|"
    r"original\s+research)\b[^.]{0,40}$")
_BODY = re.compile(r"(?i)^\W*(?:<H\d>)?\s*(?:abstract|introduction|background|summary|key\s*-?\s*words?|purpose|aims?|"
                   r"objectives?|context)\b")
# Where a part ends: the article (or its title page) starts again.
_STOP = re.compile(
    r"(?i)^\W*(?:<H\d>)?\s*(?:(?:article\s+|manuscript\s+)?title(?:\s+page)?\b|running\s+(?:title|head)|short\s+title|abstract\b|"
    r"introduction\b|background\b|key\s*-?\s*words?\b|references?\W*$|bibliography\W*$|figure\s+legends?|legends?\s+(?:to|for)\s+"
    r"figures?|(?:figure|fig\.?|table)\s*\d+\s*[:.\-–]|cover(?:ing)?\s+letter|first\s+page|type\s+of\s+(?:article|manuscript)|"
    r"(?:article|manuscript)\s+type|authors?\s*:|key\s+(?:learning\s+points?|messages?)|plain\s+language\s+summary)")
_RUNNING_HEAD = re.compile(r"^\W*(?:<RH>)?[^:\n]{1,60}?,?\s+et\s+al\.?\s*:")
_COUNT = re.compile(
    r"(?i)^\W*(?:total\s+)?(?:(?:number|no\.?)\s+of\s+(?:figures?|tables?|words?|references?|pages?|images?|photographs?|"
    r"illustrations?)|(?:total\s+|manuscript\s+|abstract\s+|text\s+|article\s+)?word\s*counts?|total\s+(?:images?|figures?|tables?|"
    r"pages?))\b\s*(?:\([^)]{0,80}\))?\s*[:\-–=]\s*(?:~?\s*\d[\d,]*\b.{0,60}|not\s+applicable\.?|nil\.?|none\.?|)\s*$")
_COUNT_MORE = re.compile(r"(?i)^\W*(?:abstract|text|manuscript|article|main\s+text|body|figures?|tables?|references?|images?)"
                         r"\s*[:\-–]\s*~?\s*\d[\d,]*\s*(?:words?\W*$|\([^)]{0,80}\)\W*$|\W*$)")
_PHONE = re.compile(r"(?i)^\W*(?:phone|ph|tel|telephone|mobile|mob|cell|contact)(?:\s*(?:no\.?|number|numbers))?\s*[:.\-–]?\s*"
                    r"\+?[\d\s()\-/,+]{6,}\W*$")
_ORCID = re.compile(r"(?i)^\W*orcid\b.{0,120}$")
_TITLE_LABEL = re.compile(r"(?i)^\W*(?:article\s+|manuscript\s+)?title\s*[:\-–—]\s*(?P<title>\S.{10,})$")
_FORM = re.compile(r"(?i)^\W*(?:copyright\s+(?:transfer\s+)?form|(?:patient\s+)?consent\s+form|(?:author\s+)?checklist|"
                   r"(?:strobe|consort|prisma|care|stard|arrive|squire|cheers)\s+checklist)\W*$")


@dataclass
class Part:
    kind: str                  # copyright_notice | cover_letter | reviewer_reply | counts | phone | title_repeat | form
    paras: list[Paragraph]     # every paragraph of the part, empty ones included
    action: str                # delete | aq | warn
    note: str = ""

    @property
    def ids(self) -> set[str]:
        return {p.id for p in self.paras}


@dataclass
class _Found:
    parts: list[Part] = field(default_factory=list)
    warnings: list[Part] = field(default_factory=list)


def settings(style: JournalStyle | None) -> dict:
    if style is None:
        return {"enabled": True}
    cfg = dict(style.get("submission", {}) or {})
    over = (cfg.pop("journals", {}) or {}).get(style.code.lower(), {})
    return _merge(cfg, over)


def _words(s: str) -> list[str]:
    return re.findall(r"\w+", s.casefold())


def _lines(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[\n\t]", text) if t.strip()]


def _heading_like(text: str, limit: int = 90) -> bool:
    t = text.strip()
    return bool(t) and len(t) <= limit and len(t.split()) <= 12


def _table_of(p: Paragraph):
    """The outermost table holding paragraph `p`."""
    out = None
    for a in p.element.iterancestors():
        if a.tag == qn("w:tbl"):
            out = a
    return out


def _is_stop(text: str) -> bool:
    t = text.strip()
    return bool(_STOP.match(t) or _RUNNING_HEAD.match(t) or _COPYRIGHT.match(t) or _COVER_HEAD.match(t)
                or (_ARTICLE_TYPE.match(t) and len(t.split()) <= 8) or (_REPLY_HEAD.match(t) and _heading_like(t)))


# --- the parts ---------------------------------------------------------------------------------

def _starts_article(text: str) -> bool:
    """The article or its title page (not the references or captions that close a letter to the editor)."""
    t = text.strip()
    return (_is_stop(t) or bool(_BODY.match(t))) and not (_CAPTIONISH.match(t) or re.match(
        r"(?i)^\W*(?:<H\d>)?\s*(?:references?|bibliography|figure\s+legends?|legends?)\b", t))


def _cover_letter(paras: list[Paragraph], i: int, used: set[int]) -> tuple[int, int, str] | None:
    """(start, end exclusive, why not) for a cover letter starting at paragraph i, or None if none starts here.
    why-not is "" when the letter is complete (deletable)."""
    p = paras[i]
    text = p.text.strip()
    heading = bool(_COVER_HEAD.match(text))
    texts = [q.text for q in paras[i:i + 12] if q.text.strip()][:5]
    addressed = bool(_TO.match(text) and _EDITOR.search(" ".join(texts[:3]))) or bool(
        re.match(r"(?i)^\W*(?:from\s*:|the\s+(?:chief\s+)?editor)", text) and _EDITOR.search(text))
    if not heading and not addressed:
        return None
    if not heading and not _SUBMIT_CUE.search(" ".join(texts)):
        return None
    # the closing formula, within the next 60 non-empty paragraphs, with no part of the article in between
    end, seen = None, 0
    j = i + 1
    while j < len(paras) and seen < 60:
        q = paras[j]
        t = q.text.strip()
        if t:
            seen += 1
            if q.in_table or j in used or _BODY.match(t) or (_STOP.match(t) and not re.match(r"(?i)^\W*title\s+of\b", t)
                                                               and not _ARTICLE_TYPE.match(t)):
                break
            if _CLOSING.match(t) or any(_CLOSING.match(x) for x in _lines(t)[:1]):
                end = j
                # "Thanking you," then "Yours sincerely," and the signature in the following paragraphs
                k, sig = j + 1, 0
                while k < len(paras) and sig < 8:
                    t2 = paras[k].text.strip()
                    if not t2:
                        k += 1
                        continue
                    if paras[k].in_table or _is_stop(t2) or k in used:
                        break
                    if _CLOSING.match(t2) or (len(t2) < 160 and _SIGNATURE.search(t2)):
                        end, sig = k, sig + 1
                        k += 1
                        continue
                    break
                break
        j += 1
    if end is None:
        return (i, i + 1, "no closing (Sincerely, Regards ...) found")
    # the article must follow: some title-page or article start within the next 40 paragraphs
    after = [q for q in paras[end + 1:] if q.text.strip()][:40]
    if not any(_starts_article(q.text) for q in after) and not (heading and not after):
        return (i, end + 1, "no manuscript follows it")
    return (i, end + 1, "")


def _reviewer_reply(paras: list[Paragraph], i: int, used: set[int]) -> tuple[int, int, str] | None:
    p = paras[i]
    t = p.text.strip()
    table_start = p.in_table and _REPLY_TABLE.match(t)
    if not table_start and not (not p.in_table and _REPLY_HEAD.match(t) and _heading_like(t)
                                and not t.endswith(".")):
        return None
    if table_start:  # the portal's table without a heading: start at its first paragraph
        tbl = _table_of(p)
        i = next(k for k in range(i, -1, -1) if k == 0 or _table_of(paras[k - 1]) is not tbl)
    j, tables, text_paras, end = i + (0 if table_start else 1), 0, 0, None
    prev_tbl = None
    while j < len(paras):
        q = paras[j]
        t2 = q.text.strip()
        if q.in_table:
            tbl = _table_of(q)
            if tbl is not prev_tbl:  # a new table: the first one, or one that is itself a reply table
                rows = [x.text for x in paras[j:j + 12] if _table_of(x) is tbl]
                if tables and not any(_REPLY_TABLE.match(r.strip()) for r in rows):
                    break
                tables += 1
                prev_tbl = tbl
            end = j
            j += 1
            continue
        if not t2:
            j += 1
            continue
        if j in used or _is_stop(t2) and not (_REPLY_HEAD.match(t2) and _heading_like(t2)) or _CAPTIONISH.match(t2):
            break
        if tables:  # text after the reply table: a "Reviewer 2" label goes on, anything else ends the part
            if _REVIEWER_LABEL.match(t2) and _heading_like(t2):
                end = j
                prev_tbl = None
                tables = 0
                j += 1
                continue
            break
        text_paras += 1
        end = j
        j += 1
    if end is None:
        return (i, i + 1, "a reply heading with nothing under it")
    stopped = j < len(paras) and paras[j].text.strip() and (_is_stop(paras[j].text) or _CAPTIONISH.match(paras[j].text))
    if not tables and not stopped and j < len(paras):
        return (i, end + 1, "could not tell where the reply ends")
    if not tables and j >= len(paras) and not _after_references(paras, i):
        return (i, end + 1, "the reply runs to the end of the file but is not after the references")
    if not tables and text_paras > 150:
        return (i, end + 1, "the reply is too long to be sure")
    return (i, end + 1, "")


_CAPTIONISH = re.compile(r"(?i)^\W*(?:figure|fig\.?|table|graph|chart|legends?)\s*\d*\s*[:.\-–]")


def _after_references(paras: list[Paragraph], i: int) -> bool:
    return any(re.match(r"(?i)^\W*(?:<H\d>)?\s*(?:references?|bibliography)\W*$", q.text.strip()) for q in paras[:i])


def _body_start(paras: list[Paragraph], used: set[int]) -> int:
    for k, p in enumerate(paras):
        if k in used or p.in_table:
            continue
        if _BODY.match(p.text) or re.match(r"(?i)^\W*(?:dear\s+(?:editor|sir)|sir\s*,)", p.text):
            return k
    return next((k for k, p in enumerate(paras) if k not in used and len(p.text) > 400), len(paras))


def detect(ms: Manuscript, style: JournalStyle | None = None) -> _Found:
    cached = getattr(ms, "_submission_parts", None)
    if cached is not None and (cached[0] is style or style is None):
        return cached[1]
    found = _Found()
    ms._submission_parts = (style, found)
    cfg = settings(style)
    if not cfg.get("enabled", True):
        return found
    paras = ms.paragraphs
    used: set[int] = set()

    def add(kind: str, lo: int, hi: int, action: str, note: str = "") -> None:
        part = Part(kind, paras[lo:hi], action, note)
        (found.warnings if action == WARN else found.parts).append(part)
        if action != WARN:
            used.update(range(lo, hi))

    # the portal's copyright notice: editors turn it into an author query ("AQ: The author(s) ...")
    for k, p in enumerate(paras[:5]):
        if not p.in_table and _COPYRIGHT.match(p.text):
            add("copyright_notice", k, k + 1, AQ if cfg.get("copyright_notice_aq", True) else WARN)
            break
    # cover letters and replies to the reviewers
    k = 0
    while k < len(paras):
        if k in used or not paras[k].text.strip():
            k += 1
            continue
        hit = _cover_letter(paras, k, used)
        kind = "cover_letter"
        if hit is None:
            hit = _reviewer_reply(paras, k, used)
            kind = "reviewer_reply"
        if hit is None:
            k += 1
            continue
        lo, hi, why = hit
        on = cfg.get(f"delete_{kind}", True)
        if why or not on:
            if not any(w.kind == kind and 0 <= lo - paras.index(w.paras[0]) <= 5 for w in found.warnings):
                add(kind, lo, lo + 1, WARN, why or "left for the editor (journal setting)")
            k += 1
            continue
        add(kind, lo, hi, DELETE)
        k = hi
    # title-page lines only the submission needs, before the abstract / main text
    body = _body_start(paras, used)
    refs = next((k for k, p in enumerate(paras) if k not in used and not p.in_table
                 and re.match(r"(?i)^\W*(?:<H\d>)?\s*(?:references?|bibliography)\W*$", p.text.strip())), len(paras))
    prev_count = False
    for k in range(max(body, refs) if refs > body else body):
        p = paras[k]
        t = p.text.strip()
        if k in used or p.in_table or not t:
            continue
        lines = _lines(t)
        if k >= body and not (_COUNT.match(lines[0]) or prev_count):
            continue  # after the abstract only the "Word count: ..." lines
        if cfg.get("delete_counts", True) and len(t) < 300 and (
                _COUNT.match(lines[0]) and all(_COUNT.match(x) or _COUNT_MORE.match(x) for x in lines[1:])
                or prev_count and all(_COUNT_MORE.match(x) for x in lines)):
            add("counts", k, k + 1, DELETE)
            prev_count = True
            continue
        prev_count = False
        if k >= body:
            continue
        if cfg.get("delete_phone", True) and len(t) < 300 and any(_PHONE.match(x) for x in lines) and all(
                _PHONE.match(x) or cfg.get("delete_orcid", False) and _ORCID.match(x) for x in lines):
            add("phone", k, k + 1, DELETE)
            continue
        if cfg.get("delete_orcid", False) and len(t) < 300 and all(_ORCID.match(x) for x in lines):
            add("orcid", k, k + 1, DELETE)
            continue
    # "Title - ..." over the abstract when the title page above already gives the title
    if cfg.get("delete_title_repeat", True):
        for k in range(min(body + 1, len(paras))):
            p = paras[k]
            m = _TITLE_LABEL.match(p.text.strip())
            if k in used or p.in_table or not m:
                continue
            nxt = next((q for n, q in enumerate(paras[k + 1:], k + 1) if q.text.strip() and n not in used), None)
            if nxt is None or not _BODY.match(nxt.text):
                continue
            words = _words(m.group("title"))
            for n, q in enumerate(paras[:k]):
                if q.in_table or not q.text.strip() or n in used:
                    continue
                have = set(_words(q.text))
                if len(words) >= 3 and sum(w in have for w in words) / len(words) >= 0.85 \
                        and len(_words(q.text)) <= len(words) + 12:
                    add("title_repeat", k, k + 1, DELETE)
                    break
    # submission forms pasted in as a label ("Copyright form", "Checklist"): the editor decides
    for k, p in enumerate(paras):
        if k not in used and not p.in_table and _FORM.match(p.text.strip()):
            add("form", k, k + 1, WARN, "a submission form label; check whether the form itself is in the file")
    return found


def skip_ids(ms: Manuscript, style: JournalStyle | None = None) -> set[str]:
    """Paragraphs that are not part of the article (deleted or turned into a query)."""
    return {i for part in detect(ms, style).parts for i in part.ids}


def deleted_ids(ms: Manuscript, style: JournalStyle | None = None) -> set[str]:
    return {i for part in detect(ms, style).parts if part.action == DELETE for i in part.ids}


def cover_letter_paragraphs(ms: Manuscript, style: JournalStyle | None = None) -> list[str]:
    """The text of the cover letter(s) found in the manuscript (the back-matter rule reads contributions there)."""
    return [p.text for part in detect(ms, style).parts if part.kind == "cover_letter" for p in part.paras if p.text.strip()]


def notice_names(ms: Manuscript) -> list[tuple[str, str]]:
    """(given names, surname) of the authors the portal's copyright notice names in full ("The author(s) Dr
    Chinmayee J Thrishulamurthy has/have not agreed ..."): the front matter expands "Chinmayee J T" with them."""
    for p in ms.paragraphs[:5]:
        m = re.match(r"(?is)^\s*(?:AQ\s*:\s*)?the\s+(?:following\s+)?authors?(?:\(s\))?\s+(?P<names>.+?)\s+ha(?:s|ve)\b", p.text)
        if not (m and _COPYRIGHT.match(p.text)):
            continue
        out = []
        for name in re.split(r"\s*,\s*|\s+and\s+", m.group("names")):
            name = re.sub(r"(?i)\bnot\s+available\b", " ", name)
            words = re.sub(r"(?i)^(?:(?:dr|prof|professor|mr|mrs|ms)\.?\s+)+", "", name.strip()).split()
            if len(words) >= 2:
                out.append((" ".join(words[:-1]), words[-1]))
        return out
    return []


_MEDIA = ("drawing", "pict", "object")


def has_media(p: Paragraph) -> bool:
    """The paragraph holds an image, chart or embedded object (never deleted as submission material)."""
    return any(el.tag.rpartition("}")[2] in _MEDIA for el in p.element.iter())


def rule_submission(ms: Manuscript, style: JournalStyle) -> list[Change]:
    """Tracked deletions of the submission-only parts; "AQ: " before the portal's copyright notice.
    Paragraphs holding an image or object are left for the editor (see `warnings`)."""
    out: list[Change] = []
    prefix = settings(style).get("copyright_notice_prefix") or "AQ: "  # JEHP editors write "Query: "
    for n, part in enumerate(detect(ms, style).parts, 1):
        prov = provenance.make("submission", PART_LABELS.get(part.kind, part.kind.replace("_", " ").capitalize()),
                               "submission material inside the manuscript file")
        for p in part.paras:
            if not p.text.strip():
                continue
            if part.action == DELETE and has_media(p):
                continue
            if part.action == DELETE:
                out.append(Change(id=f"sub{n}_{p.id}", para_id=p.id, start=0, end=len(p.text), old=p.text, new="",
                                  layer=Layer.STRUCTURE, source=Source.RULE, reason=f"submission_{part.kind}",
                                  provenance=prov))
            elif part.action == AQ and not re.match(r"\s*(?:AQ|Query)\s*:", p.text, re.I):
                at = len(p.text) - len(p.text.lstrip())
                out.append(Change(id=f"sub{n}_{p.id}", para_id=p.id, start=at, end=at, old="", new=prefix,
                                  layer=Layer.QUERY, source=Source.RULE, reason=f"submission_{part.kind}",
                                  provenance=prov))
    return out


PART_LABELS = {"copyright_notice": "Portal copyright notice", "cover_letter": "Cover letter",
               "reviewer_reply": "Reply to reviewers", "counts": "Word counts", "phone": "Phone numbers",
               "title_repeat": "Repeated title", "form": "Submission form"}


def warnings(ms: Manuscript, style: JournalStyle) -> list[dict]:
    """Editor warnings for parts that look like submission material but were not certain enough to delete."""
    out = []
    for part in detect(ms, style).warnings:
        p = part.paras[0]
        out.append({"para_id": p.id, "rule": f"submission_{part.kind}",
                    "message": f"{part.kind.replace('_', ' ')} not deleted ({part.note}): {p.text.strip()[:80]!r}"})
    for part in detect(ms, style).parts:
        for p in part.paras:
            if part.action == DELETE and has_media(p):
                out.append({"para_id": p.id, "rule": f"submission_{part.kind}",
                            "message": f"{part.kind.replace('_', ' ')}: paragraph with an image or object kept; "
                                       f"delete it by hand if it is not part of the article"})
    return out


def drop_inside_deleted(changes: list[Change]) -> list[Change]:
    """Other proposals on a paragraph this module deletes (a heading tag at its start, a copy edit) go: the
    paragraph is not part of the article."""
    gone = {c.para_id for c in changes if c.reason.startswith("submission_") and c.new == "" and c.start == 0
            and c.end == len(c.old) and c.old.strip()}
    return [c for c in changes if c.para_id not in gone or c.reason.startswith("submission_")]


def texts_without_parts(texts: list[str]) -> list[str]:
    """Paragraph texts (a title-page file) without the cover letter and reply to the reviewers in them."""
    from .docmodel import Paragraph as P
    from lxml import etree

    el = etree.Element(qn("w:p"))
    paras = [P(id=f"t{i}", element=el, style="", text=t) for i, t in enumerate(texts)]
    ms = Manuscript(path="", document=None, paragraphs=paras)
    drop = {p.id for part in detect(ms).parts if part.kind in ("cover_letter", "reviewer_reply") for p in part.paras}
    return [p.text for p in paras if p.id not in drop]
