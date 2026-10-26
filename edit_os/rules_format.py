"""Formatting rules from the pre-editing guide: italic et al., Latin terms, P values and n,
superscript bibliography citations (`rule_formatting`); and the clean-up of the author's run
formatting the style editors make (`rule_cleanup`): font colours out, column heads, labels and
section headings bold, no underlined headings, no stray bold in running text.

Each proposal is a `Change` with `format` set: formatting-only when old == new (rendered as
Word tracked formatting), or a text change whose inserted text gets the format (p -> P).
Reference entries are never touched, nor URLs and e-mail addresses.
"""

from __future__ import annotations

import re
from collections import defaultdict

from . import references
from .changes import Change, Layer
from .docmodel import Manuscript, Paragraph, run_text, text_runs
from .docmodel import _RPR as RPR
from .style import JournalStyle
from docx.oxml.ns import qn

_URL = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+\.\w+|\bdoi:\s*\S+", re.I)
_ET_AL = re.compile(r"\bet al\b(\.?)")
_LATIN = re.compile(r"\b(?:[Ii]n[ -](?:vivo|vitro|situ)|[Ee]x[ -]vivo)\b")
# p / P as a probability: "p < 0.05", "(p=.03)", "p value", "p-values". Not "p." / "pp." (pages),
# not part of a word or label ("p53", "Hb-p", "5p"), not a subscript-style "p_" form.
_P_VALUE = re.compile(
    r"(?<![\w.\-/'’])[pP](?=\s*(?:[<>=≤≥]\s*(?:[-−]?\s*(?:0?[.·]\d|\d)|NS\b)|[- ]values?\b))")
# n / N as a sample-size variable: "n = 34", "(n=12)".
_N_VAR = re.compile(r"(?<![\w.\-/'’])[nN](?=\s*=\s*\d)")
_CITATION = re.compile(r"\[(\d{1,3}(?:\s*[,–—\-]\s*\d{1,3})*)\]")

_TOGGLE_TAGS = {"italic": qn("w:i"), "bold": qn("w:b"), "small_caps": qn("w:smallCaps")}


def char_formats(p: Paragraph) -> list[dict[str, bool]]:
    """Direct run formatting of each character of `p.text` (italic, bold, superscript, ...)."""
    out = []
    for r in text_runs(p.element):
        rpr = r.find(RPR)
        f = {}
        if rpr is not None:
            for prop, tag in _TOGGLE_TAGS.items():
                el = rpr.find(tag)
                f[prop] = el is not None and el.get(qn("w:val"), "true") not in ("0", "false", "off")
            va = rpr.find(qn("w:vertAlign"))
            val = va.get(qn("w:val")) if va is not None else None
            f["superscript"], f["subscript"] = val == "superscript", val == "subscript"
        out += [f] * len(run_text(r))
    return out


def _has(fmts: list[dict], start: int, end: int, prop: str) -> bool:
    return all(f.get(prop) for f in fmts[start:end])


# A reference entry outside the detected list: "2019;12:101", or an author list "Lin YJ, Wang Q,".
_REFERENCE_LIKE = re.compile(r"\b(?:19|20)\d{2}\s*;\s*\d+\s*(?:\([^)]*\))?\s*:|"
                             r"(?:\b[A-Z][\w'’\-]+ [A-Z]{1,3}[,.]\s*){2,}")


def _paragraphs(ms: Manuscript) -> list[Paragraph]:
    """Every paragraph except the reference heading and entries (body, tables, legends)."""
    skip = {r.para_id for r in references.extract(ms)}
    head = references.find_heading(ms)
    if head is not None:
        skip.add(ms.paragraphs[head].id)
    return [p for p in ms.paragraphs
            if p.id not in skip and p.text.strip() and not _REFERENCE_LIKE.search(p.text)]


def _blocked(p: Paragraph) -> list[tuple[int, int]]:
    return [m.span() for m in _URL.finditer(p.text)]


def _free(blocked, start: int, end: int) -> bool:
    return not any(s < end and start < e for s, e in blocked)


def _fmt(p: Paragraph, start: int, end: int, new: str, fmt: dict, layer: Layer, rule: str) -> Change:
    from .rules import _change  # rules registers this module; import late to avoid a cycle

    c = _change(p, start, end, new, layer, rule)
    c.format = dict(fmt)
    return c


def _on(style: JournalStyle, key: str) -> bool:
    return bool(style.get("formatting.enabled", True) and style.get(f"formatting.{key}", False))


