"""Instruments, price coverage, and the candlestick chart.

planning.md §8 names `lightweight-charts` for price-around-event, and §10
screen 4 is what this delivers: price with the release marked on it, so the
question "what did this pair do when that number landed" is answered by
looking rather than by reading a table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.db.models import Count, Max, Min, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render

from calendar_data.models import EventRelease, Indicator
from prices import series
from prices.models import Instrument, MarketEvent, PriceCoverage, Timeframe
from sources.models import Source

PRICE_SOURCE = "dukascopy"

SPANS = [
    ("7", "1W"),
    ("30", "1M"),
    ("90", "3M"),
    ("365", "1Y"),
    ("1825", "5Y"),
    ("", "All"),
]
DEFAULT_SPAN_DAYS = 90


def index(request):
    instruments = Instrument.objects.annotate(
        months=Count("coverage", distinct=True),
        bars=Sum("coverage__bar_count"),
        first=Min("coverage__first_ts_utc"),
        last=Max("coverage__last_ts_utc"),
    )
    totals = PriceCoverage.objects.aggregate(
        months=Count("id"), bars=Sum("bar_count"),
        expected=Sum("expected_bar_count"), gaps=Sum("gap_count"),
    )
    by_timeframe = (
        PriceCoverage.objects.values("timeframe")
        .annotate(n=Count("id"), bars=Sum("bar_count"))
        .order_by("timeframe")
    )
    thin = (
        PriceCoverage.objects.exclude(expected_bar_count=0)
        .select_related("instrument")
        .order_by("bar_count")[:10]
    )
    return render(
        request,
        "prices/index.html",
        {
            "nav": "instruments",
            "instruments": instruments,
            "totals": totals,
            "by_timeframe": by_timeframe,
            "thin": thin,
            "completeness": (
                totals["bars"] / totals["expected"] * 100
                if totals["expected"] else None
            ),
            "market_events": MarketEvent.objects.all(),
        },
    )


def _resolve_window(request, symbol: str, timeframe: str):
    """Window to draw: explicit dates, a span preset, or centred on a release."""
    first, last = series.stored_window(symbol, PRICE_SOURCE, timeframe)
    if not last:
        return None, None, first, last

    # Paging back: the chart asks for the slice immediately before what it
    # already holds, so scrolling left extends history instead of hitting the
    # end of a fixed window.
    before = request.GET.get("before")
    if before:
        # "+00:00" in a query string decodes to a space unless it was encoded.
        # A bare space is never valid in an ISO offset, so restoring it is
        # safe — and without this the parse fails silently and the view
        # returns the same window, which looks like "scrolling does nothing".
        try:
            edge = datetime.fromisoformat(before.strip().replace(" ", "+"))
        except ValueError:
            edge = None
        if edge:
            if edge.tzinfo is None:
                edge = edge.replace(tzinfo=timezone.utc)
            span = request.GET.get("span") or str(DEFAULT_SPAN_DAYS)
            days = int(span) if span.isdigit() else DEFAULT_SPAN_DAYS
            start = max(edge - timedelta(days=days), first) if first else edge - timedelta(days=days)
            return start, edge, first, last

    focus_id = request.GET.get("focus")
    if focus_id and focus_id.isdigit():
        release = EventRelease.objects.filter(
            pk=int(focus_id), release_time_utc__isnull=False
        ).values_list("release_time_utc", flat=True).first()
        if release:
            return (*series.window_for(release), first, last)

    span = request.GET.get("span", str(DEFAULT_SPAN_DAYS))
    if span == "":
        return first, last, first, last
    days = int(span) if span.isdigit() else DEFAULT_SPAN_DAYS
    return last - timedelta(days=days), last, first, last


def chart(request):
    symbol = (request.GET.get("symbol") or "EURUSD").upper()
    instrument = get_object_or_404(Instrument, symbol=symbol)
    timeframe = request.GET.get("tf") or Timeframe.H1

    start, end, first, last = _resolve_window(request, symbol, timeframe)

    selected_ids = _selected_indicators(request)
    selected = list(
        Indicator.objects.filter(pk__in=selected_ids).values_list(
            "id", "currency", "name", "canonical_code"
        )
    )

    # Releases listed under the chart: the ones in view, newest first.
    releases = []
    if selected_ids and start and end:
        releases = list(
            EventRelease.objects.filter(
                indicator_id__in=selected_ids,
                release_time_utc__gte=start,
                release_time_utc__lte=end,
            )
            .select_related("indicator")
            .order_by("-release_time_utc")[:60]
        )

    # The picker filters client-side, so every candidate ships once with the
    # attributes the filters key on. ~300 rows for a pair, not 670.
    available = list(
        series.relevant_indicators(symbol)
        .annotate(n=Count("releases"))
        .order_by("-importance", "-n", "currency", "name")
        .values_list("id", "currency", "name", "n", "canonical_code", "importance")
    )

    return render(
        request,
        "prices/chart.html",
        {
            "nav": "chart",
            "instrument": instrument,
            "instruments": Instrument.objects.filter(enabled=True),
            "timeframe": timeframe,
            "timeframes": [(Timeframe.H1, "1 hour"), (Timeframe.M1, "1 minute")],
            "selected_ids": selected_ids,
            "selected": [
                {
                    "id": i, "currency": c, "name": n,
                    "code": series.short_code(c, n, code),
                }
                for i, c, n, code in selected
            ],
            "available": available,
            "pair_currencies": [instrument.base_ccy, instrument.quote_ccy],
            "impacts": [(3, "High"), (2, "Medium"), (1, "Low"), (0, "Unrated")],
            "max_overlays": series.MAX_OVERLAYS,
            "spans": SPANS,
            "span": request.GET.get("span", str(DEFAULT_SPAN_DAYS)),
            "focus": request.GET.get("focus") or "",
            "start": start,
            "end": end,
            "first": first,
            "last": last,
            "releases": releases,
            "has_data": bool(last),
            "query_base": _query_base(symbol, timeframe, selected_ids),
        },
    )


def _selected_indicators(request) -> list[int]:
    """Indicator ids to overlay, capped so markers stay readable."""
    raw = request.GET.getlist("indicator")
    ids = [int(v) for v in raw if v.isdigit()]
    return ids[: series.MAX_OVERLAYS]


def _query_base(symbol: str, timeframe: str, selected_ids: list[int]) -> str:
    parts = [f"symbol={symbol}", f"tf={timeframe}"]
    parts += [f"indicator={i}" for i in selected_ids]
    return "&".join(parts)


def candles_api(request):
    """Candles plus event markers for the chart, as JSON.

    Kept separate from the page so panning and switching indicator do not
    re-render everything — and so the payload is inspectable on its own.
    """
    symbol = (request.GET.get("symbol") or "EURUSD").upper()
    if not Instrument.objects.filter(symbol=symbol).exists():
        raise Http404(f"Unknown instrument {symbol}")

    timeframe = request.GET.get("tf") or Timeframe.H1
    start, end, first, last = _resolve_window(request, symbol, timeframe)
    if not start or not end:
        return JsonResponse(
            {
                "candles": [], "markers": [], "resolution": timeframe,
                "message": f"No {timeframe} price data stored for {symbol}.",
            }
        )

    rows, resolution = series.candles(symbol, PRICE_SOURCE, timeframe, start, end)
    markers, legend = series.event_markers(_selected_indicators(request), start, end)
    # Must run after aggregation: which bar contains a release depends on the
    # bar size actually drawn, not the one requested.
    markers = series.snap_markers(markers, rows)

    native = dict((k, n) for k, _r, n in series.AGGREGATIONS).get(timeframe, timeframe)
    return JsonResponse(
        {
            "symbol": symbol,
            "resolution": resolution,
            "requested": timeframe,
            "aggregated": resolution != native,
            "candles": rows,
            "markers": markers,
            "legend": legend,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "available_from": first.isoformat() if first else None,
            "available_to": last.isoformat() if last else None,
        }
    )
