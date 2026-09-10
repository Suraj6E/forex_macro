"""Studies — planning.md §6.8, §10 screen 9.

The study builder is P4. Until then this screen documents the specification a
study will be, so the shape of the thing is visible rather than promised: a
study is a saved configuration you edit in a form, not code. Adding a new
question must never mean editing Python.
"""

from __future__ import annotations

from django.shortcuts import render

from analytics.horizons import LADDER, WindowScheme
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
    return render(
        request,
        "studies/index.html",
        {
            "nav": "studies",
            "specs": StudySpec.objects.all()[:20],
            "runs": StudyRun.objects.select_related("study_spec")[:20],
            "hypotheses": Hypothesis.objects.all()[:20],
            "impacts": EventImpact.objects.count(),
            "curves": DecayCurve.objects.count(),
            "spec_fields": SPEC_FIELDS,
            "ladder": LADDER,
            "window_schemes": WindowScheme.CHOICES,
            "modes": Mode.choices,
            "outlier_policies": OutlierPolicy.choices,
            "samples": Sample.choices,
        },
    )
