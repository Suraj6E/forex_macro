"""The validation harness — planning.md §4.1, §5.3, §12, P2.

`analytics/volcheck.py` decides whether one timestamp agrees with price and
imports nothing from Django. This module chooses which pair to check each
indicator against, loads the bars once per symbol, stores the grades, and
pools them back into the per-indicator verdict the screen reads.

**Why this phase exists at all.** P1 will happily measure an indicator whose
timestamps are an hour out and return a decay curve with a significance level
attached. Nothing downstream can detect that; the numbers look exactly like
correct ones. The only witness to a wrong clock is the price series itself.
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from django.db.models import Count, Q
from django.db.models.functions import ExtractYear

from analytics import volcheck
from calendar_data.models import EventRelease, Indicator
from prices.models import DEFAULT_PRICE_SOURCE, Instrument, PriceCoverage
from prices.store import read_range
from quality.enums import VolCheck

logger = logging.getLogger(__name__)

#: Hourly is what the backbone currently holds for every pair, 2007 → today.
#: The check reads whatever is stored; this is the preference order.
TIMEFRAMES = ("h1", "m15", "m1")

#: Which pair carries each currency most directly against the dollar. A release
#: has to move price through a leg the pair actually has, so a EUR release is
#: checked on EUR/USD and a JPY release on USD/JPY — checking everything on one
#: pair would grade half the calendar against a currency it never touches.
REFERENCE_PAIR = {
    "USD": "EURUSD",
    "EUR": "EURUSD",
    "GBP": "GBPUSD",
    "JPY": "USDJPY",
    "CHF": "USDCHF",
    "CAD": "USDCAD",
    "AUD": "AUDUSD",
    "NZD": "NZDUSD",
}

#: The calendar carries CNY and a few others we hold no pair for. Their releases
#: can still move the dollar complex, but only indirectly, so they are checked
#: against the most liquid pair and their verdicts read accordingly.
FALLBACK_PAIR = "EURUSD"

#: volcheck's grades are plain strings (§8 — it may not import Django). This is
#: the one place the two vocabularies meet, and the assertion below keeps them
#: from drifting apart silently.
GRADE_TO_FIELD = {
    volcheck.CONFIRMED_HOUR: VolCheck.CONFIRMED_HOUR,
    volcheck.OFFSET: VolCheck.OFFSET,
    volcheck.NO_SPIKE: VolCheck.NO_SPIKE,
    volcheck.CONFOUNDED: VolCheck.CONFOUNDED,
    volcheck.UNCHECKABLE: VolCheck.UNCHECKABLE,
}
assert all(grade == field_value.value for grade, field_value in GRADE_TO_FIELD.items())

#: Grades that came from an actual comparison, as opposed to "never run" or
#: "we hold no bars there".
GRADED = (
    VolCheck.CONFIRMED_HOUR,
    VolCheck.OFFSET,
    VolCheck.NO_SPIKE,
    VolCheck.CONFOUNDED,
)


# ------------------------------------------------------------------ running --


@dataclass
class IndicatorCheck:
    indicator_id: int
    label: str
    symbol: str
    releases: int = 0
    confirmed: int = 0
    offset: int = 0
    no_spike: int = 0
    confounded: int = 0
    uncheckable: int = 0
    verdict: volcheck.Verdict | None = None
    note: str = ""


@dataclass
class RunResult:
    indicators: list = field(default_factory=list)
    releases_graded: int = 0
    skipped: list = field(default_factory=list)

    @property
    def summary(self) -> str:
        wrong = [c for c in self.indicators if c.verdict and c.verdict.wrong]
        head = (
            f"{len(self.indicators)} indicator(s) checked, "
            f"{self.releases_graded:,} release(s) graded"
        )
        if wrong:
            names = ", ".join(c.label for c in wrong[:3])
            return f"{head} — {len(wrong)} with systematically offset timestamps: {names}"
        return f"{head} — no systematic timestamp offsets found"


class Occupancy:
    """Who else is on the calendar, minute by minute, across all 19 years.

    A bar of the scan may not win if a release of equal or greater importance
    sits in it — see `analytics.volcheck`'s docstring for why. Answering that
    question per release per offset is a bisect over one sorted array, built
    once per run rather than queried per release.
    """

    def __init__(self, min_importance: int = 0):
        rows = (
            EventRelease.objects.filter(release_time_utc__isnull=False)
            .filter(indicator__importance__gte=min_importance)
            .values_list("release_time_utc", "indicator__importance", "indicator_id")
            .order_by("release_time_utc")
        )
        self._epochs: list[int] = []
        self._weight: list[int] = []
        self._owner: list[int] = []
        for moment, importance, indicator_id in rows:
            self._epochs.append(int(moment.timestamp()))
            self._weight.append(importance or 0)
            self._owner.append(indicator_id)

    def blocked_offsets(
        self,
        t0,
        *,
        bar_seconds: int,
        importance: int,
        indicator_id: int,
        neighbourhood: int,
    ) -> set[int]:
        """Offsets whose bar holds someone at least as important as us."""
        epoch = int(t0.timestamp())
        blocked = set()
        for step in range(-neighbourhood, neighbourhood + 1):
            if step == 0:
                # The stored hour is never blocked. A heavyweight there is only
                # a coincidence of our own timestamp being where it is — if the
                # clock were wrong we would be looking at a different hour — so
                # it does not systematically manufacture confirmations the way a
                # fixed neighbouring cluster manufactures offsets.
                continue
            offset_seconds = step * bar_seconds
            start = epoch + offset_seconds
            start -= start % bar_seconds
            position = bisect_left(self._epochs, start)
            while position < len(self._epochs) and self._epochs[position] < start + bar_seconds:
                if (
                    self._owner[position] != indicator_id
                    and self._weight[position] >= importance
                ):
                    blocked.add(offset_seconds)
                    break
                position += 1
        return blocked


def reference_instrument(currency: str) -> Instrument | None:
    symbol = REFERENCE_PAIR.get((currency or "").upper(), FALLBACK_PAIR)
    return Instrument.objects.filter(symbol=symbol).first()


def stored_timeframe(instrument: Instrument) -> str | None:
    """Finest bar size actually held for this pair."""
    stored = set(
        PriceCoverage.objects.filter(
            instrument=instrument, source__key=DEFAULT_PRICE_SOURCE
        ).values_list("timeframe", flat=True)
    )
    for timeframe in TIMEFRAMES:
        if timeframe in stored:
            return timeframe
    return None


def checkable_indicators(*, importance: int | None = None, currency: str = ""):
    """Indicators with enough timestamped releases for a verdict to mean anything."""
    queryset = Indicator.objects.annotate(
        n=Count("releases", filter=Q(releases__release_time_utc__isnull=False))
    ).filter(n__gte=volcheck.MIN_VERDICT_N)
    if importance is not None:
        queryset = queryset.filter(importance=importance)
    if currency:
        queryset = queryset.filter(currency=currency)
    return queryset.order_by("-importance", "-n", "currency", "name")


def run_vol_check(
    *,
    indicator: Indicator | None = None,
    importance: int | None = None,
    currency: str = "",
    max_indicators: int | None = None,
    log=lambda msg: None,
    progress=lambda frac, msg: None,
) -> RunResult:
    """Grade every timestamped release of the selected indicators against price."""
    if indicator is not None:
        targets = [indicator]
    else:
        targets = list(checkable_indicators(importance=importance, currency=currency))
        if max_indicators:
            targets = targets[:max_indicators]

    result = RunResult()
    if not targets:
        log("No indicator has enough timestamped releases to check.")
        return result

    bar_cache: dict[str, volcheck.BarIndex | None] = {}
    log("indexing the calendar for co-timed releases")
    occupancy = Occupancy()

    for position, target in enumerate(targets, start=1):
        progress(
            (position - 1) / len(targets),
            f"{position}/{len(targets)} · {target.currency} {target.name}",
        )

        instrument = reference_instrument(target.currency)
        if instrument is None:
            result.skipped.append(f"{target.name}: no pair holds {target.currency}")
            continue

        index = _bar_index(instrument, bar_cache, log)
        if index is None:
            result.skipped.append(f"{target.name}: no bars stored for {instrument.symbol}")
            continue

        check = _check_indicator(target, instrument, index, occupancy, log)
        if check.releases:
            result.indicators.append(check)
            result.releases_graded += check.releases

    progress(1.0, result.summary)
    log(result.summary)
    for note in result.skipped:
        log(f"skipped — {note}")
    return result


def _check_indicator(
    indicator: Indicator,
    instrument: Instrument,
    index: volcheck.BarIndex,
    occupancy: Occupancy,
    log,
) -> IndicatorCheck:
    releases = list(
        EventRelease.objects.filter(
            indicator=indicator, release_time_utc__isnull=False
        )
        .only("id", "release_time_utc")
        .order_by("release_time_utc")
    )
    check = IndicatorCheck(
        indicator_id=indicator.pk,
        label=f"{indicator.currency} {indicator.name}",
        symbol=instrument.symbol,
    )
    if not releases:
        return check

    # The indicator's own history, so the baseline never draws its idea of
    # "normal" from previous instances of the very event being located.
    exclude = volcheck.exclusion_epochs(r.release_time_utc for r in releases)

    rows = []
    counts: Counter = Counter()
    for release in releases:
        graded = volcheck.check_release(
            index,
            release.release_time_utc,
            exclude=exclude,
            blocked=occupancy.blocked_offsets(
                release.release_time_utc,
                bar_seconds=index.bar_seconds,
                importance=indicator.importance or 0,
                indicator_id=indicator.pk,
                neighbourhood=volcheck.NEIGHBOURHOOD,
            ),
        )
        release.vol_check = GRADE_TO_FIELD[graded.status]
        release.vol_check_offset_min = graded.offset_minutes
        release.vol_check_ratio = graded.ratio
        counts[graded.status] += 1
        rows.append(release)

    EventRelease.objects.bulk_update(
        rows,
        ["vol_check", "vol_check_offset_min", "vol_check_ratio"],
        batch_size=1000,
    )

    check.releases = len(rows)
    check.confirmed = counts[volcheck.CONFIRMED_HOUR]
    check.offset = counts[volcheck.OFFSET]
    check.no_spike = counts[volcheck.NO_SPIKE]
    check.confounded = counts[volcheck.CONFOUNDED]
    check.uncheckable = counts[volcheck.UNCHECKABLE]
    check.verdict = volcheck.summarise(
        (r.vol_check, r.vol_check_offset_min, r.vol_check_ratio) for r in rows
    )
    log(f"{check.label} × {instrument.symbol}: {check.verdict.message}")
    return check


def _bar_index(
    instrument: Instrument, cache: dict, log
) -> volcheck.BarIndex | None:
    """Every stored bar for one pair, indexed once and reused.

    Loading per indicator would re-read the same nineteen years for each of the
    dozens of US series; one read per symbol turns the sweep from minutes of
    Parquet I/O into seconds.
    """
    if instrument.symbol in cache:
        return cache[instrument.symbol]

    timeframe = stored_timeframe(instrument)
    if timeframe is None:
        cache[instrument.symbol] = None
        return None

    window = PriceCoverage.objects.filter(
        instrument=instrument, source__key=DEFAULT_PRICE_SOURCE, timeframe=timeframe
    ).order_by("month")
    first = window.first()
    last = window.last()
    if first is None:
        cache[instrument.symbol] = None
        return None

    bars = read_range(
        instrument.symbol,
        DEFAULT_PRICE_SOURCE,
        _as_datetime(first.month),
        _as_datetime(last.month) + timedelta(days=32),
        timeframe,
    )
    if bars.empty:
        cache[instrument.symbol] = None
        return None

    index = volcheck.BarIndex(bars)
    log(f"{instrument.symbol}: {len(index):,} {timeframe} bars indexed")
    cache[instrument.symbol] = index
    return index


def _as_datetime(day):
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


# ----------------------------------------------------------------- reporting --


@dataclass
class IndicatorVerdict:
    indicator_id: int
    name: str
    currency: str
    importance: int
    symbol: str
    verdict: volcheck.Verdict

    @property
    def sort_key(self):
        """Wrong clocks first, then the ones we cannot yet vouch for."""
        rank = {
            volcheck.OFFSET_CLOCK: 0,
            volcheck.SCATTERED: 1,
            volcheck.ALIGNED: 2,
            volcheck.SILENT: 3,
            volcheck.INSUFFICIENT: 4,
        }
        return (rank.get(self.verdict.status, 9), -self.verdict.checked)


def _graded_rows(**filters):
    return EventRelease.objects.filter(vol_check__in=GRADED, **filters).values_list(
        "indicator_id", "vol_check", "vol_check_offset_min", "vol_check_ratio"
    )


def verdicts() -> list[IndicatorVerdict]:
    """One pooled verdict per checked indicator, from the stored columns.

    Nothing is recomputed to draw this screen: the per-release grades are the
    stored record, and pooling them is a group-by. Re-running the check is an
    explicit action, so the page can never quietly disagree with the database.

    **Always every checked indicator — filter the result, never the input.**
    The false-discovery adjustment depends on how many hypotheses the sweep
    tested, so pooling a single currency on its own would let the same
    indicator read "clock wrong" on a filtered page and "scattered" on the
    unfiltered one. `matching()` does the narrowing afterwards.
    """
    grouped: dict[int, list] = {}
    for indicator_id, status, offset, ratio in _graded_rows():
        grouped.setdefault(indicator_id, []).append((status, offset, ratio))
    if not grouped:
        return []

    meta = {
        row["id"]: row
        for row in Indicator.objects.filter(pk__in=grouped).values(
            "id", "name", "currency", "importance"
        )
    }
    out = []
    for indicator_id, rows in grouped.items():
        info = meta.get(indicator_id)
        if info is None:
            continue
        out.append(
            IndicatorVerdict(
                indicator_id=indicator_id,
                name=info["name"],
                currency=info["currency"],
                importance=info["importance"],
                symbol=REFERENCE_PAIR.get(info["currency"], FALLBACK_PAIR),
                verdict=volcheck.summarise(rows),
            )
        )

    volcheck.apply_fdr([row.verdict for row in out])
    return sorted(out, key=lambda v: v.sort_key)


def matching(
    rows: list[IndicatorVerdict], *, importance: int | None = None, currency: str = ""
) -> list[IndicatorVerdict]:
    """The screen's filters, applied to already-pooled verdicts."""
    if importance is not None:
        rows = [row for row in rows if row.importance == importance]
    if currency:
        rows = [row for row in rows if row.currency == currency]
    return rows


