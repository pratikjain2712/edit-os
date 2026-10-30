"""Last pass over a rendered .docx: package rules Word enforces when it opens a file (LibreOffice does not).

- A chart part may be used by one drawing only. A figure moved as a tracked change keeps its deleted original and
  gains an inserted copy, both pointing at the same chart (JEHP_73_26: Word refused the whole file, "Word
  experienced an error trying to open the file"). Each further use gets its own copy of the chart part, its
  relationships and the internal parts they name (an embedded workbook, chart style and colours).
- Drawing ids (wp:docPr id) are unique in the document.
- Two tables never touch: Word joins tables with no paragraph between them into one when it shows the file
  (tables moved to the end one after another: JEHP_1602_26, JEHP_1708_26). An empty paragraph goes between them,
  as the editors leave one; in the tracked copy it is a tracked insertion.
- A chart's link to the author's own spreadsheet ("file:///C:/Users/.../data.xlsx") goes: nobody else can open
  it, and Word's Protected View refused such a file until it was unblocked (JEHP_73_26). The chart keeps its
  saved values and still draws; only the dead link back to the author's computer is removed.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import tempfile
import zipfile

_CHART_REF = re.compile(r'(<c:chart\b[^>]*?\br:id=")([^"]+)(")')
_DOCPR = re.compile(r'(<wp:docPr\b[^>]*?\bid=")(\d+)(")')
_REL = re.compile(r"<Relationship\b[^>]*/>")


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _attrs(tag: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)="([^"]*)"', tag))


def _rels_path(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", name + ".rels")


def _new_name(names: set[str], part: str) -> str:
    folder, name = posixpath.split(part)
    stem, ext = posixpath.splitext(name)
    base = re.sub(r"\d+$", "", stem)
    n = 1
    while posixpath.join(folder, f"{base}{n}{ext}") in names:
        n += 1
    return posixpath.join(folder, f"{base}{n}{ext}")


def _content_type(ct: str, part: str) -> str | None:
    m = re.search(r'<Override\b[^>]*PartName="/%s"[^>]*ContentType="([^"]+)"' % re.escape(part), ct)
    return m.group(1) if m else None


def fix(path: str) -> dict:
    """Rewrite PATH in place when needed; {"charts_copied": n, "drawing_ids_renumbered": n}."""
    with zipfile.ZipFile(path) as z:
        parts = {n: z.read(n) for n in z.namelist()}
        order = z.namelist()
    doc = parts.get("word/document.xml", b"").decode("utf-8")
    rels_name = "word/_rels/document.xml.rels"
    rels = parts.get(rels_name, b"").decode("utf-8")
    ct = parts.get("[Content_Types].xml", b"").decode("utf-8")
    report = {"charts_copied": 0, "drawing_ids_renumbered": 0, "local_links_removed": 0, "tables_separated": 0}
    by_id = {a["Id"]: a for a in (_attrs(t) for t in _REL.findall(rels)) if "Id" in a}

    # 1. one drawing per chart part
    seen: set[str] = set()
    out, pos = [], 0
    ids = set(by_id)
    for m in _CHART_REF.finditer(doc):
        rid = m.group(2)
        rel = by_id.get(rid)
        if rid not in seen or rel is None or rel.get("TargetMode") == "External":
            seen.add(rid)
            continue
        src = posixpath.normpath(posixpath.join("word", rel["Target"]))
        if src not in parts:
            continue
        names = set(parts)
        new = _new_name(names, src)
        parts[new] = parts[src]
        order.append(new)
        ctype = _content_type(ct, src)
        if ctype:
            ct = ct.replace("</Types>", f'<Override PartName="/{new}" ContentType="{ctype}"/></Types>')
        # the chart's own relationships: internal targets (embedded workbook, style, colours) copied too
        src_rels = _rels_path(src)
        if src_rels in parts:
            crels = parts[src_rels].decode("utf-8")
            for t in _REL.findall(crels):
                a = _attrs(t)
                if a.get("TargetMode") == "External" or "Target" not in a:
                    continue
                inner = posixpath.normpath(posixpath.join(posixpath.dirname(src), a["Target"]))
                if inner not in parts:
                    continue
                inner_new = _new_name(set(parts), inner)
                parts[inner_new] = parts[inner]
                order.append(inner_new)
                itype = _content_type(ct, inner)
                if itype:
                    ct = ct.replace("</Types>", f'<Override PartName="/{inner_new}" ContentType="{itype}"/></Types>')
                rel_target = posixpath.relpath(inner_new, posixpath.dirname(new))
                crels = crels.replace(t, t.replace(f'Target="{a["Target"]}"', f'Target="{rel_target}"'))
            parts[_rels_path(new)] = crels.encode("utf-8")
            order.append(_rels_path(new))
        n = 1
        while f"rId{n}" in ids:
            n += 1
        new_id = f"rId{n}"
        ids.add(new_id)
        target = posixpath.relpath(new, "word")
        rels = rels.replace("</Relationships>",
                            f'<Relationship Id="{new_id}" Type="{rel["Type"]}" Target="{target}"/></Relationships>')
        out.append(doc[pos:m.start(2)] + new_id)
        pos = m.end(2)
        report["charts_copied"] += 1
    if report["charts_copied"]:
        out.append(doc[pos:])
        doc = "".join(out)

    # 2. unique drawing ids
    used: set[int] = set()
    top = max((int(m.group(2)) for m in _DOCPR.finditer(doc)), default=0)

    def renumber(m):
        nonlocal top
        n = int(m.group(2))
        if n in used:
            top += 1
            n = top
            report["drawing_ids_renumbered"] += 1
        used.add(n)
        return f"{m.group(1)}{n}{m.group(3)}"

    doc = _DOCPR.sub(renumber, doc)

    # 4. no two tables touching
    touching = re.compile(r"(</w:tbl>)(\s*)(<w:tbl>|<w:tbl\s)")
    if touching.search(doc):
        tracked = "<w:ins " in doc or "<w:del " in doc
        next_id = max((int(i) for i in re.findall(r'\bw:id="(\d+)"', doc)), default=0) + 1

        def separate(m):
            nonlocal next_id
            report["tables_separated"] += 1
            if not tracked:
                return m.group(1) + "<w:p/>" + m.group(3)
            p = (f'<w:p><w:pPr><w:rPr><w:ins w:id="{next_id}" w:author="Edit OS" w:date="{_now()}"/></w:rPr>'
                 f"</w:pPr></w:p>")
            next_id += 1
            return m.group(1) + p + m.group(3)

        doc = touching.sub(separate, doc)

    # 3. charts linked to a file on the author's computer
    for name in [n for n in parts if re.fullmatch(r"word/charts/_rels/[^/]+\.xml\.rels", n)]:
        crels = parts[name].decode("utf-8")
        local = [_attrs(t) for t in _REL.findall(crels)
                 if _attrs(t).get("TargetMode") == "External" and _attrs(t).get("Target", "").lower().startswith("file:")]
        if not local:
            continue
        chart = posixpath.join(posixpath.dirname(posixpath.dirname(name)), posixpath.basename(name)[:-len(".rels")])
        xml = parts.get(chart, b"").decode("utf-8")
        for a in local:
            rid = a["Id"]
            xml = re.sub(r'<c:externalData\b[^>]*\br:id="%s"[^>]*(?:/>|>.*?</c:externalData>)' % re.escape(rid), "", xml,
                         flags=re.S)
            crels = re.sub(r'<Relationship\b[^>]*\bId="%s"[^>]*/>' % re.escape(rid), "", crels)
            report["local_links_removed"] += 1
        parts[chart] = xml.encode("utf-8")
        parts[name] = crels.encode("utf-8")

    if not any(report.values()):
        return report
    parts["word/document.xml"] = doc.encode("utf-8")
    parts[rels_name] = rels.encode("utf-8")
    parts["[Content_Types].xml"] = ct.encode("utf-8")
    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=os.path.dirname(os.path.abspath(path)))
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for n in dict.fromkeys(["[Content_Types].xml"] + order):
            z.writestr(n, parts[n])
    shutil.move(tmp, path)
    return report
