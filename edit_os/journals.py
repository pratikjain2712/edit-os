"""Journal names and abbreviations from NLM's list of journals (J_Medline.txt).

The list is downloaded once into a local SQLite file (`python -m edit_os journals update`)
and looked up by full title, abbreviation (with or without periods) or ISSN. Abbreviations
come only from this list or a PubMed record, never from a model.
"""

from __future__ import annotations

import os
import re
import sqlite3
import urllib.request
from dataclasses import dataclass

NLM_URL = "https://ftp.ncbi.nlm.nih.gov/pubmed/J_Medline.txt"
DEFAULT_DB = os.path.join(os.environ.get("EDIT_OS_DATA", os.path.expanduser("~/.cache/edit-os")), "journals.sqlite")


@dataclass
class Journal:
    nlm_id: str
    title: str
    med_abbr: str
    iso_abbr: str
    issn_print: str
    issn_online: str


def normalise(name: str, keep_qualifiers: bool = False) -> str:
    s = name.lower().replace("&", " and ")
    if not keep_qualifiers:
        s = re.sub(r"\([^)]*\)", " ", s)  # "(Amsterdam, Netherlands)" qualifiers
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"^\s*the\s+", "", s)
    return " ".join(s.split())


def parse_nlm(text: str) -> list[Journal]:
    out = []
    for block in text.split("-" * 56):
        fields = dict(re.findall(r"^(\w[\w ()]*?):\s?(.*)$", block, re.M))
        if fields.get("NlmId"):
            out.append(Journal(
                nlm_id=fields["NlmId"].strip(), title=fields.get("JournalTitle", "").strip(),
                med_abbr=fields.get("MedAbbr", "").strip(), iso_abbr=fields.get("IsoAbbr", "").strip(),
                issn_print=fields.get("ISSN (Print)", "").strip(), issn_online=fields.get("ISSN (Online)", "").strip(),
            ))
    return out


def build(journals: list[Journal], db_path: str = DEFAULT_DB) -> int:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    tmp = db_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE journal (nlm_id TEXT PRIMARY KEY, title TEXT, med_abbr TEXT, iso_abbr TEXT,
                              issn_print TEXT, issn_online TEXT);
        CREATE TABLE name (key TEXT, nlm_id TEXT, kind TEXT);
        CREATE INDEX name_key ON name(key);
    """)
    for j in journals:
        con.execute("INSERT OR REPLACE INTO journal VALUES (?,?,?,?,?,?)",
                    (j.nlm_id, j.title, j.med_abbr, j.iso_abbr, j.issn_print, j.issn_online))
        keys = {(normalise(j.title), "title"), (normalise(j.title.split(" : ")[0]), "title"),
                (normalise(j.med_abbr), "abbr"), (normalise(j.iso_abbr), "abbr")}
        keys |= {(i.upper(), "issn") for i in (j.issn_print, j.issn_online) if i}
        con.executemany("INSERT INTO name VALUES (?,?,?)", [(k, j.nlm_id, kind) for k, kind in keys if k])
    con.commit()
    con.close()
    os.replace(tmp, db_path)
    return len(journals)


def update(db_path: str = DEFAULT_DB, url: str = NLM_URL) -> int:
    with urllib.request.urlopen(url, timeout=120) as r:
        text = r.read().decode("utf-8", errors="replace")
    return build(parse_nlm(text), db_path)


class Directory:
    """Lookup over the local NLM journal database."""

    def __init__(self, db_path: str = DEFAULT_DB):
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"{db_path} not found; run: python -m edit_os journals update")
        self.con = sqlite3.connect(db_path, check_same_thread=False)  # shared by worker threads (serialized SQLite)
        self._cache: dict[str, list[Journal]] = {}

    def _get(self, nlm_id: str) -> Journal:
        return Journal(*self.con.execute("SELECT * FROM journal WHERE nlm_id = ?", (nlm_id,)).fetchone())

    def find(self, name: str) -> list[Journal]:
        """All journals whose title, abbreviation or ISSN matches `name` exactly (after normalising)."""
        name = name.strip()
        key = name.upper() if re.fullmatch(r"\d{4}-\d{3}[\dXx]", name) else normalise(name)
        if key not in self._cache:
            ids = {r[0] for r in self.con.execute("SELECT nlm_id FROM name WHERE key = ?", (key,))}
            self._cache[key] = [self._get(i) for i in sorted(ids)]
        return self._cache[key]

    def lookup(self, name: str) -> Journal | None:
        """The one journal matching `name`, or None when there is no match or it is ambiguous."""
        hits = self.find(name)
        if len(hits) == 1:
            return hits[0]
        # Prefer the journal whose name matches including any qualifier: "Indian J Ophthalmol"
        # is the current journal, not "Indian J Ophthalmol (Poona City)".
        exact = normalise(name, keep_qualifiers=True)
        strict = [h for h in hits if exact in {normalise(h.med_abbr, True), normalise(h.iso_abbr, True),
                                               normalise(h.title, True)}]
        if len(strict) == 1:
            return strict[0]
        if "(" not in name:
            plain = [h for h in (strict or hits) if "(" not in h.med_abbr]
            if len(plain) == 1:
                return plain[0]
        # Several journals share a title or abbreviation: keep it only if they all abbreviate the same way.
        if hits and len({h.med_abbr for h in hits}) == 1:
            return hits[0]
        return None


_directory: Directory | None = None


def directory() -> Directory | None:
    """Shared Directory, or None when the database has not been downloaded."""
    global _directory
    if _directory is None and os.path.exists(DEFAULT_DB):
        _directory = Directory(DEFAULT_DB)
    return _directory
