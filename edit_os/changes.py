"""Change records: every edit, from rules, AI or an editor, is one of these.

The manuscript file is never edited directly; tracked and clean outputs are
rendered from the accepted changes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum

# Run properties a change can set (True) or clear (False); see `Change.format`.
FORMAT_PROPERTIES = ("italic", "bold", "superscript", "subscript", "small_caps")


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
    format: dict[str, bool] | None = None

    def __post_init__(self):
        for k in self.format or {}:
            if k not in FORMAT_PROPERTIES:
                raise ValueError(f"{self.id}: unknown format property {k!r}")

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
        return cls(**d)


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
