"""LanguageTool, self-hosted: download, start on 127.0.0.1, check paragraphs, keep the safe fixes.

LanguageTool (LGPL) runs as a local Java HTTP server; no text leaves the machine.

    python -m edit_os.copyedit languagetool install   # download LanguageTool into ~/.cache/edit-os/
    python -m edit_os.copyedit languagetool start     # run the server on 127.0.0.1:8081 (foreground)
    python -m edit_os.copyedit languagetool status

or from Python, `langtool.start_server()` (background process; returns the Popen).

Matches become `Change` records only for high-precision rules (configured under
`copyedit.languagetool` in the journal style) that offer exactly one replacement. Everything
else is ignored, or kept as an editor warning when its category is listed in `warn_categories`.
Results are cached per paragraph text in ~/.cache/edit-os/languagetool_cache.sqlite.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Callable

from ..changes import Change
from ..docmodel import Manuscript
from ..style import JournalStyle
from .cache import cache_dir, download
from .common import change, hidden_offsets, near_hidden, overlaps, prose_paragraphs, protected_spans

VERSION = "6.6"
ZIP_URL = f"https://languagetool.org/download/LanguageTool-{VERSION}.zip"
DEFAULT_URL = "http://127.0.0.1:8081"


# --- server ----------------------------------------------------------------------------

def install_dir() -> str:
    return cache_dir(f"LanguageTool-{VERSION}")


def install() -> str:
    """Download and unpack the standalone LanguageTool (about 250 MB) unless already there."""
    target = install_dir()
    if os.path.exists(os.path.join(target, "languagetool-server.jar")):
        return target
    zpath = download(ZIP_URL, cache_dir("dl", f"LanguageTool-{VERSION}.zip"))
    with zipfile.ZipFile(zpath) as z:
        z.extractall(os.path.dirname(target))
    return target


def server_command(port: int = 8081, memory: str = "2g") -> list[str]:
    jar = os.path.join(install(), "languagetool-server.jar")
    java = shutil.which("java") or "java"
    # The server listens on localhost only unless --public is given.
    return [java, f"-Xmx{memory}", "-cp", jar, "org.languagetool.server.HTTPServer", "--port", str(port)]


def start_server(port: int = 8081, wait: float = 90.0, log_path: str | None = None) -> subprocess.Popen:
    """Start the server in the background and wait until it answers."""
    log = open(log_path or cache_dir("languagetool.log"), "ab")
    proc = subprocess.Popen(server_command(port), stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + wait
    while time.time() < deadline:
        if is_running(url):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"LanguageTool exited with code {proc.returncode}; see {log.name}")
        time.sleep(1)
    proc.terminate()
    raise RuntimeError("LanguageTool did not start in time")


_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route 127.0.0.1 via a proxy


def is_running(url: str = DEFAULT_URL) -> bool:
    try:
        with _LOCAL.open(f"{url}/v2/languages", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


# --- client ----------------------------------------------------------------------------

def _http_post(url: str, data: dict, timeout: float = 120) -> dict:
    body = urllib.parse.urlencode(data).encode()
    with _LOCAL.open(urllib.request.Request(url, data=body), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Client:
    """/v2/check with a per-text sqlite cache. `post` is injectable so tests stay offline."""

    def __init__(self, url: str = DEFAULT_URL, cache_path: str | None = None,
                 post: Callable[[str, dict], dict] | None = None):
        self.url = url.rstrip("/")
        self.post = post or _http_post
        self.db = sqlite3.connect(cache_path or cache_dir("languagetool_cache.sqlite"), timeout=60)
        self.db.execute("CREATE TABLE IF NOT EXISTS lt (key TEXT PRIMARY KEY, matches TEXT)")

    def check(self, text: str, language: str, **params) -> list[dict]:
        data = {"text": text, "language": language, **params}
        key = hashlib.sha256(json.dumps([VERSION, data], sort_keys=True).encode()).hexdigest()
        row = self.db.execute("SELECT matches FROM lt WHERE key = ?", (key,)).fetchone()
        if row:
            return json.loads(row[0])
        matches = self.post(f"{self.url}/v2/check", data).get("matches", [])
        self.db.execute("INSERT OR REPLACE INTO lt VALUES (?, ?)", (key, json.dumps(matches)))
        self.db.commit()
        return matches


def default_client() -> Client | None:
    url = os.environ.get("EDITOS_LANGUAGETOOL_URL", DEFAULT_URL)
    return Client(url) if is_running(url) else None


# --- matches -> changes ----------------------------------------------------------------

@dataclass
class Warning:
    para_id: str
    start: int
    end: int
    rule: str
    message: str
    suggestions: list[str]


def language(ms: Manuscript, style: JournalStyle) -> str:
    lang = style.get("copyedit.languagetool.language")
    if lang:
        return lang
    from . import spelling

    conv = spelling.convention(style, ms)
    return "en-GB" if conv == "british" else "en-US"


def _rule_ok(m: dict, cfg: dict) -> bool:
    rule = m.get("rule", {})
    rid, cat = rule.get("id", ""), rule.get("category", {}).get("id", "")
    if rid in (cfg.get("disabled_rules") or []):
        return False
    cats = cfg.get("categories") or []
    return rid in (cfg.get("rules") or []) or cat in cats or "*" in cats


def collect(ms: Manuscript, style: JournalStyle, client: Client | None = None) -> tuple[list[Change], list[Warning]]:
    cfg = style.get("copyedit.languagetool", {}) or {}
    client = client or default_client()
    if client is None or not cfg.get("enabled", True):
        return [], []
    lang = language(ms, style)
    params = {}
    if cfg.get("level"):
        params["level"] = cfg["level"]
    changes, warnings = [], []
    warn_cats = set(cfg.get("warn_categories") or [])
    for p in prose_paragraphs(ms):
        prot = protected_spans(p.text)
        lead = re.match(r"[\W_]*", p.text).end()
        if lead:
            prot.append((0, lead))  # list bullets ("- ", "– ") at the paragraph start
        hidden = hidden_offsets(p)
        try:
            matches = client.check(p.text, lang, **params)
        except OSError:  # server busy or restarting: this paragraph goes unchecked, not the manuscript
            continue
        for m in matches:
            s, e = m["offset"], m["offset"] + m["length"]
            if e > len(p.text) or overlaps(s, e, prot) or near_hidden(s, e, hidden):
                continue
            reps = [r["value"] for r in m.get("replacements", [])]
            rule = m.get("rule", {})
            if _rule_ok(m, cfg) and len(reps) == 1 and reps[0] != p.text[s:e]:
                changes.append(change(p, s, e, reps[0], "languagetool",
                                      reason=f"languagetool:{rule.get('id', '')}"))
            elif rule.get("category", {}).get("id") in warn_cats:
                warnings.append(Warning(p.id, s, e, rule.get("id", ""), m.get("message", ""), reps[:3]))
    return changes, warnings


def check(ms: Manuscript, style: JournalStyle, client: Client | None = None) -> list[Change]:
    return collect(ms, style, client)[0]


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "install":
        print(install())
    elif cmd == "start":
        port = int(argv[1]) if len(argv) > 1 else 8081
        print(f"LanguageTool {VERSION} on http://127.0.0.1:{port} (Ctrl+C to stop)")
        return subprocess.call(server_command(port))
    elif cmd == "status":
        print("running" if is_running(os.environ.get("EDITOS_LANGUAGETOOL_URL", DEFAULT_URL)) else "not running")
    else:
        print(__doc__)
        return 2
    return 0