def headline() -> dict:
    """Counts for the top of the screen — one scan, not one query per grade."""
    counts = EventRelease.objects.aggregate(
        anchorable=Count("id", filter=Q(release_time_utc__isnull=False)),
        graded=Count("id", filter=Q(vol_check__in=GRADED)),
        confirmed=Count("id", filter=Q(vol_check=VolCheck.CONFIRMED_HOUR)),
        offset=Count("id", filter=Q(vol_check=VolCheck.OFFSET)),
        no_spike=Count("id", filter=Q(vol_check=VolCheck.NO_SPIKE)),
        confounded=Count("id", filter=Q(vol_check=VolCheck.CONFOUNDED)),
        uncheckable=Count("id", filter=Q(vol_check=VolCheck.UNCHECKABLE)),
    )
    graded = counts["graded"] or 0
    spiked = counts["confirmed"] + counts["offset"]
    counts["spiked"] = spiked
    counts["confirmed_share"] = (counts["confirmed"] / spiked * 100) if spiked else 0.0
    counts["graded_share"] = (graded / counts["anchorable"] * 100) if counts["anchorable"] else 0.0
    return counts


def offset_verdicts_cheap() -> list[tuple[str, volcheck.Verdict]]:
    """Every checked indicator's verdict from two GROUP BYs.

    The Data Quality screen wants one number — how many clocks are wrong — and
    should not pay for pulling every graded release to get it. Both paths route
    through `volcheck.summarise_counts`, so the two screens cannot end up
    applying different thresholds to the same data.
    """
    quiet = {
        row["indicator_id"]: row["n"]
        for row in EventRelease.objects.filter(vol_check=VolCheck.NO_SPIKE)
        .values("indicator_id")
        .annotate(n=Count("id"))
    }
    blocked = {
        row["indicator_id"]: row["n"]
        for row in EventRelease.objects.filter(vol_check=VolCheck.CONFOUNDED)
        .values("indicator_id")
        .annotate(n=Count("id"))
    }
    offsets: dict[int, dict[int, int]] = {}
    located = EventRelease.objects.filter(
        vol_check__in=(VolCheck.CONFIRMED_HOUR, VolCheck.OFFSET),
        vol_check_offset_min__isnull=False,
    ).values("indicator_id", "vol_check_offset_min").annotate(n=Count("id"))
    for row in located:
        offsets.setdefault(row["indicator_id"], {})[row["vol_check_offset_min"]] = row["n"]

    seen = set(offsets) | set(quiet) | set(blocked)
    names = dict(Indicator.objects.filter(pk__in=seen).values_list("id", "name"))
    out = []
    for indicator_id in seen:
        verdict = volcheck.summarise_counts(
            offsets=offsets.get(indicator_id, {}),
            no_spike=quiet.get(indicator_id, 0),
            confounded=blocked.get(indicator_id, 0),
        )
        out.append((names.get(indicator_id, str(indicator_id)), verdict))

    # Same sweep-wide false-discovery control as the Timestamps screen, or the
    # two pages would count a different number of broken clocks.
    volcheck.apply_fdr([verdict for _name, verdict in out])
    return out


