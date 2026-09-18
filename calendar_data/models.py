"""Calendar side of the dataset — planning.md §9.

The identity rule is §4.4: a release is identified by
`(currency, canonical_code, reference_period, revision_no)`, **never** by its
timestamp.  Sources disagree on timestamps by a minute or two; making the
timestamp part of the key would manufacture duplicate rows for one release.
Timestamps are attributes, one per source, and their disagreement is a
measurable quality signal.
"""

from decimal import Decimal

from django.db import models

from quality.enums import (
    ActualProvenance,
    CrossSource,
    ForecastProvenance,
    Importance,
    MappingStatus,
    TimestampConfidence,
    VolCheck,
)

# MT5 stores values ×1,000,000 (§4.2 gotcha 1), so six decimal places is
# exactly lossless for that source and generous for every other.
VALUE_FIELD = dict(max_digits=24, decimal_places=6, null=True, blank=True)

CURRENCIES = [
    ("USD", "USD — United States"),
    ("EUR", "EUR — Euro area"),
    ("GBP", "GBP — United Kingdom"),
    ("JPY", "JPY — Japan"),
    ("CHF", "CHF — Switzerland"),
    ("AUD", "AUD — Australia"),
    ("CAD", "CAD — Canada"),
    ("NZD", "NZD — New Zealand"),
]


class ReleaseGroup(models.Model):
    """Events sharing one timestamp — §3.3 Case B.

    The US Employment Situation is payrolls + unemployment rate + average
    hourly earnings at one instant.  Perfectly collinear regressors; no amount
    of data separates them.  The group, not the member, is the unit of
    attribution.
    """

    key = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    country = models.CharField(max_length=64, blank=True)
    currency = models.CharField(max_length=3, choices=CURRENCIES, blank=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["currency", "key"]

    def __str__(self):
        return self.key


class Concept(models.TextChoices):
    """What *kind* of economic news this is — §6.4's pooling axis.

    Distinct from `canonical_code`, and deliberately so.  A canonical code
    identifies one series so that two sources naming it differently can be
    joined; a concept says what family it belongs to, so that "which kinds of
    news move FX most" can be answered across eight economies at once.  US CPI
    and German CPI are different series with different codes and the same
    concept.

    `SPEECH` and `POLITICAL` are separated from the data releases on purpose.
    They carry no number, so they can never have a direction, and pooling them
    with releases that do would put an unmeasurable thing in a ranking of
    measured ones.
    """

    LABOUR = "labour", "labour — employment, unemployment, claims, wages"
    INFLATION = "inflation", "inflation — CPI, PPI, price indices"
    POLICY_RATE = "policy_rate", "policy rate — decisions, minutes, facilities"
    GROWTH = "growth", "growth — GDP, production, productivity"
    SURVEY = "survey", "survey — PMI, sentiment, business confidence"
    CONSUMPTION = "consumption", "consumption — retail sales, spending, durables"
    HOUSING = "housing", "housing — permits, starts, prices, mortgages"
    TRADE = "trade", "trade — balance, current account"
    ENERGY = "energy", "energy — oil and gas inventories"
    FISCAL = "fiscal", "fiscal — budget, debt, auctions"
    SPEECH = "speech", "speech — no number, so no direction"
    POLITICAL = "political", "political — elections, votes, rulings"
    UNCLASSIFIED = "", "unclassified"


class Indicator(models.Model):
    country = models.CharField(max_length=64, blank=True)
    currency = models.CharField(max_length=3, choices=CURRENCIES, db_index=True)
    name = models.CharField(max_length=200)

    # Nullable on purpose: an indicator exists as soon as a source mentions it,
    # but only becomes canonical when a human assigns the code (§9 note).
    canonical_code = models.SlugField(max_length=64, unique=True, null=True, blank=True)
    mapping_status = models.CharField(
        max_length=16, choices=MappingStatus.choices, default=MappingStatus.AUTO, db_index=True
    )

    release_group = models.ForeignKey(
        ReleaseGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name="indicators"
    )

    concept = models.CharField(
        max_length=16,
        choices=Concept.choices,
        blank=True,
        default=Concept.UNCLASSIFIED,
        db_index=True,
        help_text="§6.4's pooling axis, derived from the name. Not the same thing "
        "as canonical_code, which identifies one series across sources.",
    )

    unit = models.CharField(max_length=32, blank=True)
    frequency = models.CharField(max_length=32, blank=True)
    importance = models.IntegerField(choices=Importance.choices, default=Importance.UNKNOWN)

    explainer_md = models.TextField(
        blank=True, help_text="Hand-written explainer (§6.10 / P6). Data, not commentary."
    )
    transmission_channel = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["currency", "name"]
        indexes = [models.Index(fields=["mapping_status", "currency"])]

    def __str__(self):
        return self.canonical_code or f"{self.currency} {self.name}"

    @property
    def is_mapped(self) -> bool:
        return bool(self.canonical_code)


class IndicatorAlias(models.Model):
    """Each source's own name for an indicator → our Indicator.

    §9 warns `canonical_code` "will be the buggiest artifact in the project".
    Making the mapping an explicit, editable table — rather than a fuzzy match
    at ingest time — is what makes it reviewable.
    """

    source = models.ForeignKey("sources.Source", on_delete=models.CASCADE, related_name="aliases")
    source_key = models.CharField(
        max_length=200, help_text="Stable identifier at the source (MT5 event_id, FF title, …)."
    )
    source_name = models.CharField(max_length=300, blank=True)
    currency = models.CharField(max_length=3, blank=True)

    indicator = models.ForeignKey(
        Indicator, on_delete=models.SET_NULL, null=True, blank=True, related_name="aliases"
    )
    first_seen_at = models.DateTimeField(auto_now_add=True)
    times_seen = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["source", "currency", "source_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "source_key"], name="uniq_alias_per_source"
            )
        ]

    def __str__(self):
        return f"{self.source_id}:{self.source_key}"


