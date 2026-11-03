"""One SQLite connection used by many worker threads at once (2026-11-03: two jobs at once failed with "bad
parameter or other API misuse" and an empty row from the journal database)."""

import threading

from edit_os import shared_db


def test_many_threads_on_one_connection(tmp_path):
    db = shared_db.connect(str(tmp_path / "c.sqlite"))
    db.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
    for i in range(200):
        db.execute("INSERT INTO t VALUES (?, ?)", (str(i), "v" + str(i)))
    db.commit()
    errors = []

    def work(n):
        try:
            for i in range(300):
                k = str((i * 7 + n) % 200)
                row = db.execute("SELECT v FROM t WHERE k = ?", (k,)).fetchone()
                assert row == ("v" + k,)
                ids = [r[0] for r in db.execute("SELECT k FROM t WHERE k LIKE ?", (k[:1] + "%",))]
                assert k in ids
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
