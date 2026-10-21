"""Job state in SQLite: jobs and the editor's decisions.

One file under the data directory; every call opens its own connection, so worker threads and
request handlers can share it. For several API containers, move this to PostgreSQL (same two
tables) -- the rest of the service only talks to `JobStore`.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    stage TEXT,
    journal TEXT NOT NULL,
    stages TEXT NOT NULL,
    input_name TEXT NOT NULL,
    input_ref TEXT,
    external_id TEXT,
    callback_url TEXT,
    review_token TEXT NOT NULL,
    error TEXT,
    summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_external ON jobs(external_id);
CREATE TABLE IF NOT EXISTS decisions (
    job_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,          -- change | insert | query
    status TEXT NOT NULL,        -- suggested | accepted | rejected
    editor TEXT,
    decided_at TEXT NOT NULL,
    new_text TEXT,               -- the editor's own wording for an accepted suggestion
    PRIMARY KEY (job_id, item_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class JobStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(decisions)")}
            if "new_text" not in cols:  # databases created before editor edits existed
                c.execute("ALTER TABLE decisions ADD COLUMN new_text TEXT")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row(r) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        d["stages"] = json.loads(d["stages"])
        d["summary"] = json.loads(d["summary"]) if d["summary"] else None
        return d

    def create(self, journal: str, stages, input_name: str, input_ref: str = "", external_id: str = "",
               callback_url: str = "") -> dict:
        job_id = uuid.uuid4().hex[:16]
        t = now()
        with self._conn() as c:
            c.execute("INSERT INTO jobs (id, status, stage, journal, stages, input_name, input_ref, external_id,"
                      " callback_url, review_token, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (job_id, QUEUED, "queued", journal, json.dumps(list(stages)), input_name, input_ref,
                       external_id or None, callback_url or None, secrets.token_urlsafe(24), t, t))
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def list(self, limit: int = 50, external_id: str | None = None) -> list[dict]:
        with self._conn() as c:
            if external_id:
                rows = c.execute("SELECT * FROM jobs WHERE external_id = ? ORDER BY created_at DESC LIMIT ?",
                                 (external_id, limit))
            else:
                rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
            return [self._row(r) for r in rows.fetchall()]

    def update(self, job_id: str, **fields) -> None:
        if "summary" in fields and fields["summary"] is not None:
            fields["summary"] = json.dumps(fields["summary"])
        fields["updated_at"] = now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def unfinished(self) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute("SELECT id FROM jobs WHERE status IN (?, ?) ORDER BY created_at",
                                            (QUEUED, RUNNING)).fetchall()]

    # --- decisions ---------------------------------------------------------------------

    def decide(self, job_id: str, items: list[tuple[str, str]], status: str, editor: str = "") -> int:
        """items: (item_id, kind). Returns how many were recorded."""
        t = now()
        with self._conn() as c:
            c.executemany("INSERT INTO decisions (job_id, item_id, kind, status, editor, decided_at) VALUES (?,?,?,?,?,?)"
                          " ON CONFLICT(job_id, item_id) DO UPDATE SET status=excluded.status,"
                          " editor=excluded.editor, decided_at=excluded.decided_at",
                          [(job_id, i, k, status, editor or None, t) for i, k in items])
        return len(items)

    def edit(self, job_id: str, item_id: str, kind: str, new_text: str, editor: str = "") -> None:
        """Accept a suggestion with the editor's own wording."""
        with self._conn() as c:
            c.execute("INSERT INTO decisions (job_id, item_id, kind, status, editor, decided_at, new_text)"
                      " VALUES (?,?,?,'accepted',?,?,?) ON CONFLICT(job_id, item_id) DO UPDATE SET"
                      " status='accepted', editor=excluded.editor, decided_at=excluded.decided_at,"
                      " new_text=excluded.new_text", (job_id, item_id, kind, editor or None, now(), new_text))

    def decisions(self, job_id: str) -> dict[str, dict]:
        with self._conn() as c:
            return {r["item_id"]: dict(r) for r in
                    c.execute("SELECT * FROM decisions WHERE job_id = ?", (job_id,)).fetchall()}
