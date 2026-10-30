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
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

_URL = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+\.\w+|\bdoi:\s*\S+", re.I)
_ET_AL = re.compile(r"\bet al\b(\.?)")
_LATIN = re.compile(r"\b(?:[Ii]n[ -](?:vivo|vitro|situ)|[Ee]x[ -]vivo)\b")
# p / P as a probability: "p < 0.05", "(p=.03)", "p value", "p-values". Not "p." / "pp." (pages),
# not part of a word or label ("p53", "Hb-p", "5p"), not a subscript-style "p_" form.
_P_VALUE = re.compile(
    r"(?<![\w.\-/'’])[pP](?=\s*(?:[<>=≤≥]\s*(?:[-−]?\s*(?:0?[.·]\d|\d)|NS\b)|[- ][Vv]alues?\b))")  # "p Value" head (1588)
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
_CAPTION_LABEL = re.compile(r"^\s*((?:Table|Figure|Fig\.?|Graph|Chart|Video|Box)\s*(?:\d+[A-Za-z]?|[IVX]{1,5})\s*[:.|])",
                            re.I)  # "Table 1:", "Table IV." (G201: Roman numerals in labels)


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


def char_style_bold(p: Paragraph, styles: _Styles) -> list[bool]:
    """For each character of `p.text`: bold only through a character style ("Strong", "Emphasis bold"), not set on
    the run itself nor by the paragraph style. A heading bold that way loses it with the author's character style
    (and reads as not bold where the style is not carried), so the heading rule sets bold on the run."""
    ppr_style = p.element.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
    para_bold = bool(styles.props(ppr_style.get(qn("w:val")) if ppr_style is not None else None).get("bold"))
    out: list[bool] = []
    for r in text_runs(p.element):
        rpr = r.find(RPR)
        rs = rpr.find(qn("w:rStyle")) if rpr is not None else None
        direct = _direct(rpr).get("bold")
        flag = (not para_bold and direct is None and rs is not None
                and bool(styles.props(rs.get(qn("w:val"))).get("bold")))
        out += [flag] * len(run_text(r))
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
        elif _split_at(found, p) is not None:
            out[p.id] = int(m.group(1))  # a run-in label split off its text: the label is the heading
    ms._heading_ends = {pid: _split_at(found, by_id[pid]) for pid in out}
    return out


