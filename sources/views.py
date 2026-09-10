"""Ingestion console — planning.md §7.1, screen 1 of §10.

Every operation this project has is a button on this page. There is no command
you are expected to remember, and no login: it is a local single-user tool, so
authentication would protect nothing.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from quality import stats
from sources import jobs, registry
from sources.models import FetchRun, Job, JobStatus, RawSnapshot, Source

ACTIVE_JOB_LIMIT = 10


def _require_post(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    return None


def console(request):
    implemented = registry.implemented_keys()
    contributions = {c["source__key"]: c for c in stats.source_contributions()}

    sources = list(
        Source.objects.annotate(
            run_count=Count("runs", distinct=True),
        )
    )
    for source in sources:
        source.has_collector = source.key in implemented
        source.latest_run = source.runs.first()
        source.cooldown = source.cooldown_remaining()
        source.contribution = contributions.get(source.key)
        source.snapshot_count = RawSnapshot.objects.filter(fetch_run__source=source).count()

    calendar_sources = [s for s in sources if s.kind in ("calendar", "actuals", "reference")]
    price_sources = [s for s in sources if s.kind == "price"]

    return render(
        request,
        "sources/console.html",
        {
            "nav": "sources",
            "calendar_sources": calendar_sources,
            "price_sources": price_sources,
            "implemented_count": len(implemented),
            "sources_total": len(sources),
            "reference_seeded": Source.objects.exists(),
        },
    )


def fetch(request, key: str):
    if (bad := _require_post(request)) is not None:
        return bad

    source = get_object_or_404(Source, key=key)
    if registry.get(source.key) is None:
        messages.warning(
            request,
            f"{source.name} is a register row without a collector yet — nothing "
            f"was queued. Its clock and fetch policy are recorded so the "
            f"collector has somewhere to land.",
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
    messages.success(request, f"Queued a fetch of {source.name} — job #{job.pk}.")
    return redirect(request.POST.get("next") or "sources:console")


def reparse(request, key: str):
    """§5.4: a parser fix is a re-parse, not a re-crawl."""
    if (bad := _require_post(request)) is not None:
        return bad

    source = get_object_or_404(Source, key=key)
    job = jobs.enqueue("reparse_snapshot", source_key=source.key)
    messages.success(
        request,
        f"Queued a re-parse of {source.name}'s latest stored snapshot — job "
        f"#{job.pk}. No network request is made.",
    )
    return redirect(request.POST.get("next") or "sources:console")


def seed(request):
    if (bad := _require_post(request)) is not None:
        return bad
    job = jobs.enqueue("seed_reference")
    messages.success(request, f"Queued reference-data seeding — job #{job.pk}.")
    return redirect(request.POST.get("next") or "sources:console")


def toggle_source(request, key: str):
    if (bad := _require_post(request)) is not None:
        return bad
    source = get_object_or_404(Source, key=key)
    source.enabled = not source.enabled
    source.save(update_fields=["enabled"])
    messages.success(
        request, f"{source.name} is now {'enabled' if source.enabled else 'disabled'}."
    )
    return redirect("sources:console")


def rerun_job(request, pk: int):
    if (bad := _require_post(request)) is not None:
        return bad
    original = get_object_or_404(Job, pk=pk)
    job = jobs.enqueue(original.kind, **(original.params_json or {}))
    messages.success(request, f"Re-queued {original.kind} as job #{job.pk}.")
    return redirect("sources:job_detail", pk=job.pk)


def jobs_list(request):
    queryset = Job.objects.all()
    kind = request.GET.get("kind") or ""
    status = request.GET.get("status") or ""
    if kind:
        queryset = queryset.filter(kind=kind)
    if status:
        queryset = queryset.filter(status=status)

    page = Paginator(queryset, 40).get_page(request.GET.get("page"))
    params = {k: v for k, v in request.GET.items() if k != "page" and v}
    return render(
        request,
        "sources/jobs.html",
        {
            "nav": "jobs",
            "page": page,
            "querystring": urlencode(params) + "&" if params else "",
            "kinds": Job.objects.values_list("kind", flat=True).distinct(),
            "statuses": JobStatus.choices,
            "kind": kind,
            "status": status,
            "runs": FetchRun.objects.select_related("source")[:15],
        },
    )


def jobs_panel(request):
    """Polled fragment. Progress lives in the DB, so a reload never loses it."""
    return render(
        request,
        "sources/_jobs.html",
        {"jobs": Job.objects.all()[:ACTIVE_JOB_LIMIT]},
    )


def job_detail(request, pk: int):
    job = get_object_or_404(Job, pk=pk)
    return render(request, "sources/job_detail.html", {"nav": "jobs", "job": job})


def run_detail(request, pk: int):
    run = get_object_or_404(
        FetchRun.objects.select_related("source", "job").prefetch_related("snapshots"), pk=pk
    )
    totals = run.snapshots.aggregate(n=Count("id"), bytes=Sum("size_bytes"))
    return render(
        request,
        "sources/run_detail.html",
        {
            "nav": "jobs",
            "run": run,
            "snapshot_count": totals["n"] or 0,
            "snapshot_bytes": totals["bytes"] or 0,
        },
    )
