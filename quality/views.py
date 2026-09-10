"""Data quality screen — planning.md §10 screen 2, "deliberately prominent".

Coverage gaps, timestamp-confidence breakdown, cross-source disagreements,
unmapped events and the forward-capture log. The point of putting this second
in the navigation is that a chart you cannot audit is worse than no chart.
"""

from __future__ import annotations

from django.db.models import Count, Q
from django.shortcuts import render

from calendar_data.models import EventRelease, Indicator, ValueRevision
from dashboard import charts
from quality import stats
from quality.enums import CrossSource, MappingStatus, VolCheck
from sources.models import FetchRun, Source


def index(request):
    breakdown = stats.quality_breakdown()
    coverage_rows, total = stats.field_coverage()

    def chart(key, label_width=210):
        return charts.hbar(
            breakdown[key],
            max_value=total or 1,
            label_width=label_width,
            empty="No releases ingested yet.",
        )

    disagreements = (
        EventRelease.objects.select_related("indicator")
        .filter(cross_source=CrossSource.DISAGREE)
        .order_by("-scheduled_time_utc")[:25]
    )
    unmapped = (
        Indicator.objects.filter(canonical_code__isnull=True)
        .exclude(mapping_status=MappingStatus.IGNORED)
        .annotate(release_count=Count("releases"))
        .order_by("-release_count")[:15]
    )
    unmapped_total = (
        Indicator.objects.filter(canonical_code__isnull=True)
        .exclude(mapping_status=MappingStatus.IGNORED)
        .count()
    )

    captures = FetchRun.objects.select_related("source").filter(
        source__key="forexfactory_weekly"
    )[:10]

    return render(
        request,
        "quality/index.html",
        {
            "nav": "quality",
            "total": total,
            "head": stats.headline(),
            "coverage_chart": charts.hbar(
                coverage_rows, max_value=total or 1, label_width=196,
                empty="No releases ingested yet."
            ),
            "timestamp_chart": chart("timestamp_confidence"),
            "forecast_chart": chart("forecast_provenance"),
            "actual_chart": chart("actual_provenance"),
            "cross_chart": chart("cross_source"),
            "volcheck_chart": chart("vol_check"),
            "disagreements": disagreements,
            "unmapped": unmapped,
            "unmapped_total": unmapped_total,
            "revisions": ValueRevision.objects.select_related(
                "event_release__indicator", "source"
            )[:15],
            "sources": Source.objects.exclude(health="unknown"),
            "unhealthy": Source.objects.filter(
                Q(health="failing") | Q(health="degraded")
            ),
            "captures": captures,
            "unchecked_pct": (
                100.0 * EventRelease.objects.filter(vol_check=VolCheck.NOT_CHECKED).count() / total
                if total else 0
            ),
        },
    )
