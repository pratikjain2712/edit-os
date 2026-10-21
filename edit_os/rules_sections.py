"""Section rules: body headings, the abstract, key words and the back matter.

What the style editors do (measured on their tracked files, IJO / ABR / NJCP / JCRT / AIAN):

- Headings: numbering is removed ("1. Introduction" -> "Introduction"), minor words in title
  case are lowered ("Materials And Methods"); IJO renames "Materials and Methods" to "Methods" and
  drops the "Introduction" heading. Nobody renames "Methods" to "Materials and Methods" (the
  generic guide's rule), so renames are per-journal settings.
- Abstract: "Abstract:" loses its colon, a missing "Abstract" heading is added, sub-headings get
  a colon and a space ("Purpose:To" -> "Purpose: To"; a sub-heading on its own line gets ": "),
  "Materials and Methods" / "Methodology" -> "Methods" (stylesheets and data).
- Key words: "Keywords:" label, comma + space between, alphabetical order, first one capitalised
  and the rest lower case unless the manuscript writes them capitalised (proper nouns), no final
  period, duplicates removed.
- Back matter: author statements that say nothing ("Funding: None", "Conflict of interest: Nil")
  are deleted and the standard "Financial support and sponsorship / Nil." and "Conflicts of
  interest / There are no conflicts of interest." sections are added before the references, as
  new paragraphs (tracked.PARAGRAPH_BREAK). A real statement keeps its text under the standard
  heading.

Everything is a `Change` on the original text; settings live under `sections:` in the style.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import references
from .changes import Change, Layer
from .docmodel import Manuscript, Paragraph
from .style import JournalStyle
from .tracked import PARAGRAPH_BREAK as BR

_NUMBERING = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s*(?=[A-Za-z])")
_TAG = re.compile(r"^\s*<H\d>\s*")
_MINOR = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of", "on", "or", "the", "to",
          "vs", "via", "with", "from", "into"}


def _change(p: Paragraph, start: int, end: int, new: str, layer: Layer, rule: str, fmt=None) -> Change:
    from .rules import _change as make  # rules registers this module; import late to avoid a cycle

    return make(p, start, end, new, layer, rule, fmt)


def _cfg(style: JournalStyle, key: str, default=None):
    return style.get(f"sections.{key}", default)


def _on(style: JournalStyle, key: str) -> bool:
    return bool(_cfg(style, "enabled", True) and _cfg(style, key, False))


def _key(text: str) -> str:
    """'1. Materials And Methods:' -> 'materials and methods'."""
    return _NUMBERING.sub("", _TAG.sub("", text)).strip().rstrip(":.-–— \t").strip().lower()


def _levels(style: JournalStyle) -> dict[str, str]:
    if not style.get("headings.tag"):
        return {}
    levels = {name: "H1" for name in style.get("headings.h1", [])}
    levels.update({name: "H2" for name in style.get("headings.h2", [])})
    return levels


def _match_case(src: str, new: str) -> str:
    """Write `new` in the case style of `src` (ALL CAPS stays caps)."""
    letters = [c for c in src if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 1:
        return new.upper()
    return new


def _title_case(text: str) -> str:
    words = text.split(" ")
    out = []
    for i, w in enumerate(words):
        if i and w.lower() in _MINOR:
            out.append(w.lower())
        elif w[:1].islower():
            out.append(w[:1].upper() + w[1:])
        else:
            out.append(w)
    return " ".join(out)


def _sentence_case(text: str) -> str:
    words = text.split(" ")
    return " ".join(w if i == 0 or not w[1:].islower() else w.lower() for i, w in enumerate(words))


def _cased(text: str, case: str) -> str:
    if case == "upper":
        return text.upper()
    if case == "title":
        return _title_case(text.lower() if text.isupper() else text)
    if case == "sentence":
        t = text.lower() if text.isupper() else text
        return t[:1].upper() + _sentence_case(t)[1:]
    return text


# --- structure: where the abstract, key words and back matter are ---------------------

_KW = re.compile(r"^(\s*(?:<H\d>)?\s*)(key\s*-?\s*words?)(\s*[:.\-–]?\s*)", re.I)
_ABSTRACT = re.compile(r"^(\s*)(abstract)\b(\s*[:.\-–]?\s*)", re.I)
_SUBHEADS = ("purpose|aims?|aims? and objectives?|background(?: and aims?| and objectives?)?|objectives?|"
             "introduction|context|importance|design|settings?(?: and design)?|study design|participants|"
             "patients|subjects|methods?|methodology|materials? and methods?|subjects? and methods?|"
             "patients? and methods?|statistical analysis(?: used)?|interventions?|main outcome measures?|"
             "observations?|case(?: presentation| description| report| summary)?|results?|findings|"
             "discussion|conclusions?(?: and relevance)?|clinical relevance|significance")
_SUB_INLINE = re.compile(r"^(\s*)(" + _SUBHEADS + r")(\s*:\s*)(?=\S)", re.I)
_SUB_ALONE = re.compile(r"^(\s*)(" + _SUBHEADS + r")(\s*:?\s*)$", re.I)


def _words(text: str) -> int:
    return len(re.findall(r"\w+", text))


@dataclass
class Layout:
    abstract_head: Paragraph | None = None   # the "Abstract" heading paragraph (or inline "Abstract: ...")
    abstract: list[Paragraph] = field(default_factory=list)  # abstract text paragraphs
    keywords: Paragraph | None = None


def layout(ms: Manuscript) -> Layout:
    """Locate the abstract and key words in the front of the manuscript."""
    cached = getattr(ms, "_sections_layout", None)
    if cached is not None:
        return cached
    paras = [p for p in ms.paragraphs if not p.in_table]
    ref_head = references.find_heading(ms)
    limit = ms.paragraphs[ref_head].id if ref_head is not None else None
    front = []
    for p in paras:
        if p.id == limit:
            break
        front.append(p)
    front = front[: max(30, len(front) * 2 // 3)]
    lay = Layout()
    kw_i = next((i for i, p in enumerate(front) if _KW.match(p.text) and len(p.text) < 400), None)
    if kw_i is not None:
        lay.keywords = front[kw_i]
    def is_head(text: str) -> bool:
        m = _ABSTRACT.match(text)
        if not m:
            return False
        rest = text[m.end():]
        return not rest.strip() or bool(_SUB_ALONE.match(rest) or _SUB_INLINE.match(rest)) or _words(rest) > 25

    head_i = next((i for i, p in enumerate(front[:kw_i] if kw_i is not None else front) if is_head(p.text)), None)
    if head_i is not None:
        lay.abstract_head = front[head_i]
        if _words(front[head_i].text) > 25:
            lay.abstract.append(front[head_i])  # inline "Abstract: text"
        stop = kw_i if kw_i is not None else len(front)
        for p in front[head_i + 1:stop]:
            if not p.text.strip():
                continue
            if kw_i is None and _key(p.text) in ("introduction", "background") and not _SUB_INLINE.match(p.text):
                break
            if kw_i is None and len(lay.abstract) >= 12:
                break
            lay.abstract.append(p)
        lay.abstract = lay.abstract[:15]
    elif kw_i is not None:
        # No heading: walk back from the key words over sub-headings and long paragraphs.
        found = []
        for p in reversed(front[:kw_i]):
            if not p.text.strip():
                continue
            if _SUB_ALONE.match(p.text) or _SUB_INLINE.match(p.text) or _words(p.text) >= 15:
                found.append(p)
                if len(found) >= 15:
                    break
            else:
                break
        lay.abstract = list(reversed(found))
    ms._sections_layout = lay
    return lay


def abstract_ids(ms: Manuscript) -> set[str]:
    """Paragraphs inside the abstract (sub-headings there are not body headings)."""
    return {p.id for p in layout(ms).abstract}


def _structured(lay: Layout) -> bool:
    return sum(1 for p in lay.abstract if _SUB_INLINE.match(p.text) or _SUB_ALONE.match(p.text)) >= 2


# --- body headings ---------------------------------------------------------------------

def rule_headings(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not _cfg(style, "enabled", True):
        return []
    levels = _levels(style)
    rename = {k.lower(): v for k, v in (_cfg(style, "headings.rename") or {}).items()}
    drop = {k.lower() for k in _cfg(style, "headings.drop") or []}
    case = _cfg(style, "headings.case", "keep")
    known = set(levels) | set(rename) | drop | {k.lower() for k in style.get("headings.h1", [])}
    skip = abstract_ids(ms) | {p.id for p in [layout(ms).abstract_head, layout(ms).keywords] if p}
    out = []
    for p in ms.paragraphs:
        text = p.text
        if p.in_table or len(text) > 60 or p.id in skip or not text.strip():
            continue
        key = _key(text)
        if key not in known:
            continue
        tag = _TAG.match(text)
        lead = tag.end() if tag else len(text) - len(text.lstrip())
        num = _NUMBERING.match(text[lead:])
        core_start = lead + (num.end() if num else 0)
        core = text[core_start:].rstrip()
        core = core[:-1].rstrip() if core.endswith(":") else core
        core_end = core_start + len(core)
        if key in drop and key not in levels:
            out.append(_change(p, 0, len(text), "", Layer.STRUCTURE, "sections_headings"))
            continue
        if num and _on(style, "headings.strip_numbering"):
            s = lead + num.start()
            out.append(_change(p, s, core_start, "", Layer.STRUCTURE, "sections_headings"))
        new = core
        if key in rename:
            new = _match_case(core, rename[key])
        if case and case != "keep" and levels.get(key, "H1") == "H1":
            new = _cased(new, case)
        elif _on(style, "headings.title_minor_words") and not new.isupper():
            new = " ".join(w.lower() if i and w.lower() in _MINOR and w[:1].isupper() else w
                           for i, w in enumerate(new.split(" ")))
        if new != core:
            level = levels.get(new.lower())
            if level and key not in levels and not tag:  # heading_tags did not know the old name
                new = f"<{level}>{new}"
            out.append(_change(p, core_start, core_end, new, Layer.STRUCTURE, "sections_headings"))
        if _on(style, "headings.bold") and new == core:
            out.append(_change(p, core_start, core_end, core, Layer.STRUCTURE, "sections_headings", {"bold": True}))
    return out


# --- abstract ---------------------------------------------------------------------------

def rule_abstract(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not _cfg(style, "enabled", True):
        return []
    lay = layout(ms)
    if not lay.abstract:
        return []
    out = []
    heading = _cfg(style, "abstract.heading", "Abstract")
    head = lay.abstract_head
    if head is not None:
        m = _ABSTRACT.match(head.text)
        if head in lay.abstract:  # inline "Abstract: text" -> heading paragraph + text
            if _on(style, "abstract.split_inline_heading"):
                out.append(_change(head, m.start(2), m.end(3), heading + BR, Layer.STRUCTURE, "sections_abstract"))
        elif _on(style, "abstract.strip_heading_colon") and m.group(3).strip():
            out.append(_change(head, m.end(2), m.end(2) + len(m.group(3).rstrip()), "", Layer.STRUCTURE,
                               "sections_abstract"))
    elif _on(style, "abstract.add_heading") and _structured(lay):  # an unstructured one is too unsure
        first = lay.abstract[0]
        i = ms.paragraphs.index(first)
        prev = ms.paragraphs[i - 1] if i else None
        if prev is not None and not prev.text and not prev.in_table:
            out.append(_change(prev, 0, 0, heading, Layer.STRUCTURE, "sections_abstract",
                               {"bold": True, "italic": False}))
        else:
            out.append(_change(first, 0, 0, heading + BR, Layer.STRUCTURE, "sections_abstract"))

    rename = {k.lower(): v for k, v in (_cfg(style, "abstract.rename") or {}).items()}
    sub_case = _cfg(style, "abstract.subheading_case", "keep")
    for p in lay.abstract:
        if p is head:
            continue
        m = _SUB_INLINE.match(p.text) or _SUB_ALONE.match(p.text)
        if not m:
            continue
        label = m.group(2)
        new = label
        if label.lower() in rename:
            new = _match_case(label, rename[label.lower()])
        new = _cased(new, sub_case) if sub_case != "keep" else new
        if new != label:
            out.append(_change(p, m.start(2), m.end(2), new, Layer.STRUCTURE, "sections_abstract"))
        sep = m.group(3)
        alone = m.re is _SUB_ALONE
        if _on(style, "abstract.subheading_colon"):
            want = ": "
            if alone and sep != want:
                out.append(_change(p, m.end(2), m.end(3), want, Layer.STRUCTURE, "sections_abstract"))
            elif not alone and sep != want and sep.strip() == ":":
                out.append(_change(p, m.end(2), m.end(3), want, Layer.STRUCTURE, "sections_abstract"))
        if _on(style, "abstract.subheading_bold") and new == label:
            out.append(_change(p, m.start(2), m.end(2), label, Layer.STRUCTURE, "sections_abstract", {"bold": True}))
    return out


# --- key words --------------------------------------------------------------------------

def _body_text(ms: Manuscript, skip: set[str]) -> str:
    """Running text of the manuscript: no titles or headings (Title Case), no reference list."""
    skip = skip | {r.para_id for r in references.extract(ms)}
    return "\n".join(p.text for p in ms.paragraphs if p.id not in skip and _words(p.text) >= 12)


def _counts(word: str, body: str) -> tuple[int, int]:
    """(lower-case uses, capitalised uses inside a sentence) of a word in the manuscript.
    Capitals inside a capitalised phrase ("Complete Androgen Insensitivity Syndrome (CAIS)")
    are the author's emphasis, not a proper noun, and do not count."""
    low = word[0].lower() + word[1:]
    cap = word[0].upper() + word[1:]
    lower = len(re.findall(r"\b" + re.escape(low) + r"\b", body))
    caps = 0
    for m in re.finditer(r"(\S*)( )" + re.escape(cap) + r"\b(?![\s\-‑]+[A-Z])", body):
        prev = m.group(1)
        if prev and not prev[0].isupper() and prev[-1] not in ".!?:":
            caps += 1
    return lower, caps


