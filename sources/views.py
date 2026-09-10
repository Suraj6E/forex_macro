"""Sources console — planning.md §7.1, screen 1 of §10.

"A Sources page lists each source with state (coverage, last fetch, health,
quality grade), a date-range picker and a Fetch button.  Pressing it enqueues
a job; the page shows live progress; the result is a run record with rows
seen/new/changed, errors, and a link to the raw snapshot."
"""

from __future__ import annotations

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from calendar_data.models import EventRelease, Indicator, SourceObservation
from quality.enums import CrossSource, ForecastProvenance, MappingStatus
from sources import jobs, registry
from sources.models import FetchRun, Job, Source

ACTIVE_JOB_LIMIT = 12


def _dataset_summary() -> dict:
    indicators = Indicator.objects.aggregate(
        total=Count("id"),
        unmapped=Count("id", filter=Q(canonical_code__isnull=True)),
    )
    releases = EventRelease.objects.aggregate(
        total=Count("id"),
        disagree=Count("id", filter=Q(cross_source=CrossSource.DISAGREE)),
        point_in_time=Count(
            "id", filter=Q(forecast_provenance=ForecastProvenance.POINT_IN_TIME)
        ),
    )
    return {
        "indicators_total": indicators["total"],
        "indicators_unmapped": indicators["unmapped"],
        "releases_total": releases["total"],
        "releases_disagree": releases["disagree"],
        "releases_point_in_time": releases["point_in_time"],
        "observations_total": SourceObservation.objects.count(),
    }


def console(request):
    implemented = registry.implemented_keys()
    sources = list(Source.objects.all())

    for source in sources:
        source.has_collector = source.key in implemented
        source.latest_run = source.runs.first()
        source.run_count = source.runs.count()
        source.cooldown = source.cooldown_remaining()

    return render(
        request,
        "sources/console.html",
        {
            "sources": sources,
            "summary": _dataset_summary(),
            "jobs": Job.objects.all()[:ACTIVE_JOB_LIMIT],
        },
    )


def fetch(request, key: str):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    source = get_object_or_404(Source, key=key)
    if registry.get(source.key) is None:
        messages.warning(
            request,
            f"{source.name} is registered but its collector is not built yet — "
            f"nothing was enqueued.",
        )
        return redirect("sources:console")

    params = {"source_key": source.key}
    for field in ("date_from", "date_to"):
        value = (request.POST.get(field) or "").strip()
        if value:
            params[field] = value
    if request.POST.get("force"):
        params["force"] = True

    job = jobs.enqueue("fetch_source", **params)
    messages.success(request, f"Queued fetch for {source.name} (job #{job.pk}).")
    return redirect("sources:console")


def jobs_panel(request):
    """Polled fragment.  Progress lives in the DB, so a reload never loses it."""
    return render(
        request,
        "sources/_jobs.html",
        {"jobs": Job.objects.all()[:ACTIVE_JOB_LIMIT], "summary": _dataset_summary()},
    )


def job_detail(request, pk: int):
    job = get_object_or_404(Job, pk=pk)
    return render(request, "sources/job_detail.html", {"job": job})


def run_detail(request, pk: int):
    run = get_object_or_404(
        FetchRun.objects.select_related("source", "job").prefetch_related("snapshots"), pk=pk
    )
    return render(request, "sources/run_detail.html", {"run": run})


def unmapped(request):
    """§9: the "unmapped events" report.  `canonical_code` will be the
    buggiest artifact in the project, so it gets a screen from day one."""
    indicators = (
        Indicator.objects.filter(canonical_code__isnull=True)
        .exclude(mapping_status=MappingStatus.IGNORED)
        .annotate(alias_count=Count("aliases"), release_count=Count("releases", distinct=True))
        .order_by("currency", "name")
    )
    return render(request, "sources/unmapped.html", {"indicators": indicators})