class EventRelease(models.Model):
    """One release of one indicator for one reference period."""

    indicator = models.ForeignKey(Indicator, on_delete=models.CASCADE, related_name="releases")
    release_group = models.ForeignKey(
        ReleaseGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name="releases"
    )

    reference_period = models.CharField(
        max_length=64,
        help_text="Normalised label for the period the number describes, "
        "e.g. '2024-01', '2024-Q1'. Part of the identity key (§4.4).",
    )
    # Not in §9; added because the label alone does not sort chronologically.
    reference_period_start = models.DateField(null=True, blank=True)
    revision_no = models.PositiveSmallIntegerField(default=0)

    release_time_utc = models.DateTimeField(null=True, blank=True, db_index=True)
    scheduled_time_utc = models.DateTimeField(null=True, blank=True)
    trading_day = models.DateField(
        null=True,
        blank=True,
        help_text="Derived, anchored to 17:00 America/New_York (§4.1). Never "
        "inferred from the UTC calendar date.",
    )

    # §4.3: three different numbers can occupy the same 'actual' cell.  They
    # get separate columns and are never coalesced silently.
    actual_first_print = models.DecimalField(**VALUE_FIELD)
    actual_current = models.DecimalField(**VALUE_FIELD)
    forecast_point_in_time = models.DecimalField(**VALUE_FIELD)
    forecast_stored = models.DecimalField(**VALUE_FIELD)
    forecast_modelled = models.DecimalField(**VALUE_FIELD)
    previous = models.DecimalField(**VALUE_FIELD)
    revised_previous = models.DecimalField(**VALUE_FIELD)

    surprise_raw = models.DecimalField(**VALUE_FIELD)
    surprise_z = models.FloatField(null=True, blank=True)

    # §5.3 — visible quality fields.
    timestamp_confidence = models.CharField(
        max_length=16,
        choices=TimestampConfidence.choices,
        default=TimestampConfidence.INFERRED,
    )
    forecast_provenance = models.CharField(
        max_length=16, choices=ForecastProvenance.choices, default=ForecastProvenance.NONE
    )
    actual_provenance = models.CharField(
        max_length=24, choices=ActualProvenance.choices, default=ActualProvenance.UNKNOWN
    )
    cross_source = models.CharField(
        max_length=16, choices=CrossSource.choices, default=CrossSource.SINGLE
    )
    vol_check = models.CharField(
        max_length=16, choices=VolCheck.choices, default=VolCheck.NOT_CHECKED
    )
    # Not in §9; the grade alone cannot tell you *how* wrong a timestamp is, and
    # "wrong by exactly one hour, always" is the diagnosis the DST risk needs.
    vol_check_offset_min = models.IntegerField(
        null=True,
        blank=True,
        help_text="Signed minutes from the stored timestamp to where the "
        "volatility spike actually was. 0 when the spike is in the stored bar.",
    )
    vol_check_ratio = models.FloatField(
        null=True,
        blank=True,
        help_text="Movement in the stored bar as a multiple of what this pair "
        "normally does in the same weekday-and-hour slot.",
    )
    confounded = models.BooleanField(
        default=False, help_text="Another event within ±15 min. Window scales with horizon (§6.6)."
    )

    source_map_json = models.JSONField(
        default=dict,
        blank=True,
        help_text="field -> source key. The merged row is a composite, not a "
        "copy of one source (§4.4).",
    )

    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    #: Fields the consolidation layer treats as merged values.
    MERGEABLE_FIELDS = (
        "release_time_utc",
        "scheduled_time_utc",
        "actual_first_print",
        "actual_current",
        "forecast_point_in_time",
        "forecast_stored",
        "previous",
        "revised_previous",
    )

    class Meta:
        ordering = ["-release_time_utc"]
        constraints = [
            models.UniqueConstraint(
                fields=["indicator", "reference_period", "revision_no"],
                name="uniq_release_identity",
            )
        ]
        indexes = [
            models.Index(fields=["release_time_utc", "indicator"]),
            models.Index(fields=["trading_day"]),
        ]

    def __str__(self):
        when = self.release_time_utc or self.scheduled_time_utc
        stamp = f"{when:%Y-%m-%d %H:%M}Z" if when else "unscheduled"
        return f"{self.indicator} {self.reference_period} @ {stamp}"

    @property
    def actual(self) -> Decimal | None:
        """Preferred actual, first print first.  Explicit, never silent —
        callers that care which one they got should read the columns."""
        return self.actual_first_print if self.actual_first_print is not None else self.actual_current

    @property
    def forecast(self) -> Decimal | None:
        if self.forecast_point_in_time is not None:
            return self.forecast_point_in_time
        return self.forecast_stored


