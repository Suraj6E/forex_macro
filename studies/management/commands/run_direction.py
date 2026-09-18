"""Fit the direction curve for every pairing the event study has measured.

The fit reads `EventImpact` rather than price, so this is seconds of work per
pairing and it only makes sense where a Mode A study has already run.  With no
arguments it sweeps every pairing that has stored impacts at the current engine
version, which is the normal way to use it: run the event studies, then run
this once.
"""

from django.core.management.base import BaseCommand

from calendar_data.models import Indicator
from prices.models import Instrument
from studies.directions import run_direction
from studies.engine import ENGINE_VERSION
from studies.models import EventImpact


class Command(BaseCommand):
    help = "Regress the signed move on the change vs previous, per horizon."

    def add_arguments(self, parser):
        parser.add_argument("--indicator", type=int, help="Only this indicator id.")
        parser.add_argument("--instrument", type=int, help="Only this instrument id.")
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Only print pairings that produced a coefficient.",
        )

    def handle(self, *args, **options):
        pairings = (
            EventImpact.objects.filter(engine_version=ENGINE_VERSION)
            .values_list("event_release__indicator_id", "instrument_id")
            .distinct()
        )
        if options["indicator"]:
            pairings = pairings.filter(event_release__indicator_id=options["indicator"])
        if options["instrument"]:
            pairings = pairings.filter(instrument_id=options["instrument"])

        pairings = sorted(set(pairings))
        if not pairings:
            self.stdout.write(
                "No stored impacts at engine "
                f"{ENGINE_VERSION}. Run the event study first — this fit reads its "
                "output rather than re-measuring price."
            )
            return

        indicators = Indicator.objects.in_bulk([p[0] for p in pairings])
        instruments = Instrument.objects.in_bulk([p[1] for p in pairings])

        self.stdout.write(f"{len(pairings)} pairing(s) with stored impacts.")
        found = 0
        for indicator_id, instrument_id in pairings:
            indicator = indicators[indicator_id]
            instrument = instruments[instrument_id]
            result = run_direction(indicator, instrument)
            if result.curve_points:
                found += 1
            label = f"{indicator.currency} {indicator.name} -> {instrument.symbol}"
            if options["quiet"] and "Strongest response" not in result.notes:
                continue
            self.stdout.write(f"  {label}")
            self.stdout.write(f"    {result.notes}")

        self.stdout.write(
            self.style.SUCCESS(f"{found} pairing(s) produced a direction curve.")
        )
