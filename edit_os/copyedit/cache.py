"""Where copy-edit resources live: downloaded word lists and the LanguageTool server.

Everything big is downloaded once into ~/.cache/edit-os/ (override with EDITOS_CACHE).
"""

from __future__ import annotations

import os
import urllib.request


def cache_dir(*parts: str) -> str:
    root = os.environ.get("EDITOS_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "edit-os")
    path = os.path.join(root, *parts)
    os.makedirs(os.path.dirname(path) if parts else path, exist_ok=True)
    return path


def download(url: str, dest: str, timeout: int = 600) -> str:
    """Download `url` to `dest` (atomically) unless it is already there."""
    if os.path.exists(dest):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=timeout) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    os.replace(tmp, dest)
    return dest
