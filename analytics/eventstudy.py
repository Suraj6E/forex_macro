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
from dataclasses import dataclass, field
from datetime import datetime, timedelta

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
    index = bars["ts_utc"].searchsorted(moment, side="right") - 1
    if index < 0 or index >= len(bars):
        return None
    return float(bars["open"].iloc[index])


def price_at(bars: pd.DataFrame, moment: datetime) -> float | None:
    """Close of the bar containing `moment`."""
    index = bars["ts_utc"].searchsorted(moment, side="right") - 1
    if index < 0 or index >= len(bars):
        return None
    return float(bars["close"].iloc[index])


def _slice(bars: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    mask = (bars["ts_utc"] >= start) & (bars["ts_utc"] <= end)
    return bars.loc[mask]


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
    window = _slice(bars, start, end)

    # Against *open* hours, not calendar hours — see open_seconds_between.
    open_seconds = open_seconds_between(start, end)
    result.bars_expected = max(int(open_seconds // bar_seconds), 1)
    result.n_bars = len(window)

    ret = raw_return(bars, t0, horizon)
    if ret is None or result.n_bars < result.bars_expected * MIN_BAR_COVERAGE:
        return result

    result.ret = ret
    result.abs_ret = abs(ret)

    if len(window) > 1:
        steps = np.log(window["close"].to_numpy() / window["open"].to_numpy())
        steps = steps[np.isfinite(steps)]
        if steps.size:
            result.realized_vol = float(np.sqrt(np.sum(steps ** 2)))

    anchor = price_before(bars, t0)
    if anchor and len(window):
        highs = window["high"].to_numpy()
        lows = window["low"].to_numpy()
        result.mfe = float(np.log(np.max(highs) / anchor))
        result.mae = float(np.log(np.min(lows) / anchor))

    return result


def matched_baseline(
    bars: pd.DataFrame, t0: datetime, horizon: Horizon, weeks: int = BASELINE_WEEKS
) -> tuple[float | None, float | None, float | None, int]:
    """What this pair normally does at this weekday and hour.

    §6.1 specifies matched weekday-and-hour periods over the prior N weeks. The
    match matters: FX has a strong intraday and intraweek shape, so comparing a
    Friday 12:30 move against an all-hours average would attribute the ordinary
    London-New York overlap to the release.
    """
    samples = []
    for week in range(1, weeks + 1):
        moment = t0 - timedelta(weeks=week)
        value = raw_return(bars, moment, horizon)
        if value is not None and math.isfinite(value):
            samples.append(value)

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


def measure_event(
    bars: pd.DataFrame,
    t0: datetime,
    horizons=None,
    *,
    baseline_weeks: int = BASELINE_WEEKS,
) -> list[WindowMeasurement]:
    """One release against one instrument, across the whole ladder."""
    bars = _price_frame(bars)
    if bars.empty:
        return []

    bar_seconds = _bar_seconds(bars)
    rungs = horizons if horizons is not None else supported_horizons(bar_seconds)

    out = []
    for horizon in rungs:
        measurement = measure_window(bars, t0, horizon, bar_seconds)
        if measurement.usable:
            mean, sd, abs_mean, n = matched_baseline(bars, t0, horizon, baseline_weeks)
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

            ratios = np.array(
                [m.abs_ratio for m in group if m.abs_ratio is not None], dtype=float
            )
            if ratios.size:
                point.abs_ratio = float(np.median(ratios))
        points.append(point)
    return points


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
