"""Score the formatting rules against a style editor's tracked formatting changes.

The editor's formatting changes are read from the tracked file (te.docx): every run outside
<w:ins> / <w:del> whose <w:rPr> holds a <w:rPrChange> is compared old -> new, property by
property (bold, italic, underline, superscript, colour, size, highlight, font, ...), and every
<w:pPrChange> gives the paragraph properties it changed (alignment, spacing, indents). Adjacent
runs with the same change are merged into one span.

The tool's formatting changes are the format-only `Change` records `rules.run_all` proposes for
the original manuscript. Paragraphs are paired by their pre-edit text (as in evaluate.py); a tool
span matches an editor span in the same paragraph when they overlap and make the same change
(e.g. "bold-": bold removed, "color-": colour removed, "superscript+").

Per change and per paragraph context (heading, body, table, caption, reference, front, list):
editor count, tool count, matched, precision, recall. "moot": tool changes on text the editor
deleted or rewrote (the paragraph did not pair), left out of precision; "unpair": editor changes in
paragraphs no original paragraph pairs with, left out of recall.

  --diff         the editor's changes are the difference between the original runs and the
                 tracked file's final runs, so formatting made with tracking off counts too
  --context      break each change down by paragraph context
  --by-rule      precision of each rule's changes (and per journal with --by-journal)
  --examples L   list the editor's changes with label L (L@tool: the tool's), --rule R to filter
  --only R,R     run only these rules;  --editor-only: survey the editors' changes, no rules
  --consistency  in how many articles the editor makes each change

Usage:
  python tools/eval_format.py DATA_DIR JOURNAL|all [options]
  python tools/eval_format.py --pair ORIGINAL.docx EDITOR.docx JOURNAL [options]
"""

from __future__ import annotations

import glob
import io
import os
import re
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lxml import etree  # noqa: E402

from edit_os import docmodel, docview, frontmatter, rules, style as style_mod  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_OFF = ("0", "false", "off", "none")
_TOGGLES = {"bold": "b", "italic": "i", "underline": "u", "strike": "strike", "caps": "caps",
            "small_caps": "smallCaps"}
_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def rpr_props(rpr) -> dict:
    """Direct run properties of a <w:rPr> (None: nothing set), normalised for comparison."""
    out = {}
    get = (lambda tag: rpr.find(W + tag)) if rpr is not None else (lambda tag: None)
    for name, tag in _TOGGLES.items():
        el = get(tag)
        out[name] = el is not None and (el.get(W + "val") or "true").lower() not in _OFF
    va = get("vertAlign")
    va = va.get(W + "val") if va is not None else None
    out["superscript"], out["subscript"] = va == "superscript", va == "subscript"
    c = get("color")
    c = (c.get(W + "val") or "").upper() if c is not None else ""
    out["color"] = None if c in ("", "AUTO") else c
    sz = get("sz")
    out["size"] = sz.get(W + "val") if sz is not None else None
    hl = get("highlight")
    hl = hl.get(W + "val") if hl is not None else None
    shd = get("shd")
    fill = (shd.get(W + "fill") or "auto").upper() if shd is not None else "AUTO"
    out["highlight"] = None if hl in (None, "none") and fill in ("AUTO", "FFFFFF") else (hl or fill)
    f = get("rFonts")
    out["font"] = (f.get(W + "ascii") or f.get(W + "hAnsi")) if f is not None else None
    return out


def labels(old: dict, new: dict) -> list[str]:
    """Changes between two property sets: "bold+", "color-", "size~", ..."""
    out = []
    for k in old:
        a, b = old[k], new[k]
        if a == b:
            continue
        if isinstance(a, bool):
            out.append(k + ("+" if b else "-"))
        else:
            out.append(k + ("-" if b is None else "+" if a is None else "~"))
    return out


_PPR_PROPS = {"jc": "jc", "spacing": "spacing", "ind": "ind"}


def ppr_props(ppr) -> dict:
    out = {}
    for name, tag in _PPR_PROPS.items():
        el = ppr.find(W + tag) if ppr is not None else None
        out[name] = None if el is None else tuple(sorted((k.split("}")[-1], v) for k, v in el.attrib.items()))
    return out