def offset_histogram(indicator_id: int) -> list[tuple[str, int]]:
    """Where the spikes actually landed, for one indicator.

    This is the picture that separates "the clock is wrong" from "these
    releases are weak": a wrong clock puts one bar far above the others; weak
    releases spread thinly across the whole neighbourhood.
    """
    rows = (
        EventRelease.objects.filter(
            indicator_id=indicator_id,
            vol_check__in=(VolCheck.CONFIRMED_HOUR, VolCheck.OFFSET),
            vol_check_offset_min__isnull=False,
        )
        .values("vol_check_offset_min")
        .annotate(n=Count("id"))
    )
    found = {row["vol_check_offset_min"]: row["n"] for row in rows}
    # The hourly grid, plus any offset actually stored that does not sit on it —
    # a finer backbone would produce 15-minute offsets, and dropping them from
    # the picture would understate exactly the cases worth looking at.
    span = max([abs(k) for k in found] + [volcheck.NEIGHBOURHOOD * 60])
    grid = set(range(-span, span + 60, 60)) | set(found)
    return [(_offset_label(minutes), found.get(minutes, 0)) for minutes in sorted(grid)]


def _offset_label(minutes: int) -> str:
    if minutes == 0:
        return "on time"
    if minutes % 60 == 0:
        return f"{minutes // 60:+d}h"
    return f"{minutes:+d}m"


