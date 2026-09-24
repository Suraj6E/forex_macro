"""Running an event study — planning.md §6.1, §6.2, P1.

The vertical slice: one indicator against one instrument, every release in
history, measured across the horizon ladder and pooled into a decay curve.

`analytics/eventstudy.py` does the arithmetic and imports nothing from Django;
this module loads the bars, walks the releases, and stores the results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from analytics import eventstudy
from analytics.horizons import LADDER, WindowScheme
from calendar_data.models import EventRelease, Indicator
from prices.models import (
    DEFAULT_PRICE_SOURCE,
    TIMEFRAME_SECONDS,
    Instrument,
    PriceCoverage,
)
from prices.store import read_range
from studies.models import (
    AttributionConfidence,
    DecayCurve,
    EventImpact,
    Mode,
    OutlierPolicy,
)

logger = logging.getLogger(__name__)

#: Bumped whenever the measurement changes, so a methodology change
#: invalidates the cache instead of mixing generations (§9).
#:
#: es-2 — the matched baseline now skips look-back windows containing another
#: release of the same indicator. Without it a weekly release is compared
#: against itself and its measured effect collapses to ~1× by construction.
#:
#: es-3 — three corrections from the September 2026 review (report.md §3.3):
#: the pooled ratio is mean over mean, so 1.0 means normal (it read ~0.70 for
#: nothing at +1h); a contaminated baseline week is replaced by the same hour a
#: day or two away instead of skipped, so weekly series keep their baseline at
#: every horizon; and rows are flagged as outliers (§4.6) rather than never.
ENGINE_VERSION = "es-3"

PRICE_SOURCE = DEFAULT_PRICE_SOURCE

#: Below this, §6.6's minimum-n gate applies: no coefficient is reported.
MIN_N = 20


@dataclass
class StudyResult:
    indicator_id: int
    instrument_id: int
    timeframe: str
    events_total: int = 0
    events_measured: int = 0
    impacts_written: int = 0
    curve_points: int = 0
    skipped_no_price: int = 0
    horizons: list = None
    notes: str = ""

    def __post_init__(self):
        self.horizons = self.horizons or []


def _attribution(seconds: int) -> str:
    """§6.6's badge: high at minutes, degrading to low at weeks.

    The reasoning is §3.2's: the event's effect is roughly fixed while noise
    grows with the square root of time, so the share of variance it can explain
    falls as the window lengthens.
    """
    hours = abs(seconds) / 3600
    if hours <= 4:
        return AttributionConfidence.HIGH
    if hours <= 72:
        return AttributionConfidence.MEDIUM
    return AttributionConfidence.LOW


def available_timeframe(instrument: Instrument) -> str | None:
    """Finest bar size actually stored for this instrument."""
    stored = set(
        PriceCoverage.objects.filter(
            instrument=instrument, source__key=PRICE_SOURCE
        ).values_list("timeframe", flat=True)
    )
    for timeframe in ("m1", "m15", "h1", "d1"):
        if timeframe in stored:
            return timeframe
    return None


def run_study(
    indicator: Indicator,
    instrument: Instrument,
    *,
    timeframe: str | None = None,
    limit: int | None = None,
    log=lambda msg: None,
    progress=lambda frac, msg: None,
) -> StudyResult:
    """Measure every release of `indicator` against `instrument`."""
    timeframe = timeframe or available_timeframe(instrument)
    if timeframe is None:
        raise RuntimeError(
            f"No price data stored for {instrument.symbol}. Collect it first."
        )

    bar_seconds = TIMEFRAME_SECONDS.get(timeframe, 3600)
    horizons = eventstudy.supported_horizons(bar_seconds)
    result = StudyResult(
        indicator_id=indicator.pk,
        instrument_id=instrument.pk,
        timeframe=timeframe,
        horizons=[h.label for h in horizons],
    )

    releases = list(
        EventRelease.objects.filter(
            indicator=indicator, release_time_utc__isnull=False
        ).order_by("release_time_utc")
    )
    if limit:
        releases = releases[-limit:]
    result.events_total = len(releases)
    if not releases:
        result.notes = "This indicator has no timestamped releases to measure."
        return result

    # One read covering every window, rather than a Parquet read per release.
    # The baseline can walk up to four times its twelve weeks back, and the
    # ladder reaches a month forward.
    span_before = timedelta(weeks=eventstudy.BASELINE_WEEKS * 4 + 1)
    span_after = timedelta(seconds=max(h.seconds for h in horizons) + 86400)
    bars = read_range(
        instrument.symbol,
        PRICE_SOURCE,
        releases[0].release_time_utc - span_before,
        releases[-1].release_time_utc + span_after,
        timeframe,
    )
    bars = eventstudy.prepare_bars(bars)
    log(f"{len(bars):,} {timeframe} bars loaded for {instrument.symbol}")
    if bars.empty:
        result.notes = f"No {timeframe} bars cover these releases."
        return result

    # Every release of this indicator, so the baseline never looks back at the
    # event it is supposed to be a control for.
    own_releases = [r.release_time_utc for r in releases]

    measurements_by_event = []
    measured_releases = []
    rows: list[EventImpact] = []

    for index, release in enumerate(releases, start=1):
        per_event = eventstudy.measure_event(
            bars, release.release_time_utc, horizons, exclude=own_releases
        )
        usable = [m for m in per_event if m.usable]
        if not usable:
            result.skipped_no_price += 1
            continue

        result.events_measured += 1
        measurements_by_event.append(per_event)
        measured_releases.append(release)

        if index % 20 == 0 or index == len(releases):
            progress(index / len(releases), f"{index}/{len(releases)} releases")

    flags = _outlier_flags(instrument, measured_releases, measurements_by_event, bar_seconds)
    price_source_id = _price_source_id()

    for release, per_event in zip(measured_releases, measurements_by_event):
        for measurement in per_event:
            if not measurement.usable:
                continue
            reason = flags.get((release.pk, measurement.horizon), "")
            rows.append(
                EventImpact(
                    event_release=release,
                    instrument=instrument,
                    price_source_id=price_source_id,
                    horizon=measurement.horizon,
                    window_scheme=WindowScheme.FIXED,
                    window_seconds=measurement.seconds,
                    ret=measurement.ret,
                    abs_ret=measurement.abs_ret,
                    abnormal_ret=measurement.abnormal_ret,
                    realized_vol=measurement.realized_vol,
                    mfe=measurement.mfe,
                    mae=measurement.mae,
                    baseline_abs_mean=measurement.baseline_abs_mean,
                    baseline_n=measurement.baseline_n,
                    n_bars=measurement.n_bars,
                    bars_missing=max(measurement.bars_expected - measurement.n_bars, 0),
                    is_outlier=bool(reason),
                    outlier_reason=reason,
                    engine_version=ENGINE_VERSION,
                )
            )

    with transaction.atomic():
        EventImpact.objects.filter(
            event_release__indicator=indicator,
            instrument=instrument,
            engine_version=ENGINE_VERSION,
        ).delete()
        EventImpact.objects.bulk_create(rows, batch_size=2000)
        result.impacts_written = len(rows)

        outliers_by_horizon: dict[str, int] = {}
        for _release_id, horizon in flags:
            outliers_by_horizon[horizon] = outliers_by_horizon.get(horizon, 0) + 1
        result.curve_points = _write_curve(
            indicator, instrument, measurements_by_event, outliers_by_horizon
        )

    result.notes = (
        f"{result.events_measured} of {result.events_total} releases measured "
        f"on {timeframe} bars across {len(horizons)} horizons."
    )
    log(result.notes)
    return result


def _price_source_id() -> int:
    from sources.models import Source

    return Source.objects.only("id").get(key=PRICE_SOURCE).pk


def _outlier_flags(instrument, releases, measurements_by_event, bar_seconds) -> dict:
    """§4.6's default policy, `flag`: {(release id, horizon): reason}.

    Two independent reasons; the first one found is recorded:

    - a registered instant `MarketEvent` (the SNB floor, a flash crash) falls
      inside the window, a known shock that is not this release;
    - the abnormal move is more than `OUTLIER_Z` robust sigmas from the
      pairing's median at that horizon.

    Crisis *periods* are not flags. They are regimes: a release is not an
    outlier for having happened during COVID.
    """
    from prices.models import MarketEvent

    shocks = []
    for event in MarketEvent.objects.filter(end_ts_utc__isnull=True).prefetch_related(
        "instruments"
    ):
        applies_to = {i.pk for i in event.instruments.all()}
        if not applies_to or instrument.pk in applies_to:
            shocks.append((event.ts_utc, event.label))

    flags: dict[tuple[int, str], str] = {}
    by_horizon: dict[str, list[tuple[int, float]]] = {}
    for release, per_event in zip(releases, measurements_by_event):
        t0 = release.release_time_utc
        for m in per_event:
            if not m.usable:
                continue
            by_horizon.setdefault(m.horizon, []).append((release.pk, m.abnormal_ret))
            start = min(t0, t0 + timedelta(seconds=m.seconds))
            end = max(t0, t0 + timedelta(seconds=m.seconds))
            # Widened to whole bars: the window is read from the bar holding
            # its start to the bar holding its end.
            start -= timedelta(seconds=start.timestamp() % bar_seconds)
            end += timedelta(seconds=bar_seconds - end.timestamp() % bar_seconds)
            for when, label in shocks:
                if start <= when < end:
                    flags[(release.pk, m.horizon)] = f"window contains: {label}"
                    break

    for horizon, members in by_horizon.items():
        mask = eventstudy.outlier_mask([value for _pk, value in members])
        for (pk, _value), flagged in zip(members, mask):
            if flagged and (pk, horizon) not in flags:
                flags[(pk, horizon)] = (
                    f"abnormal move over {eventstudy.OUTLIER_Z:g} robust sigmas "
                    "from the pairing's median"
                )
    return flags


def _write_curve(indicator, instrument, measurements_by_event, outliers_by_horizon) -> int:
    """Pool the per-event measurements into the §6.2 decay curve."""
    points = eventstudy.pool(measurements_by_event)
    if not points:
        return 0

    # FDR is applied to the Mode A test, which is the one being read.
    adjusted = eventstudy.benjamini_hochberg([p.abs_p_value for p in points])

    DecayCurve.objects.filter(
        indicator=indicator, instrument=instrument, engine_version=ENGINE_VERSION
    ).delete()

    rows = []
    for point, p_fdr in zip(points, adjusted):
        # §6.6's minimum-n gate: below twenty observations no coefficient is
        # reported at all — the raw scatter is the honest output instead.
        gated = point.n < MIN_N
        rows.append(
            DecayCurve(
                indicator=indicator,
                instrument=instrument,
                mode=Mode.A,
                horizon=point.horizon,
                # Mode A stores the excess absolute move: the signed mean of a
                # symmetric event is ~0 however hard price reacted, so storing
                # that as "the effect" would report every event as inert.
                effect_size=None if gated else point.mean_abs_excess,
                std_error=None if gated else point.abs_std_error,
                abs_ratio=None if gated else point.abs_ratio,
                baseline_n=point.baseline_n,
                n_outliers=outliers_by_horizon.get(point.horizon, 0),
                outlier_policy=OutlierPolicy.FLAG,
                p_raw=None if gated else point.abs_p_value,
                p_fdr=None if gated else p_fdr,
                detectability_floor=point.detectability_floor,
                n=point.n,
                attribution_confidence=_attribution(point.seconds),
                engine_version=ENGINE_VERSION,
            )
        )
    DecayCurve.objects.bulk_create(rows)
    return len(rows)


def pip_value(instrument: Instrument) -> float:
    """A pip as a fraction of price, so log returns can be shown in pips.

    Uses a representative price for the pair rather than the price at each
    release. The approximation is worth stating: EUR/USD has ranged roughly
    1.03–1.60 since 2007, so a pip figure quoted this way can be out by up to
    about a fifth at the extremes. It is still far more readable than a bare
    log return, and the ratio column carries the unit-free comparison.
    """
    reference = (
        PriceCoverage.objects.filter(instrument=instrument, source__key=PRICE_SOURCE)
        .exclude(last_ts_utc=None)
        .order_by("-month")
        .first()
    )
    price = 1.0
    if reference and reference.parquet_path:
        from prices.store import read_month

        frame = read_month(
            instrument.symbol, PRICE_SOURCE, reference.month, reference.timeframe
        )
        if not frame.empty:
            price = float(frame["close"].median())
    return float(instrument.pip_size) / price if price else float(instrument.pip_size)


def summarise_curve(rows: list[DecayCurve], pip: float) -> dict:
    """The one-sentence answer, so the page leads with meaning not a table."""
    post = [r for r in rows if not r.horizon.startswith("-")]
    pre = [r for r in rows if r.horizon.startswith("-")]

    def is_sig(row):
        return row.p_fdr is not None and row.p_fdr < 0.05

    sig_post = [r for r in post if is_sig(r)]
    peak = max(
        (r for r in post if r.effect_size is not None),
        key=lambda r: r.effect_size,
        default=None,
    )
    # A pre-release horizon where the market goes measurably *quiet* is the
    # liquidity-withdrawal signature of §3.4 — worth naming, not burying.
    quiet = [r for r in pre if is_sig(r) and r.effect_size is not None and r.effect_size < 0]

    return {
        "n": max((r.n for r in rows), default=0),
        "peak": peak,
        "peak_pips": None if peak is None or peak.effect_size is None
                     else peak.effect_size / pip,
        "peak_ratio": None if peak is None else peak.abs_ratio,
        "last_significant": sig_post[-1].horizon if sig_post else None,
        "first_insignificant": next(
            (r.horizon for r in post if not is_sig(r)
             and sig_post and r.horizon != sig_post[-1]
             and post.index(r) > post.index(sig_post[-1])),
            None,
        ),
        "quiet_before": quiet[-1].horizon if quiet else None,
        "quiet_ratio": quiet[-1].abs_ratio if quiet else None,
        "any_effect": bool(sig_post),
    }


def event_scatter(indicator_id: int, instrument_id: int, horizon: str, pip: float):
    """Per-release absolute move at one horizon, plus the normal level.

    An average hides whether an effect is steady or driven by a handful of
    crises; this is the raw spread behind it.
    """
    rows = list(
        EventImpact.objects.filter(
            event_release__indicator_id=indicator_id,
            instrument_id=instrument_id,
            horizon=horizon,
            engine_version=ENGINE_VERSION,
            abs_ret__isnull=False,
        )
        .order_by("event_release__release_time_utc")
        .values_list("event_release__release_time_utc", "abs_ret", "is_outlier")
    )
    # The flag rides along so the chart can draw flagged releases hollow.
    scatter = [(when, abs_ret / pip, flagged) for when, abs_ret, flagged in rows]

    normal = None
    curve = DecayCurve.objects.filter(
        indicator_id=indicator_id, instrument_id=instrument_id,
        horizon=horizon, engine_version=ENGINE_VERSION,
    ).first()
    if curve and curve.effect_size is not None and scatter:
        mean_abs = sum(v for _w, v, _f in scatter) / len(scatter)
        normal = mean_abs - curve.effect_size / pip
    return scatter, normal


def curve_for(indicator_id: int, instrument_id: int) -> list[DecayCurve]:
    """The newest curve stored for this pairing, whatever engine made it.

    Filtering strictly on the current version would make every previously
    measured study disappear the moment the method changes — silently, which
    is worse than showing an older result and labelling it. The page reports
    which engine produced the curve and flags it when it is not the current
    one; §9's cache-invalidation intent is served by the label, not by hiding
    the work.
    """
    from analytics.horizons import BY_LABEL

    stored = DecayCurve.objects.filter(
        indicator_id=indicator_id, instrument_id=instrument_id
    )
    versions = set(stored.values_list("engine_version", flat=True))
    if not versions:
        return []
    chosen = ENGINE_VERSION if ENGINE_VERSION in versions else max(versions)

    rows = list(stored.filter(engine_version=chosen))
    return sorted(
        rows, key=lambda r: BY_LABEL[r.horizon].seconds if r.horizon in BY_LABEL else 0
    )
