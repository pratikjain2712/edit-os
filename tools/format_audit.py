"""Formatting audit of a delivered .docx: spacing and layout, element by element.

For every block in the body (paragraph or table) in order, its kind (article type, title, authors, H1/H2/H3,
abstract, keywords, body text, caption, table, blank line, ...), then per kind: blank lines before and after,
paragraph spacing before / after (pt), line spacing, first-line indent, alignment, font, size, bold / italic.
Compared with the stylesheet / guide where they speak (blank lines around the title and headings) and with the
editors' own files where they are silent (`--against te|ce` reads the article folder's te.docx / ce.docx).

Usage:
    python tools/format_audit.py FILE.docx [--journal jehp]
    python tools/format_audit.py --pairs OUT_DIR ARTICLE_DIR... --journal jehp --against te
        (OUT_DIR/<article>/*_clean.docx against ARTICLE_DIR/te.docx)
Manuscripts are client-confidential: the report shows kinds and numbers, never the text.
"""

from __future__ import annotations

import argparse
import collections
import glob
import io
import os
import re
import sys
import zipfile

from lxml import etree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# What the stylesheets (S) and the pre-editing guide (G) ask: blank lines before / after; None = not stated.
RULES = {
    "title": (1, 1),        # G39 "Empty paragraph break ... before and after document title"
    "H1": (1, 1),           # S "H1 ... add an extra return ... before and after heading" (G134 too)
    "H2": (1, 0),           # S "H2 ... single line space before and not after" (wins over G134 "before and after")
    "H3": (1, 0),           # S "H3 ... single line space before and not after"
}


def _accept_all(xml: bytes) -> etree._Element:
    """The document as it reads with every tracked change accepted (deleted runs and paragraphs gone)."""
    root = etree.fromstring(xml)
    for d in list(root.iter(W + "del")):
        par = d.getparent()
        if par is not None and etree.QName(par).localname != "rPr":
            par.remove(d)
    for p in list(root.iter(W + "p")):  # a paragraph whose mark is deleted joins the next: drop it when empty
        rpr = p.find(W + "pPr/" + W + "rPr")
        if rpr is not None and rpr.find(W + "del") is not None and not "".join(p.itertext()).strip():
            p.getparent().remove(p)
    for d in list(root.iter(W + "moveFrom")):
        d.getparent().remove(d)
    return root


def _text(e) -> str:
    return "".join(t.text or "" for t in e.iter(W + "t"))


def _styles(z: zipfile.ZipFile) -> dict:
    """styleId -> merged pPr/rPr facts (spacing, size, bold...) along basedOn, plus the document defaults."""
    try:
        s = etree.fromstring(z.read("word/styles.xml"))
    except KeyError:
        return {}
    raw = {}
    for st in s.iter(W + "style"):
        raw[st.get(W + "styleId")] = st
    defaults = {}
    dd = s.find(W + "docDefaults")
    if dd is not None:
        defaults = _facts(dd.find(W + "pPrDefault/" + W + "pPr"), dd.find(W + "rPrDefault/" + W + "rPr"))

    def merged(sid, seen=()):
        st = raw.get(sid)
        if st is None or sid in seen:
            return dict(defaults)
        base = st.find(W + "basedOn")
        out = merged(base.get(W + "val"), seen + (sid,)) if base is not None else dict(defaults)
        out.update({k: v for k, v in _facts(st.find(W + "pPr"), st.find(W + "rPr")).items() if v is not None})
        return out

    out = {sid: merged(sid) for sid in raw}
    default_para = next((st.get(W + "styleId") for st in raw.values()
                         if st.get(W + "type") == "paragraph" and st.get(W + "default") == "1"), None)
    out[None] = out.get(default_para, dict(defaults))
    return out