def _own(r, p) -> tuple[bool, bool] | None:
    """(inside w:ins, inside w:del) for a run of paragraph p; None for runs of nested paragraphs."""
    ins = dele = False
    for a in r.iterancestors():
        if a is p:
            return ins, dele
        if a.tag in (W + "ins", W + "moveTo"):
            ins = True
        elif a.tag in (W + "del", W + "moveFrom"):
            dele = True
        elif a.tag in (W + "p", _FALLBACK):
            return None
    return None


def _run_text(r) -> str:
    out = []
    for c in r:
        if c.tag in (W + "t", W + "delText"):
            out.append(c.text or "")
        elif c.tag == W + "tab":
            out.append("\t")
        elif c.tag in (W + "br", W + "cr"):
            out.append("\n")
    return "".join(out)


def editor_changes(docx_bytes: bytes) -> list[dict]:
    """One record per paragraph of the tracked file: its pre-edit text, the run formatting changes
    (start, end, label, text) in that text, and the paragraph property changes."""
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(docx_bytes)).read("word/document.xml"))
    out = []
    for p in root.iter(W + "p"):
        if any(a.tag == _FALLBACK for a in p.iterancestors()):
            continue
        pos, before, spans, deleted, final = 0, [], [], [], []
        for r in p.iter(W + "r"):
            own = _own(r, p)
            if own is None or own[0]:
                continue
            t = _run_text(r)
            rpr = r.find(W + "rPr")
            ch = rpr.find(W + "rPrChange") if rpr is not None else None
            if ch is not None and not own[1] and t:
                for lab in labels(rpr_props(ch.find(W + "rPr")), rpr_props(rpr)):
                    last = next((s for s in reversed(spans) if s["label"] == lab), None)
                    if last and last["end"] == pos:
                        last["end"], last["text"] = pos + len(t), last["text"] + t
                    else:
                        spans.append(dict(start=pos, end=pos + len(t), label=lab, text=t))
            if own[1] and t:
                if deleted and deleted[-1][1] == pos:
                    deleted[-1][1] = pos + len(t)
                else:
                    deleted.append([pos, pos + len(t)])
            props = None if own[1] else rpr_props(rpr)
            final += [props] * len(t)
            before.append(t)
            pos += len(t)
        ppr = p.find(W + "pPr")
        pch = ppr.find(W + "pPrChange") if ppr is not None else None
        plabels = labels(ppr_props(pch.find(W + "pPr")), ppr_props(ppr)) if pch is not None else []
        text = "".join(before)
        for lab in plabels:
            spans.append(dict(start=0, end=len(text), label="para:" + lab, text=text))
        out.append(dict(before=text, spans=spans, deleted=deleted, final=final))
    return out


def diff_spans(p, rec, shift: int) -> list[dict]:
    """The editor's run formatting changes as the difference between the original paragraph's runs
    and the tracked file's final runs, tracked or not (Word may have formatting tracking off)."""
    orig = []
    for r in docmodel.text_runs(p.element):
        orig += [rpr_props(r.find(W + "rPr"))] * len(docmodel.run_text(r))
    spans: list[dict] = []
    for i, fin in enumerate(rec["final"]):
        j = i + shift
        if fin is None or not 0 <= j < len(orig) or p.text[j].isspace():
            continue
        for lab in labels(orig[j], fin):
            last = next((s for s in reversed(spans) if s["label"] == lab), None)
            if last and (last["end"] == j or not p.text[last["end"]:j].strip()):
                last["end"], last["text"] = j + 1, p.text[last["start"]:j + 1]
            else:
                spans.append(dict(start=j, end=j + 1, label=lab, text=p.text[j]))
    return spans


def tool_spans(changes) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for c in changes:
        # A text change that keeps its text and formats it ("et al" -> italic "et al.") counts too.
        if not (c.format_only or (c.format and c.old.strip() and c.new.startswith(c.old))):
            continue
        for k, v in c.format.items():
            lab = k + ("+" if v is True else "-")
            out[c.para_id].append(dict(start=c.start, end=c.end, label=lab, text=c.old, rule=c.reason))
    return out


def context(d: dict) -> str:
    k = d["kind"]
    if d.get("front") and k in ("body", "heading", "list"):
        return "front"
    return k


