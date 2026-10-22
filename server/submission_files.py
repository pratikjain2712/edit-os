"""The submission's own files (title page, figures, XML, forms, ...) for the review screen's Files tab.

A job's input is a submission .zip or a single .docx (`input/` in the job folder). Files are listed from
the zip's directory and read straight out of it: nothing is extracted to disk, nested archives are listed
as plain files and never opened, and a file can only be asked for by its exact name in the listing (so no
path a client sends ever reaches the filesystem).

    list_files(input_path)            [{path, name, kind, size, media_type, view, manuscript, label}]
    read(input_path, path)            (bytes, media type, inline?) -- TIFF / BMP converted to PNG
    view(input_path, path)            a viewable form: docx paragraphs, metadata card, image / pdf / download
    metadata_card(data, ext)          the card for an XML or article_metadata.json, parsed deterministically
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import posixpath
import re
import tempfile
import zipfile
from datetime import datetime

from lxml import etree

from edit_os import docview, frontmatter, pipeline

KINDS = ("manuscript", "title_page", "figure", "table", "xml", "metadata", "form", "cover_letter", "other")
MAX_MEMBER = 300 * 1024 * 1024  # bytes; larger members are refused (zip bombs, runaway uploads)
MAX_RATIO = 400  # uncompressed / compressed; above this a member is refused

_IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp"}  # browsers show these as they are
_CONVERT = {".tif", ".tiff", ".bmp"}  # converted to PNG on the fly
_FIG_OTHER = {".pptx", ".ppt", ".eps", ".svg", ".emf", ".wmf", ".ai", ".psd", ".heic"}
_INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}
_MEDIA = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".doc": "application/msword", ".ppt": "application/vnd.ms-powerpoint", ".xls": "application/vnd.ms-excel",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".pdf": "application/pdf", ".xml": "application/xml", ".json": "application/json", ".zip": "application/zip",
}

_FORM = re.compile(r"contribut|copyright|consent|declaration|disclosure|conflict|\bcoi\b|icmje|checklist|ethic|"
                   r"approval|certificate|undertaking|\bforms?\b|signature", re.I)
_COVER = re.compile(r"cover(?:ing)?[\s_-]*letter|submission[\s_-]*letter|letter[\s_-]*to[\s_-]*(?:the[\s_-]*)?editor", re.I)
_TABLE = re.compile(r"(?<![a-z])tables?(?![a-z])|(?<![a-z])tab[\s_.-]*\d", re.I)
_FIG = re.compile(r"(?<![a-z])(?:fig(?:ure)?s?|pictures?|images?|photo(?:graph)?s?|graphs?|charts?)(?![a-z])"
                  r"|(?<![a-z])fig[\s_.-]*\d|^\d{1,2}\s*[a-z]?$", re.I)
_NUM = re.compile(r"(?<![a-z])(fig(?:ure)?s?|tables?|tab)\s*[-_.]?\s*(?:no\.?\s*)?(\d{1,2})(?!\d)\s*[-_]?\s*\(?([a-z])?\)?(?![a-z])", re.I)


class NotFound(Exception):
    pass


class Refused(Exception):
    pass


# --- listing ---------------------------------------------------------------------------------------

def _safe_member(name: str) -> bool:
    if name.endswith("/") or name.startswith("__MACOSX") or "\\" in name or "\0" in name:
        return False
    parts = name.split("/")
    base = parts[-1]
    if not base or base.startswith(".") or base in ("Thumbs.db", "desktop.ini"):
        return False
    return not name.startswith("/") and ".." not in parts and posixpath.normpath(name) == name


def _xml_roles(z: zipfile.ZipFile, names: list[str]) -> dict[str, str]:
    """What the portal XML says about the other files (firstpagefile, images and their type, qr_code)."""
    roles: dict[str, str] = {}
    for n in names:
        if not n.lower().endswith(".xml") or z.getinfo(n).file_size > 5 * 1024 * 1024:
            continue
        root = _parse(z.read(n))
        if root is None:
            continue
        for tag, role in (("firstpagefile", "title_page"), ("articlefile", "manuscript"), ("qr_code", "qr_code")):
            v = _text(root.find(tag))
            if v:
                roles[v] = role
        for img in root.iter("image"):
            f, t = _text(img.find("image_file_name")), _text(img.find("image_type")).lower()
            if f:
                roles[f] = "table" if "table" in t else "figure"
    return roles


def _kind(name: str, roles: dict[str, str], manuscript: str | None) -> str:
    base = posixpath.basename(name)
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if name == manuscript:
        return "manuscript"
    role = roles.get(base)
    if role == "qr_code":
        return "other"
    if role and role != "manuscript":
        return role
    if ext == ".xml":
        return "xml"
    if ext == ".json":
        return "metadata"
    if ext in (".docx", ".doc", ".pdf") and frontmatter._FIRST_PAGE.search(base):
        return "title_page"
    if _COVER.search(stem):
        return "cover_letter"
    if _FORM.search(stem):
        return "form"
    if _TABLE.search(stem) and ext in (".docx", ".doc", ".xlsx", ".xls", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"):
        return "table"
    if ext in _IMAGE | _CONVERT | _FIG_OTHER:
        return "figure"
    if ext == ".pdf" and _FIG.search(stem):
        return "figure"
    return "other"


def _label(name: str, kind: str) -> str:
    if kind not in ("figure", "table"):
        return {"manuscript": "Manuscript", "title_page": "Title page", "xml": "Submission XML",
                "metadata": "Article metadata", "form": "Form", "cover_letter": "Cover letter"}.get(kind, "")
    m = _NUM.search(os.path.splitext(posixpath.basename(name))[0])
    if not m:
        return "Figure" if kind == "figure" else "Table"
    word = "Table" if m.group(1).lower().startswith("tab") else "Figure"
    return f"{word} {int(m.group(2))}{(m.group(3) or '').upper()}"


def _view(ext: str) -> str:
    if ext in _IMAGE | _CONVERT:
        return "image"
    return {".docx": "docx", ".pdf": "pdf", ".xml": "metadata", ".json": "metadata"}.get(ext, "download")


def media_type(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return _MEDIA.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


_ORDER = {k: i for i, k in enumerate(("manuscript", "title_page", "figure", "table", "cover_letter", "form",
                                      "metadata", "xml", "other"))}


def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _is_zip(input_path: str) -> bool:
    """A submission .zip (as pipeline.edit decides: by name; a .docx is a zip too but is one file)."""
    return input_path.lower().endswith(".zip") and zipfile.is_zipfile(input_path)


def list_files(input_path: str) -> list[dict]:
    """The submission's files, manuscript first, then title page, figures (by number), tables, letters, forms,
    metadata and the rest."""
    base = os.path.basename(input_path)
    if not _is_zip(input_path):
        ext = os.path.splitext(base)[1].lower()
        return [_entry(base, os.path.getsize(input_path), "manuscript" if ext == ".docx" else "other", ext)]
    with zipfile.ZipFile(input_path) as z:
        names = [i.filename for i in z.infolist() if _safe_member(i.filename)]
        manuscript = pipeline.manuscript_member(z)
        roles = _xml_roles(z, names)
        out = [_entry(n, z.getinfo(n).file_size, _kind(n, roles, manuscript), os.path.splitext(n)[1].lower())
               for n in names]
    for e in out:
        if roles.get(e["name"]) == "qr_code":
            e["label"] = "QR code"
    return sorted(out, key=lambda e: (_ORDER[e["kind"]], _natural(e["label"] or ""), _natural(e["path"])))


def _entry(path: str, size: int, kind: str, ext: str) -> dict:
    view = _view(ext)
    if ext == ".docx" and kind == "manuscript":
        view = "docx"
    return {"path": path, "name": posixpath.basename(path), "kind": kind, "size": size, "ext": ext.lstrip("."),
            "media_type": media_type(path), "view": view, "manuscript": kind == "manuscript",
            "label": _label(path, kind)}


def find(input_path: str, path: str) -> dict:
    """The listed file named PATH exactly; NotFound for anything else (traversal, nested names, unknown)."""
    for e in list_files(input_path):
        if e["path"] == path:
            return e
    raise NotFound(path)


# --- reading ---------------------------------------------------------------------------------------

def _read_member(input_path: str, path: str) -> bytes:
    if not _is_zip(input_path):
        if path != os.path.basename(input_path):
            raise NotFound(path)
        with open(input_path, "rb") as f:
            return f.read()
    with zipfile.ZipFile(input_path) as z:
        info = z.getinfo(path)
        if info.file_size > MAX_MEMBER or (info.compress_size and info.file_size / info.compress_size > MAX_RATIO
                                           and info.file_size > 10 * 1024 * 1024):
            raise Refused(f"{path}: too large to open here")
        with z.open(info) as f:
            data = f.read(MAX_MEMBER + 1)
        if len(data) > MAX_MEMBER:
            raise Refused(f"{path}: too large to open here")
        return data


def to_png(data: bytes) -> bytes:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)
            if im.mode in ("I;16", "I;16B", "I;16L", "I"):
                im = im.point(lambda v: v / 256).convert("L")
            elif im.mode not in ("RGB", "RGBA", "L", "LA", "P", "1"):
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, "PNG", optimize=False)
            return out.getvalue()
    except Exception as e:  # PIL raises many kinds (incl. DecompressionBombError)
        raise Refused(f"cannot convert the image: {type(e).__name__}") from e


def read(input_path: str, path: str, convert: bool = True) -> tuple[bytes, str, bool, str]:
    """(content, media type, inline?, download name) for one listed file; TIFF / BMP come back as PNG
    unless CONVERT is false (the original, as a download)."""
    e = find(input_path, path)
    data = _read_member(input_path, e["path"])
    name, ext = e["name"], "." + e["ext"]
    if ext in _CONVERT and convert:
        data, media, name = to_png(data), "image/png", os.path.splitext(name)[0] + ".png"
    else:
        media = e["media_type"]
    return data, media, media in _INLINE and convert, name


# --- viewing ---------------------------------------------------------------------------------------

def view(input_path: str, path: str) -> dict:
    e = find(input_path, path)
    out = {"file": e, "view": e["view"]}
    if e["view"] == "docx":
        data = _read_member(input_path, e["path"])
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "file.docx")
            with open(p, "wb") as f:
                f.write(data)
            try:
                out.update(docview.document(p))
            except Exception as ex:  # a damaged .docx: offer the download instead
                out.update(view="download", error=f"cannot read this .docx ({type(ex).__name__})")
    elif e["view"] == "metadata":
        card = metadata_card(_read_member(input_path, e["path"]), "." + e["ext"])
        if card is None:
            out.update(view="download", error="cannot read this file")
        else:
            out["card"] = card
    return out


def _parse(data: bytes):
    try:
        return etree.fromstring(data, etree.XMLParser(recover=True, resolve_entities=False, no_network=True,
                                                      huge_tree=False))
    except etree.XMLSyntaxError:
        return None


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _keywords(s: str) -> list[str]:
    return [k.strip(" .") for k in re.split(r"[;,\n]|\s{2,}", s or "") if k.strip(" .")]


def _iso(d) -> str:
    return d.strftime("%Y-%m-%d") if isinstance(d, datetime) else str(d)


def _name_case(name: str) -> str:
    """Portal exports often shout ("DHEERAJ KUMAR YADAV"); show such a name in name case."""
    if not name.isupper():
        return name
    return " ".join("-".join(p.capitalize() for p in w.split("-")) if len(w) > 1 else w for w in name.split())


def _join(*parts) -> str:
    return ", ".join(p.strip() for p in parts if p and p.strip())


def _card() -> dict:
    return {"source": "", "article_type": "", "title": "", "running_title": "", "article_id": "", "journal": "",
            "doi": "", "keywords": [], "word_count": "", "authors": [], "corresponding": None, "dates": [],
            "figures": [], "extra": []}


def metadata_card(data: bytes, ext: str) -> dict | None:
    """Article type, title, authors with affiliations, corresponding author, dates, keywords: from the
    portal XML, a JATS XML or article_metadata.json. Deterministic; None when the file cannot be read."""
    if ext == ".json":
        return _json_card(data)
    root = _parse(data)
    if root is None:
        return None
    m = frontmatter.parse_xml(data)
    c = _card()
    c.update(article_type=m.article_type, title=m.title, running_title=m.running_title)
    c["dates"] = [{"label": k.capitalize(), "date": _iso(m.dates[k])} for k in ("received", "revised", "accepted")
                  if k in m.dates]
    if root.tag == "article":  # JATS
        c["source"] = "JATS XML"
        c["doi"] = _text(root.find(".//article-id[@pub-id-type='doi']"))
        c["journal"] = _text(root.find(".//journal-title"))
        c["keywords"] = [_text(k) for k in root.iter("kwd") if _text(k)]
        for a in m.authors:
            aff = a.get("aff") or {}
            c["authors"].append({"name": " ".join(x for x in (a.get("given"), a.get("surname")) if x),
                                 "prefix": a.get("prefix", ""), "affiliation": _join(*aff.values()),
                                 "email": a.get("email", ""), "corresponding": bool(a.get("corresponding"))})
    else:  # the submission portal's XML
        c["source"] = "Submission XML"
        c["article_id"] = _text(root.find("id"))
        c["doi"] = _text(root.find("doi"))
        c["keywords"] = _keywords(_text(root.find("keywords")))
        c["word_count"] = _text(root.find("wordCount"))
        phase = _text(root.find("CurrentPhase"))
        if phase:
            c["extra"].append({"label": "Phase", "value": phase})
        trial = _text(root.find("trial_registration_no"))
        if trial:
            c["extra"].append({"label": "Trial registration", "value": trial})
        for a, el in zip(m.authors, root.iter("author")):
            aff = a.get("aff") or {}
            author = {"name": _name_case(" ".join(x for x in (a.get("given"), a.get("surname")) if x)), "prefix": a.get("prefix", ""),
                      "affiliation": _join(aff.get("department", ""), aff.get("institution", ""), aff.get("city", ""),
                                           aff.get("state", ""), aff.get("country", "")),
                      "email": a.get("email", ""), "corresponding": bool(a.get("corresponding")),
                      "orcid": _text(el.find("orcid"))}
            c["authors"].append(author)
            if author["corresponding"] and c["corresponding"] is None:
                t = lambda tag: _text(el.find(tag))  # noqa: E731
                addr = [t("corresponding_address_1"), t("corresponding_address_2"), t("corresponding_address_3")]
                place = [t("corresponding_city"), t("corresponding_state"), t("corresponding_zip"), t("corresponding_country")]
                if not any(addr):
                    addr = [t("department"), t("institute"), t("address"), t("address_2"), t("address_3")]
                    place = [t("city"), t("state"), t("zip"), t("country")]
                c["corresponding"] = {"name": author["name"], "prefix": author["prefix"], "email": author["email"],
                                      "address": _join(*addr, *place)}
        for img in root.iter("image"):
            f = _text(img.find("image_file_name"))
            if f:
                c["figures"].append({"file": f, "type": _text(img.find("image_type")), "legend": _text(img.find("legend"))})
    if c["corresponding"] is None:
        ca = next((a for a in c["authors"] if a["corresponding"]), None)
        if ca:
            c["corresponding"] = {"name": ca["name"], "prefix": ca.get("prefix", ""), "email": ca["email"], "address": ca["affiliation"]}
    return c


def _json_card(data: bytes) -> dict | None:
    try:
        d = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict):
        return None
    c = _card()
    c["source"] = "Article metadata"
    s = lambda *keys: next((str(d[k]).strip() for k in keys if isinstance(d.get(k), (str, int, float)) and str(d[k]).strip()), "")  # noqa: E731
    c.update(article_type=s("article_type", "articletype", "type"), title=s("title", "article_title"),
             running_title=s("running_title", "short_title"), article_id=s("article_id", "id", "manuscript_id"),
             journal=s("journal", "journal_code", "journal_name"), doi=s("doi"), word_count=s("word_count", "wordCount"))
    kw = d.get("keywords")
    c["keywords"] = [str(k).strip() for k in kw if str(k).strip()] if isinstance(kw, list) else _keywords(s("keywords"))
    authors = d.get("authors") if isinstance(d.get("authors"), list) else []

    def order(a):  # "1", "2", ... or a word for the first author ("Primary")
        o = str(a.get("author_order") or "").strip().lower()
        if o in ("primary", "first", "main"):
            return 0
        return int(o) if o.isdigit() else 10 ** 6
    for a in sorted((a for a in authors if isinstance(a, dict)), key=order):
        name = str(a.get("name") or " ".join(str(a.get(k) or "") for k in ("given", "surname")).strip())
        prefix = ""
        pm = re.match(r"(?i)^(dr|prof|mr|mrs|ms)\.?\s+", name)
        if pm:
            prefix, name = pm.group(1).capitalize(), name[pm.end():]
        name = _name_case(name)
        corr = a.get("corresponding_author", a.get("corresponding"))
        c["authors"].append({"name": name, "prefix": prefix, "affiliation": str(a.get("affiliation") or ""),
                             "email": str(a.get("email") or ""), "corresponding": corr is True or str(corr).lower() in ("y", "yes", "true", "1"),
                             "orcid": str(a.get("orcid") or "")})
    ca = next((a for a in c["authors"] if a["corresponding"]), None)
    if ca:
        c["corresponding"] = {"name": ca["name"], "prefix": ca["prefix"], "email": ca["email"], "address": ca["affiliation"]}
    files = d.get("files") if isinstance(d.get("files"), dict) else {}
    for k, label in (("manuscript_file", "Manuscript file"), ("fp_file", "Title page file")):
        if isinstance(files.get(k), str) and files[k] and files[k].lower() != "missing":
            c["extra"].append({"label": label, "value": files[k]})
    for k, label in (("source", "Source"), ("status", "Export status")):
        if s(k):
            c["extra"].append({"label": label, "value": s(k)})
    return c
