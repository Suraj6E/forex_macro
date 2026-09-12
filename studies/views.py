"""Studies — planning.md §6.2, §10 screen 5.

The decay curve is the deliverable: effect size against horizon, with n, the
uncertainty and the detectability floor beside it so a null result reads as
"no effect larger than X" rather than "no effect".
"""

from __future__ import annotations

from django.contrib import messages
from django.db.models import Count, Max, Q
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from analytics.horizons import LADDER, WindowScheme
from calendar_data.models import Indicator
from dashboard import charts
from prices.models import Instrument
from sources import jobs
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

    indicator = Indicator.objects.filter(pk=indicator_id).first() if indicator_id else None
    instrument = (
        Instrument.objects.filter(pk=instrument_id).first()
        if instrument_id
        else Instrument.objects.filter(symbol="EURUSD").first()
    )

    curve_rows, points, headline, rows = [], [], None, []
    scatter, scatter_horizon, scatter_normal = [], None, None

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
            # Counting with a filter rather than joining and de-duplicating:
            # the join form costs ~12s against 86,000 releases because DISTINCT
            # has to sort the whole product.
            "indicators": Indicator.objects.annotate(
                n=Count("releases", filter=Q(releases__release_time_utc__isnull=False))
            )
            .filter(n__gte=engine.MIN_N)
            .order_by("-n", "currency", "name")
            .values_list("id", "currency", "name", "n")[:400],
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
            "engine_version": engine.ENGINE_VERSION,
            "min_n": engine.MIN_N,
        },
    )


def run(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    indicator = get_object_or_404(Indicator, pk=request.POST.get("indicator"))
    instrument = get_object_or_404(Instrument, pk=request.POST.get("instrument"))

    job = jobs.enqueue(
        "run_study", indicator_id=indicator.pk, instrument_id=instrument.pk
    )
    messages.success(
        request,
        f"Measuring {indicator.currency} {indicator.name} against "
        f"{instrument.symbol} — job #{job.pk}. This reads every release and "
        f"its surrounding weeks, so it takes a minute.",
    )
    return redirect(
        f"/studies/event-study/?indicator={indicator.pk}&instrument={instrument.pk}"
    )
