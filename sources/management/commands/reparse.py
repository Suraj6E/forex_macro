"""CLI wrapper around sources.reparsing.reparse_snapshot (§5.4)."""

from django.core.management.base import BaseCommand, CommandError

from sources.models import RawSnapshot, Source
from sources.reparsing import ReparseError, reparse_snapshot


class Command(BaseCommand):
    help = "Re-run the current normaliser over an already-fetched snapshot."

    def add_arguments(self, parser):
        parser.add_argument("source_key")
        parser.add_argument("--snapshot", type=int, help="Snapshot id. Defaults to the latest.")
        parser.add_argument("--dry-run", action="store_true", help="Parse and report, write nothing.")

    def handle(self, *args, **options):
        try:
            source = Source.objects.get(key=options["source_key"])
        except Source.DoesNotExist as exc:
            raise CommandError(f"No source {options['source_key']!r}") from exc

        snapshot = None
        if options["snapshot"]:
            snapshot = RawSnapshot.objects.filter(pk=options["snapshot"]).first()
            if snapshot is None:
                raise CommandError(f"No snapshot #{options['snapshot']}")

        try:
            result = reparse_snapshot(
                source, snapshot, log=self.stdout.write, dry_run=options["dry_run"]
            )
        except ReparseError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(result["summary"]))
