"""Studies — planning.md §6.2, §10 screen 5.

The decay curve is the deliverable: effect size against horizon, with n, the
uncertainty and the detectability floor beside it so a null result reads as
"no effect larger than X" rather than "no effect".
"""

from __future__ import annotations

from django.contrib import messages
from django.db.models import Count, Max, Q
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from analytics.horizons import LADDER, WindowScheme
from calendar_data.models import Indicator
from dashboard import charts
from prices.models import Instrument
from sources import jobs
from sources.models import Job, JobStatus
from studies import engine
from studies.models import (
    DecayCurve,
    EventImpact,
    Hypothesis,
    Mode,
    OutlierPolicy,
    Sample,
    StudyRun,
    StudySpec,
)

SPEC_FIELDS = [
    ("events", "indicator codes, release groups, countries, importance tier"),
    ("instruments", "which of the 7 pairs"),
    ("mode", "A (event-only) | B (surprise-conditioned)"),
    ("windows", "fixed ladder | until_next_own | until_next_either | until_next_any"),
    ("baseline", "matched weekday-hour | trailing mean | none"),
    ("shape", "jump | drift | both — an optional lens, off by default"),
    ("controls", "other events in window, session, regime"),
    ("conditions", "state variables as filters or interaction terms"),
    ("pooling", "none | across pairs | across countries | across event class"),
    ("outliers", "include | flag | winsorise | exclude"),
    ("sample", "explore | holdout | full"),
]


def index(request):
    """Overview: what has been measured, and the specification still to come."""
    computed = (
        DecayCurve.objects.filter(engine_version=engine.ENGINE_VERSION)
        .values("indicator__id", "indicator__currency", "indicator__name",
                "instrument__id", "instrument__symbol")
        .annotate(points=Count("id"), best_n=Max("n"))
        .order_by("indicator__currency", "indicator__name")
    )
    return render(
        request,
        "studies/index.html",
        {
            "nav": "studies",
            "computed": computed,
            "impacts": EventImpact.objects.filter(
                engine_version=engine.ENGINE_VERSION
            ).count(),
            "specs": StudySpec.objects.all()[:20],
            "runs": StudyRun.objects.select_related("study_spec")[:20],
            "hypotheses": Hypothesis.objects.all()[:20],
            "spec_fields": SPEC_FIELDS,
            "ladder": LADDER,
            "window_schemes": WindowScheme.CHOICES,
            "modes": Mode.choices,
            "outlier_policies": OutlierPolicy.choices,
            "samples": Sample.choices,
        },
    )


def event_study(request):
    """Run and read one indicator × one instrument."""
    indicator_id = request.GET.get("indicator")
    instrument_id = request.GET.get("instrument")
    importance = request.GET.get("importance") or ""

    indicator = Indicator.objects.filter(pk=indicator_id).first() if indicator_id else None
    instrument = (
        Instrument.objects.filter(pk=instrument_id).first()
        if instrument_id
        else Instrument.objects.filter(symbol="EURUSD").first()
    )

    # A release in a third currency cannot move this pair through a leg it does
    # not have, so it has no business in the picker — nor in the result, which
    # is why a selection that survives a pair change is dropped rather than
    # silently measured. Same for a selection the impact filter excludes: a
    # dropdown showing "pick a release" above a curve is a lie about what is
    # on screen.
    if indicator and instrument and not instrument.involves(indicator.currency):
        indicator = None
    if indicator and importance and indicator.importance != int(importance):
        indicator = None

    indicators = Indicator.objects.annotate(
        # Counting with a filter rather than joining and de-duplicating: the
        # join form costs ~12s against 86,000 releases because DISTINCT has to
        # sort the whole product.
        n=Count("releases", filter=Q(releases__release_time_utc__isnull=False))
    ).filter(n__gte=engine.MIN_N)
    if instrument:
        indicators = indicators.filter(
            Q(currency=instrument.base_ccy) | Q(currency=instrument.quote_ccy)
        )
    if importance:
        indicators = indicators.filter(importance=importance)

    curve_rows, points, headline, rows = [], [], None, []
    scatter, scatter_horizon, scatter_normal = [], None, None
    stored_version = None
    pending = None

    # Changing the dropdown should just show the answer. When there is no
    # stored curve the answer has to be computed first, so start it here and
    # let the page report progress — rather than making the reader press a
    # button whose only purpose is to say "yes, I meant it".
    if indicator and instrument:
        pending = _pending_job(indicator.pk, instrument.pk)
        if pending is None and not _has_curve(indicator.pk, instrument.pk):
            pending = jobs.enqueue(
                "run_study", indicator_id=indicator.pk, instrument_id=instrument.pk
            )

    if indicator and instrument:
        curve_rows = engine.curve_for(indicator.pk, instrument.pk)
        pip = engine.pip_value(instrument)

        for row in curve_rows:
            significant = row.p_fdr is not None and row.p_fdr < 0.05
            points.append({
                "horizon": row.horizon,
                "value": row.effect_size,
                "error": row.std_error,
                "ratio": row.r_squared,      # stored as the observed/normal ratio
                "p": row.p_fdr,
                "n": row.n,
                "significant": significant,
                "gated": row.effect_size is None and row.n > 0,
            })
            rows.append({
                "row": row,
                "significant": significant,
                # Log returns as bare decimals are unreadable; pips are the
                # unit this market is actually quoted and thought in.
                "pips": None if row.effect_size is None else row.effect_size / pip,
                "pips_error": None if row.std_error is None else row.std_error / pip,
                "floor_pips": None if row.detectability_floor is None
                              else row.detectability_floor / pip,
                "quieter": row.effect_size is not None and row.effect_size < 0,
            })

        headline = engine.summarise_curve(curve_rows, pip)
        stored_version = curve_rows[0].engine_version if curve_rows else None

        peak = headline.get("peak")
        if peak is not None:
            scatter_horizon = peak.horizon
            scatter, scatter_normal = engine.event_scatter(
                indicator.pk, instrument.pk, peak.horizon, pip
            )

    return render(
        request,
        "studies/event_study.html",
        {
            "nav": "studies",
            "indicator": indicator,
            "instrument": instrument,
            "instruments": Instrument.objects.filter(enabled=True),
            "indicators": indicators.order_by("-n", "currency", "name").values_list(
                "id", "currency", "name", "n"
            )[:400],
            "importance": importance,
            "importances": [(3, "High"), (2, "Medium"), (1, "Low"), (0, "Unrated")],
            "pair_currencies": (
                [instrument.base_ccy, instrument.quote_ccy] if instrument else []
            ),
            "curve": curve_rows,
            "rows": rows,
            "ratio_chart": charts.ratio(points),
            "chart": charts.decay(points),
            "scatter_chart": charts.dots(
                scatter, reference=scatter_normal, reference_label="normal move",
                empty="No individual releases to plot.",
            ),
            "scatter_horizon": scatter_horizon,
            "scatter_normal": scatter_normal,
            "scatter_n": len(scatter),
            "headline": headline,
            "pending": pending,
            "engine_version": engine.ENGINE_VERSION,
            "stored_version": stored_version,
            "stale": bool(stored_version and stored_version != engine.ENGINE_VERSION),
            "min_n": engine.MIN_N,
        },
    )