def score(orig_path: str, editor_bytes: bytes, journal: str, front_sources=None, only=None,
          editor_only=False) -> list[dict]:
    """Rows (label, context, side: editor / tool / matched, text) for one article."""
    ms = docmodel.load(orig_path)
    if front_sources is not None:
        ms.front_sources = front_sources
    kinds = {d["id"]: context(d) for d in docview.document(orig_path)["paragraphs"]}
    ours = {} if editor_only else tool_spans(rules.run_all(ms, style_mod.load(journal), only=only).changes)
    editor: dict[str, list[dict]] = defaultdict(list)
    for rec in editor_changes(editor_bytes):
        editor[rec["before"].strip()].append(rec)
    rows = []
    for p in ms.paragraphs:
        key = p.text.strip()
        ctx = kinds.get(p.id, "body")
        q = editor.get(key) if key else None
        ed, gone, paired = [], [], bool(q)
        if q:
            rec = q.pop(0)
            shift = p.text.find(key) - rec["before"].find(key)
            ed = [dict(s, start=s["start"] + shift, end=s["end"] + shift) for s in rec["spans"]
                  if not DIFF or s["label"].startswith("para:")]
            if DIFF:
                ed += diff_spans(p, rec, shift)
            gone = [(a + shift, b + shift) for a, b in rec["deleted"]]
        tl = ours.get(p.id, [])
        for s in ed:
            hit = any(t["label"] == s["label"] and t["start"] < s["end"] and s["start"] < t["end"] for t in tl)
            rows.append(dict(label=s["label"], ctx=ctx, side="editor", hit=hit, text=s["text"], para=p.text))
        for t in tl:
            hit = any(t["label"] == s["label"] and t["start"] < s["end"] and s["start"] < t["end"] for s in ed)
            # Text the editor deleted (often to retype it) says nothing about its formatting.
            moot = not hit and any(a <= t["start"] and t["end"] <= b for a, b in gone)
            rows.append(dict(label=t["label"], ctx=ctx, side="moot" if moot or not paired else "tool", hit=hit, text=t["text"], para=p.text,
                             rule=t["rule"]))
    for q in editor.values():  # the editor's paragraphs no original paragraph paired with
        for rec in q:
            rows += [dict(label=s["label"], ctx="unpaired", side="unpaired", hit=False, text=s["text"],
                          para=rec["before"]) for s in rec["spans"]]
    return rows


def report(rows: list[dict], by_context: bool, title: str = "") -> None:
    agg: dict = defaultdict(Counter)
    for r in rows:
        keys = [(r["label"], "")] + ([(r["label"], r["ctx"])] if by_context else [])
        for k in keys:
            agg[k][r["side"]] += 1
            if r["hit"]:
                agg[k][r["side"] + "_hit"] += 1
    if title:
        print(title)
    print(f"  {'change':16s} {'context':10s} {'editor':>6s} {'tool':>6s} {'match':>6s} {'prec':>6s} {'recall':>6s}"
          f" {'moot':>5s} {'unpair':>6s}")
    tot = Counter()
    for (lab, ctx), c in sorted(agg.items(), key=lambda kv: (-agg[(kv[0][0], '')]['editor'] - agg[(kv[0][0], '')]['tool'],
                                                               kv[0][0], kv[0][1] != '', -kv[1]['editor'])):
        if not ctx:
            tot.update(c)
        prec = f"{c['tool_hit'] / c['tool']:.0%}" if c["tool"] else "-"
        rec = f"{c['editor_hit'] / c['editor']:.0%}" if c["editor"] else "-"
        print(f"  {lab:16s} {ctx or 'all':10s} {c['editor']:6d} {c['tool']:6d} {c['editor_hit']:6d} {prec:>6s} {rec:>6s}"
              f" {c['moot']:5d} {c['unpaired']:6d}")
    prec = f"{tot['tool_hit'] / tot['tool']:.0%}" if tot["tool"] else "-"
    rec = f"{tot['editor_hit'] / tot['editor']:.0%}" if tot["editor"] else "-"
    print(f"  {'TOTAL':16s} {'':10s} {tot['editor']:6d} {tot['tool']:6d} {tot['editor_hit']:6d} {prec:>6s} {rec:>6s}"
          f" {tot['moot']:5d} {tot['unpaired']:6d}")


def by_rule(rows: list[dict]) -> None:
    """Precision of each rule's formatting changes (moot: text the editor deleted or rewrote)."""
    agg: dict = defaultdict(Counter)
    for r in rows:
        if r["side"] in ("tool", "moot"):
            agg[(r["rule"], r["label"])][r["side"]] += 1
            agg[(r["rule"], r["label"])]["hit"] += r["hit"]
    print(f"\n  {'rule':22s} {'change':12s} {'tool':>6s} {'match':>6s} {'prec':>6s} {'moot':>5s}")
    for (rule, lab), c in sorted(agg.items()):
        prec = f"{c['hit'] / c['tool']:.0%}" if c["tool"] else "-"
        print(f"  {rule:22s} {lab:12s} {c['tool']:6d} {c['hit']:6d} {prec:>6s} {c['moot']:5d}")


