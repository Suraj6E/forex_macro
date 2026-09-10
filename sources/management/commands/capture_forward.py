"""Weekly forward capture — planning.md §7.3.

    Premise: point-in-time forecast can only be captured *before* the release.
    Premise: you want continuous forward analysis, indefinitely.
    Inference: a missed week is permanently lost — unlike historical data, it
    cannot be re-fetched later.
    Conclusion: one scheduled task, and only one.

This runs the job **in-process** rather than enqueueing it, so a Windows Task
Scheduler entry (`python manage.py capture_forward`) does the work and exits.
Set `FXMACRO_WORKER=0` in the scheduled task's environment so no worker thread
is spawned for a process that is about to die.

Suggested cadence (§7.3): Sunday before the week opens for the forward
snapshot; Saturday for the actuals backfill once an MT5 collector exists.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from sources import jobs
from sources.models import Job, JobStatus, Source


class Command(BaseCommand):
    help = "Capture next week's calendar before the releases happen (§7.3)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            default="forexfactory_weekly",
            help="Source key to capture. Defaults to the ForexFactory weekly feed.",
        )
        parser.add_argument(
            "--queue-only",
            action="store_true",
            help="Enqueue and return instead of running here (needs a live worker).",
        )

    def handle(self, *args, **options):
        key = options["source"]
        if not Source.objects.filter(key=key).exists():
            raise CommandError(f"No source {key!r}. Run `manage.py seed_reference` first.")

        job = jobs.enqueue("fetch_source", source_key=key)
        if options["queue_only"]:
            self.stdout.write(f"queued job #{job.pk}")
            return

        Job.objects.filter(pk=job.pk).update(
            status=JobStatus.RUNNING, started_at=timezone.now()
        )
        job.refresh_from_db()
        jobs.run_job(job)
        job.refresh_from_db()

        if job.status != JobStatus.SUCCESS:
            self.stderr.write(job.error_text or "capture failed")
            raise CommandError(f"job #{job.pk} finished {job.status}")

        self.stdout.write(self.style.SUCCESS(job.result_json.get("summary", "captured")))
        if job.result_json.get("notes"):
            self.stdout.write(job.result_json["notes"])
