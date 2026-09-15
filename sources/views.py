"""Ingestion console — planning.md §7.1, screen 1 of §10.

Every operation this project has is a button on this page. There is no command
you are expected to remember, and no login: it is a local single-user tool, so
authentication would protect nothing.
"""

from __future__ import annotations

import json
import uuid
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.http import Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from prices.models import Instrument, PriceCoverage
from quality import stats
from sources import jobs, registry
from sources.models import FetchRun, Job, JobStatus, RawSnapshot, Source

ACTIVE_JOB_LIMIT = 10
UPLOAD_MAX_BYTES = 200 * 1024 * 1024
RAW_VIEW_BYTES = 40_000


def _require_post(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    return None


def _decorate(source: Source) -> Source:
    collector = registry.get(source.key)
    source.collector = collector
    source.has_collector = collector is not None
    source.needs_symbols = bool(collector and collector.needs_symbols)
    source.accepts_upload = bool(collector and collector.accepts_upload)
    source.requires_date_range = bool(collector and collector.requires_date_range)
    source.cooldown = source.cooldown_remaining()
    return source


def console(request):
    contributions = {c["source__key"]: c for c in stats.source_contributions()}
    bars = {
        row["source__key"]: row["bars"]
        for row in PriceCoverage.objects.values("source__key").annotate(bars=Sum("bar_count"))
    }

    sources = list(Source.objects.annotate(run_count=Count("runs", distinct=True)))
    for source in sources:
        _decorate(source)
        source.latest_run = source.runs.first()
        source.contribution = contributions.get(source.key)
        source.bars = bars.get(source.key, 0)
        source.snapshot_count = RawSnapshot.objects.filter(fetch_run__source=source).count()

    return render(
        request,
        "sources/console.html",
        {
            "nav": "sources",
            "calendar_sources": [
                s for s in sources if s.kind in ("calendar", "actuals", "reference")
            ],
            "price_sources": [s for s in sources if s.kind == "price"],
            "instruments": Instrument.objects.filter(enabled=True),
            "implemented_count": len(registry.implemented_keys()),
            "sources_total": len(sources),
        },
    )


def _collect_params(request) -> dict:
    params: dict = {}
    for field in ("date_from", "date_to"):
        value = (request.POST.get(field) or "").strip()
        if value:
            params[field] = value
    symbols = request.POST.getlist("symbols")
    if symbols:
        params["symbols"] = symbols
    timeframe = (request.POST.get("timeframe") or "").strip()
    if timeframe:
        params["timeframe"] = timeframe
    if request.POST.get("force"):
        params["force"] = True
    return params


def _stage_upload(uploaded) -> tuple[str, str]:
    if uploaded.size > UPLOAD_MAX_BYTES:
        raise ValueError(
            f"{uploaded.name} is {uploaded.size / 1e6:.0f} MB; the limit is "
            f"{UPLOAD_MAX_BYTES / 1e6:.0f} MB."
        )
    relative = f"_uploads/{uuid.uuid4().hex}-{uploaded.name}"
    target = settings.RAW_DIR / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        for chunk in uploaded.chunks():
            handle.write(chunk)
    return relative, uploaded.name


def fetch(request, key: str):
    """One entry point for Fetch, Preview and Upload — they differ by flag."""
    if (bad := _require_post(request)) is not None:
        return bad

    source = get_object_or_404(Source, key=key)
    collector = registry.get(source.key)
    if collector is None:
        messages.warning(
            request,
            f"{source.name} is a register row without a collector yet — nothing "
            f"was queued. Its clock and fetch policy are recorded so the "
            f"collector has somewhere to land.",
        )
        return redirect("sources:console")

    params = _collect_params(request)
    preview = bool(request.POST.get("preview"))
    if preview:
        params["preview_only"] = True

    uploaded = request.FILES.getlist("file")
    if uploaded:
        if not collector.accepts_upload:
            messages.error(request, f"{source.name} does not accept file uploads.")
            return redirect("sources:console")
        staged = []
        try:
            for item in uploaded:
                staged.append(_stage_upload(item))
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("sources:console")
        params["uploads"] = staged

    if collector.requires_date_range and not (params.get("date_from") and params.get("date_to")):
        messages.error(
            request,
            f"{source.name} needs both a start and an end date — it will not "
            f"guess a range, because an unbounded one here is hundreds of "
            f"gigabytes.",
        )
        return redirect("sources:console")

    if collector.needs_symbols and not params.get("symbols"):
        messages.error(request, f"Select at least one instrument for {source.name}.")
        return redirect("sources:console")

    job = jobs.enqueue("fetch_source", source_key=source.key, **params)
    messages.success(
        request,
        f"Queued a {'preview of' if preview else 'fetch of'} {source.name} — "
        f"job #{job.pk}."
        + (" Nothing will be written to the dataset." if preview else ""),
    )
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


def source_detail(request, key: str):
    """Per-source configuration — what it collects is a setting, not code."""
    source = _decorate(get_object_or_404(Source, key=key))

    if request.method == "POST":
        raw = request.POST.get("config_json") or "{}"
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("Configuration must be a JSON object.")
        except (json.JSONDecodeError, ValueError) as exc:
            messages.error(request, f"Configuration not saved: {exc}")
        else:
            source.config_json = parsed
            for field in ("timezone_rule", "terms_note", "depth_note"):
                if field in request.POST:
                    setattr(source, field, request.POST[field])
            source.save()
            messages.success(request, f"Configuration saved for {source.name}.")
            return redirect("sources:detail", key=source.key)

    defaults = {}
    if source.key == "dbnomics":
        from collectors.dbnomics import DEFAULT_SERIES

        defaults = {"series": DEFAULT_SERIES}
    elif source.key == "mt5_calendar":
        from collectors.mt5_calendar import default_export_dirs

        defaults = {
            "export_dir": str(default_export_dirs()[0]) if default_export_dirs() else "",
            "filename_glob": "fxmacro_calendar*.csv",
        }
    elif source.key == "dukascopy":
        defaults = {"delay_seconds": 0.05}
    elif source.key == "histdata":
        defaults = {"import_dir": "", "import_glob": "*.zip"}
    elif source.key == "forexfactory_pages":
        defaults = {"delay_seconds": 2.5, "max_retries": 2}

    return render(
        request,
        "sources/source_detail.html",
        {
            "nav": "sources",
            "source": source,
            "config_text": json.dumps(source.config_json or {}, indent=2),
            "defaults_text": json.dumps(defaults, indent=2) if defaults else "",
            "runs": source.runs.all()[:15],
            "instruments": Instrument.objects.filter(enabled=True),
            "contribution": next(
                (c for c in stats.source_contributions() if c["source__key"] == source.key),
                None,
            ),
        },
    )


def prune_history(request):
    """Clear ingest logs and stale raw payloads (§5.4)."""
    if (bad := _require_post(request)) is not None:
        return bad

    from sources.pruning import prune

    result = prune(drop_payloads=bool(request.POST.get("drop_payloads")))
    messages.success(request, result["summary"])
    return redirect(request.POST.get("next") or "sources:jobs")


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

    from sources.pruning import prunable

    page = Paginator(queryset, 40).get_page(request.GET.get("page"))
    params = {k: v for k, v in request.GET.items() if k != "page" and v}
    return render(
        request,
        "sources/jobs.html",
        {
            "nav": "jobs",
            "page": page,
            "prunable": prunable(),
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
    return render(request, "sources/_jobs.html", {"jobs": Job.objects.all()[:ACTIVE_JOB_LIMIT]})


def job_status(request, pk: int):
    """One job's progress as JSON, for a page that is waiting on it.

    Lives here rather than in whichever app pressed the button: progress is a
    property of the job table, and a second copy of this in every app would
    drift.
    """
    job = get_object_or_404(Job, pk=pk)
    last_error = (job.error_text or "").strip().splitlines()
    return JsonResponse(
        {
            "status": job.status,
            "percent": job.progress_percent,
            "message": job.message or "",
            "done": job.status == JobStatus.SUCCESS,
            "failed": job.status in (JobStatus.FAILED, JobStatus.CANCELLED),
            "error": last_error[-1][:200] if last_error else "",
        }
    )


def job_detail(request, pk: int):
    job = get_object_or_404(Job, pk=pk)
    return render(request, "sources/job_detail.html", {"nav": "jobs", "job": job})


def run_detail(request, pk: int):
    run = get_object_or_404(
        FetchRun.objects.select_related("source", "job").prefetch_related("snapshots"), pk=pk
    )
    totals = run.snapshots.aggregate(n=Count("id"), bytes=Sum("size_bytes"))
    sample = run.sample_json or {}
    return render(
        request,
        "sources/run_detail.html",
        {
            "nav": "jobs",
            "run": run,
            "snapshot_count": totals["n"] or 0,
            "snapshot_bytes": totals["bytes"] or 0,
            "sample_columns": sample.get("columns") or [],
            "sample_rows": sample.get("rows") or [],
            "sample_caption": sample.get("caption") or "",
            "sample_total": sample.get("total") or 0,
        },
    )


def snapshot_raw(request, pk: int):
    """Show the head of a stored payload, exactly as it arrived.

    The most direct answer to "what is actually being fetched" — everything
    else on the console is a derived view of these bytes.
    """
    snapshot = get_object_or_404(RawSnapshot.objects.select_related("fetch_run__source"), pk=pk)
    path = settings.RAW_DIR / snapshot.path
    if not path.exists():
        raise Http404(f"Payload is recorded but missing at {path}")

    head = path.read_bytes()[:RAW_VIEW_BYTES]
    lower = snapshot.path.lower()
    binary = lower.endswith((".zip", ".bi5", ".gz"))

    text = ""
    if not binary:
        text = head.decode("utf-8", errors="replace")
        if "json" in snapshot.content_type or lower.endswith(".json"):
            try:
                text = json.dumps(json.loads(text), indent=2)[:RAW_VIEW_BYTES]
            except json.JSONDecodeError:
                pass

    if request.GET.get("download"):
        response = HttpResponse(
            path.read_bytes(), content_type=snapshot.content_type or "application/octet-stream"
        )
        response["Content-Disposition"] = f'attachment; filename="{path.name}"'
        return response

    return render(
        request,
        "sources/snapshot.html",
        {
            "nav": "jobs",
            "snapshot": snapshot,
            "text": text,
            "binary": binary,
            "truncated": snapshot.size_bytes > RAW_VIEW_BYTES,
            "shown_bytes": len(head),
        },
    )
