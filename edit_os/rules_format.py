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


def _tagged_headings(ms: Manuscript, style: JournalStyle) -> set[str]:
    """Paragraphs the heading rules tag <H1> / <H2> / <H3> as a whole: a short line of its own (not a run-in
    label split off its text)."""
    from . import rules, rules_sections

    found = rules.rule_heading_tags(ms, style) + rules_sections.rule_headings(ms, style) \
        + rules_sections.rule_subheadings(ms, style)
    by_id = {p.id: p for p in ms.paragraphs}
    out = set()
    for c in found:
        p = by_id.get(c.para_id)
        if p is None or c.format_only or not c.new.startswith("<H") or p.text[:c.start].strip():
            continue
        text = p.text.strip()
        if len(text) <= 80 and not re.search(r":\s*\S", text):
            out.add(p.id)
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
    tagged = _tagged_headings(ms, style) if _on(style, "heading_bold_tagged") else set()
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

        if p.id in tagged and not front and not label:
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