def _facts(ppr, rpr) -> dict:
    f = {}
    if ppr is not None:
        sp = ppr.find(W + "spacing")
        if sp is not None:
            for k, a in (("before", "before"), ("after", "after"), ("line", "line")):
                if sp.get(W + a) is not None:
                    f[k] = int(sp.get(W + a))
            if sp.get(W + "lineRule") is not None:
                f["lineRule"] = sp.get(W + "lineRule")
            if sp.get(W + "beforeAutospacing") == "1":
                f["before"] = "auto"
            if sp.get(W + "afterAutospacing") == "1":
                f["after"] = "auto"
        if ppr.find(W + "contextualSpacing") is not None:
            f["contextual"] = ppr.find(W + "contextualSpacing").get(W + "val", "1") not in ("0", "false")
        ind = ppr.find(W + "ind")
        if ind is not None:
            for a in ("firstLine", "hanging", "left", "start"):
                if ind.get(W + a) is not None:
                    f["ind_" + a] = int(ind.get(W + a))
        jc = ppr.find(W + "jc")
        if jc is not None:
            f["jc"] = jc.get(W + "val")
    if rpr is not None:
        sz = rpr.find(W + "sz")
        if sz is not None:
            f["sz"] = int(sz.get(W + "val")) / 2
        for k in ("b", "i"):
            e = rpr.find(W + k)
            if e is not None:
                f[k] = e.get(W + "val", "1") not in ("0", "false")
        rf = rpr.find(W + "rFonts")
        if rf is not None and rf.get(W + "ascii"):
            f["font"] = rf.get(W + "ascii")
    return f


def _para_facts(p, styles) -> dict:
    ppr = p.find(W + "pPr")
    sid = ppr.find(W + "pStyle").get(W + "val") if ppr is not None and ppr.find(W + "pStyle") is not None else None
    f = dict(styles.get(sid, styles.get(None, {})))
    f.update({k: v for k, v in _facts(ppr, None).items() if v is not None})
    runs = [r for r in p.iter(W + "r") if _text(r).strip()]
    if runs:
        rf = [_facts(None, r.find(W + "rPr")) for r in runs]
        for k in ("sz", "b", "i", "font"):
            vals = collections.Counter(x.get(k, f.get(k)) for x in rf)
            f[k] = vals.most_common(1)[0][0]
    return f


def kinds(body, front_end: int) -> list[str]:
    out = []
    seen_title = False
    for k, e in enumerate(body):
        tag = etree.QName(e).localname
        if tag == "tbl":
            out.append("table")
            continue
        if tag != "p":
            out.append(tag)
            continue
        t = _text(e).strip()
        if not t:
            out.append("graphic" if e.find(".//" + W + "drawing") is not None else "blank")
            continue
        m = re.match(r"<(H\d)>", t)
        if m:
            out.append(m.group(1))
        elif re.match(r"<RH>", t):
            out.append("running head")
        elif re.match(r"(Technical Editing|Copyediting) done|Query:|AQ\d*:", t, re.I):
            out.append("sign-off/notes")
        elif re.fullmatch(r"(Original|Review|Case|Short|Brief|Letter|Editorial|Research|Systematic)\b.{0,40}", t) \
                and k < front_end:
            out.append("article type")
        elif k < front_end and not seen_title and len(t) > 25 and not re.match(r"(Address|E-?mail|How to cite)", t):
            out.append("title")
            seen_title = True
        elif re.match(r"(Table|Figure|Fig\.?|Graph) \d+[.:]", t):
            out.append("caption")
        elif re.match(r"Keywords?\s*:", t, re.I):
            out.append("keywords")
        elif re.match(r"(Abstract|ABSTRACT)\s*$", t):
            out.append("abstract heading")
        elif k < front_end:
            out.append("front matter")
        else:
            out.append("body text")
    return out


