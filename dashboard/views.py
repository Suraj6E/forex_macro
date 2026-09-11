"""Overview — what the dataset holds, shown rather than tabulated.

Every number here comes from an aggregate query. Nothing loads the release
table into Python.
"""

from __future__ import annotations

from django.db.models import Count, Q, Sum
from django.shortcuts import render

from calendar_data import series
from calendar_data.models import Indicator
from dashboard import charts
from dashboard.roadmap import PHASES
from quality import stats
from sources import registry
from sources.models import Job, RawSnapshot, Source

#: Enough to see the shape of the register without becoming a second table.
TOP_INDICATORS = 10


def index(request):
    head = stats.headline()
    coverage_rows, total_releases = stats.field_coverage()

    top = list(
        Indicator.objects.annotate(n=Count("releases"))
        .filter(n__gt=0)
        .order_by("-n")
        .values_list("currency", "name", "n")[:TOP_INDICATORS]
    )
    high_impact = list(
        Indicator.objects.filter(importance=3)
        .annotate(n=Count("releases"))
        .filter(n__gt=0)
        .order_by("-n")
        .values_list("currency", "name", "n")[:TOP_INDICATORS]
    )

    snapshots = RawSnapshot.objects.aggregate(n=Count("id"), bytes=Sum("size_bytes"))

    return render(
        request,
        "dashboard/index.html",
        {
            "nav": "dashboard",
            "head": head,
            "total_releases": total_releases,
            "per_year_chart": charts.vbar(
                series.releases_per_year(), height=168, axis_every=2,
                empty="No released events yet.",
            ),
            "currency_chart": charts.hbar(
                stats.by_currency(), label_width=64, empty="No releases yet."
            ),
            "coverage_chart": charts.hbar(
                coverage_rows, max_value=total_releases or 1, label_width=190,
                empty="No releases ingested yet.",
            ),
            "top_chart": charts.hbar(
                [(f"{c}  {n[:30]}", count) for c, n, count in top],
                label_width=230, empty="No indicators yet.",
            ),
            "impact_chart": charts.hbar(
                [(f"{c}  {n[:30]}", count) for c, n, count in high_impact],
                label_width=230, empty="No high-impact indicators yet.",
            ),
            "contributions": stats.source_contributions(),
            "sources_total": Source.objects.count(),
            "sources_implemented": len(registry.implemented_keys()),
            "snapshot_count": snapshots["n"] or 0,
            "snapshot_bytes": snapshots["bytes"] or 0,
            "recent_jobs": Job.objects.all()[:5],
            "phases": PHASES,
        },
    )
