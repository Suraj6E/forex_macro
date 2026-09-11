"""Calendar browser, event detail and the canonical-code mapping screen.

planning.md §10 screens 3, 4 and the "unmapped events" report of §9.

The mapping screen exists because `canonical_code` "will be the buggiest
artifact in the project" — it needs a working surface, not a database client.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.db.models.functions import Coalesce
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from calendar_data.models import CURRENCIES, EventRelease, Indicator, ReleaseGroup
from quality.enums import (
    CrossSource,
    ForecastProvenance,
    Importance,
    MappingStatus,
    TimestampConfidence,
    VolCheck,
)

PAGE_SIZE = 50

#: An event study is anchored on t0. A row with no instant cannot anchor one;
#: it can only fill the `actual` column of a row that does. That distinction is
#: the single most important thing this screen has to communicate, so it is a
#: first-class filter rather than something you infer from an em-dash.
ANCHORABLE = Q(release_time_utc__isnull=False) | Q(scheduled_time_utc__isnull=False)

QUALITY_FILTERS = {
    "anchorable": (ANCHORABLE, "has a timestamp — can anchor a study"),
    "period_only": (~ANCHORABLE, "period-indexed only — cannot anchor a study"),
    "provisional": (
        Q(reference_period__startswith="release:"),
        "provisionally keyed — no reporting period",
    ),
    "disagree": (Q(cross_source=CrossSource.DISAGREE), "cross-source disagreement"),
    "point_in_time": (
        Q(forecast_provenance=ForecastProvenance.POINT_IN_TIME),
        "point-in-time forecast",
    ),
    "has_actual": (
        Q(actual_first_print__isnull=False) | Q(actual_current__isnull=False),
        "has an actual",
    ),
    "no_actual": (
        Q(actual_first_print__isnull=True, actual_current__isnull=True),
        "no actual yet",
    ),
    "unmapped": (Q(indicator__canonical_code__isnull=True), "indicator unmapped"),
    "unchecked": (Q(vol_check=VolCheck.NOT_CHECKED), "timestamp not vol-checked"),
    "confounded": (Q(confounded=True), "another event within ±15 min"),
}


def _querystring(request, drop=("page",)) -> str:
    params = {k: v for k, v in request.GET.items() if k not in drop and v}
    return urlencode(params) + "&" if params else ""


def browser(request):
    queryset = EventRelease.objects.select_related("indicator", "release_group").annotate(
        anchor=Coalesce("release_time_utc", "scheduled_time_utc")
    )

    search = (request.GET.get("q") or "").strip()
    currency = request.GET.get("currency") or ""
    importance = request.GET.get("importance") or ""
    flag = request.GET.get("flag") or ""
    date_from = request.GET.get("from") or ""
    date_to = request.GET.get("to") or ""

    if search:
        queryset = queryset.filter(
            Q(indicator__name__icontains=search)
            | Q(indicator__canonical_code__icontains=search)
            | Q(reference_period__icontains=search)
        )
    if currency:
        queryset = queryset.filter(indicator__currency=currency)
    if importance:
        queryset = queryset.filter(indicator__importance=importance)
    if flag in QUALITY_FILTERS:
        queryset = queryset.filter(QUALITY_FILTERS[flag][0])
    if date_from:
        queryset = queryset.filter(anchor__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(anchor__date__lte=date_to)

    # Timestamped rows first, newest first; period-indexed rows sort by the
    # period they describe. Sorting everything by a null timestamp is what made
    # this screen look empty when it was merely mixed.
    queryset = queryset.order_by(
        F("anchor").desc(nulls_last=True),
        F("reference_period_start").desc(nulls_last=True),
        "indicator__currency",
    )
    page = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))

    split = EventRelease.objects.aggregate(
        anchorable=Count("id", filter=ANCHORABLE),
        period_only=Count("id", filter=~ANCHORABLE),
    )

    return render(
        request,
        "calendar_data/browser.html",
        {
            "nav": "calendar",
            "page": page,
            "querystring": _querystring(request),
            "currencies": CURRENCIES,
            "importances": Importance.choices,
            "quality_filters": [(k, v[1]) for k, v in QUALITY_FILTERS.items()],
            "q": search,
            "currency": currency,
            "importance": importance,
            "flag": flag,
            "date_from": date_from,
            "date_to": date_to,
            "match_count": page.paginator.count,
            "total_count": EventRelease.objects.count(),
            "anchorable_count": split["anchorable"],
            "period_only_count": split["period_only"],
        },
    )


def event(request, pk: int):
    release = get_object_or_404(
        EventRelease.objects.select_related("indicator", "release_group"), pk=pk
    )
    observations = release.observations.select_related("source", "fetch_run")
    revisions = release.revisions.select_related("source")

    # §9: the merged row is a composite. Show which source owns which field
    # rather than presenting a value as if it had one origin.
    source_map = release.source_map_json or {}
    fields = []
    for name in EventRelease.MERGEABLE_FIELDS:
        fields.append(
            {
                "name": name,
                "label": name.replace("_", " "),
                "value": getattr(release, name),
                "source": source_map.get(name),
            }
        )

    siblings = (
        EventRelease.objects.select_related("indicator")
        .filter(indicator=release.indicator)
        .exclude(pk=release.pk)
        .order_by("-scheduled_time_utc")[:12]
    )

    co_timed = []
    if release.scheduled_time_utc:
        co_timed = (
            EventRelease.objects.select_related("indicator")
            .filter(scheduled_time_utc=release.scheduled_time_utc)
            .exclude(pk=release.pk)
        )

    return render(
        request,
        "calendar_data/event.html",
        {
            "nav": "calendar",
            "release": release,
            "fields": fields,
            "observations": observations,
            "revisions": revisions,
            "siblings": siblings,
            "co_timed": co_timed,
        },
    )


def indicators(request):
    queryset = Indicator.objects.annotate(
        alias_count=Count("aliases", distinct=True),
        release_count=Count("releases", distinct=True),
    )

    state = request.GET.get("state") or "all"
    currency = request.GET.get("currency") or ""
    search = (request.GET.get("q") or "").strip()

    if state == "unmapped":
        queryset = queryset.filter(canonical_code__isnull=True).exclude(
            mapping_status=MappingStatus.IGNORED
        )
    elif state == "mapped":
        queryset = queryset.filter(canonical_code__isnull=False)
    elif state == "ignored":
        queryset = queryset.filter(mapping_status=MappingStatus.IGNORED)

    if currency:
        queryset = queryset.filter(currency=currency)
    if search:
        queryset = queryset.filter(
            Q(name__icontains=search) | Q(canonical_code__icontains=search)
        )

    queryset = queryset.order_by("-release_count", "currency", "name")
    page = Paginator(queryset, 60).get_page(request.GET.get("page"))

    counts = Indicator.objects.aggregate(
        total=Count("id"),
        mapped=Count("id", filter=Q(canonical_code__isnull=False)),
        ignored=Count("id", filter=Q(mapping_status=MappingStatus.IGNORED)),
    )

    return render(
        request,
        "calendar_data/indicators.html",
        {
            "nav": "indicators",
            "page": page,
            "querystring": _querystring(request),
            "state": state,
            "currency": currency,
            "q": search,
            "currencies": CURRENCIES,
            "importances": Importance.choices,
            "mapping_states": MappingStatus.choices,
            "groups": ReleaseGroup.objects.all(),
            "counts": counts,
            "unmapped": counts["total"] - counts["mapped"],
        },
    )


def indicator_update(request, pk: int):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    indicator = get_object_or_404(Indicator, pk=pk)
    code = (request.POST.get("canonical_code") or "").strip().upper().replace(" ", "_")

    if code:
        clash = Indicator.objects.filter(canonical_code=code).exclude(pk=indicator.pk).first()
        if clash:
            messages.error(
                request,
                f"{code} is already used by “{clash.name}” ({clash.currency}). "
                f"Two sources naming the same release differently should be merged, "
                f"not given the same code twice.",
            )
            return redirect(request.POST.get("next") or "calendar_data:indicators")
        indicator.canonical_code = code
        indicator.mapping_status = MappingStatus.MAPPED
    else:
        indicator.canonical_code = None
        if indicator.mapping_status == MappingStatus.MAPPED:
            indicator.mapping_status = MappingStatus.AUTO

    if request.POST.get("mapping_status"):
        indicator.mapping_status = request.POST["mapping_status"]
    if request.POST.get("importance"):
        indicator.importance = int(request.POST["importance"])

    group_id = request.POST.get("release_group")
    indicator.release_group_id = int(group_id) if group_id else None

    indicator.save()

    # Keep existing releases consistent with the indicator's group: §3.3 Case B
    # attributes the move to the group, so the link has to be on the release too.
    indicator.releases.update(release_group=indicator.release_group)

    messages.success(request, f"Updated “{indicator.name}”.")
    return redirect(request.POST.get("next") or "calendar_data:indicators")


def indicator_detail(request, pk: int):
    indicator = get_object_or_404(Indicator, pk=pk)
    return render(
        request,
        "calendar_data/indicator_detail.html",
        {
            "nav": "indicators",
            "indicator": indicator,
            "aliases": indicator.aliases.select_related("source"),
            "releases": indicator.releases.order_by("-scheduled_time_utc")[:60],
            "release_total": indicator.releases.count(),
            "groups": ReleaseGroup.objects.all(),
            "importances": Importance.choices,
            "mapping_states": MappingStatus.choices,
            "confidences": TimestampConfidence.choices,
        },
    )
