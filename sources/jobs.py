"""DB-backed job queue and its worker thread — planning.md §7.2.

    Premise: 1,600 HistData files and 19 years of MT5 calendar export take
    minutes to hours.
    Inference: a synchronous request times out; a page refresh restarts it.
    Inference: so it must run outside the request cycle with state in the DB,
    so progress survives a reload.
    Premise: single user, local, occasional button presses.
    Conclusion: a `job` table plus a worker thread started with Django.  Same
    durability, no Redis to supervise.

The thread is deliberately single: one user pressing buttons does not need
concurrency, and serialising fetches is also the politest thing to do to the
sources (§5.4).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from typing import Callable

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from sources.models import Job, JobStatus

logger = logging.getLogger(__name__)

JobHandler = Callable[[Job], dict]
_HANDLERS: dict[str, JobHandler] = {}

_worker_thread: threading.Thread | None = None
_worker_stop = threading.Event()
_start_lock = threading.Lock()


def handler(kind: str):
    """Register a job handler.  Returns a dict, stored as `result_json`."""

    def decorator(func: JobHandler) -> JobHandler:
        _HANDLERS[kind] = func
        return func

    return decorator


def enqueue(kind: str, **params) -> Job:
    if kind not in _HANDLERS:
        raise ValueError(f"No handler registered for job kind {kind!r}")
    return Job.objects.create(kind=kind, params_json=params)


def set_progress(job: Job, fraction: float, message: str = ""):
    fraction = max(0.0, min(1.0, float(fraction)))
    fields = {"progress": fraction}
    if message:
        fields["message"] = message[:300]
    Job.objects.filter(pk=job.pk).update(**fields)


def _claim_next() -> Job | None:
    """Atomic claim.  The UPDATE ... WHERE status='queued' is what makes this
    safe if a second worker ever exists."""
    with transaction.atomic():
        candidate = Job.objects.filter(status=JobStatus.QUEUED).order_by("created_at").first()
        if candidate is None:
            return None
        claimed = Job.objects.filter(pk=candidate.pk, status=JobStatus.QUEUED).update(
            status=JobStatus.RUNNING, started_at=timezone.now(), progress=0.0
        )
        if not claimed:
            return None
    candidate.refresh_from_db()
    return candidate


def run_job(job: Job) -> None:
    """Execute one job in the calling thread.  Used by the worker and by
    `manage.py run_job` for debugging without a server."""
    func = _HANDLERS.get(job.kind)
    if func is None:
        _finish(job, JobStatus.FAILED, error=f"No handler for job kind {job.kind!r}")
        return

    job.append_log(f"start {job.kind} {job.params_json}")
    try:
        result = func(job) or {}
    except Exception as exc:  # noqa: BLE001 — the console is the error channel
        detail = traceback.format_exc()
        logger.exception("job %s failed", job.pk)
        job.append_log(f"FAILED: {exc}")
        _finish(job, JobStatus.FAILED, error=detail)
        return

    job.append_log("done")
    _finish(job, JobStatus.SUCCESS, result=result)


def _finish(job: Job, status: str, *, result: dict | None = None, error: str = ""):
    fields = {
        "status": status,
        "finished_at": timezone.now(),
        "result_json": result or {},
        "error_text": error,
    }
    if status == JobStatus.SUCCESS:
        # A failed job keeps whatever progress it reached — that is a clue.
        fields["progress"] = 1.0
    Job.objects.filter(pk=job.pk).update(**fields)


def _loop():
    logger.info("fxmacro worker started")
    while not _worker_stop.is_set():
        close_old_connections()
        try:
            job = _claim_next()
        except Exception:  # noqa: BLE001 — a locked DB must not kill the worker
            logger.exception("worker failed to claim a job")
            _worker_stop.wait(settings.WORKER_POLL_SECONDS)
            continue

        if job is None:
            _worker_stop.wait(settings.WORKER_POLL_SECONDS)
            continue

        run_job(job)
        close_old_connections()
    logger.info("fxmacro worker stopped")


#: Commands that must never spawn a worker: they either mutate the schema or
#: are expected to exit immediately.
_NO_WORKER_COMMANDS = {
    "migrate",
    "makemigrations",
    "collectstatic",
    "test",
    "shell",
    "dbshell",
    "createsuperuser",
    "check",
    "showmigrations",
    "flush",
    "loaddata",
    "dumpdata",
}


def should_start_worker() -> bool:
    if not settings.RUN_WORKER:
        return False
    argv = sys.argv
    if len(argv) > 1 and argv[1] in _NO_WORKER_COMMANDS:
        return False
    # runserver's autoreloader runs ready() twice; only the child should work.
    # With --noreload there is no child, so RUN_MAIN is never set and the
    # check has to be skipped or the worker never starts at all.
    if (
        len(argv) > 1
        and argv[1] == "runserver"
        and "--noreload" not in argv
        and os.environ.get("RUN_MAIN") != "true"
    ):
        return False
    return True


def start_worker() -> None:
    global _worker_thread
    with _start_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_stop.clear()
        _worker_thread = threading.Thread(target=_loop, name="fxmacro-worker", daemon=True)
        _worker_thread.start()


def stop_worker(timeout: float = 5.0) -> None:
    _worker_stop.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=timeout)