def _keyword_case(kw: str, body: str) -> str:
    """Lower-case a keyword's words unless the manuscript writes them capitalised (proper nouns:
    "Goldenhar syndrome", "Iran"); capitalise lower-case words the manuscript capitalises."""
    out = []
    for w in kw.split(" "):
        head = re.split(r"[-‐‑/’']", w)[0]
        if len(head) > 1 and head.isalpha() and head[1:].islower():
            lower, caps = _counts(head, body)
            if head[0].isupper() and caps <= lower:
                w = w[0].lower() + w[1:]
            elif head[0].islower() and caps >= 2 and not lower:
                w = w[0].upper() + w[1:]
        out.append(w)
    return " ".join(out)


def keyword_list(text: str) -> tuple[int, list[str]] | None:
    """(offset where the list starts, keywords) for a key words paragraph."""
    m = _KW.match(text)
    if not m:
        return None
    return m.end(), _split_keywords(text[m.end():])


def _split_keywords(rest: str) -> list[str]:
    parts = re.split(r"[;,](?![^()]*\))", rest)  # not inside "(CPFE; IPF)"
    return [k.strip() for k in parts if k.strip()]


def _list_paragraph(ms: Manuscript, kw: Paragraph) -> Paragraph | None:
    """The paragraph after a bare "Key words" line, when it holds the list."""
    i = ms.paragraphs.index(kw)
    nxt = next((q for q in ms.paragraphs[i + 1:i + 3] if q.text.strip()), None)
    if nxt is None or _words(nxt.text) > 40 or not re.search(r"[;,]", nxt.text) or _SUB_INLINE.match(nxt.text):
        return None
    return nxt