def _has_curve(indicator_id: int, instrument_id: int) -> bool:
    return DecayCurve.objects.filter(
        indicator_id=indicator_id, instrument_id=instrument_id
    ).exists()


def _pending_job(indicator_id: int, instrument_id: int):
    """An in-flight measurement for this pairing, if any.

    Checked before enqueueing so a reload — or a second tab — does not stack
    duplicate multi-minute jobs onto the queue.
    """
    return (
        Job.objects.filter(
            kind="run_study",
            status__in=[JobStatus.QUEUED, JobStatus.RUNNING],
            params_json__indicator_id=indicator_id,
            params_json__instrument_id=instrument_id,
        )
        .order_by("-id")
        .first()
    )


def status(request):
    """Progress of a measurement, polled by the page while it runs."""
    job = get_object_or_404(Job, pk=request.GET.get("job"))
    return JsonResponse(
        {
            "status": job.status,
            "percent": job.progress_percent,
            "message": job.message or "",
            "done": job.status == JobStatus.SUCCESS,
            "failed": job.status in (JobStatus.FAILED, JobStatus.CANCELLED),
            "error": (job.error_text or "").strip().splitlines()[-1:] and
                     (job.error_text or "").strip().splitlines()[-1][:200] or "",
        }
    )


def run(request):
    """Kept for an explicit re-measure of a curve that already exists."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    indicator = get_object_or_404(Indicator, pk=request.POST.get("indicator"))
    instrument = get_object_or_404(Instrument, pk=request.POST.get("instrument"))

    existing = _pending_job(indicator.pk, instrument.pk)
    if existing is None:
        jobs.enqueue(
            "run_study", indicator_id=indicator.pk, instrument_id=instrument.pk
        )
    else:
        messages.success(request, "Already measuring — showing progress.")
    return redirect(
        f"/studies/event-study/?indicator={indicator.pk}&instrument={instrument.pk}"
    )


# --- §6.4 ranking -----------------------------------------------------------

#: The horizon the ranking is read at.  §3.2's reasoning: the event's effect is
#: roughly fixed while noise grows with the square root of time, so the shortest
#: post-release window the hourly backbone supports is where a ranking is most
#: attributable.  Longer horizons stay on the page as columns, not as the order.
RANK_HORIZON = "+1h"


def ranking(request):
    """Which kinds of news actually move FX — measured, then compared against
    the rating the calendar publishes (§6.4).

    Ranked on the **ratio**, not on the pip move: a pip is worth a different
    amount of information in USDJPY than in EURUSD, so pooling raw magnitudes
    across pairs would rank the volatile pairs rather than the news. The ratio —
    how many times its own matched-normal move the pair made — is unitless and
    comparable, which is what pooling across eight economies requires.
    """
    from calendar_data.models import Concept
    from studies.directions import DIRECTION_VERSION

    horizon = request.GET.get("horizon") or RANK_HORIZON

    # Mode A supplies the magnitude. `r_squared` carries abs_ratio for Mode A
    # rows — an overload inherited from the engine, and the reason this view
    # never prints that column under its field name.
    measured = (
        DecayCurve.objects.filter(
            mode=Mode.A,
            engine_version=engine.ENGINE_VERSION,
            horizon=horizon,
            effect_size__isnull=False,
        )
        .exclude(indicator__concept="")
        .values(
            "indicator__id",
            "indicator__name",
            "indicator__currency",
            "indicator__concept",
            "indicator__importance",
            "indicator__release_group_id",
            "indicator__release_group__name",
            "instrument__symbol",
        )
        .annotate(ratio=Max("r_squared"), excess=Max("effect_size"), releases=Max("n"))
    )

    # Co-timed releases are one measurement wearing several names: NZD
    # Employment Change and NZD Unemployment Rate are published at the same
    # instant, so their curves are identical by construction (§3.3 Case B).
    # Counting both would inflate whichever concept happens to bundle the most
    # indicators into one report — which is labour, every time.
    counted: set[tuple] = set()

    by_concept: dict[str, dict] = {}
    by_indicator: dict[int, dict] = {}
    for row in measured:
        ratio = row["ratio"]
        if ratio is None:
            continue
        concept = row["indicator__concept"]
        group = row["indicator__release_group_id"]
        token = (
            ("group", group, row["instrument__symbol"])
            if group
            else ("indicator", row["indicator__id"], row["instrument__symbol"])
        )
        double_counted = token in counted
        counted.add(token)
        bucket = by_concept.setdefault(
            concept,
            dict(concept=concept, label=Concept(concept).label, ratios=[], pairs=0,
                 releases=0, indicators=set(), importances=[], best=0.0, best_name=""),
        )
        if not double_counted:
            bucket["ratios"].append(ratio)
            bucket["pairs"] += 1
            bucket["releases"] += row["releases"] or 0
            bucket["importances"].append(row["indicator__importance"])
        bucket["indicators"].add(row["indicator__id"])
        if ratio > bucket["best"]:
            bucket["best"] = ratio
            bucket["best_name"] = f"{row['indicator__currency']} {row['indicator__name']}"

        key = row["indicator__id"]
        entry = by_indicator.setdefault(
            key,
            dict(
                id=key,
                name=row["indicator__name"],
                currency=row["indicator__currency"],
                concept=concept,
                concept_label=Concept(concept).label.split(" — ")[0],
                importance=row["indicator__importance"],
                group=row["indicator__release_group__name"] or "",
                shared=bool(group),
                ratios=[],
                releases=0,
                pairs=[],
            ),
        )
        entry["ratios"].append(ratio)
        entry["releases"] = max(entry["releases"], row["releases"] or 0)
        entry["pairs"].append(row["instrument__symbol"])

    concepts = []
    for bucket in by_concept.values():
        ratios = sorted(bucket["ratios"])
        bucket["median_ratio"] = _median(ratios)
        bucket["n_indicators"] = len(bucket["indicators"])
        bucket["published"] = (
            sum(bucket["importances"]) / len(bucket["importances"])
            if bucket["importances"]
            else 0
        )
        concepts.append(bucket)
    concepts.sort(key=lambda b: b["median_ratio"], reverse=True)

    # §6.4's actual question: where does the measured order disagree with the
    # published traffic light? Both are turned into ranks so a 1-to-3 scale and
    # a ratio can be compared at all.
    published_order = sorted(concepts, key=lambda b: b["published"], reverse=True)
    published_rank = {b["concept"]: i + 1 for i, b in enumerate(published_order)}
    for index, bucket in enumerate(concepts, start=1):
        bucket["rank"] = index
        bucket["published_rank"] = published_rank[bucket["concept"]]
        bucket["disagreement"] = bucket["published_rank"] - index

    indicators = sorted(
        by_indicator.values(), key=lambda e: _median(sorted(e["ratios"])), reverse=True
    )
    for entry in indicators:
        entry["median_ratio"] = _median(sorted(entry["ratios"]))
        entry["pairs"] = sorted(set(entry["pairs"]))

    # Direction: the sign, from the third mode. Read at the same horizon so the
    # magnitude and the sign on one row describe the same window.
    directions = {
        row["indicator_id"]: row
        for row in DecayCurve.objects.filter(
            mode=Mode.DIRECTION,
            engine_version=DIRECTION_VERSION,
            horizon=horizon,
            effect_size__isnull=False,
        ).values("indicator_id", "effect_size", "p_fdr", "n", "instrument__symbol")
    }
    for entry in indicators:
        entry["direction"] = directions.get(entry["id"])

    return render(
        request,
        "studies/ranking.html",
        {
            "nav": "studies",
            "horizon": horizon,
            "ladder": [h for h in LADDER if h.seconds > 0],
            "concepts": concepts,
            "indicators": indicators[:60],
            "indicator_total": len(indicators),
            "covered_pairs": sum(b["pairs"] for b in concepts),
        },
    )


def _median(ordered: list[float]) -> float:
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
