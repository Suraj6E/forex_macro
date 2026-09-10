"""Re-parse a stored snapshot — planning.md §5.4.

    "Raw snapshots (so a parser fix is a re-parse, not a re-crawl), per-source
    fetch policy in the DB, parser version stamped on every row, and a
    schema-drift alarm when a parser's output shape changes."

This is that first clause made real.  No network call: the bytes are already
on disk, content-hashed.  It is also how you verify the merge is idempotent
(§7.2) without spending a request against a source that rate-limits.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from consolidation.merge import apply_observation
from sources import registry
from sources.models import FetchRun, JobStatus, RawSnapshot, Source


class Command(BaseCommand):
    help = "Re-run the current normaliser over an already-fetched snapshot."

    def add_arguments(self, parser):
        parser.add_argument("source_key")
        parser.add_argument(
            "--snapshot", type=int, help="Snapshot id. Defaults to the most recent."
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report, but write nothing.",
        )

    def handle(self, *args, **options):
        key = options["source_key"]
        try:
            source = Source.objects.get(key=key)
        except Source.DoesNotExist as exc:
            raise CommandError(f"No source {key!r}") from exc

        collector = registry.get(key)
        if collector is None or collector.reparse is None:
            raise CommandError(f"Source {key!r} has no offline re-parse path.")

        if options["snapshot"]:
            snapshot = RawSnapshot.objects.filter(pk=options["snapshot"]).first()
        else:
            snapshot = (
                RawSnapshot.objects.filter(fetch_run__source=source)
                .order_by("-fetched_at")
                .first()
            )
        if snapshot is None:
            raise CommandError(f"No stored snapshot for {key!r}.")

        path = settings.RAW_DIR / snapshot.path
        if not path.exists():
            raise CommandError(f"Snapshot {snapshot.pk} is recorded but missing at {path}")

        content = path.read_bytes()
        # The original capture instant, not now — see collectors/forexfactory.reparse.
        rows = collector.reparse(content, captured_at=snapshot.fetched_at)
        self.stdout.write(
            f"snapshot #{snapshot.pk} ({snapshot.size_bytes} bytes, captured "
            f"{snapshot.fetched_at:%Y-%m-%d %H:%M}Z) -> {len(rows)} rows "
            f"via parser {collector.parser_version}"
        )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("dry run — nothing written"))
            return

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

        self.stdout.write(
            self.style.SUCCESS(
                f"{len(rows)} seen, {new} new, {changed} changed, "
                f"{disagreed} disagreements, {unmapped} newly unmapped "
                f"(run #{run.pk})"
            )
        )