def dst_split(indicator_id: int) -> list[dict]:
    """The same verdict computed separately for US summer and winter.

    §12 rates a DST shift High likelihood and High impact, and it has a
    signature no other bug shares: the same indicator resolves to one offset in
    EDT and an offset exactly one hour different in EST. Pooled together those
    two cancel into "scattered"; split apart they name the bug.
    """
    rows = EventRelease.objects.filter(
        indicator_id=indicator_id, vol_check__in=GRADED
    ).values_list("release_time_utc", "vol_check", "vol_check_offset_min", "vol_check_ratio")

    halves: dict[str, list] = {"EDT": [], "EST": []}
    for moment, status, offset, ratio in rows:
        if moment is None:
            continue
        halves[volcheck.dst_window(moment)].append((status, offset, ratio))

    return [
        {
            "window": window,
            "label": "US summer (EDT)" if window == "EDT" else "US winter (EST)",
            "verdict": volcheck.summarise(entries),
        }
        for window, entries in halves.items()
    ]


def coverage_by_year() -> list[dict]:
    """Releases we could grade against releases we hold a timestamp for.

    The P2 coverage question, which is not the same as the price-gap question:
    a month of bars can be complete and still leave a release ungradable,
    because the baseline needs twelve matched weekday-hour slots behind it and
    the first months of the price history have nothing behind them.
    """
    rows = (
        EventRelease.objects.filter(release_time_utc__isnull=False)
        .annotate(year=ExtractYear("release_time_utc"))
        .values("year")
        .annotate(
            anchorable=Count("id"),
            graded=Count("id", filter=Q(vol_check__in=GRADED)),
            uncheckable=Count("id", filter=Q(vol_check=VolCheck.UNCHECKABLE)),
        )
        .order_by("year")
    )
    return list(rows)


