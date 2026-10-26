"""Score the front-matter step against the style editor's front matter.

The editors rebuild the title page as new paragraphs at the top of the manuscript (from the
title-page file and the submission XML), so paragraph pairing (tools/evaluate.py) cannot see it.
Here the editor's block is read from te.docx (from the `<RH>` line to the cite line) and split
into fields; our block comes from `frontmatter.plan`. Per field: how often ours is identical to the
editor's (whitespace normalised) and the mean similarity (1 - normalised token edit distance).

Usage: python tools/eval_frontmatter.py DATA_DIR JOURNAL|all [--show] [--field NAME] [--by-journal]
                                         [--set front_matter.KEY=YAML_VALUE ...]
"""

from __future__ import annotations

import glob
import os
import re
import sys
import tempfile
from collections import defaultdict

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evaluate import distance, manuscript_from_zip, tracked_pairs  # noqa: E402

from edit_os import docmodel, frontmatter, style as style_mod  # noqa: E402

FIELDS = ["running_head", "article_type", "title", "authors", "affiliations", "corr_name", "corr_address",
          "corr_email", "dates", "cite"]
_CORR = re.compile(r"^(Address for correspondence|Correspondence to|Correspondence|Corresponding author)\s*:", re.I)
_DATE = re.compile(r"^(Received|Submitted|Revised|Accepted)\s*:", re.I)
_CITE = re.compile(r"^(Cite this article as|How to cite this article)\s*:", re.I)
_EMAIL = re.compile(r"^E-?mail\s*:", re.I)
_STOP = re.compile(r"^(<H\d>)?\s*(abstract|key\s*-?words?)\b", re.I)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" ,")


def editor_fields(te: bytes) -> dict[str, str]:
    after = [norm(a) for _, a in tracked_pairs(te) if a.strip()]
    start = next((i for i, a in enumerate(after) if a.startswith("<RH>")), None)
    if start is None:
        return {}
    lines = after[start:]
    if ":" not in lines[0] and len(lines) > 1 and ":" in lines[1]:  # "<RH>Setyorini" / "et al.: ???"
        lines = [norm(lines[0] + " " + lines[1])] + lines[2:]
    out = {"running_head": lines[0]}
    i = 1
    for name in ("article_type", "title", "authors"):
        if i < len(lines) and re.match(r"(Dear|Sir|Respected)\b", lines[i]):
            # a letter: the author group and correspondence follow the letter, before the references
            i = next((k for k in range(i, len(lines)) if _CORR.match(lines[k])), len(lines))
            break
        if i < len(lines) and not (_CORR.match(lines[i]) or _DATE.match(lines[i])):
            out[name] = lines[i]
            i += 1
            if name == "title":  # a title over two paragraphs ("...: " / "A comprehensive framework ...")
                while i < len(lines) and _title_rest(out[name], lines[i]):
                    out[name] = norm(out[name] + " " + lines[i])
                    i += 1
            if name == "authors":  # one author per paragraph
                while i < len(lines) and _name_line(lines[i]) and _name_line(out[name].split(", ")[-1]):
                    out[name] = norm(out[name] + ", " + lines[i])
                    i += 1
    aff = []
    while "authors" in out and i < len(lines) and not (
            _CORR.match(lines[i]) or _DATE.match(lines[i]) or _CITE.match(lines[i]) or lines[i].upper().startswith("ORCID")):
        aff.append(lines[i])
        i += 1
    if aff:
        out["affiliations"] = norm(" ".join(aff))
    while i < len(lines) and lines[i].upper().startswith("ORCID"):
        i += 1
    if i < len(lines) and _CORR.match(lines[i]):
        corr = [lines[i]]
        i += 1
        while i < len(lines) and not (_DATE.match(lines[i]) or _CITE.match(lines[i]) or _STOP.match(lines[i])
                                      or len(corr) > 6):
            corr.append(lines[i])
            i += 1
        email = [c for c in corr if _EMAIL.match(c) or re.search(r"\bE-?mail\s*:", c)]
        body = [c for c in corr[1:] if c not in email and not c.upper().startswith("ORCID")]
        name = corr[0]
        if len(corr) > 1 and re.fullmatch(r"(Address for correspondence|Correspondence to)\s*:\s*(Dr\.|Prof\.)?", name):
            name = norm(name + " " + body.pop(0)) if body else name  # label and name split over two paragraphs
        out["corr_name"] = name
        if body:
            out["corr_address"] = norm(" ".join(body))
        if email:
            out["corr_email"] = email[0]
    dates = []
    while i < len(lines) and _DATE.match(lines[i]):
        dates.append(lines[i])
        i += 1
    if dates:
        out["dates"] = norm(" ".join(dates))
    for line in lines[i:i + 3]:
        if _CITE.match(line):
            out["cite"] = line
            break
        if _STOP.match(line):
            break
    return out


_AFF_WORD = re.compile(r"(?i)\b(department|university|college|hospital|institute|school|faculty|centre|center|"
                       r"laboratory|unit|division|india|clinic|ministry)\b")


