"""Run one job: fetch its input, run `pipeline.edit`, persist outputs, notify the caller.

`process` is what any queue calls. `python -m server.worker JOB_ID` runs one job by hand (and is
the entry point a separate SQS/Redis consumer would wrap).
"""

from __future__ import annotations

import glob
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import urllib.request

from edit_os import pipeline

from . import db
from .config import Settings
from .storage import LocalStorage, fetch_s3_object, make_storage

log = logging.getLogger("editos.worker")

# One lock per job: a re-render and a decision write must not interleave with a running render.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def job_lock(job_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(job_id, threading.Lock())


def out_dir(storage: LocalStorage, job_id: str) -> str:
    return os.path.join(storage.job_dir(job_id), "out")


def input_file(storage: LocalStorage, job: dict, settings: Settings) -> str:
    """The job's input on local disk; downloaded from S3 when the job was created from an S3 key."""
    in_dir = os.path.join(storage.job_dir(job["id"]), "input")
    local = glob.glob(os.path.join(in_dir, "*"))
    if local:
        return local[0]
    ref = job.get("input_ref") or ""
    if ref.startswith("s3://"):
        bucket, _, key = ref[5:].partition("/")
        return fetch_s3_object(bucket, key, os.path.join(in_dir, os.path.basename(key)), settings.aws_region)
    if storage.restore(job["id"]):
        local = glob.glob(os.path.join(in_dir, "*"))
        if local:
            return local[0]
    raise FileNotFoundError("job input is missing")


def notify(job: dict, settings: Settings, review_url: str) -> None:
    """POST the outcome to the job's callback URL (the dashboard), signed when a secret is set."""
    if not job.get("callback_url"):
        return
    body = json.dumps({"job_id": job["id"], "external_id": job.get("external_id"), "status": job["status"],
                       "error": job.get("error"), "review_url": review_url,
                       "qc_passed": (job.get("summary") or {}).get("qc", {}).get("passed")}).encode()
    headers = {"Content-Type": "application/json"}
    if settings.callback_secret:
        headers["X-EditOS-Signature"] = "sha256=" + hmac.new(settings.callback_secret.encode(), body,
                                                             hashlib.sha256).hexdigest()
    try:
        urllib.request.urlopen(urllib.request.Request(job["callback_url"], body, headers, method="POST"), timeout=15)
    except Exception as e:  # the dashboard can also poll GET /api/jobs/{id}
        log.warning("callback for job %s failed: %s", job["id"], e)


def review_url(settings: Settings, job: dict) -> str:
    return f"{settings.public_url}/review/{job['id']}?token={job['review_token']}"


def compact_summary(s: dict) -> dict:
    return {k: s[k] for k in ("changes", "front_matter_inserts", "queries", "delivery_ready")} | {
        "qc": {k: s["qc"][k] for k in ("passed", "basis", "errors", "warnings")}}


def process(job_id: str, settings: Settings, store: db.JobStore, storage: LocalStorage) -> None:
    job = store.get(job_id)
    if job is None or job["status"] == db.DONE:
        return
    store.update(job_id, status=db.RUNNING, stage="starting", error=None)
    try:
        with job_lock(job_id):
            src = input_file(storage, job, settings)
            out = pipeline.edit(src, job["journal"], out_dir(storage, job_id), tuple(job["stages"]),
                                progress=lambda m: store.update(job_id, stage=m),
                                counts=lambda d: store.update(job_id, summary={"live": d}))
            storage.save(job_id)
        store.update(job_id, status=db.DONE, stage="done", summary=compact_summary(out.summary))
    except Exception as e:
        log.exception("job %s failed", job_id)
        store.update(job_id, status=db.FAILED, stage="failed", error=f"{type(e).__name__}: {e}"[:2000])
    notify(store.get(job_id), settings, review_url(settings, job))


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    settings = Settings.from_env()
    process(argv[0], settings, db.JobStore(os.path.join(settings.data_dir, "jobs.sqlite")), make_storage(settings))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main(sys.argv[1:]))