def _split_at(found: list[Change], p: Paragraph) -> int | None:
    """Where the heading rules break a run-in heading off its text ("Demographic Questionnaire: This ..." ->
    heading + paragraph): the end of the heading's words, or None when the paragraph is not split."""
    from .tracked import PARAGRAPH_BREAK

    brk = [c.start for c in found if c.para_id == p.id and c.new == PARAGRAPH_BREAK and c.reason == "subheadings"]
    if not brk:
        return None
    end = min(brk)
    while end > 0 and (p.text[end - 1].isspace() or p.text[end - 1] in ":.–—-"):
        end -= 1
    return end if 0 < end <= 120 else None


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
    # G199 / S: 'The label should be "Table<space>#:<space>" followed by caption content in bold style': the whole
    # table caption in bold, not only its label (formatting.table_caption_bold)
    if _on(style, "table_caption_bold"):
        from .rules_figures import table_caption_ids
        caption_bold = table_caption_ids(ms)
    else:
        caption_bold = set()
    # G260: 'The label should be "Figure<space>#:<space>" in 12 points font size bold style followed by caption': the
    # figure caption's text plain, not bold (the editors take the author's bold out: 6 of 6), nor italic when the author
    # set the whole caption in italics (an italic name inside it stays)
    fig_plain = {}
    if _on(style, "figure_caption_plain"):
        from . import rules_figures
        for cap in rules_figures.captions(ms):
            if cap.kind == rules_figures.FIG and not ms.by_id(cap.para_id).in_table:
                fig_plain[cap.para_id] = cap.match.start("rest")
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

        if p.id in caption_bold and not front:
            emit(p, [(a, b) for a, b in _regions(text, lambda i: free(i) and not text[i].isspace()
                                                 and not f[i].get("bold"), is_("bold"))
                     if any(ch.isalnum() for ch in text[a:b])], {"bold": True}, "fmt_table_caption", Layer.TABLES_FIGURES)
        elif label and _on(style, "label_bold"):
            s, e = label
            spans = [(a + s, b + s) for a, b in _regions(
                text[s:e], lambda i: not text[s + i].isspace() and not f[s + i].get("bold"),
                lambda i: bool(f[s + i].get("bold")))]
            # A bold label with a roman colon looks the same: leave lone punctuation alone.
            emit(p, [(a, b) for a, b in spans if any(ch.isalnum() for ch in text[a:b])], {"bold": True}, "fmt_label")

        if p.id in fig_plain and not front:
            at = fig_plain[p.id]
            spans = _regions(text[at:], lambda i: not text[at + i].isspace() and bool(f[at + i].get("bold")),
                             lambda i: not f[at + i].get("bold"))
            emit(p, [(a + at, b + at) for a, b in spans if any(ch.isalnum() for ch in text[a + at:b + at])],
                 {"bold": False}, "fmt_figure_caption", Layer.TABLES_FIGURES)
            letters = [i for i, ch in enumerate(text) if ch.isalnum()]
            if letters and all(f[i].get("italic") for i in letters):
                lead = len(text) - len(text.lstrip())
                emit(p, [(lead, len(text.rstrip()))], {"italic": False}, "fmt_figure_caption", Layer.TABLES_FIGURES)

        if p.id in levels and not front and not label and levels[p.id] in italic_levels | roman_levels:
            italic = levels[p.id] in italic_levels
            emit(p, _regions(text, lambda i: not text[i].isspace() and bool(f[i].get("italic")) != italic,
                             lambda i: bool(f[i].get("italic")) == italic),
                 {"italic": italic}, "fmt_heading_italic", Layer.STRUCTURE)
        if p.id in levels and not front and not label and levels[p.id] in unbold_levels:
            emit(p, _regions(text, lambda i: not text[i].isspace() and bool(f[i].get("bold")), is_not("bold")),
                 {"bold": False}, "fmt_heading_unbold", Layer.STRUCTURE)
        if p.id in tagged and not front and not label:
            if levels[p.id] not in unbold_levels:
                end = (getattr(ms, "_heading_ends", {}) or {}).get(p.id) or len(text)
                # bold only through a character style ("Strong") counts as not bold: the run gets its own bold
                cs = char_style_bold(p, styles)
                weak = (lambda i: not f[i].get("bold") or cs[i]) if len(cs) == len(text) else is_not("bold")
                emit(p, _regions(text[:end], lambda i: not text[i].isspace() and weak(i), lambda i: not weak(i)),
                     {"bold": True}, "fmt_heading_bold", Layer.STRUCTURE)
        elif kind == "heading" and not front:
            # A section heading is short and stands alone ("Keywords: a; b" is a labelled paragraph).
            short = len(text.strip()) <= 80 and not text.rstrip().endswith(".") and not label and \
                not re.search(r":\s*\S", text)
            if d["level"] == 1 and short and _on(style, "heading_bold") and levels.get(p.id) not in unbold_levels:
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


def paragraph_line_spacing(p: Paragraph, styles: _FontStyles) -> float | None:
    """Effective line spacing of a paragraph in lines (<w:spacing w:line> with lineRule auto, 240 = single): its
    own, else its style's (basedOn), else the document default; None for an exact or at-least height."""
    def read(ppr):
        sp = ppr.find(qn("w:spacing")) if ppr is not None else None
        if sp is None or sp.get(qn("w:line")) is None:
            return "unset"
        if sp.get(qn("w:lineRule"), "auto") != "auto":
            return None
        return int(sp.get(qn("w:line"))) / 240
    v = read(p.element.find(qn("w:pPr")))
    if v != "unset":
        return v
    ps = p.element.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
    sid = ps.get(qn("w:val")) if ps is not None else styles.default_para
    for _ in range(10):
        if not sid or sid not in styles.by_id:
            break
        s = styles.by_id[sid]
        v = read(s.find(qn("w:pPr")))
        if v != "unset":
            return v
        b = s.find(qn("w:basedOn"))
        sid = b.get(qn("w:val")) if b is not None else None
    try:
        d = styles.doc.styles.element.find(f"{qn('w:docDefaults')}/{qn('w:pPrDefault')}/{qn('w:pPr')}")
    except Exception:  # noqa: BLE001
        d = None
    v = read(d)
    return 1.0 if v == "unset" else v


