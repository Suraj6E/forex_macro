"""Dashboard — the overview screen.

Answers, in order: what do we hold, where did it come from, how good is it,
and what can this tool actually do yet.
"""

from __future__ import annotations

from django.db.models import Count, Sum
from django.shortcuts import render
from django.utils import timezone

from calendar_data.models import EventRelease
from dashboard import charts
from dashboard.roadmap import PHASES
from quality import stats
from sources import registry
from sources.models import FetchRun, Job, RawSnapshot, Source


def index(request):
    head = stats.headline()
    coverage_rows, total_releases = stats.field_coverage()

    sources = list(Source.objects.all())
    implemented = registry.implemented_keys()
    with_data = {c["source__key"] for c in stats.source_contributions()}

    snapshots = RawSnapshot.objects.aggregate(n=Count("id"), bytes=Sum("size_bytes"))

    scheduled = EventRelease.objects.select_related("indicator").filter(
        scheduled_time_utc__isnull=False
    )
    upcoming = list(
        scheduled.filter(scheduled_time_utc__gte=timezone.now()).order_by(
            "scheduled_time_utc"
        )[:8]
    )
    upcoming_label = "Next scheduled"
    if not upcoming:
        # The loaded week may already be in the past; showing nothing would
        # read as "no data" when the truth is "no data ahead of now".
        upcoming = list(scheduled.order_by("-scheduled_time_utc")[:8])
        upcoming_label = "Most recent scheduled"

    return render(
        request,
        "dashboard/index.html",
        {
            "nav": "dashboard",
            "head": head,
            "total_releases": total_releases,
            "coverage_chart": charts.hbar(
                coverage_rows,
                max_value=total_releases or 1,
                label_width=196,
                empty="No releases ingested yet — fetch a source to populate this.",
            ),
            "coverage_rows": coverage_rows,
            "density_chart": charts.vbar(
                stats.releases_per_day(),
                empty="No scheduled releases in the window around today.",
            ),
            "currency_chart": charts.hbar(
                stats.by_currency(), label_width=70, empty="No releases yet."
            ),
            "importance_chart": charts.hbar(
                stats.by_importance(), label_width=90, empty="No releases yet."
            ),
            "contributions": stats.source_contributions(),
            "sources_total": len(sources),
            "sources_implemented": len(implemented),
            "sources_with_data": len(with_data),
            "snapshot_count": snapshots["n"] or 0,
            "snapshot_bytes": snapshots["bytes"] or 0,
            "recent_jobs": Job.objects.all()[:6],
            "recent_runs": FetchRun.objects.select_related("source")[:6],
            "phases": PHASES,
            "next_releases": upcoming,
            "upcoming_label": upcoming_label,
        },
    )
