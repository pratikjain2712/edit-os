"""Change records: every edit, from rules, AI or an editor, is one of these.

The manuscript file is never edited directly; tracked and clean outputs are
rendered from the accepted changes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum

# Run properties a change can set (True) or clear (False); see `Change.format`. Besides the toggles:
# "size" sets the font size in points (12, 14) or clears the direct size (False); "font" sets the font
# family ("Times New Roman") or clears it (False); "align" is the paragraph's alignment (left, center,
# right, both), set on the whole paragraph by a formatting-only change.
FORMAT_PROPERTIES = ("italic", "bold", "superscript", "subscript", "small_caps", "caps", "underline",
                     "color", "size", "highlight", "font", "align",
                     "list_number", "symbol", "indent", "para_style", "line_spacing")
# "line_spacing": a paragraph property, the paragraph's line spacing in lines (2.0 double), tracked as a paragraph
# property change (formatting.line_spacing).
# "symbol": the one character the change covers becomes Word's Symbol-font symbol (<w:sym>; formatting.symbol_font).
# "list_number": False on a formatting-only change takes the paragraph out of Word's automatic numbering (a heading the
# author numbered as a list item, rules_format.finish_headings). True: a paragraph-level property carried by a change
# that deletes a typed list label ("1.<tab>"): the
# paragraph becomes an item of Word's automatic numbering instead (tracked.render; references.label_auto_number).
# "para_style": a paragraph property: the paragraph's style id (the journal's body style, "Normal", for running text an
# author typed in a "Heading 1" style that would show in Word's navigation pane), tracked as a paragraph property change.
# "indent": a paragraph property, cleared only (False): the paragraph's direct left and first-line indentation set to
# zero (tracked as a paragraph property change), as for a table cell whose indentation becomes typed spaces (G208).
# Properties a change can only clear: the author's font colour (False removes it, "auto" sets
# automatic colour over a coloured style), highlighting / coloured shading.
PARAGRAPH_PROPERTIES = ("align", "para_style", "line_spacing")  # set on the paragraph, not its runs
CLEAR_ONLY = {"color": (False, "auto"), "highlight": (False,), "indent": (False,)}
ALIGNMENTS = ("left", "center", "right", "both")


def _valid(k: str, v) -> bool:
    if k in CLEAR_ONLY:
        return v in CLEAR_ONLY[k]
    if k == "size":
        return v is False or (isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 400)
    if k == "font":
        return v is False or (isinstance(v, str) and bool(v.strip()))
    if k == "align":
        return v in ALIGNMENTS
    if k == "para_style":
        return isinstance(v, str) and bool(v.strip())
    if k == "line_spacing":
        return isinstance(v, (int, float)) and not isinstance(v, bool) and 0.5 <= v <= 5
    return True  # the toggles: set (true) or clear (false)


class Layer(str, Enum):
    STRUCTURE = "structure"
    LANGUAGE = "language"
    HOUSE_STYLE = "house_style"
    REFERENCES = "references"
    TABLES_FIGURES = "tables_figures"
    NUMBERS = "numbers"
    QUERY = "query"


class Source(str, Enum):
    RULE = "rule"
    AI = "ai"
    EDITOR = "editor"


class Status(str, Enum):
    SUGGESTED = "suggested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass
class Change:
    id: str
    para_id: str
    start: int  # offset into the paragraph's text view
    end: int
    old: str  # must equal text[start:end]; guards against stale offsets
    new: str
    layer: Layer
    source: Source
    reason: str = ""
    status: Status = Status.SUGGESTED
    author: str = "Edit OS"
    # Run formatting, e.g. {"italic": True} or {"superscript": True, "bold": False}. With
    # old == new it is a formatting-only change over start..end (Word tracked formatting);
    # with a text change it is applied to the inserted text.
    format: dict[str, bool | str] | None = None
    # Where the suggestion comes from, for the editor (edit_os/provenance.py): which input a front-matter
    # field was read from, the PubMed / CrossRef record a reference was rebuilt from, the checker and its
    # rule id, the old -> new reference number. Optional: records saved without it still load.
    provenance: dict | None = None
    # Inserted text that opens new paragraphs (tracked.PARAGRAPH_BREAK): formatting per paragraph segment of
    # `new`, laid over `format` (None = `format` alone). A back-matter section added as "<H2>Heading", "Nil."
    # has its heading bold and its statement roman. A segment after a break may instead be a list of
    # [length, format] pieces from its start ("Keywords:" bold, the list after it roman); text past the pieces
    # takes `format`. Optional: records saved without it still load.
    segment_format: list | None = None

    def __post_init__(self):
        fmts = [self.format]
        for seg in self.segment_format or []:
            fmts += [f for _, f in seg] if isinstance(seg, list) else [seg]
        for fmt in fmts:
            for k, v in (fmt or {}).items():
                if k not in FORMAT_PROPERTIES:
                    raise ValueError(f"{self.id}: unknown format property {k!r}")
                if not _valid(k, v):
                    raise ValueError(f"{self.id}: {k} cannot be set to {v!r}")

    def segment(self, i: int) -> dict | None:
        """The formatting of paragraph segment `i` of the inserted text (pieces: the base format)."""
        over = (self.segment_format or [])[i] if i < len(self.segment_format or []) else None
        if not over or isinstance(over, list):
            return self.format
        return {**(self.format or {}), **over}

    def pieces(self, i: int, text: str) -> list[tuple[str, dict | None]]:
        """Segment `i` of the inserted text as (text, format) runs."""
        over = (self.segment_format or [])[i] if i < len(self.segment_format or []) else None
        if not isinstance(over, list):
            return [(text, self.segment(i))] if text else []
        out, at = [], 0
        for n, fmt in over:
            if text[at:at + n]:
                out.append((text[at:at + n], {**(self.format or {}), **(fmt or {})}))
            at += n
        if text[at:]:
            out.append((text[at:], self.format))
        return out

    def paragraph_props(self, i: int) -> dict:
        """Paragraph properties ("align", "para_style") of the paragraph that segment `i` of the inserted text opens
        (from its segment format, or the first of its pieces that names them)."""
        over = (self.segment_format or [])[i] if i < len(self.segment_format or []) else None
        fmts = [f for _, f in over] if isinstance(over, list) else [over]
        out = {}
        for f in fmts:
            for k in PARAGRAPH_PROPERTIES:
                if (f or {}).get(k) and k not in out:
                    out[k] = f[k]
        return out

    @property
    def format_only(self) -> bool:
        return bool(self.format) and self.old == self.new and self.end > self.start

    def to_dict(self) -> dict:
        d = asdict(self)
        d["layer"], d["source"], d["status"] = self.layer.value, self.source.value, self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Change":
        d = dict(d)
        d["layer"], d["source"] = Layer(d["layer"]), Source(d["source"])
        d["status"] = Status(d.get("status", "suggested"))
        d["format"] = d.get("format") or None
        d["provenance"] = d.get("provenance") or None
        d["segment_format"] = d.get("segment_format") or None
        return cls(**{k: v for k, v in d.items() if k in _FIELDS})


_FIELDS = set(Change.__dataclass_fields__)


@dataclass
class ChangeSet:
    changes: list[Change] = field(default_factory=list)

    def add(self, change: Change) -> Change:
        self.changes.append(change)
        return change

    def for_paragraph(self, para_id: str, statuses=(Status.ACCEPTED,)) -> list[Change]:
        return sorted(
            (c for c in self.changes if c.para_id == para_id and c.status in statuses),
            key=lambda c: (c.start, c.end),
        )

    def dump(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump([c.to_dict() for c in self.changes], f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "ChangeSet":
        with open(path, encoding="utf-8") as f:
            return cls([Change.from_dict(d) for d in json.load(f)])