def rule_line_spacing(ms: Manuscript, style: JournalStyle) -> list[Change]:
    """The whole file in the journal's line spacing (`formatting.line_spacing`; the sheet and the guide are silent:
    the style editors set it for every manuscript, double in most files: owner, 2026-10-30). Table cells too when
    `tables` (JEHP: the editors select the whole file; IJO: the cells keep the author's)."""
    cfg = style.get("formatting.line_spacing", {}) or {}
    value = cfg.get("value")
    if not value:
        return []
    styles = _FontStyles(ms)
    styles.doc = ms.document
    out = []
    for p in ms.paragraphs:
        text = p.text
        if not text.strip() or (p.in_table and not cfg.get("tables", True)):
            continue
        have = paragraph_line_spacing(p, styles)
        if have is not None and abs(have - float(value)) < 0.01:
            continue
        out.append(_fmt(p, 0, len(text), text, {"line_spacing": float(value)}, Layer.HOUSE_STYLE, "fmt_line_spacing"))
    return out


def space_new_paragraphs(ms: Manuscript, style: JournalStyle, changes, tracked: bool) -> None:
    """At render: the journal's line spacing (`formatting.line_spacing`) on the paragraphs the tool writes itself (the
    header, the front matter it rewrites, the First author block, legends and captions it moves, rows it adds). Those
    are insertions, so they are written in it directly, without a change of their own. In the tracked copy only
    those (the author's paragraphs carry their fmt_line_spacing change); in the clean copy every paragraph, once the
    line-spacing changes are all accepted. Nothing when the editor rejected one (they keep the author's spacing)."""
    from .changes import Status
    from .tracked import _ppr_insert, _ppr_of

    cfg = style.get("formatting.line_spacing", {}) or {}
    value = cfg.get("value")
    if not value:
        return
    ls = [c for c in changes.changes if c.reason == "fmt_line_spacing"]
    if any(c.status == Status.REJECTED for c in ls):
        return
    if not tracked and any(c.status != Status.ACCEPTED for c in ls):
        return
    want = str(round(float(value) * 240))
    tables = cfg.get("tables", True)
    W_ = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def inserted(p) -> bool:
        if p.find(f"{W_}pPr/{W_}rPr/{W_}ins") is not None:
            return True
        texts = list(p.iter(f"{W_}t"))
        if texts and all(any(a.tag in (f"{W_}ins", f"{W_}moveTo") for a in t.iterancestors()) for t in texts):
            return True
        return any(a.tag == f"{W_}tr" and a.find(f"{W_}trPr/{W_}ins") is not None for a in p.iterancestors())

    for p in ms.document.element.body.iter(f"{W_}p"):
        if not "".join(t.text or "" for t in p.iter(f"{W_}t")).strip():
            continue
        if not tables and any(a.tag == f"{W_}tbl" for a in p.iterancestors()):
            continue
        if tracked and not inserted(p):
            continue
        ppr = _ppr_of(p)
        sp = ppr.find(f"{W_}spacing")
        if sp is None:
            sp = OxmlElement("w:spacing")
            _ppr_insert(ppr, sp)
        sp.set(qn("w:line"), want)
        sp.set(qn("w:lineRule"), "auto")


_KEYWORDS_LABEL = re.compile(r"^\s*(?:<H\d>)?\s*(?:key\s*-?\s*words?|index\s+terms)\b", re.I)


def heading_levels(ms: Manuscript, style: JournalStyle) -> dict[str, int]:
    """{para id: level} of the paragraphs that are section headings of their own: tagged <H1> / <H2> / <H3> by
    the heading rules (or already tagged), else the first-level headings docview finds."""
    from . import docview

    from . import rules_sections

    out = dict(_tagged_headings(ms, style))
    # The structured abstract's labels ("Methods:", "Results:" on lines of their own) and the key words label are
    # run-in labels at the text size, not first-level headings (only the Abstract heading takes 14 pt).
    try:
        lay = rules_sections.layout(ms)
        not_heads = {p.id for p in lay.abstract} | ({lay.keywords.id} if lay.keywords is not None else set())
    except Exception:  # noqa: BLE001 - no layout: no exclusions
        not_heads = set()
    tagging = bool(style.get("headings.tag"))
    # (a heading the journal names -- its H1 list, renamings, the ones it drops -- keeps the heading size)
    known = rules_sections.main_heading_keys(style) | {k.lower() for k in (
        style.get("sections.headings.drop") or [])} if tagging else set()
    for d in docview.classify(ms):
        if d["id"] in out or d["kind"] != "heading" or d["front"] or d["cell"]:
            continue
        if d["id"] in not_heads or _KEYWORDS_LABEL.match(d["text"]):
            continue
        if tagging and not re.match(r"\s*<H\d>", d["text"]) and rules_sections._key(d["text"]) not in known:
            # A journal that tags its headings: a first-level heading is one the heading rules tag. A line they leave
            # untagged ("Figures", "Tables", "Plain Language Summary", "Summary of MMAT criteria:") is at the text
            # size (the JEHP / IJO editors: the legends and tables headings at 12 pt).
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


