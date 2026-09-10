"""The `fetch_source` job — planning.md §7.1.

Pressing **Fetch** enqueues this.  It records a `FetchRun`, hands the
collector an immutable place to drop payloads, folds every returned row
through the merge, and leaves behind rows seen / new / changed plus a link to
the raw snapshot.

Idempotency (§7.2): every fetch is re-runnable.  Upsert on the natural key of
§4.4; a re-fetch that changes a value writes a `value_revision` rather than
overwriting.
"""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from collectors.base import FetchContext
from consolidation.merge import apply_observation
from sources import jobs, registry
from sources.models import FetchRun, Job, JobStatus, RawSnapshot, Source


@jobs.handler("fetch_source")
def fetch_source(job: Job) -> dict:
    params = dict(job.params_json or {})
    source = Source.objects.get(key=params.pop("source_key"))

    collector = registry.get(source.key)
    if collector is None:
        raise RuntimeError(
            f"Source {source.key!r} is registered but has no collector yet. "
            f"Implemented: {sorted(registry.implemented_keys())}"
        )
    if not source.enabled:
        raise RuntimeError(f"Source {source.key!r} is disabled.")

    force = bool(params.pop("force", False))
    cooldown = source.cooldown_remaining()
    if cooldown and not force:
        hours = cooldown.total_seconds() / 3600
        raise RuntimeError(
            f"{source.key} is inside its fetch policy cooldown for another "
            f"{hours:.1f}h (min_interval_hours="
            f"{source.fetch_policy_json.get('min_interval_hours')}). "
            f"Re-fetch with force to override — but see §5.4: this source "
            f"returns HTTP 429 when polled hard."
        )

    run = FetchRun.objects.create(
        source=source,
        job=job,
        params_json=params,
        parser_version=collector.parser_version,
    )

    ctx = FetchContext(
        raw_root=settings.RAW_DIR,
        source_key=source.key,
        params=params,
        user_agent=settings.HTTP_USER_AGENT,
        timeout=settings.HTTP_TIMEOUT_SECONDS,
        log=job.append_log,
        # The collector owns the first half of the bar; merging owns the rest.
        progress=lambda frac, msg: jobs.set_progress(job, frac * 0.5, msg),
    )

    try:
        result = collector.fetch(ctx)
    except Exception as exc:  # noqa: BLE001 — surfaced on the console
        _fail(run, source, str(exc))
        raise

    for snap in result.snapshots:
        RawSnapshot.objects.create(
            fetch_run=run,
            fetched_at=snap.fetched_at,
            url=snap.url,
            path=snap.path,
            sha256=snap.sha256,
            size_bytes=snap.size_bytes,
            http_status=snap.http_status,
            content_type=snap.content_type[:120],
            duration_ms=snap.duration_ms,
        )

    total = len(result.rows)
    new = changed = disagreed = unmapped = 0

    for index, row in enumerate(result.rows, start=1):
        try:
            outcome = apply_observation(source, run, row)
        except Exception as exc:  # noqa: BLE001
            job.append_log(f"row {index}/{total} ({row.source_event_key}): {exc}")
            _fail(run, source, f"merge failed on row {index}: {exc}")
            raise

        new += outcome.created
        changed += bool(outcome.changed_fields)
        disagreed += bool(outcome.disagreed_fields)
        unmapped += outcome.newly_unmapped

        if index % 25 == 0 or index == total:
            jobs.set_progress(job, 0.5 + 0.5 * index / max(total, 1), f"merging {index}/{total}")

    run.rows_seen = total
    run.rows_new = new
    run.rows_changed = changed
    run.rows_unmapped = unmapped
    run.status = JobStatus.SUCCESS
    run.finished_at = timezone.now()
    run.notes = result.notes
    run.save()
    source.record_outcome(success=True)

    summary = (
        f"{total} seen, {new} new, {changed} changed, "
        f"{disagreed} with a cross-source disagreement, {unmapped} newly unmapped"
    )
    job.append_log(summary)
    return {
        "fetch_run_id": run.pk,
        "rows_seen": total,
        "rows_new": new,
        "rows_changed": changed,
        "rows_disagreed": disagreed,
        "rows_unmapped": unmapped,
        "summary": summary,
        "notes": result.notes,
    }


@jobs.handler("seed_reference")
def seed_reference_job(job: Job) -> dict:
    """The §5 register, the 7 majors, release groups and known dislocations."""
    from sources.seeding import seed_reference

    jobs.set_progress(job, 0.2, "seeding reference data")
    result = seed_reference(log=job.append_log)
    jobs.set_progress(job, 1.0, result["summary"])
    return result


@jobs.handler("reparse_snapshot")
def reparse_snapshot_job(job: Job) -> dict:
    """Re-run the current normaliser over stored bytes — no network call (§5.4)."""
    from sources.models import RawSnapshot
    from sources.reparsing import reparse_snapshot

    params = dict(job.params_json or {})
    source = Source.objects.get(key=params["source_key"])
    snapshot = None
    if params.get("snapshot_id"):
        snapshot = RawSnapshot.objects.filter(pk=params["snapshot_id"]).first()

    jobs.set_progress(job, 0.2, "reading snapshot")
    result = reparse_snapshot(source, snapshot, log=job.append_log)
    jobs.set_progress(job, 1.0, result["summary"])
    return result


def _fail(run: FetchRun, source: Source, message: str):
    run.status = JobStatus.FAILED
    run.finished_at = timezone.now()
    run.error_text = message
    run.save(update_fields=["status", "finished_at", "error_text"])
    source.record_outcome(success=False)
