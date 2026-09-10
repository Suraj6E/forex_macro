"""Instruments and price coverage — planning.md §4.5, §4.7.

Nothing is loaded yet, and the screen says so rather than showing an empty
chart. The coverage ledger exists because long horizons need unbroken bars
across weekends and holidays, which is a different failure mode from the short
ones.
"""

from __future__ import annotations

from django.db.models import Count, Sum
from django.shortcuts import render

from prices.models import Instrument, MarketEvent, PriceCoverage


def index(request):
    instruments = Instrument.objects.annotate(
        months=Count("coverage", distinct=True),
        bars=Sum("coverage__bar_count"),
    )
    totals = PriceCoverage.objects.aggregate(
        months=Count("id"), bars=Sum("bar_count"), gaps=Sum("gap_count")
    )
    return render(
        request,
        "prices/index.html",
        {
            "nav": "instruments",
            "instruments": instruments,
            "totals": totals,
            "coverage": PriceCoverage.objects.select_related("instrument", "source")[:50],
            "market_events": MarketEvent.objects.all(),
        },
    )