_PLAIN_CHAR = re.compile(r"[\sA-Za-z0-9!-/:-@\[-`{-~\u2010-\u2015\u2018-\u201f\u00a0]")


def _keeps_font(font: str, ch: str) -> bool:
    """A character in a symbol font keeps it: in "Symbol" / "Wingdings" an "a" is a Greek alpha or a dingbat. A
    Unicode math font ("Cambria Math") shows plain letters, digits, hyphens and quotes as themselves: those take the
    journal's font like the text around them (a "‐" pasted in Cambria Math into a reference title)."""
    if not _SYMBOL_FONT.search(font):
        return False
    return not (re.search(r"math", font, re.I) and _PLAIN_CHAR.match(ch))


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
        keep = [_keeps_font(x.get("font") or "", ch) for x, ch in zip(f, text)]

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
        # Table cells: `fonts.table_align` (G193-194 "aligned left. No other alignment should be done"); null: the
        # alignment the cells were typed with.
        target_align = (cfg.get("table_align") or None) if d["cell"] else align
        if target_align and not d["image"] and not any(True for _ in p.element.iter(*_EQUATION)):
            target = cfg.get("running_head_align", "center") if rh else target_align
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


def _merge_replaced(kept: list[Change], replaced: list[Change]) -> None:
    """Formatting merges, it does not replace: a formatting-only change over all of the text that a kept change
    replaces ("Case report" bold, the case change "Case Report") gives its formatting to the replacement, under the
    replacement's own (the font and size are carried by finish_fonts)."""
    for c in kept:
        if not c.format_only or c.reason in ("fmt_font", "fmt_align"):
            continue
        for t in replaced:
            if t is c or t.para_id != c.para_id or not (t.start <= c.start and c.end <= t.end):
                continue
            lead = len(t.old) - len(t.old.lstrip())
            if c.start <= t.start + lead and c.end >= t.start + len(t.old.rstrip()) and t.new.strip():
                carry = {k: v for k, v in c.format.items() if k not in _PARA_KEYS}
                t.format = {**carry, **(t.format or {})}
            break


_PARA_KEYS = ("align", "indent", "para_style", "symbol", "list_number", "line_spacing")


def finish_fonts(ms: Manuscript, style: JournalStyle, kept: list[Change], replaced: list[Change]) -> list[Change]:
    """After the overlap check (rules.run_all): text that a kept change replaces takes the font and size of the
    formatting change it makes moot, and the alignment stays (the paragraph does); new paragraphs a change
    opens get the journal's font and size (the heading size for <H1>, "Abstract", "Acknowledgements")."""
    from .tracked import PARAGRAPH_BREAK

    _merge_replaced(kept, replaced)
    cfg = fonts_settings(style)
    if cfg is None:
        return kept
    out = []
    fonts = [k for k in kept if k.reason == "fmt_font"]
    for c in kept:
        if not c.format_only and c.start == c.end and c.new:
            # Text inserted at or inside a span the font change covers ("Keywords" -> "Keywords:" and the key words
            # put after it, in a Calibri 18 pt heading paragraph: IJO_661) takes its font and size too.
            f = next((k for k in fonts if k.para_id == c.para_id and k.start <= c.start <= k.end), None)
            if f is not None:
                carry = {k: v for k, v in f.format.items() if k in ("size", "font")}
                c.format = {**carry, **(c.format or {})}
        cover = next((t for t in replaced if c.format_only and t is not c and t.para_id == c.para_id
                      and t.start <= c.start and c.end <= t.end), None)
        if cover is None:
            out.append(c)
            continue
        if c.reason not in ("fmt_font", "fmt_align"):
            out.append(c)  # dropped as moot by run_all (its formatting merged into the replacement)
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