def listed_keywords(ms: Manuscript) -> list[str]:
    kw = layout(ms).keywords
    if kw is None:
        return []
    _, words = keyword_list(kw.text)
    if not words and (nxt := _list_paragraph(ms, kw)) is not None:
        words = _split_keywords(nxt.text)
    return words


def rule_keywords(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not _cfg(style, "enabled", True):
        return []
    lay = layout(ms)
    out = []
    label = _cfg(style, "keywords.heading", "Keywords:")
    p = lay.keywords
    if p is None:
        if lay.abstract and _on(style, "keywords.add_missing"):
            last = lay.abstract[-1]
            out.append(_change(last, len(last.text), len(last.text), BR + label + " ", Layer.STRUCTURE,
                               "sections_keywords", {"italic": False, "bold": False}))
        return out
    text = p.text
    m = _KW.match(text)
    end_label = m.end()
    have = text[m.start(2):end_label]
    rest = text[end_label:]
    if _on(style, "keywords.fix_heading"):
        want = label + (" " if rest.strip() else "")
        if have != want:
            out.append(_change(p, m.start(2), end_label, want, Layer.STRUCTURE, "sections_keywords"))
    list_p, base = p, end_label
    if not rest.strip():
        # "Key words" on its own line, the list in the next paragraph
        nxt = _list_paragraph(ms, p)
        if nxt is None:
            return out
        list_p, base, rest = nxt, 0, nxt.text
    kws = _split_keywords(rest)
    original = list(kws)
    if _on(style, "keywords.drop_final_period") and kws and kws[-1].endswith(".") \
            and not re.search(r"\b[A-Za-z]\.$", kws[-1]):
        kws[-1] = kws[-1][:-1].rstrip()
    if _on(style, "keywords.dedupe"):
        seen, uniq = set(), []
        for k in kws:
            if k.lower() not in seen:
                seen.add(k.lower())
                uniq.append(k)
        kws = uniq
    if _on(style, "keywords.case"):
        body = _body_text(ms, {p.id, list_p.id})
        kws = [_keyword_case(k, body) for k in kws]
    if style.get("keywords.alphabetical") and _on(style, "keywords.sort"):
        kws = sorted(kws, key=lambda k: k.lower())
    if _on(style, "keywords.case") and kws and kws[0][:1].islower():
        first = kws[0].split(" ")[0]
        if first.lower() == first:  # not "mRNA", "pH"
            kws[0] = kws[0][0].upper() + kws[0][1:]
    delim = _cfg(style, "keywords.delimiter", ", ")
    new_rest = delim.join(kws)
    lead = len(rest) - len(rest.lstrip())
    trail = len(rest) - len(rest.rstrip())
    old_rest = rest[lead:len(rest) - trail]
    if new_rest != old_rest:
        out.append(_change(list_p, base + lead, base + lead + len(old_rest), new_rest, Layer.STRUCTURE,
                           "sections_keywords"))
    return out


# --- back matter -----------------------------------------------------------------------

_CONFLICT = (r"conflicts?\s+of\s+interests?(?:\s+statement)?|competing\s+interests?|"
             r"declaration\s+of\s+(?:competing\s+|conflicting\s+)?interests?|(?:financial\s+)?disclosures?(?:\s+statement)?|"
             r"conflicting\s+interests?")
_FUNDING = (r"funding(?:\s+(?:information|sources?|statement|support))?|financial\s+support(?:\s+and\s+sponsorship)?|"
            r"sources?\s+of\s+(?:funding|support)|sponsorship|grant\s+support|support")
_ACK = r"acknowledge?ments?"
_LABEL = re.compile(
    r"^\s*(?:<H\d>)?\s*(?:\d+(?:\.\d+)*\.?\s*)?(?P<label>(?P<conflict>" + _CONFLICT + r")(?:\s*(?:&|and)\s*(?P<both>"
    + _FUNDING + r"))?|(?P<funding>" + _FUNDING + r")|(?P<ack>" + _ACK + r"))"
    r"(?:\s*\([^)]{0,60}\))?(?P<sep>\s*[:.\-–—]\s*|\s+|$)", re.I)
_NIL = re.compile(r"\b(?:none|nil|no|not|nothing|n/?a|non[- ]?existent)\b", re.I)
_REAL = re.compile(r"\b(?:grant|supported|funded|funding from|award|fellowship|scheme|project no|received (?:a|funding|support)"
                   r"|thank|grateful|gratitude)\b", re.I)


def _nil(content: str) -> bool:
    c = content.strip()
    if not c:
        return True
    return _words(c) <= 30 and bool(_NIL.search(c)) and not _REAL.search(c)


def _kind(m) -> str:
    if m.group("both"):
        return "both"
    return "conflict" if m.group("conflict") else ("funding" if m.group("funding") else "ack")


_CAPTION = re.compile(r"^\s*(?:<[A-Z]+\d*>)?\s*(?:table|figure|fig\.?|graph|chart|supplementary)\s*[S]?\d", re.I)


def _back_region(ms: Manuscript) -> tuple[list[Paragraph], Paragraph | None]:
    """Paragraphs that may hold back-matter statements (from the discussion on, including any
    after the reference list), and the paragraph the missing sections are added after: the last
    text paragraph before the references, ahead of any table or figure captions."""
    ref_i = references.find_heading(ms)
    end = ref_i if ref_i is not None else len(ms.paragraphs)
    paras = ms.paragraphs[:end]
    start = 0
    for i, p in enumerate(paras):
        if _key(p.text) in ("discussion", "conclusion", "conclusions") and len(p.text) < 40:
            start = i + 1
    if not start:
        start = len(paras) * 2 // 3
    ref_ids = {r.para_id for r in references.extract(ms)}
    region = [p for p in ms.paragraphs[start:] if not p.in_table and p.id not in ref_ids
              and (ref_i is None or p is not ms.paragraphs[ref_i])]
    anchor = None
    if ref_i is not None:
        for p in paras[start:] if start < len(paras) else []:
            if p.in_table or _CAPTION.match(p.text):
                break
            if p.text.strip():
                anchor = p
        if anchor is None:
            anchor = next((p for p in reversed(paras) if p.text.strip() and not p.in_table
                           and not _CAPTION.match(p.text)), None)
    return region, anchor


def rule_back_matter(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not _on(style, "back_matter.enabled"):
        return []
    region, anchor = _back_region(ms)
    tag = "<H2>" if style.get("headings.tag") else ""
    levels = _levels(style)
    std = {}
    for sec in style.get("required_sections", []) or []:
        h = sec["heading"].lower()
        kind = "conflict" if "conflict" in h else ("funding" if "financ" in h or "fund" in h else h)
        std[kind] = sec
    out = []
    satisfied: set[str] = set()
    deleted: set[str] = set()
    delete_nil = _on(style, "back_matter.delete_nil_statements")
    ack_heading = _cfg(style, "back_matter.acknowledgement_heading")
    nonempty = [p for p in region if p.text.strip()]
    for i, p in enumerate(nonempty):
        if p.id in deleted:
            continue
        m = _LABEL.match(p.text)
        if not m or (m.group("sep") and m.group("sep").isspace() and _words(p.text[m.end():]) > 30):
            continue
        kind = _kind(m)
        if kind == "funding" and m.group("label").lower() == "support" and not m.group("sep").strip():
            continue  # a sentence starting "Support ..."
        content = p.text[m.end():]
        content_p = None
        if not content.strip():
            nxt = nonempty[i + 1] if i + 1 < len(nonempty) else None
            if nxt is not None and not _LABEL.match(nxt.text):
                content_p, content = nxt, nxt.text
        nil = _nil(content)
        kinds = ("conflict", "funding") if kind == "both" else (kind,)
        if kind == "ack":
            if nil and delete_nil and (content.strip() or content_p is None and i + 1 < len(nonempty)):
                out.append(_change(p, 0, len(p.text), "", Layer.STRUCTURE, "sections_back_matter"))
                if content_p is not None:
                    out.append(_change(content_p, 0, len(content_p.text), "", Layer.STRUCTURE, "sections_back_matter"))
                    deleted.add(content_p.id)
            elif ack_heading and not content.strip():
                s, e = m.start("ack"), m.end("ack")
                if p.text[s:e] != ack_heading:
                    out.append(_change(p, s, e, ack_heading, Layer.STRUCTURE, "sections_back_matter"))
            continue
        standard = kind in std and m.group("label").lower() == std[kind]["heading"].lower()
        if nil and content_p is not None and standard:
            # The standard heading with a "None" under it: keep both, word the statement as the journal does.
            satisfied.add(kind)
            body = content_p.text.strip()
            default = std[kind].get("default", "")
            if body.rstrip(".").lower() in ("none", "nil", "not applicable", "na", "n/a") and \
                    body.rstrip(".").lower() != default.rstrip(".").lower():
                s = content_p.text.index(body)
                new = "Nil" + body[len(body.rstrip(".")):] if kind == "funding" else default
                out.append(_change(content_p, s, s + len(body), new, Layer.STRUCTURE, "sections_back_matter"))
            deleted.add(content_p.id)
            continue
        if nil and delete_nil:
            out.append(_change(p, 0, len(p.text), "", Layer.STRUCTURE, "sections_back_matter"))
            if content_p is not None:
                out.append(_change(content_p, 0, len(content_p.text), "", Layer.STRUCTURE, "sections_back_matter"))
                deleted.add(content_p.id)
            continue
        if kind == "both" or not all(k in std for k in kinds):
            satisfied.update(kinds)  # a real joint statement: leave it to the editor
            continue
        sec = std[kind]
        satisfied.add(kind)
        if content_p is None and content.strip():
            # "Funding: Eye Research Foundation grant 12." -> heading paragraph + statement
            s = len(p.text) - len(p.text.lstrip())
            out.append(_change(p, s, m.end(), f"{tag}{sec['heading']}{BR}", Layer.STRUCTURE, "sections_back_matter"))
        else:
            s, e = m.start("label"), m.end("label")
            head_end = len(p.text.rstrip())
            label_text = p.text[s:e]
            tagged = levels.get(_key(p.text)) and not p.text.lstrip().startswith("<H")
            if label_text != sec["heading"] or (not tagged and tag and not p.text.lstrip().startswith("<H")):
                new = sec["heading"] if tagged or p.text.lstrip().startswith("<H") else tag + sec["heading"]
                out.append(_change(p, s, e, new, Layer.STRUCTURE, "sections_back_matter"))
            if not tagged and p.text[e:head_end].strip() in (":", "."):
                out.append(_change(p, e, head_end, "", Layer.STRUCTURE, "sections_back_matter"))
    if anchor is None:
        return out
    block = []
    for kind, sec in std.items():
        if kind in satisfied:
            continue
        block += [f"{tag}{sec['heading']}", sec.get("default", "")]
    if block and _on(style, "back_matter.add_missing"):
        out.append(_change(anchor, len(anchor.text), len(anchor.text), BR + BR.join(block), Layer.STRUCTURE,
                           "sections_back_matter", {"bold": False, "italic": False, "superscript": False}))
    return out


# --- author queries ------------------------------------------------------------------------

TEXT = {
    "abstract.citations": "Reference citations should not be present in the abstract. Kindly check.",
    "abstract.too_long": "As per journal style, kindly reduce the abstract to {n} words.",
    "abstract.structured_case_report": "Please note that in the Case Report there shouldn’t be any structured "
                                       "abstract, hence will have to remove the headings.",
    "keywords.too_many": "Kindly provide a maximum of {n} key words.",
    "sections.patient_consent": "Kindly confirm whether consent form obtained or not?",
}


def _is_case_report(ms: Manuscript) -> bool:
    return any(_key(p.text) in ("case report", "case reports", "case presentation", "case description",
                                "case history", "case summary")
               for p in ms.paragraphs if len(p.text) < 40)


def add_queries(ms: Manuscript, style: JournalStyle, qs) -> None:
    """Author queries for the abstract, key words and back matter (see queries.collect)."""
    from .rules import _ROUND, _SQUARE, superscript_citations

    if not _cfg(style, "enabled", True):
        return
    lay = layout(ms)
    if lay.abstract:
        if _on(style, "abstract.citation_query"):
            for p in lay.abstract:
                spans = [m.span() for m in _SQUARE.finditer(p.text)]
                spans += [m.span() for m in _ROUND.finditer(p.text)
                          if re.search(r"[A-Za-z]\s*$", p.text[:m.start()]) and int(re.findall(r"\d+", m.group(2))[0]) < 200]
                spans += [(s, e) for s, e, _ in superscript_citations(p) if s]
                if spans:
                    s, e = min(spans)
                    qs.add(p.id, "abstract.citations", s, e)
                    break
        if _on(style, "abstract.word_limit_query"):
            structured = _structured(lay)
            limit = _cfg(style, "abstract.word_limit.structured" if structured else "abstract.word_limit.unstructured")
            n = sum(_words(p.text) for p in lay.abstract)
            if limit and n > limit:
                qs.add(lay.abstract[0].id, "abstract.too_long", n=limit)
        if _on(style, "abstract.structured_case_report_query") and _structured(lay) and _is_case_report(ms):
            qs.add((lay.abstract_head or lay.abstract[0]).id, "abstract.structured_case_report")
    if lay.keywords is not None and _on(style, "keywords.max_query"):
        kws = listed_keywords(ms)
        hi = style.get("keywords.max")
        if hi and len(kws) > hi:
            qs.add(lay.keywords.id, "keywords.too_many", n=hi)
    elif lay.keywords is None and lay.abstract and _on(style, "keywords.add_missing"):
        qs.add(lay.abstract[-1].id, "keywords.missing")
    if _on(style, "back_matter.patient_consent_query") and _is_case_report(ms):
        if not any(re.search(r"\bconsent\b", p.text, re.I) for p in ms.paragraphs):
            ref_i = references.find_heading(ms)
            if ref_i is not None:
                qs.add(ms.paragraphs[ref_i].id, "sections.patient_consent")
