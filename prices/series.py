"""Candle and marker queries for the price chart.

Reads Parquet through `prices.store`, never the whole archive: a window is
selected by month files and then trimmed, so asking for a week touches one
file rather than 1,651.

planning.md §10 screen 4 — "price chart centred on t0 with windows shaded" —
is what this exists for.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone

import pandas as pd
from django.db.models import Max, Min, Q

from calendar_data.models import EventRelease, Indicator
from prices.models import Instrument, PriceCoverage
from prices.store import read_range

#: Beyond this the browser is drawing marks narrower than a pixel and the JSON
#: gets large for no visible gain, so the bars are aggregated up instead.
MAX_CANDLES = 9000

#: Coarser bars to fall back to, smallest first. Aggregating is honest where
#: dropping candles would not be: a thinned candle series would show gaps that
#: never happened.
AGGREGATIONS = [
    ("h1", "1h", "1 hour"),
    ("h4", "4h", "4 hours"),
    ("d1", "1D", "1 day"),
    ("w1", "1W", "1 week"),
]


def stored_window(symbol: str, source_key: str, timeframe: str) -> tuple:
    """Earliest and latest bar actually held, from the coverage ledger."""
    agg = PriceCoverage.objects.filter(
        instrument__symbol=symbol, source__key=source_key, timeframe=timeframe
    ).aggregate(first=Min("first_ts_utc"), last=Max("last_ts_utc"))
    return agg["first"], agg["last"]


def _resample(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    grouped = frame.set_index("ts_utc").resample(rule)
    out = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
        }
    )
    return out.dropna(subset=["open"]).reset_index()


def candles(
    symbol: str,
    source_key: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> tuple[list[dict], str]:
    """Candles for a window, aggregated up if the range is long.

    Returns (rows, label) where `label` names the bar size actually drawn —
    the chart must say so, because a chart silently showing daily bars when
    you asked for hourly is a chart that lies about its own resolution.
    """
    frame = read_range(symbol, source_key, start, end, timeframe)
    if frame.empty:
        return [], timeframe

    label = next((name for key, _rule, name in AGGREGATIONS if key == timeframe), timeframe)
    if len(frame) > MAX_CANDLES:
        for key, rule, name in AGGREGATIONS:
            if key == timeframe:
                continue
            candidate = _resample(frame, rule)
            if len(candidate) <= MAX_CANDLES:
                frame, label = candidate, name
                break
        else:
            frame, label = _resample(frame, "1W"), "1 week"

    rows = [
        {
            "time": int(pd.Timestamp(row.ts_utc).timestamp()),
            "open": round(float(row.open), 6),
            "high": round(float(row.high), 6),
            "low": round(float(row.low), 6),
            "close": round(float(row.close), 6),
        }
        for row in frame.itertuples(index=False)
    ]
    return rows, label


def relevant_indicators(symbol: str):
    """Indicators whose currency is one of the pair's two legs.

    A US release cannot move USDJPY through the yen leg, and showing every
    indicator for every pair would bury the ones that can.
    """
    instrument = Instrument.objects.filter(symbol=symbol).first()
    if instrument is None:
        return Indicator.objects.none()
    return (
        Indicator.objects.filter(
            Q(currency=instrument.base_ccy) | Q(currency=instrument.quote_ccy)
        )
        .filter(releases__release_time_utc__isnull=False)
        .distinct()
    )


#: More than this on one chart and the markers collide into an unreadable row.
MAX_OVERLAYS = 4

_SKIP_WORDS = {"the", "of", "and", "index", "rate", "change", "final", "flash"}


def _compact_number(value) -> str:
    """7180000 -> 7.18M. `:g` turns it into 7.18e+06, which no one reads."""
    number = float(value)
    magnitude = abs(number)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= limit:
            return f"{number / limit:.2f}".rstrip("0").rstrip(".") + suffix
    return f"{number:g}"


def short_code(currency: str, name: str, canonical: str | None, *, with_currency=True) -> str:
    """A few characters to print on the marker itself.

    Marker identity is carried by this label, not by colour. The candles
    already use the only two well-separated hues (up/down), and validating a
    marker palette alongside them fails: orange against the red down-candle
    measures ΔE 7.1 in normal vision, and violet against the blue up-candle
    ΔE 1.9 under protanopia. A label has no such failure mode.

    Prefers an acronym the name already contains — "ISM" reads, the initials
    "IMP" do not.
    """
    if canonical:
        return canonical[:12]

    words = [w.strip("()-,") for w in name.split()]
    acronym = next(
        (w for w in words if len(w) >= 3 and w.isupper() and w.isalpha()), None
    )
    if acronym:
        code = acronym
    else:
        significant = [w for w in words if w.lower() not in _SKIP_WORDS]
        code = "".join(w[0] for w in significant[:3]).upper() or name[:3].upper()

    return f"{currency} {code}" if with_currency else code


def event_markers(
    indicator_ids: list[int], start: datetime, end: datetime
) -> tuple[list[dict], list[dict]]:
    """Releases of the selected indicators, as markers plus a legend.

    Returns (markers, legend). One query regardless of how many indicators are
    selected.
    """
    indicator_ids = list(indicator_ids)[:MAX_OVERLAYS]
    if not indicator_ids:
        return [], []

    rows = (
        EventRelease.objects.filter(
            indicator_id__in=indicator_ids,
            release_time_utc__gte=start,
            release_time_utc__lte=end,
        )
        .order_by("release_time_utc")
        .values_list(
            "id", "indicator_id", "release_time_utc", "actual_current",
            "forecast_stored", "indicator__currency", "indicator__name",
            "indicator__canonical_code",
        )
    )

    rows = list(rows)
    # The currency prefix is noise when every overlay shares one — on EURUSD
    # with three USD series, "USD" three times says nothing.
    multi_currency = len({r[5] for r in rows}) > 1

    legend: dict[int, dict] = {}
    markers = []
    for release_id, indicator_id, when, actual, forecast, currency, name, canonical in rows:
        beat = None
        if actual is not None and forecast is not None:
            beat = actual > forecast

        code = short_code(currency, name, canonical, with_currency=multi_currency)
        entry = legend.setdefault(
            indicator_id,
            {"id": indicator_id, "code": code, "currency": currency, "name": name, "count": 0},
        )
        entry["count"] += 1

        # Decimal keeps its stored scale, so 22000 would render as
        # "22000.000000" without going through the compact formatter.
        parts = [f"{currency} {name}"]
        if actual is not None:
            parts.append(f"actual {_compact_number(actual)}")
        if forecast is not None:
            parts.append(f"forecast {_compact_number(forecast)}")

        markers.append(
            {
                "id": release_id,
                "indicator_id": indicator_id,
                "code": code,
                "time": int(when.timestamp()),
                "iso": when.isoformat(),
                "beat": beat,
                "actual": None if actual is None else float(actual),
                "forecast": None if forecast is None else float(forecast),
                "text": " · ".join(parts),
            }
        )
    return markers, list(legend.values())


def snap_markers(markers: list[dict], candles: list[dict]) -> list[dict]:
    """Move each marker onto the bar that contains it.

    lightweight-charts only draws a marker whose time equals an existing data
    point's time. A release at 12:30 against hourly bars at 12:00 and 13:00
    matches neither, so the marker silently vanishes — no error, just an empty
    chart that looks like "no events here".

    The exact instant is kept as `release_time` for the tooltip; only the
    drawing position is snapped. Which bar contains it depends on the bar size
    actually rendered, which is why this runs after aggregation rather than
    guessing the timeframe.
    """
    if not markers or not candles:
        return []

    times = [c["time"] for c in candles]
    first, last = times[0], times[-1]

    snapped = []
    for marker in markers:
        moment = marker["time"]
        if moment < first or moment > last:
            continue
        index = bisect_right(times, moment) - 1
        if index < 0:
            continue
        snapped.append({**marker, "release_time": moment, "time": times[index]})

    # Markers must be in ascending time order, and snapping can tie several to
    # one bar — the library expects them sorted regardless.
    snapped.sort(key=lambda m: m["time"])
    return snapped


def window_for(focus: datetime, before_days: int = 5, after_days: int = 5) -> tuple:
    """A window around one release — the §3.2 ladder runs -5d to +1M, so the
    default view brackets the short end of it."""
    return focus - timedelta(days=before_days), focus + timedelta(days=after_days)


def clamp(value: datetime | None, fallback: datetime) -> datetime:
    return value or fallback


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