# --- headings as they come out -------------------------------------------------------------------------
#
# Pre-editing guide: a first-level heading "14 points font size in bold style" (G133), a second-level one "12 points
# font size in bold" (G135), the abstract heading and "Acknowledgement" 14 pt bold (G110, G146). Every paragraph the
# rules leave starting with a heading tag -- tagged in place, renamed, split off a paragraph after a soft line break
# ("... STEMI.\nMaterials and Methods"), inserted as a back-matter heading -- comes out bold, at its level's size and
# in the journal's font, whatever runs and styles the author's text had and whichever rule wrote it: the pass below
# works on the paragraphs as rendered (the kept changes applied), after the overlap check. Text that a split leaves
# as running text, and statements inserted under a heading ("Nil."), are not bold. Running text the author typed in a
# "Heading 1" / "Heading 2" paragraph style (shown in Word's navigation pane) goes to the body style.

_HEAD_LINE = re.compile(r"^\s*<H(\d)>")
_HEADING_NAME = re.compile(r"^heading\s*\d$", re.I)


def _outline_style(styles: _Styles, sid: str | None) -> bool:
    """A paragraph style that is a heading in Word's navigation pane (Heading 1-9, or a style with an outline level,
    directly or through the style it is based on)."""
    for _ in range(10):
        if not sid or sid not in styles.by_id:
            return False
        s = styles.by_id[sid]
        name = s.find(qn("w:name"))
        if _HEADING_NAME.match(sid) or (name is not None and _HEADING_NAME.match(name.get(qn("w:val")) or "")):
            return True
        ol = s.find(f"{qn('w:pPr')}/{qn('w:outlineLvl')}")
        if ol is not None:
            v = ol.get(qn("w:val")) or "9"
            return v.isdigit() and int(v) < 9
        b = s.find(qn("w:basedOn"))
        sid = b.get(qn("w:val")) if b is not None else None
    return False


def _list_style(styles: _Styles, sid: str | None) -> bool:
    """Word's "List Paragraph" style (or one based on it)."""
    for _ in range(10):
        if not sid or sid not in styles.by_id:
            return False
        s = styles.by_id[sid]
        name = s.find(qn("w:name"))
        if re.fullmatch(r"list\s*paragraph", sid, re.I) or (
                name is not None and re.fullmatch(r"list\s*paragraph", name.get(qn("w:val")) or "", re.I)):
            return True
        b = s.find(qn("w:basedOn"))
        sid = b.get(qn("w:val")) if b is not None else None
    return False


def _output_paragraphs(text: str, edits: list[Change]) -> list[dict]:
    """The paragraph as `tracked.render` builds it from its text changes: one entry per resulting paragraph, with its
    atoms (("o", start, end) original text, ("i", change, segment, text) inserted text) and the (change, segment)
    that opened it (None for the paragraph itself)."""
    from .tracked import PARAGRAPH_BREAK

    outs = [{"atoms": [], "opener": None}]
    pos = 0
    for c in sorted(edits, key=lambda c: (c.start, c.end, c.new.startswith(PARAGRAPH_BREAK))):
        if c.start > pos:
            outs[-1]["atoms"].append(("o", pos, c.start))
        for k, seg in enumerate(c.new.split(PARAGRAPH_BREAK)):
            if k:
                outs.append({"atoms": [], "opener": (c, k)})
            if seg:
                outs[-1]["atoms"].append(("i", c, k, seg))
        pos = max(pos, c.end)
    if pos < len(text):
        outs[-1]["atoms"].append(("o", pos, len(text)))
    for o in outs:
        o["text"] = "".join(a[3] if a[0] == "i" else text[a[1]:a[2]] for a in o["atoms"])
    return outs


def _set_segment(c: Change, k: int, props: dict, force: bool | str = True) -> None:
    """Lay `props` over the format of segment `k` of `c`'s inserted text. `force` False: only where the format does
    not set the property already; "own": only where the segment's own format (not the change's) does not."""
    from .tracked import PARAGRAPH_BREAK

    segs = c.new.split(PARAGRAPH_BREAK)
    sf = list(c.segment_format or [None])
    sf += [None] * (len(segs) - len(sf))
    base = {} if force == "own" else (c.format or {})

    def add(have: dict) -> dict:
        return dict(props) if force is True else {kk: v for kk, v in props.items() if kk not in have}

    cur = sf[k]
    if isinstance(cur, list):
        pieces = [[n, {**(f or {}), **add({**base, **(f or {})})}] for n, f in cur]
        rest = len(segs[k]) - sum(n for n, _ in cur)
        if rest > 0:
            pieces.append([rest, add(base)])
        sf[k] = pieces
    else:
        extra = add({**base, **(cur or {})})
        if not extra:
            return
        sf[k] = {**(cur or {}), **extra}
    c.segment_format = sf


