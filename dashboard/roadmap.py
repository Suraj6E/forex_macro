"""Build phases — planning.md §11.

Rendered on the dashboard so the tool states plainly what it can and cannot do
yet.  A screen that hides its own incompleteness is how you end up trusting an
empty chart.
"""

DONE, PARTIAL, ACTIVE, PENDING = "done", "partial", "active", "pending"

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
        status=PARTIAL,
        detail="Answered: where each source thinks 08:30 New York is — the vol-check "
        "reads 259 of 359 indicators aligned and the release times track US daylight "
        "saving correctly. Still open, both waiting on the MQL5 export being run in "
        "the terminal: how far back your MT5 calendar reaches, and whether its "
        "forecast is point-in-time. HistData against Dukascopy cannot be settled "
        "until HistData zips are downloaded by hand — the site serves nothing to a "
        "non-browser.",
    ),
    dict(
        key="P1",
        title="Vertical slice, Mode A",
        status=DONE,
        detail="Runs for any indicator against any pair, not just the first slice: "
        "86,450 releases Jan 2007 → today, hourly bars for all seven majors, and the "
        "decay curve across the horizon ladder measured against the pair's own "
        "matched weekday-and-hour behaviour.",
    ),
    dict(
        key="P2",
        title="Validation harness",
        status=PARTIAL,
        detail="The volatility-spike cross-check is in and has run over all 359 "
        "indicators: 259 aligned, 97 scattered, 2 genuinely offset. Coverage "
        "reporting is on the quality screen. Still to do: cross-source comparison, "
        "which has nothing to compare until a second calendar source is loaded, and "
        "verifying ~20 events by hand against agency release pages.",
    ),
    dict(
        key="P3",
        title="Breadth and consolidation",
        status=ACTIVE,
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
