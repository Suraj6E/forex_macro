"""Data quality screen — planning.md §10 screen 2, "deliberately prominent".

The point of putting this second in the navigation is that a chart you cannot
audit is worse than no chart. Every finding states what it means and, where a
fix exists, offers it as a button.
"""

from __future__ import annotations

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from analytics import volcheck
from calendar_data.models import EventRelease, Indicator, IndicatorAlias, ValueRevision
from consolidation import dedup
from dashboard import charts
from quality import checks, stats, validation
from quality.enums import CrossSource, MappingStatus
from sources import jobs
from sources.models import FetchRun, Job, JobStatus, Source


def _require_post(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    return None


def index(request):
    breakdown = stats.quality_breakdown()
    coverage_rows, total = stats.field_coverage()
    findings = checks.run_checks()

    def chart(key, label_width=210):
        return charts.hbar(
            breakdown[key],
            max_value=total or 1,
            label_width=label_width,
            empty="No releases ingested yet.",
        )

    return render(
        request,
        "quality/index.html",
        {
            "nav": "quality",
            "total": total,
            "head": stats.headline(),
            "findings": findings,
            "problems": [f for f in findings if not f.clean],
            "clean": [f for f in findings if f.clean],
            "blocking": [f for f in findings if f.severity == checks.BLOCKING and not f.clean],
            "coverage_chart": charts.hbar(
                coverage_rows, max_value=total or 1, label_width=196,
                empty="No releases ingested yet."
            ),
            "timestamp_chart": chart("timestamp_confidence"),
            "forecast_chart": chart("forecast_provenance"),
            "cross_chart": chart("cross_source"),
            "revisions": ValueRevision.objects.select_related(
                "event_release__indicator", "source"
            )[:10],
            "captures": FetchRun.objects.select_related("source").filter(
                source__key="forexfactory_weekly"
            )[:8],
            "sources": Source.objects.exclude(health="unknown"),
        },
    )


def timestamps(request):
    """The validation harness — planning.md §4.1, §12, P2.

    Everything measured in this project is anchored to a timestamp a source
    handed us. This screen is where price gets to disagree.
    """
    importance = request.GET.get("importance") or ""
    currency = request.GET.get("currency") or ""
    indicator = Indicator.objects.filter(pk=request.GET.get("indicator")).first()

    # Pooled once over every checked indicator — the false-discovery adjustment
    # needs the whole sweep — then narrowed for display.
    pooled = validation.verdicts()
    rows = validation.matching(
        pooled, importance=int(importance) if importance else None, currency=currency
    )
    pending = (
        Job.objects.filter(kind="vol_check", status__in=[JobStatus.QUEUED, JobStatus.RUNNING])
        .order_by("-id")
        .first()
    )

    detail = None
    if indicator is not None:
        histogram = validation.offset_histogram(indicator.pk)
        # From the same pooled pass as the table, so the detail page cannot
        # contradict the row that led here.
        verdict = next(
            (row.verdict for row in pooled if row.indicator_id == indicator.pk),
            volcheck.Verdict(),
        )
        detail = {
            "indicator": indicator,
            "verdict": verdict,
            "neighbours": validation.co_timed_at_offset(
                indicator.pk, verdict.modal_offset_minutes or 0
            ),
            "symbol": validation.REFERENCE_PAIR.get(
                indicator.currency, validation.FALLBACK_PAIR
            ),
            "histogram": charts.vbar(
                histogram,
                axis_every=1,
                empty="No release of this indicator has been checked yet.",
            ),
            "dst": validation.dst_split(indicator.pk),
            "samples": validation.offset_samples(indicator.pk),
        }

    return render(
        request,
        "quality/timestamps.html",
        {
            "nav": "timestamps",
            "head": validation.headline(),
            "rows": rows,
            "detail": detail,
            "pending": pending,
            "importance": importance,
            "currency": currency,
            "importances": [(3, "High"), (2, "Medium"), (1, "Low"), (0, "Unrated")],
            "currencies": sorted(
                Indicator.objects.exclude(currency="")
                .values_list("currency", flat=True)
                .distinct()
            ),
            "coverage": validation.coverage_by_year(),
            "min_n": volcheck.MIN_VERDICT_N,
            "spike_ratio": volcheck.SPIKE_RATIO,
            "neighbourhood": volcheck.NEIGHBOURHOOD,
            # What a share of spikes landing in any one bar would be if the
            # release had nothing to do with when price moved. Every share on
            # this page is meaningless without it.
            "chance_share": round(100 / (2 * volcheck.NEIGHBOURHOOD + 1)),
            "aligned": volcheck.ALIGNED,
            "offset_clock": volcheck.OFFSET_CLOCK,
            "scattered": volcheck.SCATTERED,
            "silent": volcheck.SILENT,
        },
    )


def run_timestamps(request):
    """Queue the check. Minutes of work, so it goes to the worker, not the request."""
    if (bad := _require_post(request)) is not None:
        return bad

    params = {
        key: request.POST.get(key)
        for key in ("indicator_id", "importance", "currency")
        if request.POST.get(key)
    }
    existing = Job.objects.filter(
        kind="vol_check", status__in=[JobStatus.QUEUED, JobStatus.RUNNING]
    ).first()
    if existing is None:
        jobs.enqueue("vol_check", **params)
    else:
        messages.success(request, "Already checking — showing progress.")
    return redirect(request.POST.get("next") or "quality:timestamps")


def duplicates(request):
    """The dedup review screen.

    Exact matches can be merged in bulk. Anything softer is a suggestion —
    merging two genuinely different series into one is an error no downstream
    check would catch, so it needs a person.
    """
    exact = dedup.exact_duplicate_groups()
    similar = dedup.similar_candidate_groups()
    provisional = dedup.provisional_duplicate_groups()

    return render(
        request,
        "quality/duplicates.html",
        {
            "nav": "quality",
            "exact": exact,
            "similar": similar,
            "provisional": provisional,
            "provisional_extra": sum(len(g) - 1 for g in provisional),
            "indicator_total": Indicator.objects.count(),
        },
    )


def merge_indicator_group(request):
    if (bad := _require_post(request)) is not None:
        return bad

    ids = [int(v) for v in request.POST.getlist("indicator_ids") if v]
    primary_id = request.POST.get("primary_id")
    if len(ids) < 2:
        messages.error(request, "Select at least two indicators to merge.")
        return redirect("quality:duplicates")

    indicators = list(
        Indicator.objects.annotate(release_count=Count("releases", distinct=True)).filter(
            pk__in=ids
        )
    )
    if primary_id:
        primary = next((i for i in indicators if i.pk == int(primary_id)), None)
    else:
        group = dedup.DuplicateGroup(
            currency=indicators[0].currency, label="", members=indicators
        )
        primary = group.primary
    if primary is None:
        messages.error(request, "Could not determine which indicator to keep.")
        return redirect("quality:duplicates")

    others = [i for i in indicators if i.pk != primary.pk]
    result = dedup.merge_indicators(primary, others)
    messages.success(
        request,
        f"Merged {result['merged_indicators']} indicator(s) into “{primary.name}”: "
        f"{result['moved_releases']} release(s) moved, "
        f"{result['merged_releases']} folded into an existing release, "
        f"{result['moved_aliases']} alias(es) re-pointed. Every source's raw "
        f"claim was kept.",
    )
    return redirect(request.POST.get("next") or "quality:duplicates")


def merge_all_exact(request):
    if (bad := _require_post(request)) is not None:
        return bad

    groups = dedup.exact_duplicate_groups()
    merged = releases = 0
    for group in groups:
        result = dedup.merge_indicators(group.primary, group.others)
        merged += result["merged_indicators"]
        releases += result["moved_releases"] + result["merged_releases"]

    if merged:
        messages.success(
            request,
            f"Merged {merged} duplicate indicator(s) across {len(groups)} group(s), "
            f"covering {releases} release(s). Only exact name matches were "
            f"touched; similar-looking pairs still need your judgement.",
        )
    else:
        messages.success(request, "Nothing to merge — no two indicators share a name.")
    return redirect("quality:duplicates")


def merge_provisional(request):
    if (bad := _require_post(request)) is not None:
        return bad
    result = dedup.merge_provisional_duplicates()
    if result["absorbed"]:
        messages.success(
            request,
            f"Collapsed {result['groups']} group(s), absorbing "
            f"{result['absorbed']} duplicate release(s) and recording "
            f"{result['revisions']} value revision(s) the fork had hidden.",
        )
    else:
        messages.success(request, "No provisional duplicates found.")
    return redirect(request.POST.get("next") or "quality:duplicates")


def purge_orphans(request):
    if (bad := _require_post(request)) is not None:
        return bad
    indicators = Indicator.objects.annotate(
        n=Count("releases"), a=Count("aliases")
    ).filter(n=0, a=0)
    removed = indicators.count()
    indicators.delete()
    dangling = IndicatorAlias.objects.filter(indicator__isnull=True)
    removed_aliases = dangling.count()
    dangling.delete()
    messages.success(
        request, f"Removed {removed} orphaned indicator(s) and {removed_aliases} alias(es)."
    )
    return redirect("quality:index")


def purge_source(request, key: str):
    """Delete everything a source contributed — for when a bad configuration
    loaded junk. Releases another source also claims survive."""
    if (bad := _require_post(request)) is not None:
        return bad

    source = get_object_or_404(Source, key=key)
    if request.POST.get("confirm") != source.key:
        messages.error(
            request,
            f"Not purged. Type the source key ({source.key}) to confirm — this "
            f"deletes data and cannot be undone from the UI.",
        )
        return redirect("sources:detail", key=source.key)

    result = dedup.purge_source(source)
    messages.success(
        request,
        f"Purged {source.name}: {result['deleted_releases']} release(s) deleted, "
        f"{result['touched_releases'] - result['deleted_releases']} kept because "
        f"another source also claims them, {result['deleted_indicators']} "
        f"indicator(s) removed. Raw payloads on disk were not touched.",
    )
    return redirect("sources:detail", key=source.key)
