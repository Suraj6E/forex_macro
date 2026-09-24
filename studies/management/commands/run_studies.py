"""Re-measure event studies at the current engine version.

With no arguments it re-runs every pairing that has a stored curve at an
*older* engine version and none at the current one — which is what an engine
bump leaves behind. Pairings already current are skipped, so an interrupted
sweep resumes where it stopped. Run `run_direction` afterwards: the direction
fit reads this command's output.
"""

import time

from django.core.management.base import BaseCommand

from calendar_data.models import Indicator
from prices.models import Instrument
from studies.engine import ENGINE_VERSION, run_study
from studies.models import DecayCurve, Mode


class Command(BaseCommand):
    help = "Re-run the event study for every pairing measured by an older engine."

    def add_arguments(self, parser):
        parser.add_argument("--indicator", type=int, help="Only this indicator id.")
        parser.add_argument("--instrument", type=int, help="Only this instrument id.")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Also re-run pairings already measured at the current engine.",
        )

    def handle(self, *args, **options):
        curves = DecayCurve.objects.filter(mode=Mode.A, indicator__isnull=False)
        if options["indicator"]:
            curves = curves.filter(indicator_id=options["indicator"])
        if options["instrument"]:
            curves = curves.filter(instrument_id=options["instrument"])

        measured = set(curves.values_list("indicator_id", "instrument_id").distinct())
        current = set(
            curves.filter(engine_version=ENGINE_VERSION)
            .values_list("indicator_id", "instrument_id")
            .distinct()
        )
        pairings = sorted(measured if options["force"] else measured - current)
        if not pairings:
            self.stdout.write(f"Every measured pairing is already at {ENGINE_VERSION}.")
            return

        indicators = Indicator.objects.in_bulk([p[0] for p in pairings])
        instruments = Instrument.objects.in_bulk([p[1] for p in pairings])

        self.stdout.write(f"{len(pairings)} pairing(s) to measure at {ENGINE_VERSION}.")
        started = time.monotonic()
        for index, (indicator_id, instrument_id) in enumerate(pairings, start=1):
            indicator = indicators[indicator_id]
            instrument = instruments[instrument_id]
            result = run_study(indicator, instrument)
            self.stdout.write(
                f"  [{index}/{len(pairings)}] {indicator.currency} {indicator.name} "
                f"-> {instrument.symbol}: {result.notes}"
            )

        minutes = (time.monotonic() - started) / 60
        self.stdout.write(
            self.style.SUCCESS(f"{len(pairings)} pairing(s) measured in {minutes:.1f} min.")
        )
