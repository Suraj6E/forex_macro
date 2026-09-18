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
        detail="Answered twice over: where each source thinks 08:30 New York is — the "
        "vol-check reads 259 of 359 indicators aligned, and the agency embargo lines "
        "themselves confirm the March 2008 daylight-saving boundary from the "
        "publisher's side. The other three questions are now one manual step each, "
        "not open work: `manage.py mt5_audit` answers how far back your MT5 calendar "
        "reaches and whether its forecast is point-in-time as soon as the MQL5 export "
        "is run in the terminal; `manage.py price_compare` answers HistData against "
        "Dukascopy, on a quiet hour and on a flash crash, as soon as the zips are "
        "downloaded by hand — the site serves nothing to a non-browser.",
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
        "reporting is on the quality screen. Twenty-one releases have now been "
        "checked by hand against the issuing agency — BLS and the FOMC — and all "
        "twenty-one match on value, reporting month and release time "
        "(docs/agency_verification.md). One thing that found: a per-release "
        "`offset` grade is not a verdict about that release's clock, and three of "
        "the twenty-one carry one against an agency page that confirms the "
        "timestamp. Still to do: cross-source comparison, which has nothing to "
        "compare until a second calendar source is loaded.",
    ),
    dict(
        key="P3",
        title="Breadth and consolidation",
        status=ACTIVE,
        detail="Release groups and the concept taxonomy are in, both derived rather "
        "than assigned: 25 groups covering 55 indicators, from the release "
        "timestamps alone, and every indicator at tier 2 or above classified into "
        "one of twelve concepts from its name. The event ranking screen reads both "
        "— it pools across economies on the concept and counts a co-timed report "
        "once, not once per member. Still open: DBnomics/ALFRED, canonical codes "
        "for cross-source joining, and dataset export.",
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
        detail="Signed effect sizes, asymmetry, same-instant decomposition and "
        "leakage tests, wherever the forecast column exists — which is 16 releases "
        "and growing by a week each week. A third mode now covers part of the "
        "ground without waiting: `direction` regresses the signed move on the "
        "change against the indicator's own previous print, which 99.9% of "
        "releases carry. It measures 'higher than last time', never 'higher than "
        "expected', and is stored and labelled separately for exactly that reason.",
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