def co_timed_at_offset(
    indicator_id: int, offset_minutes: int, *, limit: int = 6, min_importance: int = 2
) -> list[tuple[str, int]]:
    """What else the calendar holds in the hour where the spikes actually land.

    An offset verdict has two possible readings and the check alone cannot tell
    them apart: the clock is wrong by that much, or something bigger reliably
    happens that much later — the ECB press conference forty-five minutes after
    the rate decision, or the 08:30 New York cluster two hours before a 10:00
    release. This is the evidence that separates them, and it is why an offset
    is shown with its neighbours rather than asserted as a bug.

    Restricted to releases that could plausibly out-move the one being checked;
    a low-impact print in the same hour explains nothing.
    """
    if not offset_minutes:
        return []

    moments = EventRelease.objects.filter(
        indicator_id=indicator_id,
        vol_check=VolCheck.OFFSET,
        vol_check_offset_min=offset_minutes,
        release_time_utc__isnull=False,
    ).values_list("release_time_utc", flat=True)
    if not moments:
        return []

    shift = timedelta(minutes=offset_minutes)
    bars = {_floor_hour(moment + shift) for moment in moments}
    candidates = (
        EventRelease.objects.filter(
            release_time_utc__gte=min(bars),
            release_time_utc__lt=max(bars) + timedelta(hours=1),
            indicator__importance__gte=min_importance,
        )
        .exclude(indicator_id=indicator_id)
        .values_list("release_time_utc", "indicator__currency", "indicator__name")
    )

    found: Counter = Counter()
    for moment, currency, name in candidates:
        if _floor_hour(moment) in bars:
            found[f"{currency} {name}"] += 1
    return found.most_common(limit)


def _floor_hour(moment):
    return moment.replace(minute=0, second=0, microsecond=0)


def offset_samples(indicator_id: int, limit: int = 8):
    """Individual releases whose spike landed somewhere else — the audit trail.

    A verdict that cannot be traced to specific releases is an assertion. These
    link straight to the price chart centred on each one, which is where a
    human settles it.
    """
    return (
        EventRelease.objects.filter(
            indicator_id=indicator_id, vol_check=VolCheck.OFFSET
        )
        .select_related("indicator")
        .order_by("-release_time_utc")[:limit]
    )
