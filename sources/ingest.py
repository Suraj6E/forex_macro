"""The `fetch_source` job — planning.md §7.1.

Pressing **Fetch** enqueues this.  It records a `FetchRun`, hands the collector
an immutable place to drop payloads, folds every returned row through the
merge, writes any price frames to the Parquet store, and leaves behind rows
seen / new / changed, a sample of the actual data, and a link to the raw
snapshot.

Idempotency (§7.2): every fetch is re-runnable.  Upsert on the natural key of
§4.4; a re-fetch that changes a value writes a `value_revision` rather than
overwriting.

**Preview mode** does everything except the writing.  It exists because a
coverage chart cannot tell you a column is fully populated with the wrong
number — you have to look at the rows.
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
    preview_only = bool(params.pop("preview_only", False))

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
    if cooldown and not force and not preview_only:
        hours = cooldown.total_seconds() / 3600
        raise RuntimeError(
            f"{source.key} is inside its fetch policy cooldown for another "
            f"{hours:.1f}h (min_interval_hours="
            f"{source.fetch_policy_json.get('min_interval_hours')}). "
            f"Re-fetch with force to override — but see §5.4: this source "
            f"returns HTTP 429 when polled hard."
        )

    # Uploads are staged on disk rather than carried in the job row: an MT5
    # export can be tens of megabytes and a JSON column is the wrong place
    # for it.
    uploads: list[tuple[str, bytes]] = []
    for relative, original_name in params.pop("uploads", []):
        staged = settings.RAW_DIR / relative
        if not staged.exists():
            raise RuntimeError(f"Uploaded file is gone: {staged}")
        uploads.append((original_name, staged.read_bytes()))

    run = FetchRun.objects.create(
        source=source,
        job=job,
        params_json=params,
        parser_version=collector.parser_version,
        preview_only=preview_only,
        notes="Preview run — nothing was written to the dataset." if preview_only else "",
    )

    ctx = FetchContext(
        raw_root=settings.RAW_DIR,
        source_key=source.key,
        params=params,
        config=dict(source.config_json or {}),
        user_agent=settings.HTTP_USER_AGENT,
        timeout=settings.HTTP_TIMEOUT_SECONDS,
        log=job.append_log,
        # The collector owns the first half of the bar; writing owns the rest.
        progress=lambda frac, msg: jobs.set_progress(job, frac * 0.5, msg),
        preview_only=preview_only,
        uploads=uploads,
    )

    try:
        result = collector.fetch(ctx)
    except Exception as exc:  # noqa: BLE001 — surfaced on the console
        _fail(run, source, str(exc), preview_only=preview_only)
        raise

    for snap in result.snapshots:
        RawSnapshot.objects.create(
            fetch_run=run,
            fetched_at=snap.fetched_at,
            url=snap.url[:2000],
            path=snap.path,
            sha256=snap.sha256,
            size_bytes=snap.size_bytes,
            http_status=snap.http_status,
            content_type=snap.content_type[:120],
            duration_ms=snap.duration_ms,
        )

    if result.preview is not None:
        run.sample_json = result.preview.as_dict()

    total = len(result.rows)
    new = changed = disagreed = unmapped = 0
    bars_written = 0

    if preview_only:
        job.append_log(
            f"preview: {total} calendar row(s), "
            f"{sum(len(f.frame) for f in result.price_frames):,} bar(s) parsed — "
            f"nothing written"
        )
        jobs.set_progress(job, 1.0, "preview complete")
    else:
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

            if index % 50 == 0 or index == total:
                jobs.set_progress(
                    job, 0.5 + 0.5 * index / max(total, 1), f"merging {index}/{total}"
                )

        bars_written = _write_price_frames(job, run, source, result.price_frames)

    run.rows_seen = total or sum(len(f.frame) for f in result.price_frames)
    run.rows_new = new
    run.rows_changed = changed
    run.rows_unmapped = unmapped
    run.bars_written = bars_written
    run.status = JobStatus.SUCCESS
    run.finished_at = timezone.now()
    run.notes = (run.notes + " " + result.notes).strip()
    run.save()

    if not preview_only:
        source.record_outcome(success=True)

    if preview_only:
        summary = f"preview only — {run.rows_seen:,} row(s) parsed, nothing written"
    elif result.price_frames:
        summary = f"{bars_written:,} bars written across {len(result.price_frames)} instrument-month(s)"
    else:
        summary = (
            f"{total} seen, {new} new, {changed} changed, "
            f"{disagreed} with a cross-source disagreement, {unmapped} newly unmapped"
        )

    job.append_log(summary)
    return {
        "fetch_run_id": run.pk,
        "preview_only": preview_only,
        "rows_seen": run.rows_seen,
        "rows_new": new,
        "rows_changed": changed,
        "rows_disagreed": disagreed,
        "rows_unmapped": unmapped,
        "bars_written": bars_written,
        "summary": summary,
        "notes": result.notes,
    }


def _write_price_frames(job: Job, run: FetchRun, source: Source, frames) -> int:
    """Hand price frames to the Parquet store and update the coverage ledger."""
    if not frames:
        return 0

    from prices.models import Instrument
    from prices.store import write_month

    written = 0
    for index, frame in enumerate(frames, start=1):
        instrument = Instrument.objects.filter(symbol=frame.symbol).first()
        if instrument is None:
            job.append_log(f"unknown instrument {frame.symbol} — skipped")
            continue

        coverage = write_month(
            instrument, source, frame.frame, frame.month, fetch_run=run
        )
        written += len(frame.frame)
        job.append_log(
            f"{frame.symbol} {frame.month:%Y-%m}: {coverage.bar_count:,} bars stored "
            f"({coverage.gap_count:,} minutes short of a complete month)"
        )
        jobs.set_progress(
            job, 0.5 + 0.5 * index / len(frames), f"writing {frame.symbol} {frame.month:%Y-%m}"
        )
    return written


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
    from sources.models import RawSnapshot as Snapshot
    from sources.reparsing import reparse_snapshot

    params = dict(job.params_json or {})
    source = Source.objects.get(key=params["source_key"])
    snapshot = None
    if params.get("snapshot_id"):
        snapshot = Snapshot.objects.filter(pk=params["snapshot_id"]).first()

    jobs.set_progress(job, 0.2, "reading snapshot")
    result = reparse_snapshot(source, snapshot, log=job.append_log)
    jobs.set_progress(job, 1.0, result["summary"])
    return result


def _fail(run: FetchRun, source: Source, message: str, *, preview_only: bool = False):
    run.status = JobStatus.FAILED
    run.finished_at = timezone.now()
    run.error_text = message
    run.save(update_fields=["status", "finished_at", "error_text"])
    if not preview_only:
        source.record_outcome(success=False)