def rule_formatting(ms: Manuscript, style: JournalStyle) -> list[Change]:
    out: list[Change] = []
    n_refs = 0
    if _on(style, "superscript_citations"):
        refs = references.extract(ms)
        n_refs = max((r.number or 0 for r in refs), default=len(refs)) or len(refs)

    for p in _paragraphs(ms):
        text, blocked = p.text, _blocked(p)
        fmts = char_formats(p)
        italic = {"italic": True}

        if _on(style, "et_al_italic"):
            for m in _ET_AL.finditer(text):
                if not _free(blocked, m.start(), m.end()):
                    continue
                if not m.group(1) and _on(style, "et_al_period"):
                    out.append(_fmt(p, m.start(), m.end(), "et al.", italic, Layer.HOUSE_STYLE, "fmt_et_al"))
                elif not _has(fmts, m.start(), m.end(), "italic"):
                    out.append(_fmt(p, m.start(), m.end(), m.group(0), italic, Layer.HOUSE_STYLE, "fmt_et_al"))

        if _on(style, "latin_italic"):
            for m in _LATIN.finditer(text):
                if _free(blocked, m.start(), m.end()) and not _has(fmts, m.start(), m.end(), "italic"):
                    out.append(_fmt(p, m.start(), m.end(), m.group(0), italic, Layer.HOUSE_STYLE, "fmt_latin"))

        if _on(style, "p_values"):
            for m in _P_VALUE.finditer(text):
                s, e = m.span()
                if not _free(blocked, s, e) or fmts[s:e] and fmts[s].get("subscript"):
                    continue
                if m.group(0) == "p" and style.get("formatting.p_capital", False):
                    out.append(_fmt(p, s, e, "P", italic, Layer.NUMBERS, "fmt_p_value"))
                elif not _has(fmts, s, e, "italic"):
                    out.append(_fmt(p, s, e, m.group(0), italic, Layer.NUMBERS, "fmt_p_value"))

        if _on(style, "n_variable_italic"):
            for m in _N_VAR.finditer(text):
                s, e = m.span()
                if _free(blocked, s, e) and not _has(fmts, s, e, "italic"):
                    out.append(_fmt(p, s, e, m.group(0), italic, Layer.NUMBERS, "fmt_n"))

        if n_refs:
            for m in _CITATION.finditer(text):
                s, e = m.span()
                if not text[:s].strip() or not _free(blocked, s, e):
                    continue  # "[1] ..." at the start of a paragraph is a list label
                if max(int(n) for n in re.findall(r"\d+", m.group(1))) > n_refs:
                    continue
                if not _has(fmts, s, e, "superscript"):
                    out.append(_fmt(p, s, e, m.group(0), {"superscript": True}, Layer.REFERENCES, "fmt_citations"))
    return out


def citation_format(style: JournalStyle) -> dict | None:
    """Format for citation brackets the citations rule inserts (None: leave as the text around)."""
    return {"superscript": True} if _on(style, "superscript_citations") else None


# --- clean-up of the author's run formatting -------------------------------------------------
#
# What the style editors do to every manuscript, measured with tools/eval_format.py against their
# tracked files: take out the author's font colours, bold the table column heads and the
# "Table 1:" / "Figure 1:" / "Keywords:" labels, bold the section headings, and take stray bold
# out of running text and table bodies. One change per contiguous span (runs that already have
# the wanted formatting are bridged), so a coloured paragraph is one suggestion, not one per run.

# Editors bold "Table 1:" / "Figure 2:" labels but leave supplementary ones as typed.
_CAPTION_LABEL = re.compile(r"^\s*((?:Table|Figure|Fig\.?|Graph|Chart|Video|Box)\s*\d+[A-Za-z]?\s*[:.|])", re.I)


