"""Study engine tables and the analytics result store — planning.md §9.

These live in `studies/` rather than `analytics/` because `analytics/` imports
nothing from Django (§8).  The library computes; this app stores and versions.

§0a is delivered here: a study is a saved specification you edit in the UI,
not code.  Adding a new question must never mean editing Python.
"""

from django.db import models

from analytics.horizons import LADDER, WindowScheme
from quality.enums import EpistemicStatus, Regime

HORIZON_CHOICES = [(h.label, h.label) for h in LADDER]


class OutlierPolicy(models.TextChoices):
    """§4.6 — defaults to `flag`, never to silent exclusion."""

    INCLUDE = "include", "include"
    FLAG = "flag", "flag (default)"
    WINSORISE = "winsorise", "winsorise"
    EXCLUDE = "exclude", "exclude"


class Mode(models.TextChoices):
    """§6.  Every study is stamped with which mode produced it.

    `DIRECTION` sits between the two and is never merged with either.  Mode A
    reports an unsigned magnitude and cannot say which way price went; Mode B
    reports the response to a *surprise* and needs a forecast the dataset does
    not yet have.  `DIRECTION` regresses the signed move on the change in the
    indicator against its own last print, which 99.9% of releases carry — a
    real and different quantity, attenuated relative to a surprise because part
    of any change was already priced.  See `analytics/direction.py`.
    """

    A = "A", "A — event-only (timestamps + price)"
    DIRECTION = "direction", "direction — signed response to the change vs previous"
    B = "B", "B — surprise-conditioned (needs the forecast column)"


class Sample(models.TextChoices):
    EXPLORE = "explore", "explore (2007–2018)"
    HOLDOUT = "holdout", "holdout (2019–)"
    FULL = "full", "full"


class AttributionConfidence(models.TextChoices):
    """§6.6 badge — high at minutes, degrading to low at weeks."""

    HIGH = "high", "high"
    MEDIUM = "medium", "medium"
    LOW = "low", "low"


class EventImpact(models.Model):
    """One row per event × instrument × horizon, pre- and post-.

    Stored per event rather than only as summaries (§6.9) so that a new
    question can usually be answered by re-querying instead of re-computing.

    Deviation from §9: `outlier_policy` there is replaced by a per-row
    `is_outlier` flag.  Winsorising and excluding are *pooling* decisions;
    measuring one event is not policy-dependent, so the policy belongs to the
    study, not to the measurement.  `window_scheme` / `window_seconds` are
    added for §3.2b, which requires window length beside every estimate.
    """

    event_release = models.ForeignKey(
        "calendar_data.EventRelease", on_delete=models.CASCADE, related_name="impacts"
    )
    instrument = models.ForeignKey("prices.Instrument", on_delete=models.CASCADE)
    price_source = models.ForeignKey("sources.Source", on_delete=models.CASCADE)

    horizon = models.CharField(max_length=16, db_index=True)
    window_scheme = models.CharField(
        max_length=24, choices=WindowScheme.CHOICES, default=WindowScheme.FIXED
    )
    window_seconds = models.IntegerField(
        help_text="Signed. Equals the ladder offset for fixed windows; measured "
        "for event-anchored ones (§3.2b)."
    )

    ret = models.FloatField(null=True, blank=True, help_text="Log return over the window.")
    abs_ret = models.FloatField(null=True, blank=True)
    abnormal_ret = models.FloatField(
        null=True,
        blank=True,
        help_text="Observed minus matched-normal. This is what makes it an "
        "event study rather than a picture of a move (§6.1).",
    )
    realized_vol = models.FloatField(null=True, blank=True)
    mfe = models.FloatField(null=True, blank=True, help_text="Maximum favourable excursion.")
    mae = models.FloatField(null=True, blank=True, help_text="Maximum adverse excursion.")
    spread_avg = models.FloatField(null=True, blank=True)

    n_bars = models.IntegerField(default=0)
    bars_missing = models.IntegerField(default=0)
    is_outlier = models.BooleanField(default=False)

    engine_version = models.CharField(max_length=32, db_index=True)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["event_release", "instrument", "horizon"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "event_release",
                    "instrument",
                    "price_source",
                    "horizon",
                    "window_scheme",
                    "engine_version",
                ],
                name="uniq_event_impact",
            )
        ]
        indexes = [models.Index(fields=["instrument", "horizon", "engine_version"])]

    def __str__(self):
        return f"{self.event_release_id} {self.instrument_id} {self.horizon}"


