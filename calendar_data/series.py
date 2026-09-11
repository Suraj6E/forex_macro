"""Series queries for charting — planning.md §0a.

Every function here returns plain tuples from a single query with an explicit
column list. Nothing instantiates a model, nothing loads a table into Python
to filter it there. At 86,000 releases that distinction is the difference
between a page that renders and a page that crawls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.db.models import Avg, Count, Max, Min, Q, StdDev
from django.db.models.functions import TruncYear

from calendar_data.models import EventRelease

#: More points than a chart can show are wasted work. A 720px plot cannot
#: resolve more than a few hundred marks, so long series are thinned for
#: display — the underlying data is untouched.
MAX_PLOT_POINTS = 420


def indicator_series(
    indicator_id: int, *, since: datetime | None = None, until: datetime | None = None
) -> list[tuple]:
    """(released_at, actual, forecast, previous) for one indicator, oldest first."""
    queryset = EventRelease.objects.filter(
        indicator_id=indicator_id,
        release_time_utc__isnull=False,
        actual_current__isnull=False,
    )
    if since:
        queryset = queryset.filter(release_time_utc__gte=since)
    if until:
        queryset = queryset.filter(release_time_utc__lte=until)
    return list(
        queryset.order_by("release_time_utc").values_list(
            "release_time_utc", "actual_current", "forecast_stored", "previous"
        )
    )


def indicator_stats(indicator_id: int) -> dict:
    """Headline numbers for one indicator, computed in SQL."""
    return EventRelease.objects.filter(indicator_id=indicator_id).aggregate(
        releases=Count("id"),
        with_actual=Count("id", filter=Q(actual_current__isnull=False)),
        with_forecast=Count("id", filter=Q(forecast_stored__isnull=False)),
        with_revision=Count("id", filter=Q(revised_previous__isnull=False)),
        first=Min("release_time_utc"),
        last=Max("release_time_utc"),
        mean=Avg("actual_current"),
        lowest=Min("actual_current"),
        highest=Max("actual_current"),
    )


def surprise_series(indicator_id: int, *, since: datetime | None = None) -> list[tuple]:
    """(released_at, actual - forecast) where both exist.

    This is `surprise_raw`, not the standardised `surprise_z` of §3.5 — it is
    not divided by the indicator's own surprise σ, so it is readable for one
    indicator and meaningless across indicators.
    """
    queryset = EventRelease.objects.filter(
        indicator_id=indicator_id,
        release_time_utc__isnull=False,
        actual_current__isnull=False,
        forecast_stored__isnull=False,
    )
    if since:
        queryset = queryset.filter(release_time_utc__gte=since)
    rows = queryset.order_by("release_time_utc").values_list(
        "release_time_utc", "actual_current", "forecast_stored"
    )
    return [(when, float(actual) - float(forecast)) for when, actual, forecast in rows]


def sparkline_values(indicator_ids: list[int], points: int = 34) -> dict[int, list[float]]:
    """Recent actuals for several indicators in one query.

    One query for the whole page rather than one per row: the alternative is
    N+1 against a table with 86,000 rows.
    """
    if not indicator_ids:
        return {}

    rows = (
        EventRelease.objects.filter(
            indicator_id__in=indicator_ids,
            release_time_utc__isnull=False,
            actual_current__isnull=False,
        )
        .order_by("indicator_id", "-release_time_utc")
        .values_list("indicator_id", "actual_current")
    )

    collected: dict[int, list[float]] = {}
    for indicator_id, actual in rows:
        bucket = collected.setdefault(indicator_id, [])
        if len(bucket) < points:
            bucket.append(float(actual))
    return {key: list(reversed(values)) for key, values in collected.items()}


def releases_per_year() -> list[tuple[str, int]]:
    rows = (
        EventRelease.objects.filter(release_time_utc__isnull=False)
        .annotate(year=TruncYear("release_time_utc"))
        .values("year")
        .annotate(n=Count("id"))
        .order_by("year")
    )
    return [(f"{r['year']:%Y}", r["n"]) for r in rows]


def thin(points: list, limit: int = MAX_PLOT_POINTS) -> list:
    """Evenly drop points a chart cannot resolve, keeping first and last."""
    if len(points) <= limit:
        return points
    step = len(points) / limit
    thinned = [points[int(i * step)] for i in range(limit)]
    if thinned[-1] is not points[-1]:
        thinned[-1] = points[-1]
    return thinned


def default_window(span_years: int | None) -> datetime | None:
    if not span_years:
        return None
    return datetime.now(timezone.utc) - timedelta(days=365 * span_years)
