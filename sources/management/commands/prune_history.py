"""CLI wrapper around sources.pruning.prune (§5.4).

The console button and this command run the same function.
"""

from django.core.management.base import BaseCommand

from sources.pruning import prunable, prune


class Command(BaseCommand):
    help = "Clear ingest job logs and stale raw payloads, keeping provenance."

    def add_arguments(self, parser):
        parser.add_argument("--keep-jobs", type=int, default=20)
        parser.add_argument("--keep-runs-per-source", type=int, default=1)
        parser.add_argument(
            "--keep-payloads",
            action="store_true",
            help="Clear job logs but leave the stored payload files in place.",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if options["dry_run"]:
            state = prunable()
            self.stdout.write(
                f"{state['jobs']} job record(s), {state['snapshots']} payload(s) "
                f"totalling {state['bytes'] / 1e6:.0f} MB would be removed."
            )
            return

        result = prune(
            keep_runs_per_source=options["keep_runs_per_source"],
            keep_jobs=options["keep_jobs"],
            drop_payloads=not options["keep_payloads"],
            log=self.stdout.write,
        )
        self.stdout.write(self.style.SUCCESS(result["summary"]))