class CurrencyState(models.Model):
    """§3.4a — currency state is first-class, not one analysis.

    Usable as a filter ("only events that followed a quiet week"), as a
    control, or as an interaction term.  Most of the "20 more edge cases"
    are state questions of this kind.
    """

    currency = models.CharField(max_length=3, db_index=True)
    ts_utc = models.DateTimeField(db_index=True)

    cum_abn_move_1d = models.FloatField(null=True, blank=True)
    cum_abn_move_3d = models.FloatField(null=True, blank=True)
    cum_abn_move_5d = models.FloatField(null=True, blank=True)
    cum_abn_move_10d = models.FloatField(null=True, blank=True)

    cum_surprise_1d = models.FloatField(null=True, blank=True)
    cum_surprise_3d = models.FloatField(null=True, blank=True)
    cum_surprise_5d = models.FloatField(null=True, blank=True)

    days_since_tier1 = models.FloatField(null=True, blank=True)
    rel_volatility = models.FloatField(
        null=True, blank=True, help_text="Realised vol relative to its own trailing average."
    )
    regime = models.CharField(max_length=20, choices=Regime.choices, blank=True)

    engine_version = models.CharField(max_length=32, default="")

    class Meta:
        ordering = ["currency", "ts_utc"]
        constraints = [
            models.UniqueConstraint(fields=["currency", "ts_utc"], name="uniq_currency_state")
        ]

    def __str__(self):
        return f"{self.currency} @ {self.ts_utc:%Y-%m-%d %H:%M}Z"


class StudySpec(models.Model):
    """The §6.8 configuration object.  Editing forks a new version.

    Defaults produce a sensible study with zero configuration; every field is
    exposed for when you want to vary it.
    """

    name = models.CharField(max_length=200)
    version = models.PositiveIntegerField(default=1)
    parent_version = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="children"
    )
    spec_json = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["name", "version"], name="uniq_spec_version")
        ]

    def __str__(self):
        return f"{self.name} v{self.version}"


class StudyRun(models.Model):
    study_spec = models.ForeignKey(StudySpec, on_delete=models.CASCADE, related_name="runs")
    job = models.OneToOneField(
        "sources.Job", on_delete=models.SET_NULL, null=True, blank=True, related_name="study_run"
    )

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    sample = models.CharField(max_length=16, choices=Sample.choices, default=Sample.FULL)

    n_observations = models.IntegerField(default=0)
    engine_version = models.CharField(max_length=32, blank=True)
    result_ref = models.TextField(blank=True, help_text="Path or cache key for the result set.")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.study_spec} run #{self.pk}"


class Hypothesis(models.Model):
    """Optional plain-English label on a run (§6.9). Nothing depends on it."""

    question_text = models.TextField()
    study_spec = models.ForeignKey(
        StudySpec, on_delete=models.SET_NULL, null=True, blank=True, related_name="hypotheses"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=16, choices=EpistemicStatus.choices, default=EpistemicStatus.HYPOTHESIS
    )
    note_md = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "hypotheses"

    def __str__(self):
        return self.question_text[:80]


class DecayCurve(models.Model):
    """Cached §6.2 output — the deliverable.

    Half-life and significance horizon are derived from these rows rather than
    stored, so a methodology change invalidates one cache, not two.

    Keyed by indicator *or* release group: §3.3 Case B means same-instant
    releases are only ever attributed to the group.
    """

    indicator = models.ForeignKey(
        "calendar_data.Indicator",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="decay_curves",
    )
    release_group = models.ForeignKey(
        "calendar_data.ReleaseGroup",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="decay_curves",
    )
    instrument = models.ForeignKey("prices.Instrument", on_delete=models.CASCADE)
    study_run = models.ForeignKey(
        StudyRun, on_delete=models.CASCADE, null=True, blank=True, related_name="curves"
    )

    mode = models.CharField(max_length=16, choices=Mode.choices)
    horizon = models.CharField(max_length=16, choices=HORIZON_CHOICES)

    effect_size = models.FloatField(null=True, blank=True)
    std_error = models.FloatField(null=True, blank=True)
    r_squared = models.FloatField(null=True, blank=True)
    p_raw = models.FloatField(null=True, blank=True)
    p_fdr = models.FloatField(
        null=True, blank=True, help_text="Benjamini–Hochberg, beside the raw p (§6.6)."
    )
    detectability_floor = models.FloatField(
        null=True,
        blank=True,
        help_text="Smallest effect this n could have detected here, so a null "
        "reads as 'no effect larger than X' (§3.2).",
    )
    n = models.IntegerField(default=0)
    attribution_confidence = models.CharField(
        max_length=8, choices=AttributionConfidence.choices, default=AttributionConfidence.LOW
    )

    regime = models.CharField(max_length=20, choices=Regime.choices, blank=True)
    outlier_policy = models.CharField(
        max_length=12, choices=OutlierPolicy.choices, default=OutlierPolicy.FLAG
    )
    engine_version = models.CharField(max_length=32)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["instrument", "horizon"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(indicator__isnull=False, release_group__isnull=True)
                | models.Q(indicator__isnull=True, release_group__isnull=False),
                name="curve_targets_exactly_one_of_indicator_or_group",
            )
        ]
        indexes = [models.Index(fields=["instrument", "mode", "engine_version"])]

    def __str__(self):
        target = self.indicator or self.release_group
        return f"{target} × {self.instrument} @ {self.horizon}"