def _split_change(c: Change, p: Paragraph, s: int, e: int, over: dict) -> list[Change]:
    """A formatting-only change cut at `s` and `e`: the part inside takes `over` on top of its format; paragraph
    properties stay with the first part."""
    from .changes import PARAGRAPH_PROPERTIES

    cuts = sorted({c.start, max(c.start, s), min(c.end, e), c.end})
    out = []
    for n, (a, b) in enumerate(zip(cuts, cuts[1:])):
        if b <= a:
            continue
        fmt = dict(c.format)
        if out:
            fmt = {k: v for k, v in fmt.items() if k not in PARAGRAPH_PROPERTIES}
        if s <= a and b <= e:
            fmt.update(over)
        if not fmt:
            continue
        part = Change(id=f"{c.id}h{n}", para_id=c.para_id, start=a, end=b, old=p.text[a:b], new=p.text[a:b],
                      layer=c.layer, source=c.source, reason=c.reason, status=c.status, author=c.author,
                      format=fmt, provenance=c.provenance)
        out.append(part)
    return out


class _ParaFormat:
    """Effective formatting of a paragraph's characters, with the kept formatting-only changes laid over it."""

    def __init__(self, p: Paragraph, styles: _FontStyles, para_style: str | None = None):
        self.p = p
        self.fonts = _font_chars_as(p, styles, para_style)
        props = run_props(p, styles)
        weak = char_style_bold(p, styles)
        n = len(p.text)
        self.ok = len(self.fonts) == n and len(props) == n
        self.bold = [bool(f.get("bold")) for f in props] if self.ok else []
        self.strong = [b and not (weak[i] if len(weak) == n else False) for i, b in enumerate(self.bold)]

    def eff(self, prop: str, i: int, want):
        if prop == "bold":
            return self.strong[i] if want else self.bold[i]
        return self.fonts[i].get(prop)


def _font_chars_as(p: Paragraph, styles: _FontStyles, para_style: str | None) -> list[dict]:
    """font_chars of `p` as it reads in paragraph style `para_style` (None: its own style)."""
    if para_style is None:
        return font_chars(p, styles)
    ppr = p.element.find(qn("w:pPr"))
    ps = ppr.find(qn("w:pStyle")) if ppr is not None else None
    old = ps.get(qn("w:val")) if ps is not None else None
    if ps is None:
        return font_chars(_StyledAs(p, para_style), styles)
    ps.set(qn("w:val"), para_style)
    try:
        return font_chars(p, styles)
    finally:
        ps.set(qn("w:val"), old)


class _StyledAs:
    """A paragraph with no style of its own read as if it had `para_style` (font_chars takes the default style)."""

    def __init__(self, p: Paragraph, sid: str):
        import copy
        self.element = copy.deepcopy(p.element)
        ppr = self.element.find(qn("w:pPr"))
        if ppr is None:
            from docx.oxml import OxmlElement
            ppr = OxmlElement("w:pPr")
            self.element.insert(0, ppr)
        from docx.oxml import OxmlElement
        ps = OxmlElement("w:pStyle")
        ps.set(qn("w:val"), sid)
        ppr.insert(0, ps)


