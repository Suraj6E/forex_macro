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

from calendar_data.models import EventRelease, Indicator, IndicatorAlias, ValueRevision
from consolidation import dedup
from dashboard import charts
from quality import checks, stats
from quality.enums import CrossSource, MappingStatus
from sources.models import FetchRun, Source


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
