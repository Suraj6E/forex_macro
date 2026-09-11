"""Sidebar counts, available on every page.

Cheap COUNTs against SQLite. They exist so the navigation itself reports the
state of the dataset — planning.md §5.3: quality is a visible field, not a
report you have to go looking for.
"""

from django.db.models import Count, Q

from calendar_data.models import EventRelease, Indicator
from prices.models import Instrument
from quality.enums import CrossSource
from sources.models import Job, JobStatus, Source


def nav_counts(request):
    if request.path.startswith("/admin"):
        return {}

    indicators = Indicator.objects.aggregate(
        total=Count("id"), unmapped=Count("id", filter=Q(canonical_code__isnull=True))
    )
    releases = EventRelease.objects.aggregate(
        total=Count("id"),
        disagree=Count("id", filter=Q(cross_source=CrossSource.DISAGREE)),
    )
    total = releases["total"]
    return {
        "nav_counts": {
            "releases": total,
            # 86,450 in a 40px-wide chip is unreadable; 86k is not.
            "releases_short": f"{total / 1000:.0f}k" if total >= 10_000 else total,
            "indicators": indicators["total"],
            "instruments": Instrument.objects.count(),
            "sources": Source.objects.count(),
            "jobs_active": Job.objects.filter(
                status__in=[JobStatus.QUEUED, JobStatus.RUNNING]
            ).count(),
            "quality_flags": indicators["unmapped"] + releases["disagree"],
        }
    }
