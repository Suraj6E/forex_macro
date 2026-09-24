"""Event study measurement — planning.md §3.1, §6.1, §6.2.

The minimum viable chain, made concrete:

    Premise 1. An economic release lands at a known instant.
    Premise 2. We have the price of each major pair on both sides of it.
    Inference 1. We can measure what price did afterwards.
    Premise 3. Price also moves for reasons unrelated to this event.
    Inference 2. A single occurrence tells us nothing; we need the average
    behaviour across many occurrences, measured against what price normally
    does at that weekday and hour.

So every measurement here comes in two forms: the raw move, and the **abnormal**
move — observed minus what this pair normally does in the same weekday-and-hour
window. §6.1: the abnormal version is what makes this an event study rather
than a picture of a move.

No Django imports (§8): this is importable from a notebook against the same
Parquet files the UI reads.
"""

from __future__ import annotations

import math
import weakref
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from analytics.horizons import LADDER, Horizon
from analytics.timeutils import open_seconds_between

#: How many matched weekday-and-hour periods to draw the "normal" from.
#: Twelve weeks is a quarter — long enough for a usable spread, short enough
#: that the volatility regime has not usually changed underneath it (§4.6).
BASELINE_WEEKS = 12

#: A window needs at least this fraction of its bars present to be measured.
#: Below it the window is short, and a short window reads as a smaller effect.
MIN_BAR_COVERAGE = 0.6


@dataclass
class WindowMeasurement:
    horizon: str
    seconds: int
    ret: float | None = None
    abs_ret: float | None = None
    abnormal_ret: float | None = None
    realized_vol: float | None = None
    mfe: float | None = None
    mae: float | None = None
    n_bars: int = 0
    bars_expected: int = 0
    baseline_mean: float | None = None
    baseline_sd: float | None = None
    baseline_abs_mean: float | None = None
    baseline_n: int = 0

    @property
    def usable(self) -> bool:
        return self.ret is not None

    @property
    def abs_excess(self) -> float | None:
        """How much further price moved than it normally does.

        **This is Mode A's measurement, not the signed return.** Without a
        forecast the direction is unsignable (§3.5): good and bad surprises
        average against each other, so the signed mean of a symmetric event is
        ~0 however violently price reacted. Absolute movement needs no forecast
        at all, which is exactly why §6.4 ranks events on it.
        """
        if self.abs_ret is None or self.baseline_abs_mean is None:
            return None
        return self.abs_ret - self.baseline_abs_mean

    @property
    def abs_ratio(self) -> float | None:
        """Observed move as a multiple of the normal move for this slot."""
        if self.abs_ret is None or not self.baseline_abs_mean:
            return None
        return self.abs_ret / self.baseline_abs_mean

    @property
    def standardised(self) -> float | None:
        """Abnormal move in units of this pair's own normal movement.

        §6.2's single-event curve: "that event's abnormal move divided by the
        pair's typical movement over the same span". The noise estimate comes
        from the pair's own surrounding weeks, not from other releases, which
        is why one event yields a complete curve.
        """
        if self.abnormal_ret is None or not self.baseline_sd:
            return None
        return self.abnormal_ret / self.baseline_sd


def supported_horizons(bar_seconds: int, ladder=LADDER) -> list[Horizon]:
    """Rungs this bar size can actually resolve.

    Hourly bars cannot answer "+5m"; pretending otherwise would return a number
    that looks like a measurement and is an artefact of the interpolation. §6.6
    wants the detectability floor stated, and this is the same idea applied to
    resolution: say what cannot be measured rather than measuring it badly.
    """
    return [h for h in ladder if abs(h.seconds) >= bar_seconds]


def _price_frame(bars: pd.DataFrame) -> pd.DataFrame:
    frame = bars.copy()
    frame["ts_utc"] = pd.to_datetime(frame["ts_utc"], utc=True)
    return frame.sort_values("ts_utc").reset_index(drop=True)


@dataclass(frozen=True)
class _Columns:
    """The bar columns as plain arrays, timestamps as UTC nanoseconds.

    A study makes hundreds of thousands of single-bar lookups; through pandas
    each one costs a column box and an `iloc`, which was three quarters of a
    study's run time.
    """

    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray


