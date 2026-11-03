"""One SQLite connection shared by the server's worker threads, used under a lock.

A connection opened with check_same_thread=False is not safe for two threads at once: their cursors interleave
(2026-11-03, two jobs at once: "InterfaceError: bad parameter or other API misuse", an empty row). Each `execute`
runs and reads its rows under the lock and returns them as a list, which has `fetchone` / `fetchall` like a cursor.
"""

from __future__ import annotations

import sqlite3
import threading


class _Rows(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class Shared:
    def __init__(self, path: str, **kw):
        self.con = sqlite3.connect(path, check_same_thread=False, **kw)
        self.lock = threading.RLock()

    def execute(self, sql: str, params=()) -> _Rows:
        with self.lock:
            return _Rows(self.con.execute(sql, params).fetchall())

    def commit(self) -> None:
        with self.lock:
            self.con.commit()


def connect(path: str, **kw) -> Shared:
    return Shared(path, **kw)