class SourceObservation(models.Model):
    """What each source said, kept verbatim.

    §9: this is what makes the dataset a product.  `EventRelease` is a derived
    view; because every source's raw claim survives here, the merge can be
    re-run with different priorities without re-crawling anything.
    """

    event_release = models.ForeignKey(
        EventRelease, on_delete=models.CASCADE, related_name="observations"
    )
    source = models.ForeignKey("sources.Source", on_delete=models.CASCADE)
    fetch_run = models.ForeignKey(
        "sources.FetchRun", on_delete=models.SET_NULL, null=True, blank=True
    )
    observed_at = models.DateTimeField(auto_now_add=True)
    raw_json = models.JSONField(default=dict)

    class Meta:
        ordering = ["-observed_at"]
        indexes = [models.Index(fields=["event_release", "source"])]

    def __str__(self):
        return f"{self.source_id} on {self.event_release_id}"


class ValueRevision(models.Model):
    """Append-only.  How you discover a site quietly changing a number (§7.2)."""

    event_release = models.ForeignKey(
        EventRelease, on_delete=models.CASCADE, related_name="revisions"
    )
    observed_at = models.DateTimeField(auto_now_add=True)
    field = models.CharField(max_length=64)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    source = models.ForeignKey(
        "sources.Source", on_delete=models.SET_NULL, null=True, blank=True
    )
    fetch_run = models.ForeignKey(
        "sources.FetchRun", on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-observed_at"]
        indexes = [models.Index(fields=["event_release", "field"])]

    def __str__(self):
        return f"{self.field}: {self.old_value!r} -> {self.new_value!r}"