def _title_rest(title: str, line: str) -> bool:
    """The second paragraph of a title: after a colon, or mostly lowercase words (authors are capitalised)."""
    words = re.findall(r"[^\W\d_][\w'’-]*", line)
    if not words or _AFF_WORD.search(line) or re.search(r"\d", line):
        return False
    lower = sum(w[0].islower() for w in words) / len(words)
    return title.rstrip().endswith(":") and lower >= 0.3 or lower >= 0.5


def _name_line(line: str) -> bool:
    """A paragraph holding one author's name ("Karnellya R. Ohol1")."""
    return bool(re.fullmatch(r"[A-Z][\w'’.-]*(?:\s+[A-Za-z][\w'’.-]*){0,4}?[\d,*]*", line)) \
        and not _AFF_WORD.search(line) and "," not in line.rstrip("0123456789,*")


def our_fields(blocks) -> dict[str, str]:
    out: dict[str, str] = {}
    corr = [b.text for b in blocks if b.role == "correspondence"]
    for b in blocks:
        if b.role in ("running_head", "article_type", "title", "authors", "affiliations", "cite"):
            out[b.role] = norm(b.text)
    dates = [norm(b.text) for b in blocks if b.role == "dates"]
    if dates:
        out["dates"] = norm(" ".join(dates))
    if corr:
        out["corr_name"] = norm(corr[0])
        email = [c for c in corr[1:] if _EMAIL.match(c)]
        rest = [c for c in corr[1:] if c not in email]
        if rest:
            out["corr_address"] = norm(" ".join(rest))
        if email:
            out["corr_email"] = norm(email[0])
    return out


def similarity(a: str, b: str) -> float:
    n = max(distance("", a), distance("", b), 1)
    return 1 - distance(a, b) / n


def article_fields(d: str, code: str, overrides=()) -> tuple[dict[str, str], dict[str, str]] | None:
    """(editor's fields, our fields) for one article folder (`original.zip`, `te.docx`), or None when it has no
    editor front matter. `overrides`: [(front_matter.KEY path, YAML value)] set on the journal style first."""
    te_path, zip_path = os.path.join(d, "te.docx"), os.path.join(d, "original.zip")
    if not (os.path.exists(te_path) and os.path.exists(zip_path)):
        return None
    with open(te_path, "rb") as f:
        ed = editor_fields(f.read())
    if not ed:
        return None
    src_bytes = manuscript_from_zip(zip_path)
    if src_bytes is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "m.docx")
        with open(path, "wb") as f:
            f.write(src_bytes)
        ms = docmodel.load(path)
    st = style_mod.load(code)
    for key, value in overrides:
        node = st.settings
        *path, last = key.split(".")
        for part in path:
            node = node.setdefault(part, {})
        node[last] = yaml.safe_load(value)
    return ed, our_fields(frontmatter.plan(ms, st, frontmatter.sources_from_zip(zip_path)).blocks)


def main(argv):
    data, journal = argv[0], argv[1].lower()
    only = argv[argv.index("--field") + 1] if "--field" in argv else None
    dirs = sorted(d for d in glob.glob(os.path.join(data, "*"))
                  if journal == "all" or os.path.basename(d).lower().startswith(journal))
    tot = {f: dict(editor=0, ours=0, exact=0, sim=0.0) for f in FIELDS}
    per_journal: dict = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0]))
    overrides = [argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--set"]
    n = 0
    for d in dirs:
        code = re.split(r"[_-]", os.path.basename(d))[0].lower()
        got = article_fields(d, code, overrides)
        if got is None:
            continue
        ed, ours = got
        n += 1
        for fld in FIELDS:
            if fld not in ed:
                continue
            t = tot[fld]
            t["editor"] += 1
            o = ours.get(fld, "")
            t["ours"] += bool(o)
            t["exact"] += o == ed[fld]
            t["sim"] += similarity(o, ed[fld])
            pj = per_journal[code][fld]
            pj[0] += 1
            pj[1] += similarity(o, ed[fld])
            pj[2] += o == ed[fld]
            if "--show" in argv and o != ed[fld] and (only is None or only == fld):
                print(f"{os.path.basename(d):26s} {fld}\n    ours:   {o[:200]!r}\n    editor: {ed[fld][:200]!r}")
    print(f"articles {n}")
    all_sim = sum(t["sim"] for t in tot.values()) / max(1, sum(t["editor"] for t in tot.values()))
    for fld, t in tot.items():
        if t["editor"]:
            print(f"  {fld:13s} editor {t['editor']:4d}  produced {t['ours']:4d}  identical {t['exact'] / t['editor']:6.1%}"
                  f"  similarity {t['sim'] / t['editor']:6.1%}")
    print(f"  {'all fields':13s} similarity {all_sim:6.1%}")
    if "--by-journal" in argv:
        for code, flds in sorted(per_journal.items()):
            cells = [f"{f[:8]} {v[2]}/{v[0]} {v[1] / v[0]:4.0%}" for f, v in flds.items() if only is None or f == only]
            print(f"  {code:18s} " + "  ".join(cells))
    u = frontmatter.USAGE
    cost = u["input_tokens"] * 0.10 / 1e6 + u["output_tokens"] * 0.50 / 1e6
    print(f"Claude: {u['calls']} calls, {u['input_tokens']} in / {u['output_tokens']} out tokens, ~${cost:.4f}")


if __name__ == "__main__":
    main(sys.argv[1:])
