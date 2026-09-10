"""Build phases — planning.md §11.

Rendered on the dashboard so the tool states plainly what it can and cannot do
yet.  A screen that hides its own incompleteness is how you end up trusting an
empty chart.
"""

DONE, ACTIVE, PENDING = "done", "active", "pending"

PHASES = [
    dict(
        key="P0",
        title="Skeleton",
        status=DONE,
        detail="Django project, the §9 model, job runner, sources console, one "
        "collector working end to end.",
    ),
    dict(
        key="P0.5",
        title="Source audit",
        status=ACTIVE,
        detail="Four questions: how far back your MT5 calendar reaches, where each "
        "source thinks 08:30 New York is, whether MT5's forecast is point-in-time, "
        "and whether HistData and Dukascopy agree. Resolves the plan's biggest "
        "unknowns in about a day.",
    ),
    dict(
        key="P1",
        title="Vertical slice, Mode A",
        status=PENDING,
        detail="US Employment Situation → EUR/USD, full history: fetch → normalise "
        "→ merge → align → decay curve across the horizon ladder. No forecast "
        "column required, which is what makes it the right first slice.",
    ),
    dict(
        key="P2",
        title="Validation harness",
        status=PENDING,
        detail="Volatility-spike cross-check against stored timestamps, cross-source "
        "comparison, coverage reporting. Manually verify ~20 events against agency "
        "release pages.",
    ),
    dict(
        key="P3",
        title="Breadth and consolidation",
        status=PENDING,
        detail="Tier-1 across 8 economies and 7 pairs, DBnomics/ALFRED, the "
        "canonical-code mapping, release groups, dataset export. Event ranking "
        "lands here — the first genuinely interesting output.",
    ),
    dict(
        key="P4",
        title="Study engine and the curve",
        status=PENDING,
        detail="Saved study specifications, single-event and pooled curves, "
        "event-anchored windows, joint regression with VIF reporting, "
        "detectability floors, regime splits.",
    ),
    dict(
        key="P4.5",
        title="Currency state",
        status=PENDING,
        detail="State variables as filters, controls and interaction terms — the "
        "dampening/amplification question and most of the edge cases after it.",
    ),
    dict(
        key="P5",
        title="Mode B — surprise",
        status=PENDING,
        detail="Signed effect sizes, asymmetry, same-instant decomposition, "
        "leakage tests, wherever the forecast column exists.",
    ),
    dict(
        key="P6",
        title="Commentary",
        status=PENDING,
        detail="Hand-written explainers for the top 15 indicators, epistemic "
        "labelling wired through.",
    ),
    dict(
        key="P7",
        title="Ongoing",
        status=PENDING,
        detail="Weekly forward capture running; dataset quality improving on its own.",
    ),
]
