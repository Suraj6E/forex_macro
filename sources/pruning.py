"""Pruning ingest history — planning.md §5.4, §7.1.

Raw payloads exist so that a parser fix is a re-parse rather than a re-crawl.
That guarantee is worth real disk: nineteen years of calendar pages is ~360 MB.
Once the rows are extracted and verified, the trade is disk against a ~20
minute re-crawl, and it is the operator's to make — so this is an explicit
action, never automatic.

What is always kept:

* **FetchRun records.** They are the provenance: what was fetched, when, by
  which parser version, and the content hash of what arrived. Deleting the
  bytes does not have to mean forgetting they existed.
* **The most recent run per source**, with its payloads, so the last thing
  each source returned can still be inspected and re-parsed.
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import Count, Sum

from sources.models import FetchRun, Job, JobStatus, RawSnapshot, Source


def _delete_files(snapshots) -> tuple[int, int]:
    removed = freed = 0
    for path, size in snapshots.values_list("path", "size_bytes"):
        target = settings.RAW_DIR / path
        try:
            if target.exists():
                target.unlink()
                removed += 1
                freed += size or 0
        except OSError:
            continue
    return removed, freed


def keep_latest_run_ids(per_source: int = 1) -> set[int]:
    keep: set[int] = set()
    for source in Source.objects.all():
        keep.update(
            source.runs.order_by("-started_at").values_list("id", flat=True)[:per_source]
        )
    return keep


def prune(
    *,
    keep_runs_per_source: int = 1,
    keep_jobs: int = 20,
    drop_payloads: bool = True,
    log=lambda msg: None,
) -> dict:
    """Clear ingest history, keeping the most recent run of each source."""
    keep = keep_latest_run_ids(keep_runs_per_source)

    stale = RawSnapshot.objects.exclude(fetch_run_id__in=keep)
    before = stale.aggregate(n=Count("id"), bytes=Sum("size_bytes"))

    files_removed = bytes_freed = 0
    if drop_payloads:
        files_removed, bytes_freed = _delete_files(stale)
        rows_removed = stale.count()
        stale.delete()
        log(f"removed {files_removed} payload file(s), {bytes_freed / 1e6:.0f} MB")
    else:
        rows_removed = 0

    # Job rows carry the per-run log text, which is the bulk of the noise.
    keep_job_ids = set(Job.objects.order_by("-created_at").values_list("id", flat=True)[:keep_jobs])
    keep_job_ids.update(
        FetchRun.objects.filter(id__in=keep, job__isnull=False).values_list("job_id", flat=True)
    )
    stale_jobs = Job.objects.exclude(id__in=keep_job_ids).exclude(
        status__in=[JobStatus.QUEUED, JobStatus.RUNNING]
    )
    jobs_removed = stale_jobs.count()
    stale_jobs.delete()

    summary = (
        f"{jobs_removed} job record(s) cleared, {rows_removed} snapshot row(s) "
        f"and {files_removed} file(s) removed, {bytes_freed / 1e6:.0f} MB freed. "
        f"{FetchRun.objects.count()} fetch run(s) kept as provenance."
    )
    log(summary)
    return {
        "jobs_removed": jobs_removed,
        "snapshot_rows_removed": rows_removed,
        "files_removed": files_removed,
        "bytes_freed": bytes_freed,
        "candidates": before["n"] or 0,
        "candidate_bytes": before["bytes"] or 0,
        "runs_kept": len(keep),
        "summary": summary,
    }


def prunable() -> dict:
    """What a prune would remove, without removing it."""
    keep = keep_latest_run_ids()
    stale = RawSnapshot.objects.exclude(fetch_run_id__in=keep)
    totals = stale.aggregate(n=Count("id"), bytes=Sum("size_bytes"))
    return {
        "snapshots": totals["n"] or 0,
        "bytes": totals["bytes"] or 0,
        "jobs": Job.objects.exclude(
            status__in=[JobStatus.QUEUED, JobStatus.RUNNING]
        ).count(),
    }
