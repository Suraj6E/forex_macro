"""Run a queued job in the foreground.

Debugging aid: the worker swallows tracebacks into `job.error_text`, which is
right for the console and wrong for a stack trace you want to read.  Run
`FXMACRO_WORKER=0 python manage.py run_job <id>` to get the failure inline.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from sources import jobs
from sources.models import Job, JobStatus


class Command(BaseCommand):
    help = "Execute a job by id in this process."

    def add_arguments(self, parser):
        parser.add_argument("job_id", type=int)
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-run a job that is not queued.",
        )

    def handle(self, *args, **options):
        try:
            job = Job.objects.get(pk=options["job_id"])
        except Job.DoesNotExist as exc:
            raise CommandError(f"No job #{options['job_id']}") from exc

        if job.status != JobStatus.QUEUED and not options["force"]:
            raise CommandError(f"Job #{job.pk} is {job.status}; pass --force to re-run.")

        Job.objects.filter(pk=job.pk).update(
            status=JobStatus.RUNNING, started_at=timezone.now(), progress=0.0, error_text=""
        )
        job.refresh_from_db()
        jobs.run_job(job)
        job.refresh_from_db()

        self.stdout.write(f"job #{job.pk} -> {job.status}")
        if job.error_text:
            self.stderr.write(job.error_text)
        elif job.result_json:
            self.stdout.write(str(job.result_json))
