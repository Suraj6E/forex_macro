"""Dataset state, computed — planning.md §5.3.

"You asked to see source and quality. These are visible fields, not internal
notes."  Everything here answers a question the console should not make you
run SQL for: what do we hold, where did it come from, and how much of it is
trustworthy.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Max, Min, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from calendar_data.models import EventRelease, Indicator, SourceObservation
from quality.enums import (
    ActualProvenance,
    CrossSource,
    ForecastProvenance,
    Importance,
    TimestampConfidence,
    VolCheck,
)

#: The value columns, in the order the §9 model lists them.  Splitting actual
#: and forecast into first-print / current / point-in-time / stored is §4.3:
#: three different numbers can occupy the same cell and they are never
#: coalesced silently.
VALUE_FIELDS = [
    ("scheduled_time_utc", "Scheduled time"),
    ("release_time_utc", "Observed release time"),
    ("forecast_point_in_time", "Forecast — point in time"),
    ("forecast_stored", "Forecast — vendor stored"),
    ("forecast_modelled", "Forecast — modelled (§6.7)"),
    ("actual_first_print", "Actual — first print"),
    ("actual_current", "Actual — current"),
    ("previous", "Previous"),
    ("revised_previous", "Revised previous"),
    ("surprise_z", "Surprise (z)"),
]


def field_coverage() -> tuple[list[tuple[str, int]], int]:
    """How many releases carry each column.  Returns (rows, total)."""
    filters = {
        name: Count("id", filter=Q(**{f"{name}__isnull": False}))
        for name, _ in VALUE_FIELDS
    }
    agg = EventRelease.objects.aggregate(total=Count("id"), **filters)
    total = agg["total"]
    return [(label, agg[name]) for name, label in VALUE_FIELDS], total


def _labelled_counts(field: str, choices) -> list[tuple[str, int]]:
    counts = dict(
        EventRelease.objects.values_list(field)
        .annotate(n=Count("id"))
        .values_list(field, "n")
    )
    return [(label, counts.get(value, 0)) for value, label in choices]


def quality_breakdown() -> dict[str, list[tuple[str, int]]]:
    return {
        "timestamp_confidence": _labelled_counts(
            "timestamp_confidence", TimestampConfidence.choices
        ),
        "forecast_provenance": _labelled_counts(
            "forecast_provenance", ForecastProvenance.choices
        ),
        "actual_provenance": _labelled_counts("actual_provenance", ActualProvenance.choices),
        "cross_source": _labelled_counts("cross_source", CrossSource.choices),
        "vol_check": _labelled_counts("vol_check", VolCheck.choices),
    }


def source_contributions() -> list[dict]:
    """Which source gave us what.  §4.4: the merged row is a composite, not a
    copy of one source, so 'rows' here means claims observed, not rows owned."""
    return list(
        SourceObservation.objects.values("source__key", "source__name")
        .annotate(
            observations=Count("id"),
            releases=Count("event_release", distinct=True),
            indicators=Count("event_release__indicator", distinct=True),
            earliest=Min("event_release__scheduled_time_utc"),
            latest=Max("event_release__scheduled_time_utc"),
        )
        .order_by("-observations")
    )


def releases_per_day(days_back: int = 7, days_forward: int = 14) -> list[tuple[str, int]]:
    """Event density around today, from whatever is loaded."""
    now = timezone.now()
    start = (now - timedelta(days=days_back)).date()
    end = (now + timedelta(days=days_forward)).date()

    counts = dict(
        EventRelease.objects.filter(
            scheduled_time_utc__date__gte=start, scheduled_time_utc__date__lte=end
        )
        .annotate(day=TruncDate("scheduled_time_utc"))
        .values_list("day")
        .annotate(n=Count("id"))
        .values_list("day", "n")
    )
    out = []
    day = start
    while day <= end:
        out.append((day.strftime("%d %b"), counts.get(day, 0)))
        day += timedelta(days=1)
    return out


def by_currency() -> list[tuple[str, int]]:
    rows = (
        EventRelease.objects.values("indicator__currency")
        .annotate(n=Count("id"))
        .order_by("-n")
    )
    return [(r["indicator__currency"] or "—", r["n"]) for r in rows]


def by_importance() -> list[tuple[str, int]]:
    labels = dict(Importance.choices)
    rows = (
        EventRelease.objects.values("indicator__importance")
        .annotate(n=Count("id"))
        .order_by("-indicator__importance")
    )
    return [(labels.get(r["indicator__importance"], "unknown"), r["n"]) for r in rows]


def headline() -> dict:
    indicators = Indicator.objects.aggregate(
        total=Count("id"),
        mapped=Count("id", filter=Q(canonical_code__isnull=False)),
    )
    releases = EventRelease.objects.aggregate(
        total=Count("id"),
        disagree=Count("id", filter=Q(cross_source=CrossSource.DISAGREE)),
        point_in_time=Count(
            "id", filter=Q(forecast_provenance=ForecastProvenance.POINT_IN_TIME)
        ),
        with_actual=Count("id", filter=Q(actual_first_print__isnull=False)
                          | Q(actual_current__isnull=False)),
        earliest=Min("scheduled_time_utc"),
        latest=Max("scheduled_time_utc"),
        unchecked=Count("id", filter=Q(vol_check=VolCheck.NOT_CHECKED)),
    )
    return {
        "indicators_total": indicators["total"],
        "indicators_mapped": indicators["mapped"],
        "indicators_unmapped": indicators["total"] - indicators["mapped"],
        "observations": SourceObservation.objects.count(),
        **releases,
    }