#: Arrays for frames that went through `prepare_bars`, keyed by id and
#: dropped when the frame is collected. Not in `frame.attrs`: pandas deep-
#: copies attrs on every slice.
_COLUMNS: dict[int, _Columns] = {}

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _build_columns(bars: pd.DataFrame) -> _Columns:
    stamps = bars["ts_utc"].dt.tz_convert("UTC").dt.tz_localize(None)
    return _Columns(
        ts=stamps.to_numpy(dtype="datetime64[ns]").view("int64"),
        open=bars["open"].to_numpy(dtype=float),
        high=bars["high"].to_numpy(dtype=float),
        low=bars["low"].to_numpy(dtype=float),
        close=bars["close"].to_numpy(dtype=float),
    )


def _columns(bars: pd.DataFrame) -> _Columns:
    cached = _COLUMNS.get(id(bars))
    if cached is not None:
        return cached
    return _build_columns(_price_frame(bars))


def _ns(moment: datetime) -> int:
    """Exact UTC nanoseconds; float seconds would lose the low digits."""
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1000


def prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Sorted, UTC-typed bars with their lookup arrays built once.

    The frame is treated as read-only from here on: the arrays are not
    rebuilt if it is edited in place.
    """
    frame = _price_frame(bars)
    frame.attrs["prepared"] = True
    key = id(frame)
    _COLUMNS[key] = _build_columns(frame)
    weakref.finalize(frame, _COLUMNS.pop, key, None)
    return frame


def _bar_seconds(bars: pd.DataFrame) -> int:
    if len(bars) < 2:
        return 3600
    deltas = bars["ts_utc"].diff().dropna().dt.total_seconds()
    return int(deltas.mode().iloc[0]) if len(deltas) else 3600


def price_before(bars: pd.DataFrame, moment: datetime) -> float | None:
    """The last price known at or before `moment`.

    Uses the **open** of the bar containing the instant — the price when that
    bar began, which precedes the release. With hourly bars that is up to one
    bar early; the granularity is a property of the data, and stating it is
    better than interpolating a number that implies precision we do not have.
    """
    cols = _columns(bars)
    index = int(np.searchsorted(cols.ts, _ns(moment), side="right")) - 1
    if index < 0 or index >= cols.ts.size:
        return None
    return float(cols.open[index])


def price_at(bars: pd.DataFrame, moment: datetime) -> float | None:
    """Close of the bar containing `moment`."""
    cols = _columns(bars)
    index = int(np.searchsorted(cols.ts, _ns(moment), side="right")) - 1
    if index < 0 or index >= cols.ts.size:
        return None
    return float(cols.close[index])


def raw_return(bars: pd.DataFrame, t0: datetime, horizon: Horizon) -> float | None:
    """Log return over the horizon.

    Post-release horizons measure t0 → t0+h. Pre-release horizons measure
    t0+h → t0, i.e. the move running *into* the release — which is what §3.4
    calls anticipation, and it only reads correctly with that sign convention.
    """
    anchor = price_before(bars, t0)
    if anchor is None or anchor <= 0:
        return None

    other_moment = t0 + timedelta(seconds=horizon.seconds)
    if horizon.seconds >= 0:
        other = price_at(bars, other_moment)
        if other is None or other <= 0:
            return None
        return math.log(other / anchor)

    other = price_before(bars, other_moment)
    if other is None or other <= 0:
        return None
    return math.log(anchor / other)


def measure_window(bars: pd.DataFrame, t0: datetime, horizon: Horizon,
                   bar_seconds: int) -> WindowMeasurement:
    """Everything §6.1 asks for at one rung of the ladder."""
    result = WindowMeasurement(horizon=horizon.label, seconds=horizon.seconds)

    start = min(t0, t0 + timedelta(seconds=horizon.seconds))
    end = max(t0, t0 + timedelta(seconds=horizon.seconds))
    cols = _columns(bars)
    lo = int(np.searchsorted(cols.ts, _ns(start), side="left"))
    hi = int(np.searchsorted(cols.ts, _ns(end), side="right"))

    # Against *open* hours, not calendar hours — see open_seconds_between.
    open_seconds = open_seconds_between(start, end)
    result.bars_expected = max(int(open_seconds // bar_seconds), 1)
    result.n_bars = hi - lo

    ret = raw_return(bars, t0, horizon)
    if ret is None or result.n_bars < result.bars_expected * MIN_BAR_COVERAGE:
        return result

    result.ret = ret
    result.abs_ret = abs(ret)

    if result.n_bars > 1:
        steps = np.log(cols.close[lo:hi] / cols.open[lo:hi])
        steps = steps[np.isfinite(steps)]
        if steps.size:
            result.realized_vol = float(np.sqrt(np.sum(steps ** 2)))

    anchor = price_before(bars, t0)
    if anchor and result.n_bars:
        result.mfe = float(np.log(np.max(cols.high[lo:hi]) / anchor))
        result.mae = float(np.log(np.min(cols.low[lo:hi]) / anchor))

    return result


#: Same-hour substitutes, nearest first, for a week whose matched slot is
#: contaminated. Weekends are skipped when they come up.
SUBSTITUTE_DAYS = (-1, 1, -2, 2)


def matched_baseline(
    bars: pd.DataFrame,
    t0: datetime,
    horizon: Horizon,
    weeks: int = BASELINE_WEEKS,
    exclude: list[datetime] | None = None,
    max_lookback_weeks: int | None = None,
) -> tuple[float | None, float | None, float | None, int]:
    """What this pair normally does at this weekday and hour.

    §6.1 specifies matched weekday-and-hour periods over the prior N weeks. The
    match matters: FX has a strong intraday and intraweek shape, so comparing a
    Friday 12:30 move against an all-hours average would attribute the ordinary
    London–New York overlap to the release.

    **`exclude` is what stops the comparison being circular.** A weekly release
    recurs on the same weekday at the same hour, so every look-back lands on a
    previous instance of the event itself — measured on the real data, 69% of
    Natural Gas Storage baseline windows contained another Natural Gas Storage
    release. The event is then compared against itself and the ratio collapses
    to 1 by construction.

    es-2 skipped such windows and walked further back, which fails for a weekly
    series: *every* same-weekday look-back holds a release, so the survivors
    were the holiday-shifted weeks and DST crossings (Claims kept 469 of 1,020
    releases at +1h and 41 at +1w; Natural Gas Storage kept none at +1w). Two
    changes fix that:

    - **A contaminated week is replaced, not skipped.** The same hour one or
      two days either side stands in for it, so the sample stays within the
      twelve weeks it is meant to come from. A monthly series only ever needs
      this for the one week the previous print lands in.
    - **Contamination is counted, not detected.** A +1w window after a weekly
      release necessarily contains the next one, so a baseline window holding
      one release is like-for-like there, not circular. A candidate may hold
      as many of the indicator's releases as the event window holds besides
      the event itself — at +1h that is zero, which is the old rule.

    A baseline window may never contain `t0` itself: at +1M the one-week
    look-back would otherwise measure the event as its own normal.
    """
    excluded = sorted(exclude or [])
    ceiling = max_lookback_weeks or weeks * 4
    allowed = _others_in_window(excluded, t0, horizon, t0)

    samples = []
    for week in range(1, ceiling + 1):
        if len(samples) >= weeks:
            break
        slot = t0 - timedelta(weeks=week)
        for shift in (0, *SUBSTITUTE_DAYS):
            moment = slot + timedelta(days=shift)
            if shift and moment.weekday() >= 5:
                continue
            if max(moment, moment + timedelta(seconds=horizon.seconds)) >= t0:
                continue
            if _others_in_window(excluded, moment, horizon) > allowed:
                continue
            value = raw_return(bars, moment, horizon)
            if value is not None and math.isfinite(value):
                samples.append(value)
                break

    if len(samples) < 3:
        return None, None, None, len(samples)

    array = np.array(samples, dtype=float)
    # Sample standard deviation: with ~12 observations the population form is
    # biased low, and this number is the denominator of every effect size.
    return (
        float(array.mean()),
        float(array.std(ddof=1)),
        float(np.abs(array).mean()),
        len(samples),
    )


def _window_contains(excluded: list[datetime], moment: datetime, horizon: Horizon) -> bool:
    """Does this candidate baseline window hold an excluded release?"""
    return _others_in_window(excluded, moment, horizon) > 0


def _others_in_window(excluded: list[datetime], moment: datetime, horizon: Horizon,
                      skip: datetime | None = None) -> int:
    """How many excluded releases the window's measured move contains.

    The ends follow how `raw_return` reads them. A post-release window runs
    from the open of the bar holding `moment` to the close of the bar holding
    the far end, so a release at either end is inside it. A pre-release window
    ends at the *open* of the bar holding `moment`, before anything released
    at that instant has moved price, so its near end is open.
    """
    if not excluded:
        return 0
    start = min(moment, moment + timedelta(seconds=horizon.seconds))
    end = max(moment, moment + timedelta(seconds=horizon.seconds))
    if horizon.seconds < 0:
        count = bisect_left(excluded, end) - bisect_left(excluded, start)
        if skip is not None and start <= skip < end:
            count -= bisect_right(excluded, skip) - bisect_left(excluded, skip)
        return count
    count = bisect_right(excluded, end) - bisect_left(excluded, start)
    if skip is not None and start <= skip <= end:
        count -= bisect_right(excluded, skip) - bisect_left(excluded, skip)
    return count


def measure_event(
    bars: pd.DataFrame,
    t0: datetime,
    horizons=None,
    *,
    baseline_weeks: int = BASELINE_WEEKS,
    exclude: list[datetime] | None = None,
) -> list[WindowMeasurement]:
    """One release against one instrument, across the whole ladder.

    Pass bars through `prepare_bars` once when measuring many releases against
    the same frame; otherwise every call copies and sorts the whole history.
    """
    if not bars.attrs.get("prepared"):
        bars = prepare_bars(bars)
    if bars.empty:
        return []

    bar_seconds = _bar_seconds(bars)
    rungs = horizons if horizons is not None else supported_horizons(bar_seconds)

    out = []
    for horizon in rungs:
        measurement = measure_window(bars, t0, horizon, bar_seconds)
        if measurement.usable:
            mean, sd, abs_mean, n = matched_baseline(
                bars, t0, horizon, baseline_weeks, exclude=exclude
            )
            measurement.baseline_mean = mean
            measurement.baseline_sd = sd
            measurement.baseline_abs_mean = abs_mean
            measurement.baseline_n = n
            if mean is not None:
                measurement.abnormal_ret = measurement.ret - mean
        out.append(measurement)
    return out


@dataclass
class PooledPoint:
    horizon: str
    seconds: int
    n: int = 0

    #: Signed abnormal return. Near zero for any symmetric event in Mode A —
    #: that is the expected result, not a failure. It becomes the headline in
    #: Mode B, where the surprise supplies a direction.
    mean_abnormal: float | None = None
    std_error: float | None = None
    t_stat: float | None = None
    p_value: float | None = None

    #: **The Mode A headline** (§6.4): how much further price moved than it
    #: normally does in the same weekday-and-hour slot, and whether that excess
    #: is distinguishable from zero.
    mean_abs_excess: float | None = None
    abs_std_error: float | None = None
    abs_t_stat: float | None = None
    abs_p_value: float | None = None
    abs_ratio: float | None = None
    #: Median matched weeks behind each release's normal. Low means the
    #: baseline search struggled, and the ratio rests on a thin denominator.
    baseline_n: int | None = None

    hit_rate: float | None = None
    detectability_floor: float | None = None
    samples: list = field(default_factory=list)

    @property
    def significant(self) -> bool:
        """Mode A significance: did price move more than usual?"""
        return self.abs_p_value is not None and self.abs_p_value < 0.05

    @property
    def signed_significant(self) -> bool:
        return self.p_value is not None and self.p_value < 0.05


def pool(measurements_by_event: list[list[WindowMeasurement]]) -> list[PooledPoint]:
    """Average across occurrences — §6.2's level 2 curve.

    Returns one point per horizon with n, standard error, t and p, plus the
    **detectability floor**: the smallest effect this many observations could
    have resolved. §6.6 — a null result then reads as "no effect larger than
    X", which is informative, rather than "no effect", which is not.
    """
    buckets: dict[str, list[WindowMeasurement]] = {}
    order: list[tuple[str, int]] = []
    for event in measurements_by_event:
        for measurement in event:
            if measurement.abnormal_ret is None:
                continue
            if measurement.horizon not in buckets:
                buckets[measurement.horizon] = []
                order.append((measurement.horizon, measurement.seconds))
            buckets[measurement.horizon].append(measurement)

    points = []
    for label, seconds in sorted(order, key=lambda item: item[1]):
        group = buckets[label]
        values = np.array([m.abnormal_ret for m in group], dtype=float)
        n = values.size
        point = PooledPoint(horizon=label, seconds=seconds, n=n, samples=values.tolist())

        if n >= 2:
            mean = float(values.mean())
            sd = float(values.std(ddof=1))
            se = sd / math.sqrt(n) if sd > 0 else None
            point.mean_abnormal = mean
            point.std_error = se
            point.hit_rate = float((values > 0).mean())
            if se:
                point.t_stat = mean / se
                point.p_value = _two_sided_p(point.t_stat, n - 1)
                # 80% power at 5% two-sided needs roughly 2.8 standard errors.
                point.detectability_floor = 2.8 * se

            # Mode A: excess absolute movement over the matched normal.
            excess = np.array(
                [m.abs_excess for m in group if m.abs_excess is not None], dtype=float
            )
            if excess.size >= 2:
                abs_mean = float(excess.mean())
                abs_sd = float(excess.std(ddof=1))
                abs_se = abs_sd / math.sqrt(excess.size) if abs_sd > 0 else None
                point.mean_abs_excess = abs_mean
                point.abs_std_error = abs_se
                if abs_se:
                    point.abs_t_stat = abs_mean / abs_se
                    point.abs_p_value = _two_sided_p(point.abs_t_stat, excess.size - 1)

            point.abs_ratio = pooled_ratio(group)
            counts = [m.baseline_n for m in group if m.baseline_n]
            if counts:
                point.baseline_n = int(np.median(counts))
        points.append(point)
    return points


def pooled_ratio(group: list[WindowMeasurement]) -> float | None:
    """Mean observed move over mean normal move — reads 1.0 when nothing happened.

    es-2 took the median of per-event ratios, whose denominator is a mean. For
    a Gaussian, median|x| / mean|x| is 0.845, and hourly FX returns are fatter
    tailed than that: at 400 random non-event hours on EUR/USD the old figure
    read 0.70 at +1h, so "1.0×" on the ranking meant a 43% excess and Core
    PCE's 0.76× was normal, not quiet. A ratio of means has the same numerator
    and denominator statistic, so its neutral point is 1.0 at every horizon.
    """
    pairs = [
        (m.abs_ret, m.baseline_abs_mean)
        for m in group
        if m.abs_ret is not None and m.baseline_abs_mean
    ]
    if not pairs:
        return None
    normal = sum(b for _a, b in pairs)
    return sum(a for a, _b in pairs) / normal if normal > 0 else None


#: Robust z beyond which one release is flagged. Chosen on the stored es-2
#: rows at +1h and +1d: it flags about 3 in 10,000 — the SNB floor removal
#: (z −43) and introduction, the Brexit night, the October 2008 prints. It
#: also catches genuine reactions (US CPI, November 2022, USDJPY at z −11),
#: which is why a flag marks a row and never silently drops it.
OUTLIER_Z = 10.0


def outlier_mask(values: list[float | None], threshold: float = OUTLIER_Z) -> list[bool]:
    """§4.6's `flag`: which values sit implausibly far from the rest.

    Median and MAD, not mean and SD — one 1,133-pip hour inflates the SD enough
    to hide itself. A flag is a mark on the row, not an exclusion: what a
    flagged row does to a result is the pooling step's decision.
    """
    finite = np.array([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if finite.size < 5:
        return [False] * len(values)
    centre = float(np.median(finite))
    spread = float(np.median(np.abs(finite - centre))) * 1.4826
    if spread <= 0:
        return [False] * len(values)
    return [
        v is not None and math.isfinite(v) and abs(v - centre) / spread > threshold
        for v in values
    ]


def _two_sided_p(t_stat: float, degrees: int) -> float | None:
    if degrees < 1:
        return None
    try:
        from scipy import stats
    except ImportError:  # pragma: no cover - scipy is a declared dependency
        return None
    return float(2 * stats.t.sf(abs(t_stat), degrees))


def benjamini_hochberg(p_values: list[float | None], alpha: float = 0.05) -> list[float | None]:
    """FDR-adjusted p-values — §6.6.

    Seven pairs × forty indicators × seventeen horizons is tens of thousands of
    tests; at p<0.05 a large crop of spurious "significant" results is
    guaranteed by chance. The adjusted value sits beside the raw one rather
    than replacing it.
    """
    indexed = [(i, p) for i, p in enumerate(p_values) if p is not None]
    if not indexed:
        return list(p_values)

    indexed.sort(key=lambda item: item[1])
    total = len(indexed)
    adjusted: dict[int, float] = {}
    previous = 1.0
    for rank in range(total - 1, -1, -1):
        index, p = indexed[rank]
        value = min(previous, p * total / (rank + 1))
        adjusted[index] = value
        previous = value

    return [adjusted.get(i) for i in range(len(p_values))]
