"""Quality control — planning.md §5.3, §10 screen 2.

Each check answers one question the dataset should not make you write SQL for,
and each carries the reason it matters. Severity is about *consequence*, not
tidiness:

* `blocking` — this data cannot be used for what the project is for.
* `warning` — usable, but a result built on it needs a caveat.
* `info` — worth knowing; nothing is wrong.

Checks are cheap queries computed on demand. Nothing here mutates anything;
the fixes are separate, explicit actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Count, Q

from calendar_data.models import EventRelease, Indicator, IndicatorAlias
from consolidation import dedup
from normalisers.base import PERIOD_UNKNOWN_PREFIX
from prices.models import PriceCoverage
from quality.enums import CrossSource, MappingStatus, VolCheck

BLOCKING, WARNING, INFO, OK = "blocking", "warning", "info", "ok"

#: Catch-all magnitude guard. Set above real macro data and below the LONG_MIN
#: sentinel: Japan's current account is genuinely ¥2.48 trillion and its money
#: stock is of order ¥10¹⁵, so a threshold anywhere near 10¹² flags correct
#: data. LONG_MIN is 9.2×10¹⁸.
ABSURD_VALUE = Decimal("1e17")

#: MQL5 writes LONG_MIN for "no value". Two ways it reaches a column: raw, or
#: divided by the 1,000,000 scaling. Both are exact numbers, so detect the
#: sentinel itself rather than guessing from magnitude — a threshold big enough
#: to be safe would miss the scaled form entirely.
LONG_MIN = Decimal("-9223372036854775808")
LONG_MIN_SCALED = LONG_MIN / Decimal(1_000_000)
SENTINEL_TOLERANCE = Decimal("0.000001")

VALUE_COLUMNS = (
    "actual_first_print",
    "actual_current",
    "forecast_point_in_time",
    "forecast_stored",
    "previous",
    "revised_previous",
)


@dataclass
class Finding:
    code: str
    title: str
    severity: str
    count: int
    total: int = 0
    detail: str = ""
    why: str = ""
    action_label: str = ""
    action_url: str = ""
    link: str = ""
    samples: list = field(default_factory=list)

    @property
    def share(self) -> float:
        return (self.count / self.total * 100) if self.total else 0.0

    @property
    def clean(self) -> bool:
        return self.count == 0


def run_checks() -> list[Finding]:
    total = EventRelease.objects.count()
    findings = [
        _no_timestamp(total),
        _provisional_periods(total),
        _duplicate_indicators(),
        _provisional_duplicates(),
        _unmapped_indicators(),
        _cross_source_disagreements(total),
        _sentinel_values(total),
        _absurd_values(total),
        _missing_period_start(total),
        _orphans(),
        _vol_check(total),
        _price_gaps(),
    ]
    order = {BLOCKING: 0, WARNING: 1, INFO: 2, OK: 3}
    return sorted(findings, key=lambda f: (order[f.severity], -f.count))


# --------------------------------------------------------------------------


def _no_timestamp(total: int) -> Finding:
    queryset = EventRelease.objects.filter(
        release_time_utc__isnull=True, scheduled_time_utc__isnull=True
    ).select_related("indicator")
    count = queryset.count()

    by_source = (
        queryset.values("observations__source__key")
        .annotate(n=Count("id", distinct=True))
        .order_by("-n")
    )
    detail = ", ".join(
        f"{row['observations__source__key'] or 'unknown'}: {row['n']:,}" for row in by_source[:4]
    )

    return Finding(
        code="no_timestamp",
        title="Releases with no timestamp",
        severity=WARNING if count else OK,
        count=count,
        total=total,
        detail=detail,
        why="An event study is anchored on t0. A row with no instant cannot "
        "anchor one — it can only fill the `actual` column of a row that does. "
        "This is the expected shape of a period-indexed source like DBnomics, "
        "not a fault, but it means most of the dataset is not yet measurable "
        "and the count should stop growing once a calendar source with real "
        "release times is loaded.",
        action_label="Show them",
        link="/calendar/?flag=period_only",
        samples=list(queryset.order_by("-reference_period_start")[:5]),
    )


def _provisional_periods(total: int) -> Finding:
    count = EventRelease.objects.filter(
        reference_period__startswith=PERIOD_UNKNOWN_PREFIX
    ).count()
    return Finding(
        code="provisional_period",
        title="Releases keyed provisionally",
        severity=WARNING if count else OK,
        count=count,
        total=total,
        why="The §4.4 identity key is (currency, code, reporting period, "
        "revision). A source that supplies no reporting period — the "
        "ForexFactory weekly feed — gets a provisional key built from its "
        "scheduled minute instead. Those rows cannot be joined to an agency "
        "figure for the same period until a source that does carry `period` "
        "supplies one and they are re-keyed.",
        link="/calendar/?flag=provisional",
    )


def _duplicate_indicators() -> Finding:
    # Only the cheap exact-match pass runs here. The similar-name scan is
    # O(n²) over every indicator and belongs on the screen dedicated to it,
    # not on every load of the quality overview.
    exact = dedup.exact_duplicate_groups()
    count = sum(len(g.members) - 1 for g in exact)
    return Finding(
        code="duplicate_indicators",
        title="Duplicate indicators",
        severity=WARNING if count else OK,
        count=count,
        total=Indicator.objects.count(),
        detail=(
            f"{len(exact)} exact group(s). Similar-but-not-identical names are "
            f"reviewed on the duplicates screen."
            if exact
            else "No two indicators share a normalised name."
        ),
        why="Every source names a release its own way, so the first ingest from "
        "each creates its own indicator. Until they are merged under one "
        "canonical code, the same economic series is several unrelated rows and "
        "nothing joins across sources.",
        action_label="Review duplicates",
        action_url="quality:duplicates",
    )


def _provisional_duplicates() -> Finding:
    # A SQL-only upper bound. Loading the rows to apply the ±2 minute grouping
    # is the duplicates screen's job; doing it here made this page slower on
    # every reload.
    count = dedup.provisional_duplicate_candidate_count()
    return Finding(
        code="provisional_duplicates",
        title="Duplicate releases from provisional keys",
        severity=WARNING if count else OK,
        count=count,
        detail="Same indicator, same day — an upper bound. The duplicates "
        "screen applies the ±2 minute test and reports the exact figure."
        if count
        else "No indicator has two provisionally keyed releases on one day.",
        why="Provisional keys carry the scheduled minute, so a source that "
        "shifts a release time by a minute forks a second row for one event. "
        "That was the deliberate trade-off: keying on the date alone would "
        "instead have silently merged two genuinely different same-day "
        "releases, and a visible duplicate is recoverable where a silent merge "
        "is not. This is the recovery.",
        action_label="Merge them",
        action_url="quality:merge_provisional",
    )


def _unmapped_indicators() -> Finding:
    count = (
        Indicator.objects.filter(canonical_code__isnull=True)
        .exclude(mapping_status=MappingStatus.IGNORED)
        .count()
    )
    return Finding(
        code="unmapped_indicators",
        title="Indicators without a canonical code",
        severity=WARNING if count else OK,
        count=count,
        total=Indicator.objects.count(),
        why="A canonical code is what lets the same release from MT5, "
        "ForexFactory and a statistical agency become one row. Without it a "
        "source's data is an island.",
        action_label="Map them",
        link="/indicators/?state=unmapped",
    )


def _cross_source_disagreements(total: int) -> Finding:
    queryset = EventRelease.objects.filter(cross_source=CrossSource.DISAGREE)
    return Finding(
        code="cross_source",
        title="Cross-source disagreements",
        severity=INFO if queryset.exists() else OK,
        count=queryset.count(),
        total=total,
        why="Both values are retained and nothing is silently picked. A "
        "disagreement is information: usually one source has a bug, sometimes "
        "it is a genuine revision we had not modelled.",
        link="/calendar/?flag=disagree",
        samples=list(queryset.select_related("indicator")[:5]),
    )


def _sentinel_values(total: int) -> Finding:
    """LONG_MIN that reached a value column, raw or scaled."""
    condition = Q()
    for column in VALUE_COLUMNS:
        for sentinel in (LONG_MIN, LONG_MIN_SCALED):
            condition |= Q(
                **{
                    f"{column}__gte": sentinel - SENTINEL_TOLERANCE,
                    f"{column}__lte": sentinel + SENTINEL_TOLERANCE,
                }
            )
    queryset = EventRelease.objects.filter(condition).select_related("indicator")
    count = queryset.count()
    return Finding(
        code="long_min_sentinel",
        title="LONG_MIN read as a number",
        severity=BLOCKING if count else OK,
        count=count,
        total=total,
        detail=f"Detects both {LONG_MIN:.3e} and its ÷1,000,000 form.",
        why="MQL5 writes LONG_MIN for an unset field rather than null. One that "
        "escapes the null check becomes a −9.2×10¹⁸ observation that quietly "
        "destroys every standard deviation computed from the sample. The "
        "sentinel is an exact value, so this looks for it exactly instead of "
        "guessing from magnitude.",
        samples=list(queryset[:5]),
    )


def _absurd_values(total: int) -> Finding:
    condition = Q()
    for column in VALUE_COLUMNS:
        condition |= Q(**{f"{column}__gt": ABSURD_VALUE}) | Q(
            **{f"{column}__lt": -ABSURD_VALUE}
        )
    queryset = EventRelease.objects.filter(condition).select_related("indicator")
    count = queryset.count()
    return Finding(
        code="absurd_values",
        title="Values outside any plausible range",
        severity=WARNING if count else OK,
        count=count,
        total=total,
        detail=f"Threshold ±{ABSURD_VALUE:.0e}.",
        why="A catch-all for scaling mistakes. The threshold sits deliberately "
        "high: Japan's current account really is of order ¥10¹², so anything "
        "tighter flags correct data as broken. Magnitude alone cannot catch a "
        "missed ÷1,000,000 on a small series — a CPI of 3.2 stored as 3,200,000 "
        "looks unremarkable — which is why the unit tests pin that conversion "
        "directly.",
        samples=list(queryset[:5]),
    )


def _missing_period_start(total: int) -> Finding:
    count = EventRelease.objects.filter(reference_period_start__isnull=True).count()
    return Finding(
        code="missing_period_start",
        title="Reference periods that do not sort",
        severity=INFO if count else OK,
        count=count,
        total=total,
        why="The period label alone does not order chronologically, so a "
        "parsed start date sits beside it. Rows without one still merge "
        "correctly; they just cannot be sorted or windowed by period.",
    )


def _orphans() -> Finding:
    indicators = Indicator.objects.annotate(
        n=Count("releases"), a=Count("aliases")
    ).filter(n=0, a=0)
    aliases = IndicatorAlias.objects.filter(indicator__isnull=True)
    count = indicators.count() + aliases.count()
    return Finding(
        code="orphans",
        title="Orphaned indicators and aliases",
        severity=INFO if count else OK,
        count=count,
        detail=f"{indicators.count()} indicator(s) with nothing attached, "
        f"{aliases.count()} alias(es) pointing nowhere.",
        why="Left behind by a purge or a failed merge. Harmless, but they "
        "inflate every 'how many indicators do we track' number.",
        action_label="Remove them",
        action_url="quality:purge_orphans",
    )


def _vol_check(total: int) -> Finding:
    count = EventRelease.objects.filter(vol_check=VolCheck.NOT_CHECKED).count()
    anchorable = EventRelease.objects.filter(
        Q(release_time_utc__isnull=False) | Q(scheduled_time_utc__isnull=False)
    ).count()
    return Finding(
        code="vol_check",
        title="Timestamps never verified against price",
        severity=INFO,
        count=count,
        total=total,
        detail=f"{anchorable:,} release(s) have a timestamp that could be checked.",
        why="The check confirms a realised-volatility spike lands within ±2 "
        "minutes of the stored timestamp, and flags the row if it does not. It "
        "is how this project finds its own bugs — a timestamp wrong by an hour "
        "produces output that is wrong but entirely plausible. It needs the M1 "
        "price backbone, so it arrives with P2.",
    )


def _price_gaps() -> Finding:
    rows = PriceCoverage.objects.all()
    incomplete = [c for c in rows if c.expected_bar_count and c.gap_count]
    total_gap = sum(c.gap_count for c in incomplete)
    return Finding(
        code="price_gaps",
        title="Incomplete price months",
        severity=WARNING if incomplete else OK,
        count=len(incomplete),
        total=rows.count(),
        detail=f"{total_gap:,} minute(s) missing across {len(incomplete)} month(s)."
        if incomplete
        else "No price data loaded yet.",
        why="A −5d … +1M window needs unbroken bars across weekends and "
        "holidays. A gap inside a window silently shortens it, and a shortened "
        "window looks like a smaller effect.",
        link="/instruments/",
    )