def audit(xml: bytes, styles: dict) -> dict:
    body = _accept_all(xml).find(W + "body")
    blocks = [e for e in body if etree.QName(e).localname in ("p", "tbl")]
    texts = [_text(e).strip() for e in blocks]
    front_end = next((k for k, t in enumerate(texts) if re.match(r"(Abstract|ABSTRACT)\b", t)), min(25, len(blocks)))
    ks = kinds(blocks, front_end)
    per = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    for k, (e, kind) in enumerate(zip(blocks, ks)):
        if kind == "blank":
            continue
        before = 0
        j = k - 1
        while j >= 0 and ks[j] == "blank":
            before, j = before + 1, j - 1
        after = 0
        j = k + 1
        while j < len(ks) and ks[j] == "blank":
            after, j = after + 1, j + 1
        d = per[kind]
        d["n"]["n"] += 1
        d["blank lines before"][min(before, 2)] += 1
        d["blank lines after"][min(after, 2)] += 1
        if kind == "table":
            d["next block"][ks[j] if j < len(ks) else "end"] += 1
            prev = k - 1
            while prev >= 0 and ks[prev] == "blank":
                prev -= 1
            d["previous block"][ks[prev] if prev >= 0 else "start"] += 1
            continue
        f = _para_facts(e, styles)
        tw = lambda v: v if v in (None, "auto") else round(v / 20)  # noqa: E731 - twips -> pt
        d["space before (pt)"][tw(f.get("before", 0))] += 1
        d["space after (pt)"][tw(f.get("after", 0))] += 1
        rule = f.get("lineRule", "auto")
        line = f.get("line")
        d["line spacing"][("%.2g×" % (line / 240)) if line and rule == "auto" else (f"{tw(line)} pt" if line else "single")] += 1
        d["first-line indent (pt)"][tw(f.get("ind_firstLine", 0)) - tw(f.get("ind_hanging", 0))] += 1
        d["align"][f.get("jc") or "left"] += 1
        d["size"][f.get("sz")] += 1
        d["bold"][bool(f.get("b"))] += 1
        d["italic"][bool(f.get("i"))] += 1
        d["font"][f.get("font")] += 1
    return {k: {m: dict(c) for m, c in v.items()} for k, v in per.items()}


def load(path: str) -> tuple[bytes, dict]:
    z = zipfile.ZipFile(path)
    return z.read("word/document.xml"), _styles(z)


def show(name: str, rep: dict, other: dict | None = None, other_name: str = "") -> None:
    order = ["article type", "title", "front matter", "abstract heading", "keywords", "H1", "H2", "H3", "body text",
             "caption", "table", "graphic", "running head", "sign-off/notes"]
    print(f"== {name}" + (f"   (vs {other_name})" if other else ""))
    for kind in [k for k in order if k in rep] + [k for k in rep if k not in order]:
        d = rep[kind]
        rule = RULES.get(kind)
        print(f"  {kind}  ({d['n']['n']})" + (f"  rule: {rule[0]} blank before, {rule[1]} after" if rule else ""))
        for metric, c in d.items():
            if metric == "n":
                continue
            o = (other or {}).get(kind, {}).get(metric)
            fmt = lambda x: ", ".join(f"{k}: {v}" for k, v in sorted(x.items(), key=lambda kv: -kv[1]))  # noqa: E731
            print(f"     {metric:24} ours {fmt(c):40}" + (f" | editor {fmt(o)}" if o else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--pairs", help="OUT_DIR holding <article>/*_clean.docx; the paths are article folders")
    ap.add_argument("--against", default="te", choices=["te", "ce"])
    ap.add_argument("--journal")
    a = ap.parse_args(argv)
    if a.pairs:
        tot_o, tot_e = collections.defaultdict(lambda: collections.defaultdict(collections.Counter)), \
            collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
        for adir in a.paths:
            art = os.path.basename(adir.rstrip("/"))
            ours = glob.glob(os.path.join(a.pairs, art, "*_clean.docx"))
            ed = os.path.join(adir, f"{a.against}.docx")
            if not ours or not os.path.exists(ed):
                continue
            ro, re_ = audit(*load(ours[0])), audit(*load(ed))
            for tot, r in ((tot_o, ro), (tot_e, re_)):
                for kind, d in r.items():
                    for m, c in d.items():
                        tot[kind][m].update(c)
        show(f"{len(a.paths)} articles, ours (clean copy)", {k: dict(v) for k, v in tot_o.items()},
             {k: dict(v) for k, v in tot_e.items()}, f"editor {a.against}.docx")
        return 0
    for p in a.paths:
        show(os.path.basename(p), audit(*load(p)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
