"""Source register, fetch runs, raw snapshots and the job queue.

planning.md §5 ("each of these is a row in a `source` table with its own fetch
button, policy and health — not a hardcoded script") and §7.2 (why a job
runner, and which one).

Deviation from §9, noted deliberately: §9 puts `snapshot_path` and
`snapshot_sha256` on `fetch_run`.  One fetch can produce many payloads —
HistData is ~1,600 monthly zips (§4.5) — so snapshots are a child table and
the run keeps the counters.
"""

from datetime import timedelta

from django.db import models
from django.db.models.functions import Concat
from django.utils import timezone


class SourceKind(models.TextChoices):
    CALENDAR = "calendar", "calendar / events"
    PRICE = "price", "price"
    ACTUALS = "actuals", "actuals / statistics"
    REFERENCE = "reference", "reference"


class Health(models.TextChoices):
    UNKNOWN = "unknown", "never fetched"
    OK = "ok", "ok"
    DEGRADED = "degraded", "degraded"
    FAILING = "failing", "failing"
    DISABLED = "disabled", "disabled"


class RobotsStatus(models.TextChoices):
    ALLOWED = "allowed", "allowed"
    RESTRICTED = "restricted", "restricted — see terms note"
    DISALLOWED = "disallowed", "disallowed"
    NOT_APPLICABLE = "n_a", "not applicable (local / API)"
    UNCHECKED = "unchecked", "unchecked"


class Source(models.Model):
    key = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=16, choices=SourceKind.choices)
    base_url = models.URLField(blank=True)

    # §4.1: every source stamps in whatever zone it likes.  This documents
    # which converter the normaliser applies; there is no default.
    timezone_rule = models.CharField(
        max_length=200,
        blank=True,
        help_text="Human-readable clock rule, e.g. 'fixed UTC-5, no DST' or "
        "'MT5 trade server time, DST-corrected'.",
    )

    fetch_policy_json = models.JSONField(
        default=dict,
        blank=True,
        help_text="Rate limits, cadence, retry policy. §5.4: politeness is "
        "self-interested here.",
    )
    terms_note = models.TextField(blank=True)
    robots_status = models.CharField(
        max_length=16, choices=RobotsStatus.choices, default=RobotsStatus.UNCHECKED
    )

    gives = models.CharField(
        max_length=300, blank=True, help_text="Which fields this source supplies (§5.1/§5.2)."
    )
    depth_note = models.CharField(
        max_length=200, blank=True, help_text="Known or measured history depth."
    )
    role = models.CharField(max_length=200, blank=True)

    enabled = models.BooleanField(default=True)
    supports_date_range = models.BooleanField(
        default=True,
        help_text="False for feeds that only ever return 'now', e.g. the "
        "ForexFactory weekly feed.",
    )

    last_fetch_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    health = models.CharField(max_length=16, choices=Health.choices, default=Health.UNKNOWN)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "key"]

    def __str__(self):
        return self.key

    def cooldown_remaining(self) -> timedelta | None:
        """How long until this source may be fetched again.

        §5.4: politeness here is self-interested, not ethical — a locked-out
        tool is a broken tool.  The policy lives in the DB rather than in a
        collector, so changing it is an edit, not a deploy.
        """
        hours = (self.fetch_policy_json or {}).get("min_interval_hours")
        if not hours or not self.last_success_at:
            return None
        ready_at = self.last_success_at + timedelta(hours=float(hours))
        remaining = ready_at - timezone.now()
        return remaining if remaining.total_seconds() > 0 else None

    def record_outcome(self, *, success: bool):
        now = timezone.now()
        self.last_fetch_at = now
        if success:
            self.last_success_at = now
            self.consecutive_failures = 0
            self.health = Health.OK
        else:
            self.consecutive_failures += 1
            self.health = Health.FAILING if self.consecutive_failures >= 3 else Health.DEGRADED
        self.save(
            update_fields=[
                "last_fetch_at",
                "last_success_at",
                "consecutive_failures",
                "health",
            ]
        )


class JobStatus(models.TextChoices):
    QUEUED = "queued", "queued"
    RUNNING = "running", "running"
    SUCCESS = "success", "success"
    FAILED = "failed", "failed"
    CANCELLED = "cancelled", "cancelled"


class Job(models.Model):
    """DB-backed queue, drained by a worker thread (§7.2).

    No Celery, no Redis: one user, occasional button presses, and progress has
    to survive a page reload — which a DB row does and an in-process future
    does not.
    """

    kind = models.SlugField(max_length=64)
    params_json = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=JobStatus.choices, default=JobStatus.QUEUED, db_index=True
    )

    progress = models.FloatField(default=0.0, help_text="0.0 – 1.0")
    message = models.CharField(max_length=300, blank=True)
    log = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_text = models.TextField(blank=True)
    result_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"{self.kind}#{self.pk} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in {JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELLED}

    @property
    def progress_percent(self) -> int:
        return int(round(self.progress * 100))

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at:
            return None
        end = self.finished_at or timezone.now()
        return (end - self.started_at).total_seconds()

    def append_log(self, line: str):
        stamped = f"{timezone.now():%H:%M:%S} {line}"
        Job.objects.filter(pk=self.pk).update(
            log=Concat(
                models.F("log"), models.Value(stamped + "\n"), output_field=models.TextField()
            )
        )


class FetchRun(models.Model):
    """One press of a Fetch button (§7.1)."""

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="runs")
    job = models.OneToOneField(
        Job, on_delete=models.SET_NULL, null=True, blank=True, related_name="fetch_run"
    )

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=JobStatus.choices, default=JobStatus.RUNNING
    )
    params_json = models.JSONField(default=dict, blank=True)

    rows_seen = models.IntegerField(default=0)
    rows_new = models.IntegerField(default=0)
    rows_changed = models.IntegerField(default=0)
    rows_unmapped = models.IntegerField(default=0)

    error_text = models.TextField(blank=True)
    parser_version = models.CharField(max_length=32, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["source", "-started_at"])]

    def __str__(self):
        return f"{self.source.key} @ {self.started_at:%Y-%m-%d %H:%M}"


class RawSnapshot(models.Model):
    """Immutable fetched payload on disk, content-hashed (§5.4).

    Kept so that a parser fix is a re-parse, not a re-crawl.
    """

    fetch_run = models.ForeignKey(FetchRun, on_delete=models.CASCADE, related_name="snapshots")
    fetched_at = models.DateTimeField(default=timezone.now)

    url = models.TextField(blank=True)
    path = models.TextField(help_text="Relative to settings.RAW_DIR.")
    sha256 = models.CharField(max_length=64, db_index=True)
    size_bytes = models.BigIntegerField(default=0)
    http_status = models.IntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=120, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["fetched_at"]

    def __str__(self):
        return f"{self.path} ({self.sha256[:12]})"