def examples(rows: list[dict], label: str, n: int = 40) -> None:
    lab, _, side = label.partition("@")
    side = side or "editor"
    for r in [r for r in rows if r["label"] == lab and r["side"] == side
              and (not RULE_FILTER or r.get("rule") == RULE_FILTER)][:n]:
        mark = "HIT " if r["hit"] else "MISS"
        print(f"  {mark} [{r['ctx']:8s}] {r['text'][:60]!r:64s} | {r['para'][:90]!r}")


def manuscript_from_zip(path: str) -> bytes | None:
    z = zipfile.ZipFile(path)
    docs = [n for n in z.namelist() if n.lower().endswith(".docx")]
    named = [n for n in docs if "manuscript" in os.path.basename(n).lower()]
    pick = named or sorted(docs, key=lambda n: -z.getinfo(n).file_size)
    return z.read(pick[0]) if pick else None


def article_rows(d: str, journal: str, only=None, editor_only=False) -> list[dict] | None:
    te, zp = os.path.join(d, "te.docx"), os.path.join(d, "original.zip")
    if not (os.path.exists(te) and os.path.exists(zp)):
        return None
    src = manuscript_from_zip(zp)
    if src is None:
        return None
    with open(te, "rb") as f:
        te_bytes = f.read()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "m.docx")
        with open(path, "wb") as f:
            f.write(src)
        return score(path, te_bytes, journal, frontmatter.sources_from_zip(zp), only, editor_only)


RULE_FILTER = None
DIFF = False  # --diff: editor changes = original runs vs final runs (also untracked formatting)


def main(argv):
    global RULE_FILTER, DIFF
    DIFF = "--diff" in argv
    RULE_FILTER = argv[argv.index("--rule") + 1] if "--rule" in argv else None
    by_ctx = "--context" in argv
    ex = argv[argv.index("--examples") + 1] if "--examples" in argv else None
    only = argv[argv.index("--only") + 1].split(",") if "--only" in argv else None
    editor_only = "--editor-only" in argv
    if argv[0] == "--pair":
        orig, ed, journal = argv[1:4]
        with open(ed, "rb") as f:
            rows = score(orig, f.read(), journal, only=only, editor_only=editor_only)
        report(rows, by_ctx, os.path.basename(orig))
        if "--by-rule" in argv:
            by_rule(rows)
        if ex:
            examples(rows, ex, 400)
        return
    data, journal = argv[0], argv[1].lower()
    dirs = sorted(d for d in glob.glob(os.path.join(data, "*"))
                  if journal == "all" or os.path.basename(d).lower().startswith((journal + "_", journal + "-")))
    allrows, per_journal = [], defaultdict(list)
    for d in dirs:
        code = re.split(r"[_-]", os.path.basename(d))[0].lower() if journal == "all" else journal
        try:
            rows = article_rows(d, code, only, editor_only)
        except Exception as e:  # noqa: BLE001 - one broken file must not stop the survey
            print(f"  {os.path.basename(d)}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if rows is None:
            continue
        for r in rows:
            r["article"] = os.path.basename(d)
        allrows += rows
        per_journal[style_mod.resolve(code)].append(rows)
    report(allrows, by_ctx, f"{journal}: {len(dirs)} articles")
    if "--by-journal" in argv:
        for code, arts in sorted(per_journal.items()):
            report([r for rows in arts for r in rows], False, f"\n{code}: {len(arts)} articles")
            if "--by-rule" in argv:
                by_rule([r for rows in arts for r in rows])
    if "--consistency" in argv:  # in how many articles does the editor make each change?
        n_art = Counter()
        for lab in {r["label"] for r in allrows}:
            n_art[lab] = len({r["article"] for r in allrows if r["label"] == lab and r["side"] == "editor"})
        print("\narticles with each editor change:", dict(n_art.most_common(30)))
    if "--by-rule" in argv:
        by_rule(allrows)
    if ex:
        examples(allrows, ex, 200)


if __name__ == "__main__":
    main(sys.argv[1:])
