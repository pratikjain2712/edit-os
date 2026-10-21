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
  heading. A nil statement becomes the standard section in place when that keeps the order. A bare
  "Declarations" heading and inline nil ethics statements ("Ethics: Not required") go.
- Patient consent: an author's statement without a heading gets one; with none at all and
  identifiable patient images (decided from the article type and figure legends), the standard
  statement is added with the stylesheet's query. Authors' contributions: an unlabelled list gets
  its heading; per journal, the list from the title page / contribution form / cover letter is added.
  New sections go in the journal's order (consent, contributions, financial support, conflicts).

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

# --- sub-headings --------------------------------------------------------------------
#
# "Statistical analysis" on its own line inside Methods -> "<H2>Statistical analysis". Measured on the
# style editors' files (160 articles): a short line (at most 8 words) inside a main section, without a
# final period, starting with a capital (or "2.1"), followed by running text, is tagged in 92% of cases
# when it is bold or italic, and in 93% when it is plain and the next paragraph is long (>= 200 chars).
# Statements ("Funding", "Not applicable"), legends and "ABBR: expansion" lines are left out.

_SUB_SKIP = re.compile(
    r"^(acknowledg|funding|financial|conflicts?\b|competing|authors?['’]?\s+contrib|declaration|ethic|"
    r"availability|data availability|source of support|figure legends?|legends?\b|key\s*-?\s*words?|"
    r"not applicable|not statistically|none\b|nil\b|consent|abbreviations?|list of abbreviations|tables?\b|"
    r"figures?\b|statements?\b|authors?\b|text\b)", re.I)
_SUB_NUMBER = re.compile(r"^(\d+(?:\.\d+)+)\.?\s+(?=[A-Z])")
_RUN_IN = re.compile(r"^[A-Z][\w’'\- ]{2,40}:\s+[A-Z]")
_ABBR_LINE = re.compile(r"^[A-Z][A-Za-z]*[A-Z]\w*\s*[:=–,-]\s")


def _all_fmt(p: Paragraph, prop: str) -> bool:
    from .rules_format import char_formats

    letters = [f for f, c in zip(char_formats(p), p.text) if c.strip()]
    return bool(letters) and all(f.get(prop) for f in letters)