def _force(p: Paragraph, s: int, e: int, wants: dict, pf: _ParaFormat, mine: list[Change],
           layer: Layer) -> list[Change]:
    """Formatting-only changes so that every character of p.text[s:e] ends up with `wants` ({"bold": True, "size":
    14, "font": ...}): kept changes that set a property otherwise are cut and corrected (in `mine`, in place), the
    characters no change covers get a change where their own formatting differs. Returns the new changes."""
    if e <= s or not wants or not pf.ok:
        return []
    for c in list(mine):
        if not c.format_only or not (c.start < e and s < c.end):
            continue
        over = {k: v for k, v in wants.items() if k in c.format and c.format[k] != v}
        if over:
            at = mine.index(c)
            mine[at:at + 1] = _split_change(c, p, s, e, over)
    text = p.text
    covered = [set() for _ in range(e - s)]
    for c in mine:
        if c.format_only and c.start < e and s < c.end:
            for i in range(max(c.start, s), min(c.end, e)):
                covered[i - s].update(k for k in wants if k in c.format)
    need, free = [], []
    for i in range(s, e):
        keep_font = "font" in wants and _keeps_font(pf.fonts[i].get("font") or "", text[i])
        fr = {k for k in wants if k not in covered[i - s] and not (k == "font" and keep_font)}
        nd = {}
        for k in fr:
            have = pf.eff(k, i, wants[k])
            if k == "size":
                differs = have is None or abs(float(have) - float(wants[k])) > 0.01
            else:
                differs = have != wants[k]
            if differs and not (k == "bold" and text[i].isspace()):
                nd[k] = wants[k]
        need.append(nd)
        free.append(fr)
    out: list[Change] = []
    region = None  # [start, end, props]

    def close():
        if region is None:
            return
        a, b, props = region
        while b > a and text[b - 1].isspace():
            b -= 1
        while a < b and text[a].isspace():
            a += 1
        if b > a:
            rule = "fmt_heading_bold" if "bold" in props else "fmt_font"
            out.append(_fmt(p, a, b, text[a:b], props, layer, rule))

    for i in range(s, e):
        nd, fr = need[i - s], free[i - s]
        if region is not None and (nd == region[2] or (set(nd) <= set(region[2]) and set(region[2]) <= fr)):
            region[1] = i + 1
            continue
        close()
        region = [i, i + 1, nd] if nd else None
    close()
    return out


def _all_bold(p: Paragraph, atoms: list, pf: _ParaFormat, mine: list[Change]) -> bool:
    """Every letter of the original text `atoms` comes out bold (the kept formatting changes laid over the runs)."""
    seen = False
    for _, s, e in atoms:
        for i in range(s, e):
            if not p.text[i].isalnum():
                continue
            seen = True
            val = next((c.format["bold"] for c in mine if c.format_only and "bold" in c.format
                        and c.start <= i < c.end), None)
            if not (pf.bold[i] if val is None else val):
                return False
    return seen


