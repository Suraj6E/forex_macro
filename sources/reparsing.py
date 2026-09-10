"""Re-parse a stored snapshot — planning.md §5.4.

    "Raw snapshots (so a parser fix is a re-parse, not a re-crawl), per-source
    fetch policy in the DB, parser version stamped on every row…"

No network call: the bytes are already on disk, content-hashed.  It is also
how you check the merge is idempotent (§7.2) without spending a request
against a source that rate-limits.

Shared by the console button and `manage.py reparse`.
"""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from consolidation.merge import apply_observation
from sources import registry
from sources.models import FetchRun, JobStatus, RawSnapshot, Source


class ReparseError(RuntimeError):
    pass


def latest_snapshot(source: Source) -> RawSnapshot | None:
    return (
        RawSnapshot.objects.filter(fetch_run__source=source).order_by("-fetched_at").first()
    )


def reparse_snapshot(
    source: Source,
    snapshot: RawSnapshot | None = None,
    *,
    log=lambda msg: None,
    dry_run: bool = False,
) -> dict:
    collector = registry.get(source.key)
    if collector is None or collector.reparse is None:
        raise ReparseError(f"Source {source.key!r} has no offline re-parse path.")

    snapshot = snapshot or latest_snapshot(source)
    if snapshot is None:
        raise ReparseError(f"No stored snapshot for {source.key!r} — fetch it first.")

    path = settings.RAW_DIR / snapshot.path
    if not path.exists():
        raise ReparseError(f"Snapshot #{snapshot.pk} is recorded but missing at {path}")

    content = path.read_bytes()
    # The original capture instant, not now: it is what decides whether a
    # forecast is point-in-time (§4.3).  Re-parsing with the current clock
    # would silently downgrade every forecast in the archive.
    rows = collector.reparse(content, captured_at=snapshot.fetched_at)
    log(
        f"snapshot #{snapshot.pk} ({snapshot.size_bytes} bytes, captured "
        f"{snapshot.fetched_at:%Y-%m-%d %H:%M}Z) -> {len(rows)} rows via parser "
        f"{collector.parser_version}"
    )

    if dry_run:
        return {"rows_seen": len(rows), "dry_run": True, "summary": f"{len(rows)} rows parsed"}

    run = FetchRun.objects.create(
        source=source,
        params_json={"reparse_of_snapshot": snapshot.pk},
        parser_version=collector.parser_version,
        notes=f"Re-parse of snapshot #{snapshot.pk}. No network call.",
    )

    new = changed = disagreed = unmapped = 0
    for row in rows:
        outcome = apply_observation(source, run, row)
        new += outcome.created
        changed += bool(outcome.changed_fields)
        disagreed += bool(outcome.disagreed_fields)
        unmapped += outcome.newly_unmapped

    run.rows_seen = len(rows)
    run.rows_new = new
    run.rows_changed = changed
    run.rows_unmapped = unmapped
    run.status = JobStatus.SUCCESS
    run.finished_at = timezone.now()
    run.save()

    summary = (
        f"{len(rows)} seen, {new} new, {changed} changed, "
        f"{disagreed} disagreements, {unmapped} newly unmapped"
    )
    log(summary)
    return {
        "fetch_run_id": run.pk,
        "snapshot_id": snapshot.pk,
        "rows_seen": len(rows),
        "rows_new": new,
        "rows_changed": changed,
        "rows_disagreed": disagreed,
        "rows_unmapped": unmapped,
        "summary": summary,
    }
