"""Journal style: a shared base rule set plus per-journal overrides (edit_os/styles/*.yaml)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

STYLES_DIR = os.path.join(os.path.dirname(__file__), "styles")

# Article-id prefixes that differ from the style file's name (styles/<code>.yaml).
ALIASES = {
    "neurol": "ni",                # Neurology India (Neurol-India-D-...)
    "neurol-india": "ni",
    "lungindia": "lung",           # Lung India
    "smj": "sgmj",                 # Singapore Medical Journal (SMJ-...)
    "dshmj": "ijn_dshmj",          # IJN & DSHMJ share one stylesheet
    "ijn": "ijn_dshmj",
    "nbcs": "njbcs",               # NJBCS pubcode
}


def resolve(code: str) -> str:
    """Style file name for a journal code or article-id prefix."""
    c = code.lower()
    return ALIASES.get(c, c)


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@dataclass
class JournalStyle:
    code: str
    name: str
    settings: dict = field(default_factory=dict)

    def get(self, path: str, default=None):
        node = self.settings
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def load(code: str) -> JournalStyle:
    with open(os.path.join(STYLES_DIR, "base.yaml"), encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    path = os.path.join(STYLES_DIR, f"{resolve(code)}.yaml")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            settings = _merge(settings, yaml.safe_load(f) or {})
    return JournalStyle(code=code.upper(), name=settings.get("name", code.upper()), settings=settings)
