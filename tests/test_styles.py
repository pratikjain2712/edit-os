"""Every journal style file loads, has a name, and only sets keys the code reads (those in base.yaml)."""

import glob
import os

import pytest
import yaml

from edit_os import style

# Keys a journal file may set that base.yaml does not define. Add a key here only when the
# code reads it (style.get) and base.yaml has no sensible default for it.
DOCUMENTED_NEW_KEYS: set[str] = set()

FILES = sorted(f for f in glob.glob(os.path.join(style.STYLES_DIR, "*.yaml")) if not f.endswith("base.yaml"))


def _paths(node, prefix="", base=None):
    """Dotted key paths of a mapping; lists and scalars are leaves, and so are free-form maps
    (settings whose base value is an empty mapping, and heading rename tables)."""
    out = set()
    for k, v in node.items():
        p = f"{prefix}{k}"
        out.add(p)
        b = base.get(k) if isinstance(base, dict) else None
        if isinstance(v, dict) and b != {} and k != "rename":
            out |= _paths(v, p + ".", b)
    return out


def _base():
    with open(os.path.join(style.STYLES_DIR, "base.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_there_is_a_file_per_stylesheet():
    assert len(FILES) >= 42


@pytest.mark.parametrize("path", FILES, ids=os.path.basename)
def test_style_file(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    data = yaml.safe_load(text) or {}
    assert isinstance(data.get("name"), str) and data["name"].strip()
    unknown = _paths(data, base=_base()) - _paths(_base()) - DOCUMENTED_NEW_KEYS
    assert not unknown, f"keys the code does not read: {sorted(unknown)}"
    code = os.path.basename(path)[:-5]
    st = style.load(code)
    assert st.name == data["name"]
    # Overrides keep the base value's type (a list stays a list, a flag stays a flag).
    for key in _paths(data, base=_base()) - {"name"}:
        b = st.get(key)
        node = _base()
        for part in key.split("."):
            node = node[part]
        if node is not None and b is not None:
            assert isinstance(b, type(node)) or {type(b), type(node)} <= {int, float}, key
    for key in ("headings.h1", "headings.h2"):
        assert all(isinstance(h, str) and h == h.lower() for h in st.get(key)), key


@pytest.mark.parametrize("prefix,code", sorted(style.ALIASES.items()))
def test_aliases_point_at_style_files(prefix, code):
    assert os.path.exists(os.path.join(style.STYLES_DIR, f"{code}.yaml"))
    assert style.load(prefix).settings == style.load(code).settings


def test_unknown_journal_falls_back_to_base():
    assert style.load("nosuchjournal").name == _base()["name"]