def _visible(color) -> bool:
    """A font colour that shows as colour: not automatic, not black or near-black, not white."""
    if not color or color.upper() == "AUTO":
        return False
    try:
        rgb = [int(color[i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return False
    return max(rgb) > 0x40 and min(rgb) < 0xF0


class _Styles:
    """Bold / italic / colour of paragraph styles, following basedOn (run formatting a paragraph
    gets without any direct formatting)."""

    def __init__(self, ms: Manuscript):
        self.by_id = {}
        try:
            root = ms.document.styles.element
        except Exception:  # noqa: BLE001 - a package without styles.xml
            root = None
        if root is not None:
            for s in root.iter(qn("w:style")):
                self.by_id[s.get(qn("w:styleId"))] = s
        self.cache: dict = {}

    def props(self, style_id: str | None, depth: int = 0) -> dict:
        if not style_id or style_id not in self.by_id or depth > 10:
            return {}
        if style_id in self.cache:
            return self.cache[style_id]
        s = self.by_id[style_id]
        based = s.find(qn("w:basedOn"))
        out = dict(self.props(based.get(qn("w:val")), depth + 1)) if based is not None else {}
        out.update(_direct(s.find(RPR)))
        self.cache[style_id] = out
        return out


def _toggle(rpr, tag):
    el = rpr.find(qn(tag))
    if el is None:
        return None
    return el.get(qn("w:val"), "true").lower() not in ("0", "false", "off", "none")


def _direct(rpr) -> dict:
    """Bold, italic, underline and colour set directly on a <w:rPr> (absent keys: not set)."""
    out = {}
    if rpr is None:
        return out
    for prop, tag in (("bold", "w:b"), ("italic", "w:i"), ("underline", "w:u")):
        v = _toggle(rpr, tag)
        if v is not None:
            out[prop] = v
    c = rpr.find(qn("w:color"))
    if c is not None:
        out["color"] = c.get(qn("w:val"))
    return out


def run_props(p: Paragraph, styles: _Styles) -> list[dict]:
    """Effective bold / italic / underline / colour of each character of `p.text` (paragraph style,
    character style, then direct formatting), with `style_color`: the paragraph style's colour."""
    ppr_style = p.element.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
    base = styles.props(ppr_style.get(qn("w:val")) if ppr_style is not None else None)
    out = []
    for r in text_runs(p.element):
        rpr = r.find(RPR)
        f = dict(base)
        if rpr is not None:
            rs = rpr.find(qn("w:rStyle"))
            if rs is not None:
                f.update(styles.props(rs.get(qn("w:val"))))
        f.update(_direct(rpr))
        f["style_color"] = base.get("color")
        out += [f] * len(run_text(r))
    return out


_BRIDGE = 3  # at most this many letters already in the wanted state are bridged


def _regions(text: str, need, done) -> list[tuple[int, int]]:
    """Maximal spans of characters that need the change. A gap between two such spans is bridged
    when its characters are already in the wanted state and it is only white space and punctuation
    or at most a short word, so one formatted phrase is one change, not one per run."""
    out: list[list[int]] = []
    gap_ok, gap_letters = True, 0
    for i, ch in enumerate(text):
        if need(i):
            if out and gap_ok and gap_letters <= _BRIDGE:
                out[-1][1] = i + 1
            else:
                out.append([i, i + 1])
            gap_ok, gap_letters = True, 0
        elif ch.isspace():
            continue
        elif done(i):
            gap_letters += ch.isalnum()
        else:
            gap_ok = False
    return [(s, e) for s, e in out]


def _header_rows(paras: list[dict]) -> dict:
    """{table id: header row indexes}: rows marked as repeating header rows, else the first row,
    or the second when the first holds only the table's caption."""
    rows: dict = defaultdict(lambda: defaultdict(list))
    for d in paras:
        if d["cell"]:
            rows[d["cell"]["table"]][d["cell"]["row"]].append(d)
    out = {}
    for t, by_row in rows.items():
        if len(by_row) < 2 or max(len({d["cell"]["col"] for d in ds}) for ds in by_row.values()) < 2:
            continue  # a one-row or one-column table is a box, not a data table
        marked = {i for i, ds in by_row.items()
                  if any(_tbl_header(d["el"]) for d in ds)}
        if marked:
            out[t] = marked
            continue
        order = sorted(by_row)
        cells = by_row[order[0]]
        text = " ".join(d["text"] for d in cells).strip()
        title = len({d["cell"]["col"] for d in cells}) == 1 and (_CAPTION_LABEL.match(text) or not text)
        head = order[1] if title else order[0]
        out[t] = {head}
        # A head spanning columns ("LBN group" over "Number | %") has sub-heads in the next row.
        nxt = order.index(head) + 1
        if nxt < len(order) - 1 and any(_spans_columns(d["el"]) for d in by_row[head]) and \
                all(sum(ch.isdigit() for ch in d["text"]) <= 1 for d in by_row[order[nxt]]):
            out[t].add(order[nxt])  # (data rows hold numbers; a sub-head row hardly any)
    return out


def _spans_columns(p_el) -> bool:
    tc = next((a for a in p_el.iterancestors() if a.tag == qn("w:tc")), None)
    span = tc.find(f"{qn('w:tcPr')}/{qn('w:gridSpan')}") if tc is not None else None
    return span is not None and (span.get(qn("w:val")) or "1").isdigit() and int(span.get(qn("w:val"))) > 1


def _tbl_header(p_el) -> bool:
    tr = next((a for a in p_el.iterancestors() if a.tag == qn("w:tr")), None)
    return tr is not None and tr.find(f"{qn('w:trPr')}/{qn('w:tblHeader')}") is not None


def _label_span(text: str, labels: list[str]) -> tuple[int, int] | None:
    m = _CAPTION_LABEL.match(text)
    if m:
        return m.span(1)
    for lab in labels:
        m = re.match(rf"^\s*({re.escape(lab)}\s*:)", text, re.I)
        if m:
            return m.span(1)
    return None


def _tagged_headings(ms: Manuscript, style: JournalStyle) -> dict[str, int]:
    """{para id: level} of the paragraphs the heading rules tag <H1> / <H2> / <H3> as a whole: a short line of its
    own (not a run-in label split off its text)."""
    from . import rules, rules_sections

    found = rules.rule_heading_tags(ms, style) + rules_sections.rule_headings(ms, style) \
        + rules_sections.rule_subheadings(ms, style)
    by_id = {p.id: p for p in ms.paragraphs}
    out: dict[str, int] = {}
    for c in found:
        p = by_id.get(c.para_id)
        m = re.match(r"<H(\d)>", c.new)
        if p is None or c.format_only or not m or p.text[:c.start].strip():
            continue
        text = p.text.strip()
        if len(text) <= 80 and not re.search(r":\s*\S", text):
            out[p.id] = int(m.group(1))
    return out


def rule_cleanup(ms: Manuscript, style: JournalStyle) -> list[Change]:
    """Clean up the author's run formatting (settings under `formatting:`, see base.yaml)."""
    from . import docview  # docview imports references only; late import keeps module load light

    if not style.get("formatting.enabled", True):
        return []
    paras = docview.classify(ms)
    by_id = {p.id: p for p in ms.paragraphs}
    for d in paras:
        d["el"] = by_id[d["id"]].element
    heads = _header_rows(paras) if _on(style, "table_head_bold") or _on(style, "table_body_unbold") else {}
    labels = style.get("formatting.bold_labels", []) or []
    styles = _Styles(ms)
    italic_levels = set(style.get("formatting.heading_italic_levels", []) or [])
    roman_levels = set(style.get("formatting.heading_roman_levels", []) or [])
    unbold_levels = set(style.get("formatting.heading_unbold_levels", []) or [])  # IJO H3 (owner, 2026-10-26)
    levels = _tagged_headings(ms, style) \
        if _on(style, "heading_bold_tagged") or italic_levels or roman_levels or unbold_levels else {}
    bold_levels = set(style.get("formatting.heading_bold_levels", [1, 2, 3]) or [])
    # IJO: "H3 | Sentence Case, Italics" is not bold
    tagged = {pid for pid, n in levels.items() if n in bold_levels} if _on(style, "heading_bold_tagged") else set()
    out: list[Change] = []

    def emit(p, spans, fmt, rule, layer=Layer.HOUSE_STYLE):
        for s, e in spans:
            out.append(_fmt(p, s, e, p.text[s:e], fmt, layer, rule))

    for d in paras:
        p = by_id[d["id"]]
        text = p.text
        if not text.strip():
            continue
        f = run_props(p, styles)
        if len(f) != len(text):
            continue
        kind, front = d["kind"], d["front"]
        blocked = _blocked(p)

        def free(i):
            return _free(blocked, i, i + 1)

        def is_(prop):
            return lambda i: bool(f[i].get(prop))

        def is_not(prop):
            return lambda i: not f[i].get(prop)

        if _on(style, "remove_font_color"):
            style_col = _visible(f[0].get("style_color")) if f else False
            emit(p, _regions(text, lambda i: free(i) and not text[i].isspace() and _visible(f[i].get("color")),
                             lambda i: not _visible(f[i].get("color"))),
                 {"color": "auto" if style_col else False}, "fmt_color")

        if kind == "reference":
            continue

        cell = d["cell"]
        head_row = cell is not None and cell["table"] in heads and cell["row"] in heads[cell["table"]]
        body_row = cell is not None and cell["table"] in heads and cell["row"] > max(heads[cell["table"]])
        label = _label_span(text, labels) if kind in ("caption", "body", "table", "list", "heading") and not front \
            else None

        if head_row and _on(style, "table_head_bold") and not _CAPTION_LABEL.match(text):
            emit(p, _regions(text, lambda i: not text[i].isspace() and not f[i].get("bold"), is_("bold")),
                 {"bold": True}, "fmt_table_head", Layer.TABLES_FIGURES)
        elif body_row and _on(style, "table_body_unbold") and not head_row:
            emit(p, _regions(text, lambda i: not text[i].isspace() and bool(f[i].get("bold")), is_not("bold")),
                 {"bold": False}, "fmt_table_body", Layer.TABLES_FIGURES)

        if label and _on(style, "label_bold"):
            s, e = label
            spans = [(a + s, b + s) for a, b in _regions(
                text[s:e], lambda i: not text[s + i].isspace() and not f[s + i].get("bold"),
                lambda i: bool(f[s + i].get("bold")))]
            # A bold label with a roman colon looks the same: leave lone punctuation alone.
            emit(p, [(a, b) for a, b in spans if any(ch.isalnum() for ch in text[a:b])], {"bold": True}, "fmt_label")

        if p.id in levels and not front and not label and levels[p.id] in italic_levels | roman_levels:
            italic = levels[p.id] in italic_levels
            emit(p, _regions(text, lambda i: not text[i].isspace() and bool(f[i].get("italic")) != italic,
                             lambda i: bool(f[i].get("italic")) == italic),
                 {"italic": italic}, "fmt_heading_italic", Layer.STRUCTURE)
        if p.id in levels and not front and not label and levels[p.id] in unbold_levels:
            emit(p, _regions(text, lambda i: not text[i].isspace() and bool(f[i].get("bold")), is_not("bold")),
                 {"bold": False}, "fmt_heading_unbold", Layer.STRUCTURE)
        if p.id in tagged and not front and not label and levels[p.id] not in unbold_levels:
            emit(p, _regions(text, lambda i: not text[i].isspace() and not f[i].get("bold"), is_("bold")),
                 {"bold": True}, "fmt_heading_bold", Layer.STRUCTURE)
        elif kind == "heading" and not front:
            # A section heading is short and stands alone ("Keywords: a; b" is a labelled paragraph).
            short = len(text.strip()) <= 80 and not text.rstrip().endswith(".") and not label and \
                not re.search(r":\s*\S", text)
            if d["level"] == 1 and short and _on(style, "heading_bold"):
                emit(p, _regions(text, lambda i: not text[i].isspace() and not f[i].get("bold"), is_("bold")),
                     {"bold": True}, "fmt_heading_bold", Layer.STRUCTURE)
            if _on(style, "heading_no_underline"):
                emit(p, _regions(text, lambda i: not text[i].isspace() and bool(f[i].get("underline")),
                                 is_not("underline")), {"underline": False}, "fmt_heading_underline",
                     Layer.STRUCTURE)

        if kind in ("body", "list") and not front and _on(style, "body_unbold"):
            spans = _regions(text, lambda i: not text[i].isspace() and bool(f[i].get("bold")), is_not("bold"))
            lead = len(text) - len(text.lstrip())
            keep = []
            for s, e in spans:
                if s <= lead or ":" in text[s:e] or text[e:].lstrip()[:1] == ":":
                    continue  # a run-in heading, list term or label ("Methods:") stays bold
                if not any(ch.isalnum() for ch in text[s:e]):
                    continue  # bold punctuation shows nothing
                keep.append((s, e))
            emit(p, keep, {"bold": False}, "fmt_body_bold")
    return out


# --- font family, font size and alignment ----------------------------------------------------
#
# Pre-editing guide: paragraphs, author group, affiliations, correspondence, abstract text, the running head,
# tables and the figure / graph labels "12 points font size" (G16, G41, G65, G86, G111, G142, G193, G260, G272);
# the manuscript type, the title, the abstract heading, first-level headings and the Acknowledgement heading
# "14 points" (G32, G37, G110, G133, G146); "left aligned" (G32-G142), the running head "center aligned" (G16).
# The guide names no font family: `formatting.fonts.family` is the journal's (what its editors set).
# Settings: `formatting.fonts` (base.yaml). Runs in a symbol font (Symbol, Wingdings, ...) keep their font.

_SYMBOL_FONT = re.compile(r"symbol|wingdings|webdings|dingbat|mt extra|math", re.I)
_ABSTRACT_HEAD = re.compile(r"^\s*(?:<H\d>)?\s*abstract\s*[:.]?\s*$", re.I)
_ACK_HEAD = re.compile(r"^\s*(?:<H\d>)?\s*acknowledge?ments?\s*[:.]?\s*$", re.I)
_H1_TAG = re.compile(r"^\s*<H1>")
_EQUATION = ("{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath", qn("w:object"))


def fonts_settings(style: JournalStyle) -> dict | None:
    """`formatting.fonts` when switched on (and formatting is), else None."""
    cfg = style.get("formatting.fonts") or {}
    if not style.get("formatting.enabled", True) or not cfg.get("enabled"):
        return None
    return cfg


def insert_format(style: JournalStyle, role: str | None = None) -> dict | None:
    """Run format for a paragraph the tool inserts (front-matter blocks, header lines, legends heading): the
    journal's font and size, the heading size for the article type and title. None: fonts are off."""
    cfg = fonts_settings(style)
    if cfg is None:
        return None
    big = role in ("article_type", "title", "h1")
    fmt = {"size": cfg.get("heading_size", 14) if big else cfg.get("size", 12)}
    if cfg.get("family"):
        fmt["font"] = cfg["family"]
    return fmt


def insert_align(style: JournalStyle, role: str | None = None) -> str | None:
    """Alignment of an inserted paragraph: the running head centred (G16), the rest as `fonts.align`."""
    cfg = fonts_settings(style)
    if cfg is None or not cfg.get("align"):
        return None
    return cfg.get("running_head_align", "center") if role == "running_head" else cfg["align"]


class _FontStyles(_Styles):
    """Font size (points) and font family of paragraph and character styles, with the document defaults
    and the theme fonts; a paragraph without a style takes the default paragraph style."""

    def __init__(self, ms: Manuscript):
        super().__init__(ms)
        self.theme: dict[str, str] = {}
        self.defaults: dict = {}
        self.default_para = None
        try:
            root = ms.document.styles.element
        except Exception:  # noqa: BLE001
            root = None
        try:
            for part in ms.document.part.package.iter_parts():
                if part.content_type.endswith("theme+xml"):
                    from lxml import etree
                    t = etree.fromstring(part.blob)
                    a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
                    for kind in ("major", "minor"):
                        el = t.find(f".//{a}{kind}Font/{a}latin")
                        if el is not None and el.get("typeface"):
                            self.theme[kind] = el.get("typeface")
                    break
        except Exception:  # noqa: BLE001 - no theme: theme fonts stay unresolved
            pass
        if root is not None:
            d = root.find(f"{qn('w:docDefaults')}/{qn('w:rPrDefault')}/{qn('w:rPr')}")
            self.defaults = self.font_direct(d)
            for s in root.iter(qn("w:style")):
                if s.get(qn("w:type")) == "paragraph" and s.get(qn("w:default")) in ("1", "true", "on"):
                    self.default_para = s.get(qn("w:styleId"))
        self.fcache: dict = {}

    def font_direct(self, rpr) -> dict:
        out = {}
        if rpr is None:
            return out
        sz = rpr.find(qn("w:sz"))
        if sz is not None and (sz.get(qn("w:val")) or "").isdigit():
            out["size"] = int(sz.get(qn("w:val"))) / 2
        f = rpr.find(qn("w:rFonts"))
        if f is not None:
            name = f.get(qn("w:ascii")) or f.get(qn("w:hAnsi"))
            theme = f.get(qn("w:asciiTheme")) or f.get(qn("w:hAnsiTheme"))
            if theme:  # a theme font wins over the named one
                name = self.theme.get("major" if theme.startswith("major") else "minor", name)
            if name:
                out["font"] = name
        return out

    def font_props(self, style_id: str | None, depth: int = 0) -> dict:
        if not style_id or style_id not in self.by_id or depth > 10:
            return {}
        if style_id in self.fcache:
            return self.fcache[style_id]
        s = self.by_id[style_id]
        based = s.find(qn("w:basedOn"))
        out = dict(self.font_props(based.get(qn("w:val")), depth + 1)) if based is not None else {}
        out.update(self.font_direct(s.find(RPR)))
        self.fcache[style_id] = out
        return out


def font_chars(p: Paragraph, styles: _FontStyles) -> list[dict]:
    """Effective font size (points) and family of each character of `p.text`."""
    ppr_style = p.element.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
    sid = ppr_style.get(qn("w:val")) if ppr_style is not None else styles.default_para
    base = {"size": 10.0, "font": "Times New Roman"}  # Word's own defaults when nothing sets them
    base.update(styles.defaults)
    base.update(styles.font_props(sid))
    out = []
    for r in text_runs(p.element):
        rpr = r.find(RPR)
        f = dict(base)
        if rpr is not None:
            rs = rpr.find(qn("w:rStyle"))
            if rs is not None:
                f.update(styles.font_props(rs.get(qn("w:val"))))
            f.update(styles.font_direct(rpr))
        out += [f] * len(run_text(r))
    return out


def paragraph_alignment(p: Paragraph, styles: _FontStyles) -> str:
    """Effective alignment of a paragraph: its own <w:jc>, else its style's (basedOn), else left."""
    _JC = {"start": "left", "end": "right", "distribute": "both"}
    jc = p.element.find(f"{qn('w:pPr')}/{qn('w:jc')}")
    if jc is not None:
        v = jc.get(qn("w:val"))
        return _JC.get(v, v)
    ps = p.element.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
    sid = ps.get(qn("w:val")) if ps is not None else styles.default_para
    for _ in range(10):
        if not sid or sid not in styles.by_id:
            break
        s = styles.by_id[sid]
        jc = s.find(f"{qn('w:pPr')}/{qn('w:jc')}")
        if jc is not None:
            v = jc.get(qn("w:val"))
            return _JC.get(v, v)
        b = s.find(qn("w:basedOn"))
        sid = b.get(qn("w:val")) if b is not None else None
    return "left"


def heading_levels(ms: Manuscript, style: JournalStyle) -> dict[str, int]:
    """{para id: level} of the paragraphs that are section headings of their own: tagged <H1> / <H2> / <H3> by
    the heading rules (or already tagged), else the first-level headings docview finds."""
    from . import docview

    out = dict(_tagged_headings(ms, style))
    for d in docview.classify(ms):
        if d["id"] in out or d["kind"] != "heading" or d["front"] or d["cell"]:
            continue
        text = d["text"].strip()
        m = re.match(r"<H(\d)>", text)
        if m:
            out[d["id"]] = int(m.group(1))
        elif d["level"] == 1 and len(text) <= 80 and not re.search(r":\s*\S", text):
            out[d["id"]] = 1
    return out


def _front_roles(ms: Manuscript, style: JournalStyle) -> dict[str, str]:
    from . import frontmatter

    try:
        pl = frontmatter.plan(ms, style)
    except Exception:  # noqa: BLE001 - no front-matter plan: no front-matter roles
        return {}
    return dict(pl.fields.roles) if pl.fields is not None else {}


def heading_size_text(text: str) -> bool:
    """A heading line that takes the 14-point heading size: <H1>, "Abstract", "Acknowledgement(s)"."""
    return bool(_H1_TAG.match(text) or _ABSTRACT_HEAD.match(text) or _ACK_HEAD.match(text))


def rule_fonts(ms: Manuscript, style: JournalStyle) -> list[Change]:
    """The journal's font family and the guide's font sizes on every run, and its alignment on every paragraph
    (settings `formatting.fonts`). One change per contiguous span that needs it."""
    from . import docview

    cfg = fonts_settings(style)
    if cfg is None:
        return []
    family = cfg.get("family") or None
    size, big = cfg.get("size", 12), cfg.get("heading_size", 14)
    align = cfg.get("align") or None
    styles = _FontStyles(ms)
    levels = heading_levels(ms, style)
    roles = _front_roles(ms, style)
    by_id = {p.id: p for p in ms.paragraphs}
    out: list[Change] = []
    for d in docview.classify(ms):
        p = by_id[d["id"]]
        text = p.text
        if not text.strip():
            continue
        role = roles.get(p.id)
        rh = role == "running_title" or text.lstrip().startswith("<RH>")
        heading = levels.get(p.id) == 1 or role in ("article_type", "title") or \
            (not d["cell"] and (_ABSTRACT_HEAD.match(text) or _ACK_HEAD.match(text)))
        want = big if heading and not rh else size
        f = font_chars(p, styles)
        if len(f) != len(text):
            continue
        keep = [bool(_SYMBOL_FONT.search(x.get("font") or "")) for x in f]

        def need(i):
            if keep[i]:
                return False
            return abs((f[i].get("size") or 0) - want) > 0.01 or bool(family and f[i].get("font") != family)

        # One change from the first character that needs it to the last, broken only at symbol-font runs (runs
        # already right in between take the same format, which changes nothing they show).
        spans: list[tuple[int, int]] = []
        open_at = last = None
        for i in range(len(text) + 1):
            if i == len(text) or keep[i]:
                if open_at is not None:
                    spans.append((open_at, last + 1))
                open_at = None
            elif need(i):
                open_at = i if open_at is None else open_at
                last = i
        fmt = {"size": want, **({"font": family} if family else {})}
        para_align = None
        if align and not d["cell"] and not d["image"] and not any(True for _ in p.element.iter(*_EQUATION)):
            target = cfg.get("running_head_align", "center") if rh else align
            if paragraph_alignment(p, styles) != target:
                para_align = target
        lead, tail = len(text) - len(text.lstrip()), len(text.rstrip())
        if para_align and len(spans) == 1 and spans[0][0] <= lead and spans[0][1] >= tail:
            out.append(_fmt(p, spans[0][0], spans[0][1], text[spans[0][0]:spans[0][1]],
                            {**fmt, "align": para_align}, Layer.HOUSE_STYLE, "fmt_font"))
            continue
        for s, e in spans:
            out.append(_fmt(p, s, e, text[s:e], fmt, Layer.HOUSE_STYLE, "fmt_font"))
        if para_align:
            out.append(_fmt(p, 0, len(text), text, {"align": para_align}, Layer.HOUSE_STYLE, "fmt_align"))
    return out


def finish_fonts(ms: Manuscript, style: JournalStyle, kept: list[Change], replaced: list[Change]) -> list[Change]:
    """After the overlap check (rules.run_all): text that a kept change replaces takes the font and size of the
    formatting change it makes moot, and the alignment stays (the paragraph does); new paragraphs a change
    opens get the journal's font and size (the heading size for <H1>, "Abstract", "Acknowledgements")."""
    from .tracked import PARAGRAPH_BREAK

    cfg = fonts_settings(style)
    if cfg is None:
        return kept
    out = []
    for c in kept:
        cover = next((t for t in replaced if c.format_only and t is not c and t.para_id == c.para_id
                      and t.start <= c.start and c.end <= t.end), None)
        if cover is None or c.reason not in ("fmt_font", "fmt_align"):
            out.append(c)
            continue
        carry = {k: v for k, v in c.format.items() if k in ("size", "font")}
        if carry:
            cover.format = {**carry, **(cover.format or {})}
        if "align" in c.format and cover.new.strip(PARAGRAPH_BREAK + " "):
            c.format = {"align": c.format["align"]}
            c.reason = "fmt_align"
            out.append(c)
    texts = {p.id: p.text for p in ms.paragraphs}
    for c in out:
        if c.format_only or PARAGRAPH_BREAK not in c.new:
            continue
        segs = c.new.split(PARAGRAPH_BREAK)
        sf = list(c.segment_format or [None])
        sf += [None] * (len(segs) - len(sf))
        # The first segment ends the paragraph the change is in: a heading split off its text ("ABSTRACT: We ..."
        # -> "Abstract" + a new paragraph) takes the heading size when nothing stands before it.
        first = 0 if heading_size_text(segs[0]) and not texts.get(c.para_id, "")[:c.start].strip() else 1
        for i in range(first, len(segs)):
            base = insert_format(style, "h1" if heading_size_text(segs[i]) else None) or {}
            if isinstance(sf[i], list):  # [length, format] pieces: the text past them takes `format`, so a last piece
                pieces = [[n, {**base, **(fm or {})}] for n, fm in sf[i]]
                rest = len(segs[i]) - sum(n for n, _ in pieces)
                sf[i] = pieces + ([[rest, dict(base)]] if rest > 0 else [])
            else:
                sf[i] = {**base, **(sf[i] or {})}
        c.segment_format = sf
    return out


# --- Symbol font -----------------------------------------------------------------------------------------
#
# Owner (2026-10-26; IJO, JEHP): Greek letters, ±, ≤, ≥ and the minus sign in the Symbol font, never ×, ®, ©, µ.
# Word shows Unicode text in a run set to "Symbol" through font substitution (≤ or a Greek letter is not in the
# Symbol font's own code page), so each character becomes Word's inserted symbol, <w:sym w:font="Symbol"
# w:char="F0B1"/>, as the IJO authors' files have it (IJO_1130: 26 of them). The text view reads <w:sym> back as
# the Unicode character (docmodel.sym_char), so QC and the other rules still see "±". A Greek mu before a letter
# ("μg", "μm") is the micro sign of a unit and is left as typed.

def symbol_settings(style: JournalStyle) -> dict | None:
    cfg = style.get("formatting.symbol_font") or {}
    return cfg if style.get("formatting.enabled", True) and cfg.get("enabled") else None


def symbol_offsets(text: str) -> list[int]:
    """Offsets of the characters that go into the Symbol font."""
    from .docmodel import SYMBOL_CODES

    return [i for i, ch in enumerate(text) if ch in SYMBOL_CODES
            and not (ch == "μ" and text[i + 1:i + 2].isalpha())]


def rule_symbols(ms: Manuscript, style: JournalStyle) -> list[Change]:
    """One formatting change {"symbol": True} per typed character (not one already inserted as a symbol)."""
    from docx.oxml.ns import qn

    if symbol_settings(style) is None:
        return []
    out: list[Change] = []
    for p in ms.paragraphs:
        if not p.text.strip():
            continue
        typed = set()
        pos = 0
        for r in text_runs(p.element):
            n = len(run_text(r))
            if r.find(qn("w:t")) is not None:
                typed.update(range(pos, pos + n))
            pos += n
        for i in symbol_offsets(p.text):
            if i in typed:
                out.append(_fmt(p, i, i + 1, p.text[i], {"symbol": True}, Layer.HOUSE_STYLE, "fmt_symbol"))
    return out


def finish_symbols(style: JournalStyle, kept: list[Change]) -> list[Change]:
    """After the overlap check: the Greek letters, ± ... that a kept change writes go in as symbols too (its
    inserted text split into [length, format] pieces)."""
    from .tracked import PARAGRAPH_BREAK

    if symbol_settings(style) is None:
        return kept
    for c in kept:
        if c.format_only or not c.new:
            continue
        segs = c.new.split(PARAGRAPH_BREAK)
        if not any(symbol_offsets(seg) for seg in segs):
            continue
        sf = list(c.segment_format or [None])
        sf += [None] * (len(segs) - len(sf))
        if any(isinstance(x, list) for x in sf):
            continue  # pieces of their own: left as typed
        for k, seg in enumerate(segs):
            at = set(symbol_offsets(seg))
            if not at:
                continue
            base = sf[k] or {}
            pieces, i = [], 0
            while i < len(seg):
                j = i + 1
                if i not in at:
                    while j < len(seg) and j not in at:
                        j += 1
                pieces.append([j - i, {**base, "symbol": True} if i in at else (dict(base) or None)])
                i = j
            sf[k] = pieces
        c.segment_format = sf
    return kept