def rule_subheadings(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not style.get("headings.tag") or not style.get("headings.subheadings", False):
        return []
    from . import rules_figures

    max_words = style.get("headings.subheading_max_words", 8)
    min_next = style.get("headings.subheading_plain_min_next", 200)
    main = ({n.lower() for n in style.get("headings.h1", [])} | set(_levels(style))
            | {k.lower() for k in (_cfg(style, "headings.rename") or {})}
            | {k.lower() for k in (_cfg(style, "headings.drop") or [])}
            | {"introduction", "background", "materials and methods", "material and methods", "methods",
               "results", "discussion", "case report", "case presentation", "case description"})
    known = set(_levels(style)) | {"abstract", "references", "conclusion", "conclusions"}
    ref_i = references.find_heading(ms)
    paras = ms.paragraphs[:ref_i] if ref_i is not None else ms.paragraphs
    skip = abstract_ids(ms) | {p.id for p in [layout(ms).abstract_head, layout(ms).keywords] if p}
    found = []  # (paragraph, bold, italic, numbering, section number)
    in_body = False
    section = 0
    run_in: dict[int, int] = {}  # section -> paragraphs opening with a run-in label ("Histopathology: The ...")
    for i, p in enumerate(paras):
        text = p.text.strip()
        if not text or p.in_table or p.id in skip:
            continue
        key = _key(p.text)
        if key in main:
            in_body = key not in ("conclusion", "conclusions")
            section += 1
            continue
        if _RUN_IN.match(text) and len(text) > 100:
            run_in[section] = run_in.get(section, 0) + 1
        if not in_body or key in known or len(text) > 100 or _TAG.match(text):
            continue
        if text.endswith((".", ",", ";")) or len(text.split()) > max_words or _SUB_SKIP.match(text) \
                or _ABBR_LINE.match(text) or rules_figures.caption_match(p) is not None:
            continue
        num = _SUB_NUMBER.match(text)
        if ":" in text[:-1] or not (text[:1].isupper() or num):  # "Comparator (C): Non-smokers"
            continue
        nxt = next((q for q in paras[i + 1:] if q.text.strip()), None)
        if nxt is None or nxt.in_table or len(nxt.text) < 100:
            continue  # a list item or a label over a table, not a heading over running text
        bold, italic = _all_fmt(p, "bold"), _all_fmt(p, "italic")
        if not (bold or italic) and (text.endswith(":") or len(nxt.text) < min_next):
            continue
        found.append((p, bold, italic, num, section))
    has_bold = any(f[1] for f in found)
    out = []
    for p, bold, italic, num, sec in found:
        if p.text.rstrip().endswith(":") and run_in.get(sec, 0) >= 2:
            continue  # "Staging and Treatment:" among run-in labels ("Ocular examination: The ...") is one of them
        # Italic heads under bold ones, and "2.1.1"-numbered heads, are third level.
        level = "H3" if (italic and not bold and has_bold) or (num and num.group(1).count(".") >= 2) else "H2"
        lead = len(p.text) - len(p.text.lstrip())
        out.append(_change(p, lead, lead, f"<{level}>", Layer.STRUCTURE, "subheadings"))
        if style.get("headings.strip_trailing_colon", True) and p.text.rstrip().endswith(":"):
            k = len(p.text.rstrip()) - 1
            out.append(_change(p, k, k + 1, "", Layer.STRUCTURE, "subheadings"))
    return out


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

_PL = r"(?:\(s\)|s)?"  # "Conflict(s)", "Source(s)", "Disclosure(s)"
_CONFLICT = (r"(?:declaration\s+of\s+)?conflict" + _PL + r"\s+of\s+interest" + _PL + r"(?:\s+statement)?"
             r"(?:\s+and\s+(?:any\s+)?disclosure" + _PL + r")?|"
             r"competing\s+interest" + _PL + r"(?:\s+(?:statement|declaration))?|"
             r"declaration\s+of\s+(?:competing\s+|conflicting\s+)?interest" + _PL + r"|"
             r"(?:financial\s+)?disclosure" + _PL + r"(?:\s+statement)?|"
             r"conflicting\s+interest" + _PL + r"(?:\s+statement)?|"
             r"financial\s+(?:relationship|interest)" + _PL + r"(?:\s+and\s+sponsor(?:ship|ership))?")
_FUNDING = (r"funding(?:\s+(?:information|sources?|statement|support|details))?|"
            r"financial\s+support(?:\s+and\s+sponsorship)?|"
            r"source" + _PL + r"\s+of\s+(?:funding|support)(?:\s*/\s*funding)?|"
            r"sponsorship|grant\s+support|support(?:\s*/\s*funding)?")
_ACK = r"acknowledge?ments?|acknowledge(?=\s*[:\-–—])"
_LABEL = re.compile(
    r"^\s*(?:<H\d>)?\s*(?:\d+(?:\.\d+)*\.?\s*)?(?P<label>(?P<conflict>" + _CONFLICT + r")(?:\s*(?:&|and)\s*(?P<both>"
    + _FUNDING + r"))?|(?P<funding>" + _FUNDING + r")|(?P<ack>" + _ACK + r"))"
    r"(?:\s*\([^)]{0,60}\))?(?P<sep>\s*[:.\-–—]\s*|\s+|$)", re.I)
_NIL = re.compile(r"\b(?:none|nil|no|not|nothing|n/?a|non[- ]?existent)\b", re.I)
_REAL = re.compile(r"\b(?:grant|supported|funded|funding from|award|fellowship|scheme|project no|received (?:a|funding|support)"
                   r"|thank|grateful|gratitude)\b", re.I)
_NEGATION = re.compile(r"\b(?:no|not|nothing|none|neither|nor|without)\b", re.I)


def _nil(content: str) -> bool:
    """A statement that says nothing: "None", "Nil.", "The authors declare no conflicts of interest",
    "This review has no specific grant from any funding agency" (a negated "grant" is not real support)."""
    c = content.strip()
    if not c:
        return True
    if _words(c) > 30 or not _NIL.search(c):
        return False
    return not any(not _NEGATION.search(c[max(0, m.start() - 60):m.start()]) for m in _REAL.finditer(c))


def _kind(m) -> str:
    if m.group("both"):
        return "both"
    return "conflict" if m.group("conflict") else ("funding" if m.group("funding") else "ack")


_CAPTION = re.compile(r"^\s*(?:<[A-Z]+\d*>)?\s*(?:table|figure|fig\.?|graph|chart|supplementary)\s*[S]?\d", re.I)

# A sentence with no label that only says there is nothing to declare ("No conflict of interest.",
# "The authors have nothing to disclose.", "This work did not receive any funding ..."): editors delete it.
_NIL_SENTENCE = re.compile(r"^\s*(?:the\s+authors?(?:\(s\))?|all\s+(?:the\s+)?authors|authors|we|none\s+of\s+the\s+authors|no"
                           r"|there\s+(?:is|are|was|were)\s+no|this\s+(?:work|study|research|article|review|manuscript"
                           r"|systematic\s+review|case(?:\s+report)?))\b", re.I)
_NIL_TOPIC = re.compile(r"conflict|competing|\binterests?\b|funding|financial|\bgrant|sponsor|disclos|relationship", re.I)

# "Declarations" / "Statements and declarations": a container heading over the author statements; editors
# delete it (13 of 14 in the training files) as the statements become the journal's own sections.
_DECLARATIONS = re.compile(r"^\s*(?:<H\d>)?\s*(?:\d+\.?\s*)?(?:(?:ethical\s+)?declarations|statements?\s+and\s+declarations"
                           r"|declarations?\s+and\s+statements?|compliance\s+with\s+ethical\s+standards)\s*:?\s*$", re.I)
# Ethics / consent statements that say nothing ("Ethics: Not required for single case report.", "Consent for
# publication: Not applicable"): editors delete them (7 of 7).
_ETHICS = re.compile(
    r"^\s*(?:<H\d>)?\s*(?:\d+(?:\.\d+)*\.?\s*)?(?P<label>(?:ethic(?:s|al)|ethics\s+committee)(?:\s+(?:approval|statement|clearance"
    r"|declaration|committee\s+approval|considerations?))?(?:\s+and\s+consent(?:\s+to\s+participate)?(?:\s+and\s+publication)?)?"
    r"|consent\s+(?:for|to)\s+(?:publication|participate)(?:\s+and\s+(?:publication|publish))?"
    r"|institutional\s+review\s+board(?:\s+statement)?)(?P<sep>\s*[:.\-–—]\s*|\s*$)", re.I)
_ETHICS_NIL = re.compile(r"^\s*(?:not\s+(?:applicable|required|needed|necessary|sought)|n/?a\b|none\b|nil\b|exempt|waived)", re.I)

# Consent: a heading ("Declaration of patient consent", "Patient Consent:", "Consent for publication") or a
# statement without one ("Written informed consent was obtained from the patient for the publication ...").
_CONSENT_HEAD = re.compile(
    r"^\s*(?:<H\d>)?\s*(?:\d+(?:\.\d+)*\.?\s*)?(?P<label>(?:authors?[’'`]?s?[’'`]?\s+)?declaration(?:\s+statement)?\s+of\s+"
    r"(?:the\s+)?(?:patients?[’'`]?s?[’'`]?\s+)?consent|patients?[’'`]?s?[’'`]?\s+consent(?:\s+(?:statement|form|for\s+publication))?"
    r"|participants?[’'`]?s?[’'`]?\s+consent|informed\s+consent(?:\s+statement)?)(?P<sep>\s*[:.\-–—]\s*|\s*$)", re.I)
_CONSENT_GIVEN = re.compile(r"\bconsent\b.{0,200}\b(?:obtained|given|taken|signed|provided|received)\b|"
                            r"\b(?:obtained|given|taken|signed|provided|certify)\b.{0,120}\bconsent\b", re.I)
_CONSENT_PUBLICATION = re.compile(r"\b(?:publication|publish\w*|images?|photo\w*|clinical\s+(?:information|details)"
                                  r"|case\s+report|this\s+case|reported\s+in\s+the\s+journal)\b", re.I)

# Authors' contributions: a heading ("Authors' contributions", "Author Contributions (CRediT)") or an inline label.
_CONTRIB = re.compile(
    r"^\s*(?:<H\d>)?\s*(?:\d+(?:\.\d+)*\.?\s*)?(?P<label>(?P<author>(?:authors?[’'`]?s?[’'`]?|authorship|individual)\s+)?"
    r"contributions?(?:\s+of\s+(?:the\s+)?authors?)?(?:\s+(?:statement|details))?(?:\s*\(credit\))?"
    r"|credit\s+(?:authorship\s+)?(?:contribution\s+)?statement)(?P<sep>\s*[:.\-–—]\s*|\s+|$)", re.I)
_CONTRIB_WORDS = re.compile(
    r"\b(?:concept\w*|design\w*|intellectual|literature|data|manuscript|writing|wrote|draft\w*|review\w*|edit\w*|analy[sz]\w*"
    r"|statistic\w*|supervis\w*|investigation|methodology|clinical|surg\w*|acquisition|interpretation|curation|visuali[sz]\w*"
    r"|validation|resources|software|approv\w*|responsible|performed|collect\w*|critical\w*|revis\w*|stud(?:y|ies)|patients?)\b",
    re.I)
_UP = "A-ZÀ-ÖØ-Þİ"
# "Prof. Dr. Asha K. R. – Concept, design, ..."; "A.B.: Conceptualization, ..."; "Ravi Kumar (RK): ..."
_CONTRIB_LINE = re.compile(rf"^\s*(?:[•➢▪*]\s*)?(?:(?:prof|dr|mr|mrs|ms)\.?\s+)*(?:[{_UP}][\w.’'\-]*\.?\s*){{1,6}}"
                           rf"(?:\([{_UP}. ]{{1,10}}\)\s*)?[:–—\-]\s+\S")
_TEMPLATE = re.compile(r"[\[\]□☐✓]|^author\s+name|^contributor\s*\d|write the first letter|^role\b|tick", re.I)
_OTHER_LABEL = re.compile(r"^\s*(?:corresponding|address|e-?mail|word\s+count|no\.?\s+of|number\s+of|thank|sincerely|yours|"
                          r"regards|declaration|statement\s+on|data\s+(?:availability|sharing|supporting)|availability|key\s*words?|"
                          r"running\s+title|orcid|ethic|consent|funding|conflict|acknowledg|financial|source|competing|"
                          r"meeting|presentation|copyright|guarantor|we\s+(?:hope|look)|the\s+manuscript\s+(?:has|is)\s+not)",
                          re.I)
_PHOTO_LEGEND = re.compile(r"clinical\s+(?:photo(?:graph)?s?|pictures?|images?)|(?:facial|external|full[- ]face)\s+"
                           r"(?:photo(?:graph)?s?|appearance|view)|photo(?:graph)?s?\s+of\s+the\s+(?:patient|face|child|baby|infant)"
                           r"|patient[’']?s\s+face", re.I)
_LEGEND = re.compile(r"^\s*(?:<[A-Z]+\d*>)?\s*(?:figure|fig\.?)\s*\d", re.I)
_CASE_TYPE = re.compile(r"\bcase\s+(?:reports?|series|snippets?|history)\b|clinicopathologic", re.I)
_IMAGE_TYPE = re.compile(r"\bimages?\b|neuroimage|photo\s*(?:essay|quiz)", re.I)
_LETTER_TYPE = re.compile(r"\bletters?\b|correspondence", re.I)


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
    # A label statement read as the last entries of a reference list ("Conflicts of interests: None" after
    # the references) is back matter, not a reference.
    ref_ids = {r.para_id for r in references.extract(ms)
               if not (_LABEL.match(r.text) and _words(r.text) <= 12 and _LABEL.match(r.text).group("sep").strip())}
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


def article_type(ms: Manuscript) -> str:
    """The article type from the submission XML ("Case Report", "Letter to Editor"), when there is one."""
    src = getattr(ms, "front_sources", None)
    return (getattr(getattr(src, "meta", None), "article_type", "") or "").strip()


def _is_case_report(ms: Manuscript) -> bool:
    if _CASE_TYPE.search(article_type(ms)):
        return True
    return any(_key(p.text) in ("case report", "case reports", "case presentation", "case description",
                                "case history", "case summary")
               for p in ms.paragraphs if len(p.text) < 40)


def has_consent_statement(ms: Manuscript) -> bool:
    return any(re.search(r"\bconsent\b", p.text, re.I) for p in ms.paragraphs)


def identifiable_patient_images(ms: Manuscript) -> bool:
    """Does the article print images of a patient (the stylesheets' "images with any identifiable patient
    information")? Decided from the article type and the figure legends only:
    an image article (Images in Neurology, Ophthalmic Images, ...) with figures, or a case report / case series /
    letter whose legends show clinical photographs ("Clinical photograph showing ...", "facial appearance")."""
    legends = [p.text for p in ms.paragraphs if _LEGEND.match(p.text)]
    if not legends:
        return False
    kind = article_type(ms)
    if _IMAGE_TYPE.search(kind):
        return True
    patient_article = bool(_CASE_TYPE.search(kind) or _LETTER_TYPE.search(kind)) or (not kind and _is_case_report(ms))
    return patient_article and any(_PHOTO_LEGEND.search(t) for t in legends)


def _contribution_items(paras: list[str]) -> list[str]:
    """The contribution statements after an "Author contributions" label in a title page, contribution
    form or cover letter: the label's own text and the lines after it that name what authors did. A list of
    names, a tick-box table or a form template gives nothing (nothing is invented)."""
    for i, text in enumerate(paras):
        m = _CONTRIB.match(text)
        if not m or (m.group("sep").isspace() and not m.group("author")) or len(text) > 2000:
            continue
        items = [t.strip() for t in re.split(r"\s*\n\s*", text[m.end():]) if t.strip()]
        if items and not all(_CONTRIB_WORDS.search(t) and not _TEMPLATE.search(t) for t in items):
            continue
        for nxt in paras[i + 1:i + 30]:
            lines = [t.strip() for t in re.split(r"\s*\n\s*", nxt) if t.strip()]
            if not lines or any(len(t) > 700 or _TEMPLATE.search(t) or _OTHER_LABEL.match(t) or _CONTRIB.match(t)
                                or not _CONTRIB_WORDS.search(t) for t in lines):
                break
            items += lines
        if items:
            return items
    return []


def contributions_from_sources(ms: Manuscript, skip: set[str] = frozenset()) -> list[str]:
    """Authors' contributions given with the submission: the title-page file, a contribution form, or a cover
    letter inside the manuscript (paragraphs in `skip`, the back matter, are not read)."""
    src = getattr(ms, "front_sources", None)
    pools = [list(getattr(src, "first_page", None) or [])] + [list(f) for f in getattr(src, "contribution_forms", None) or []]
    pools.append([p.text for p in ms.paragraphs if p.id not in skip and not p.in_table])
    for pool in pools:
        items = _contribution_items(pool)
        if items:
            return items
    return []


@dataclass
class _Section:
    kind: str           # consent | contributions | funding | conflict
    para: Paragraph     # its first paragraph (heading, or the statement itself)
    heading: bool       # para is a heading line (heading_tags may insert at its start)
    end: Paragraph | None = None  # its last paragraph, when known


def _sentence_label(label: str) -> str:
    label = re.sub(r"\s+", " ", label).strip()
    return label if label.isupper() else label[:1] + _sentence_case(label)[1:]


def back_matter_plan(ms: Manuscript, style: JournalStyle, run: bool = False) -> dict:
    """What the back-matter rule does to the consent and contributions sections, for the queries:
    "exists" | "added" | "missing" | None (not checked). With `run`, works it out if the rule has not run."""
    cached = getattr(ms, "_back_matter_plan", None)
    if cached is not None and cached[0] is style:
        return cached[1]
    plan = {"consent": None, "contributions": None}
    ms._back_matter_plan = (style, plan)
    if run and _on(style, "back_matter.enabled"):
        _statements(ms, style)
    return plan


def rule_back_matter(ms: Manuscript, style: JournalStyle) -> list[Change]:
    if not _on(style, "back_matter.enabled"):
        return []
    return _statements(ms, style) + _supplementary(ms, style)


def _statements(ms: Manuscript, style: JournalStyle) -> list[Change]:
    region, anchor = _back_region(ms)
    tag = "<H2>" if style.get("headings.tag") else ""
    levels = _levels(style)
    plan = back_matter_plan(ms, style)
    plan.update(consent=None, contributions=None)
    std = {}
    for sec in style.get("required_sections", []) or []:
        h = sec["heading"].lower()
        kind = "conflict" if "conflict" in h else ("funding" if "financ" in h or "fund" in h else h)
        std[kind] = sec
    out = []
    satisfied: set[str] = set()
    deleted: set[str] = set()   # paragraphs this rule deletes
    consumed: set[str] = set()  # statement paragraphs read with the label before them
    sections: list[_Section] = []
    candidates = []             # nil statements of a required section, which may become that section in place
    delete_nil = _on(style, "back_matter.delete_nil_statements")
    ack_heading = _cfg(style, "back_matter.acknowledgement_heading")
    consent_cfg = _cfg(style, "back_matter.consent") or {}
    contrib_cfg = _cfg(style, "back_matter.contributions") or {}
    consent_on = _cfg(style, "enabled", True) and consent_cfg.get("enabled", False)
    contrib_on = _cfg(style, "enabled", True) and contrib_cfg.get("enabled", False)
    nonempty = [p for p in region if p.text.strip()]
    by_text = {p.id: p.text for p in ms.paragraphs}
    ref_i = references.find_heading(ms)
    # After the reference list (legends, leftovers) a consent or contributions section counts as present, but
    # editors do not restyle it there.
    after_refs = {p.id for p in ms.paragraphs[ref_i:]} if ref_i is not None else set()

    def delete(p: Paragraph):
        out.append(_change(p, 0, len(p.text), "", Layer.STRUCTURE, "sections_back_matter"))
        deleted.add(p.id)

    def label_only(p: Paragraph) -> bool:
        return bool(p.text.strip()) and _words(p.text) <= 8 and not p.text.rstrip().endswith(".")

    def tag_heading(p: Paragraph, m, name: str, rename: str | None = None):
        """Tag a heading line the journal's heading list does not know, drop its colon, sentence case."""
        s, e = m.start("label"), m.end("label")
        label = p.text[s:e]
        known = levels.get(_key(p.text)) or p.text.lstrip().startswith("<H")
        new = rename or _sentence_label(label)
        if not known and tag:
            new = tag + new
        if new != label:
            out.append(_change(p, s, e, new, Layer.STRUCTURE, f"sections_{name}"))
        end = len(p.text.rstrip())
        if not known and p.text[e:end].strip() in (":", ".", "-", "–", "—"):
            out.append(_change(p, e, end, "", Layer.STRUCTURE, f"sections_{name}"))

    def split_inline(p: Paragraph, m, name: str, rename: str | None = None):
        """"Patient Consent: Written informed ..." -> heading paragraph + statement."""
        s = len(p.text) - len(p.text.lstrip())
        out.append(_change(p, s, m.end(), f"{tag}{rename or _sentence_label(m.group('label'))}{BR}", Layer.STRUCTURE,
                           f"sections_{name}"))

    for i, p in enumerate(nonempty):
        if p.id in deleted or p.id in consumed:
            continue
        nxt = nonempty[i + 1] if i + 1 < len(nonempty) else None
        # "Declarations" container heading, nil ethics statements
        if _on(style, "back_matter.delete_declarations_heading") and _DECLARATIONS.match(p.text):
            delete(p)
            continue
        em = _ETHICS.match(p.text)
        if em and _on(style, "back_matter.delete_nil_ethics"):
            # Inline only: a heading with "Not applicable" on its own line is kept (ABR editors tag it).
            content = p.text[em.end():]
            if content.strip() and em.group("sep").strip() and _ETHICS_NIL.match(content) and _words(content) <= 25:
                delete(p)
            continue
        # consent
        cm = _CONSENT_HEAD.match(p.text)
        if cm and consent_on:
            content = p.text[cm.end():]
            sections.append(_Section("consent", p, not content.strip()))
            if p.id in after_refs:
                continue
            rename = consent_cfg.get("heading") if consent_cfg.get("rename_heading") and re.search(
                r"patient|declaration", cm.group("label"), re.I) else None
            if content.strip() and cm.group("sep").strip() and _words(content) > 3:
                split_inline(p, cm, "consent", rename)
            elif not content.strip():
                tag_heading(p, cm, "consent", rename)
                if nxt is not None:
                    consumed.add(nxt.id)
            continue
        if consent_on and consent_cfg.get("head_unlabelled", True) and _CONSENT_GIVEN.search(p.text) \
                and _CONSENT_PUBLICATION.search(p.text) and _words(p.text) <= 90 \
                and not any(s.kind == "consent" for s in sections):
            prev = nonempty[i - 1] if i else None
            if p.id in after_refs:
                sections.append(_Section("consent", p, False))
            elif prev is None or prev.id in deleted or not label_only(prev):
                sections.append(_Section("consent", p, False))
                out.append(_change(p, 0, 0, f"{tag}{consent_cfg.get('heading', 'Declaration of patient consent')}{BR}",
                                   Layer.STRUCTURE, "sections_consent", {"bold": False, "italic": False}))
            continue
        # authors' contributions
        km = _CONTRIB.match(p.text)
        if km and contrib_on and not km.group("author") and not km.group("label").lower().startswith("credit"):
            km = None  # "Contribution: This author helped ..." under each author's name is not the section's label
        if km and contrib_on:
            content = p.text[km.end():]
            sections.append(_Section("contributions", p, not content.strip()))
            if p.id in after_refs:
                continue
            if content.strip() and km.group("sep").strip():
                if contrib_cfg.get("split_inline", True):
                    split_inline(p, km, "contributions")
            elif not content.strip():
                tag_heading(p, km, "contributions")
            continue
        if contrib_on and contrib_cfg.get("head_unlabelled", True) and _CONTRIB_LINE.match(p.text) \
                and len(_CONTRIB_WORDS.findall(p.text)) >= 2 and not _OTHER_LABEL.match(p.text) \
                and not any(s.kind == "contributions" for s in sections):
            prev = nonempty[i - 1] if i else None
            if p.id in after_refs:
                sections.append(_Section("contributions", p, False))
            elif prev is None or prev.id in deleted or not label_only(prev) or _LABEL.match(prev.text):
                sections.append(_Section("contributions", p, False))
                out.append(_change(p, 0, 0, f"{tag}{contrib_cfg.get('heading', 'Author contributions')}{BR}",
                                   Layer.STRUCTURE, "sections_contributions", {"bold": False, "italic": False}))
            continue
        # funding / conflicts / acknowledgement
        m = _LABEL.match(p.text)
        if not m or (m.group("sep") and m.group("sep").isspace() and _words(p.text[m.end():]) > 30):
            if delete_nil and _NIL_SENTENCE.match(p.text) and _NIL_TOPIC.search(p.text) and _words(p.text) <= 35 \
                    and _nil(p.text):
                prev = nonempty[i - 1] if i else None
                if prev is None or prev.id in deleted or not label_only(prev):
                    delete(p)
            continue
        kind = _kind(m)
        if kind == "funding" and m.group("label").lower() == "support" and not m.group("sep").strip():
            continue  # a sentence starting "Support ..."
        content = p.text[m.end():]
        content_p = None
        if not content.strip():
            if nxt is not None and not _LABEL.match(nxt.text):
                content_p, content = nxt, nxt.text
                consumed.add(nxt.id)
        nil = _nil(content)
        kinds = ("conflict", "funding") if kind == "both" else (kind,)
        if kind == "ack":
            if nil and delete_nil and (content.strip() or content_p is None and i + 1 < len(nonempty)):
                delete(p)
                if content_p is not None:
                    delete(content_p)
            elif ack_heading and not p.text[m.end():].strip():
                s, e = m.start("ack"), m.end("ack")
                if p.text[s:e] != ack_heading:
                    out.append(_change(p, s, e, ack_heading, Layer.STRUCTURE, "sections_back_matter"))
            continue
        standard = kind in std and m.group("label").lower() == std[kind]["heading"].lower()
        if nil and content_p is not None and standard:
            # The standard heading with a "None" under it: keep both, word the statement as the journal does.
            satisfied.add(kind)
            sections.append(_Section(kind, p, True))
            body = content_p.text.strip()
            default = std[kind].get("default", "")
            short = body.rstrip(".").lower() in ("none", "nil", "not applicable", "na", "n/a")
            if body and body.rstrip(".").lower() != default.rstrip(".").lower() and (short or _nil(body)):
                # "None" -> "Nil" / the journal's wording; "The authors declare no conflict of interest" -> the same
                s = content_p.text.index(body)
                new = ("Nil" + body[len(body.rstrip(".")):] if short else "Nil.") if kind == "funding" else default
                out.append(_change(content_p, s, s + len(body), new, Layer.STRUCTURE, "sections_back_matter"))
            continue
        if nil and delete_nil:
            delete(p)
            if content_p is not None:
                delete(content_p)
            if kind in std and p.id not in after_refs and (m.group("sep").strip() or content_p is not None):
                candidates.append((kind, p, m, content_p))
            continue
        if kind == "both" or not all(k in std for k in kinds):
            satisfied.update(kinds)  # a real joint statement: leave it to the editor
            continue
        sec = std[kind]
        satisfied.add(kind)
        sections.append(_Section(kind, p, content_p is not None or not content.strip()))
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

    # A nil statement becomes the journal's section in place ("Funding: None" -> "Financial support and sponsorship
    # / Nil.") when that keeps the journal's order; the rest stay deleted and the sections are added below.
    order = list(_cfg(style, "back_matter.order") or ["consent", "contributions", "funding", "conflict"])
    rank = {k: i for i, k in enumerate(order)}
    pos = {p.id: i for i, p in enumerate(ms.paragraphs)}
    done: set[str] = set(satisfied)
    for kind, p, m, content_p in candidates:
        if kind in done or kind not in std or kind not in rank:
            continue
        here = [x for x in sections if x.para.id not in deleted]
        if any(rank.get(k, -1) < rank[kind] and k not in done for k in std) \
                or any(rank.get(x.kind, -1) > rank[kind] and pos[x.para.id] < pos[p.id] for x in here) \
                or any(rank.get(x.kind, -1) < rank[kind] and pos[x.para.id] > pos[p.id] for x in here):
            continue
        sec = std[kind]
        out[:] = [c for c in out if not (c.para_id in (p.id, getattr(content_p, "id", None))
                                          and c.start == 0 and c.end == len(by_text[c.para_id]) and not c.new)]
        deleted.discard(p.id)
        s0, e0 = m.start("label"), m.end("label")
        lead = len(p.text) - len(p.text.lstrip())
        out.append(_change(p, lead, e0, f"{tag}{sec['heading']}", Layer.STRUCTURE, "sections_back_matter"))
        if content_p is None:
            out.append(_change(p, e0, len(p.text), BR + sec.get("default", ""), Layer.STRUCTURE, "sections_back_matter"))
        else:
            if e0 < len(p.text):
                out.append(_change(p, e0, len(p.text), "", Layer.STRUCTURE, "sections_back_matter"))
            deleted.discard(content_p.id)
            out.append(_change(content_p, 0, len(content_p.text), sec.get("default", ""), Layer.STRUCTURE,
                               "sections_back_matter"))
        done.add(kind)
        satisfied.add(kind)
        sections.append(_Section(kind, p, True, content_p or p))

    # --- sections to add: the standard statements, patient consent, authors' contributions ---
    new: dict[str, list[str]] = {}
    for kind, sec in std.items():
        if kind not in satisfied:
            new[kind] = [f"{tag}{sec['heading']}", sec.get("default", "")]
    have = {s.kind for s in sections}
    if consent_on:
        if "consent" in have or has_consent_statement(ms):
            plan["consent"] = "exists"
        elif consent_cfg.get("add_missing", True) and identifiable_patient_images(ms):
            new["consent"] = [f"{tag}{consent_cfg.get('heading', 'Declaration of patient consent')}",
                              consent_cfg.get("text", "")]
            plan["consent"] = "added"
        else:
            plan["consent"] = "missing"
    if contrib_on:
        if "contributions" in have:
            plan["contributions"] = "exists"
        else:
            items = contributions_from_sources(ms, {p.id for p in region}) if contrib_cfg.get("from_sources") else []
            if items:
                new["contributions"] = [f"{tag}{contrib_cfg.get('heading', 'Author contributions')}"] + items
                plan["contributions"] = "added"
            else:
                plan["contributions"] = "missing"
    if not _on(style, "back_matter.add_missing"):
        new = {k: v for k, v in new.items() if k in ("consent", "contributions")}
    if anchor is None:
        # No reference list found: add the sections after the last statement read here.
        seen = [p for p in nonempty if p.id in deleted or p.id in consumed or any(x.para is p for x in sections)]
        anchor = seen[-1] if seen else None
    if not new or anchor is None:
        return out
    # Each new section goes before the first section already there that comes after it in the journal's order
    # (consent, contributions, financial support, conflicts of interest), else with the others before the references.
    # A new section goes after the one before it in that order when that one is there (and nothing that comes
    # after it in the order stands between), else before the first one after it, else before the references.
    kept = [s for s in sections if s.para.id not in deleted]
    starts = {s.para.id for s in sections}
    at = {p.id: i for i, p in enumerate(nonempty)}

    def section_end(x: _Section) -> Paragraph:
        if x.end is not None:
            return x.end
        last = x.para
        for q in nonempty[at[x.para.id] + 1:at[x.para.id] + 31]:
            if q.id in starts or q.id in deleted or q.in_table or _CAPTION.match(q.text) or (q.id in after_refs) != (
                    x.para.id in after_refs) or _key(q.text) in ("references", "reference", "bibliography") or any(
                    r.match(q.text) for r in (_LABEL, _CONSENT_HEAD, _CONTRIB, _ETHICS, _DECLARATIONS)) \
                    or (x.kind != "contributions" and label_only(q)):
                break
            last = q
        return last

    targets: dict[tuple[str, str], list[str]] = {}
    for kind in sorted(new, key=lambda k: rank.get(k, len(order))):
        r = rank.get(kind, len(order))
        later = [s for s in kept if rank.get(s.kind, -1) > r]
        before = min(later, key=lambda s: pos[s.para.id]) if later else None
        earlier = [s for s in kept if rank.get(s.kind, len(order)) < r and s.para.id not in after_refs]
        prev_end = max((section_end(s) for s in earlier), key=lambda q: pos[q.id]) if earlier else None
        if prev_end is not None and pos[prev_end.id] < pos[anchor.id] and all(
                q.id in deleted for q in nonempty if pos[prev_end.id] < pos[q.id] <= pos[anchor.id]):
            prev_end = anchor  # only deleted statements between: with the others, after them
        if prev_end is not None and (before is None or pos[prev_end.id] < pos[before.para.id]):
            targets.setdefault(("after", prev_end.id), []).extend(new[kind])
        elif before is None or pos[before.para.id] > pos[anchor.id]:
            targets.setdefault(("after", anchor.id), []).extend(new[kind])
        elif not before.heading:
            targets.setdefault(("before", before.para.id), []).extend(new[kind])
        else:
            # Before a heading line (heading_tags inserts its tag there): after the paragraph above it.
            j = ms.paragraphs.index(before.para)
            above = next((q for q in reversed(ms.paragraphs[:j]) if q.text.strip() and not q.in_table), None)
            if above is None:
                continue
            targets.setdefault(("after", above.id), []).extend(new[kind])
    by_id = {p.id: p for p in ms.paragraphs}
    fmt = {"bold": False, "italic": False, "superscript": False}
    consent_text = (new.get("consent") or [None, None])[1] if plan.get("consent") == "added" else None
    for (where, pid), lines in targets.items():
        p = by_id[pid]
        # The standard consent statement asserts that consent was obtained: its own reason, so that autopilot can hold it.
        reason = "sections_back_matter.consent_added" if consent_text and consent_text in lines else "sections_back_matter"
        if where == "after":
            out.append(_change(p, len(p.text), len(p.text), BR + BR.join(lines), Layer.STRUCTURE, reason, fmt))
        else:
            # Merge with a heading this rule adds at the same point ("before" an unlabelled statement).
            mine = next((c for c in out if c.para_id == pid and c.start == c.end == 0), None)
            if mine is not None:
                out.remove(mine)
                out.append(_change(p, 0, 0, BR.join(lines) + BR + mine.new, Layer.STRUCTURE, reason, mine.format))
            else:
                out.append(_change(p, 0, 0, BR.join(lines) + BR, Layer.STRUCTURE, reason, fmt))
    return out


def _supplementary(ms: Manuscript, style: JournalStyle) -> list[Change]:
    """The stylesheets' "Supplementary Material" heading over supporting information (sections.back_matter.
    supplementary_heading). Off by default: no editor added it in the training files (0 of 6 articles)."""
    heading = _cfg(style, "back_matter.supplementary_heading")
    if not heading or not _on(style, "back_matter.enabled"):
        return []
    tag = "<H2>" if style.get("headings.tag") else ""
    region, _ = _back_region(ms)
    head = re.compile(r"^\s*(?:<H\d>)?\s*(?P<label>(?:supporting|supplementary|supplemental)\s+(?:information|materials?|data"
                      r"|files?|digital\s+content))\s*:?\s*$", re.I)
    for p in region:
        m = head.match(p.text)
        if m:
            if m.group("label") != heading:
                return [_change(p, m.start("label"), m.end("label"), heading, Layer.STRUCTURE, "sections_back_matter")]
            return []
    first = next((p for p in region if re.match(r"^\s*(?:supplementary|supplemental)\s+(?:table|figure|fig\.?|file|material"
                                                r"|video|appendix)\s*S?\d", p.text, re.I)), None)
    if first is None:
        return []
    return [_change(first, 0, 0, f"{tag}{heading}{BR}", Layer.STRUCTURE, "sections_back_matter",
                    {"bold": False, "italic": False})]


# --- author queries ------------------------------------------------------------------------

TEXT = {
    "abstract.citations": "Reference citations should not be present in the abstract. Kindly check.",
    "abstract.too_long": "As per journal style, kindly reduce the abstract to {n} words.",
    "abstract.structured_case_report": "Please note that in the Case Report there shouldn’t be any structured "
                                       "abstract, hence will have to remove the headings.",
    "keywords.too_many": "Kindly provide a maximum of {n} key words.",
    "sections.patient_consent": "Kindly confirm whether consent form obtained or not?",
    "sections.consent_added": "Please ensure that patient consent form has been obtained.",
    "sections.contributions_missing": "Please provide authors’ contributions.",
}


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
    if _cfg(style, "back_matter.enabled", True):
        ref_i = references.find_heading(ms)
        at = ms.paragraphs[ref_i].id if ref_i is not None else None
        plan = back_matter_plan(ms, style, run=True)
        consent_cfg = _cfg(style, "back_matter.consent") or {}
        if plan.get("consent") == "added" and consent_cfg.get("query", True) and at:
            qs.add(at, "sections.consent_added")
        elif _on(style, "back_matter.patient_consent_query") and _is_case_report(ms) \
                and not has_consent_statement(ms) and at:
            qs.add(at, "sections.patient_consent")
        if plan.get("contributions") == "missing" and (_cfg(style, "back_matter.contributions") or {}).get(
                "query_missing") and at:
            qs.add(at, "sections.contributions_missing")
