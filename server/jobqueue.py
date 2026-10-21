"""Job queue. Start simple: a thread pool inside the API process.

The API only calls `submit(job_id)`; the worker side is `worker.process(job_id)`. To scale out,
replace `ThreadQueue` with a queue whose `submit` sends the job id to SQS (or Redis) and run
`python -m server.worker` consumers that call `worker.process` -- job state is already in the
store, not in memory, so nothing else changes.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Protocol

log = logging.getLogger("editos.queue")


class JobQueue(Protocol):
    def submit(self, job_id: str) -> None: ...
    def shutdown(self) -> None: ...


class ThreadQueue:
    def __init__(self, handler: Callable[[str], None], workers: int = 2):
        self.handler = handler
        self.pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="editos-job")
        self.futures: dict[str, Future] = {}

    def submit(self, job_id: str) -> None:
        self.futures[job_id] = self.pool.submit(self._run, job_id)

    def _run(self, job_id: str) -> None:
        try:
            self.handler(job_id)
        except Exception:  # the handler records failures on the job; never kill the pool
            log.exception("job %s crashed", job_id)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