def finish_headings(ms: Manuscript, style: JournalStyle, kept: list[Change]) -> list[Change]:
    """After the overlap check (rules.run_all): every paragraph that comes out as a heading (<H1>, <H2>, <H3>, the
    abstract and Acknowledgement headings) bold at its level's size in the journal's font; text split off a heading or
    inserted under one, not bold; running text out of the "Heading N" paragraph styles; new paragraphs aligned."""
    from . import docview

    cfg = fonts_settings(style)
    bold_on = _on(style, "heading_bold_tagged")
    if cfg is None and not bold_on:
        return kept
    bold_levels = set(style.get("formatting.heading_bold_levels", [1, 2, 3]) or [])
    unbold_levels = set(style.get("formatting.heading_unbold_levels", []) or [])
    family = (cfg or {}).get("family") or None
    styles = _FontStyles(ms)
    body_style = styles.default_para or "Normal"
    restyle_on = bool(style.get("formatting.body_style_from_headings", True))
    by_para: dict[str, list[Change]] = defaultdict(list)
    for c in kept:
        by_para[c.para_id].append(c)
    try:
        info = {d["id"]: d for d in docview.classify(ms)}
    except Exception:  # noqa: BLE001 - a document docview cannot read: headings only
        info = {}
    added: list[Change] = []
    replaced: dict[str, list[Change]] = {}

    def wants_for(level: int) -> dict:
        w = {}
        if bold_on and level in unbold_levels:
            w["bold"] = False
        elif bold_on and level in bold_levels:
            w["bold"] = True
        if cfg is not None:
            w["size"] = cfg.get("heading_size", 14) if level == 1 else cfg.get("size", 12)
            if family:
                w["font"] = family
        return w

    for p in ms.paragraphs:
        cs = by_para.get(p.id, [])
        text = p.text
        d = info.get(p.id, {})
        outline = restyle_on and not p.in_table and not d.get("front") and _outline_style(styles, p.style or None)
        if not text.strip() and not cs:
            continue
        edits = [c for c in cs if not c.format_only]
        if not edits and not outline and not _HEAD_LINE.match(text) and not heading_size_text(text):
            continue
        outs = _output_paragraphs(text, edits)
        mine = list(cs)
        pf = None
        new: list[Change] = []
        heads = []
        for o in outs:
            m = _HEAD_LINE.match(o["text"])
            level = int(m.group(1)) if m else (1 if not p.in_table and heading_size_text(o["text"]) else None)
            heads.append(level)
        has_head = any(h is not None for h in heads)
        restyled = False
        head_style = None  # the style a heading paragraph is read in: the body style it goes to, else its own
        for j, o in enumerate(outs):
            if not o["text"].strip():
                continue
            level = heads[j]
            if level is not None:
                wants = wants_for(level)
                if restyled and o["opener"] is not None:  # the paragraph it was split from goes to the body style
                    _set_segment(o["opener"][0], o["opener"][1], {"para_style": p.style})
                orig = [a for a in o["atoms"] if a[0] == "o"]
                if j == 0 and orig and not p.in_table and (_list_style(styles, p.style) or p.numbered):
                    # a heading the author set as a list item ("List Paragraph", Word's numbering: 997, 1559) is a
                    # paragraph of the body style, its automatic number gone as a typed one is (strip_sub_numbering)
                    strip = style.get("sections.headings.strip_numbering" if level == 1
                                      else "sections.headings.strip_sub_numbering", False)
                    para = {"para_style": body_style} if _list_style(styles, p.style) else {}
                    if p.numbered and strip:
                        para["list_number"] = False
                    if p.numbered and not strip:
                        para = {}
                    if para:
                        new.append(_fmt(p, orig[0][1], orig[0][2], text[orig[0][1]:orig[0][2]], para,
                                        Layer.STRUCTURE, "fmt_heading_style"))
                        if "para_style" in para:
                            head_style = body_style
                if not wants:
                    continue
                for a in o["atoms"]:
                    if a[0] == "i":
                        _set_segment(a[1], a[2], wants)
                    else:
                        pf = pf or _ParaFormat(p, styles, head_style)
                        new += _force(p, a[1], a[2], wants, pf, mine, Layer.STRUCTURE)
                continue
            # running text: inserted under a heading, not bold; out of a heading paragraph style
            if bold_on and has_head:
                for a in o["atoms"]:
                    if a[0] == "i" and a[2] > 0:
                        _set_segment(a[1], a[2], {"bold": False}, force="own")
            short_head = len(o["text"].strip()) <= 80 and not o["text"].rstrip().endswith((".", ":")) and \
                d.get("kind") == "heading" and j == 0 and len(outs) == 1
            orig = [a for a in o["atoms"] if a[0] == "o"]
            restyle = outline and not short_head
            if restyle:
                if o["opener"] is not None:
                    _set_segment(o["opener"][0], o["opener"][1], {"para_style": body_style})
                elif orig:
                    restyled = True
                    new.append(_fmt(p, orig[0][1], orig[0][2], text[orig[0][1]:orig[0][2]],
                                    {"para_style": body_style}, Layer.HOUSE_STYLE, "fmt_body_style"))
            w = {}
            if cfg is not None and (restyle or has_head):
                w = {"size": cfg.get("size", 12), **({"font": family} if family else {})}
            if restyle and w:
                for a in o["atoms"]:
                    if a[0] == "i":
                        _set_segment(a[1], a[2], w, force=False)
            pfx = _ParaFormat(p, styles, body_style if restyle else None) if (w or has_head) and orig else None
            if bold_on and has_head and pfx is not None and pfx.ok and _all_bold(p, orig, pfx, mine):
                # the statement a heading was split from, bold with it ("Conflicts of interest -Nil", 924): plain
                w = {**w, "bold": False}
            for a in orig:
                new += _force(p, a[1], a[2], w, pfx, mine, Layer.HOUSE_STYLE)
        # new paragraphs: the journal's alignment (they copy the paragraph's properties, which may be justified)
        align = (cfg or {}).get("table_align") if p.in_table else (cfg or {}).get("align")
        if align and len(outs) > 1:
            set_to = next((c.format["align"] for c in mine if c.format_only and "align" in c.format), None)
            have = set_to or paragraph_alignment(p, styles)
            for o in outs[1:]:
                c, k = o["opener"]
                target = cfg.get("running_head_align", "center") if o["text"].lstrip().startswith("<RH>") else align
                if target != have:
                    _set_segment(c, k, {"align": target})
        if new or mine != cs:
            replaced[p.id] = mine
            added += new
    if not replaced:
        return kept
    out = []
    done = set()
    for c in kept:
        if c.para_id in replaced:
            if c.para_id not in done:
                out += replaced[c.para_id]
                done.add(c.para_id)
            continue
        out.append(c)
    return out + added
