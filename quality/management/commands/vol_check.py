"""CLI wrapper around quality.validation.run_vol_check (§4.1, P2).

The Timestamps screen's button and this command run the same function. This
exists for scripting — re-validating after a new price sweep, say — not because
you are expected to use it.
"""

from django.core.management.base import BaseCommand

from calendar_data.models import Indicator
from quality import validation


class Command(BaseCommand):
    help = "Check stored release timestamps against the price series."

    def add_arguments(self, parser):
        parser.add_argument(
            "--importance",
            type=int,
            help="Only this impact tier (3 high, 2 medium, 1 low). Default: all.",
        )
        parser.add_argument("--currency", default="", help="Only this currency.")
        parser.add_argument("--indicator", type=int, help="Only this indicator id.")
        parser.add_argument(
            "--max-indicators",
            type=int,
            help="Stop after this many — useful for a quick look.",
        )

    def handle(self, *args, **options):
        indicator = None
        if options["indicator"]:
            indicator = Indicator.objects.get(pk=options["indicator"])

        result = validation.run_vol_check(
            indicator=indicator,
            importance=options["importance"],
            currency=options["currency"],
            max_indicators=options["max_indicators"],
            log=self.stdout.write,
        )

        wrong = [c for c in result.indicators if c.verdict and c.verdict.wrong]
        for check in wrong:
            self.stdout.write(
                self.style.WARNING(f"{check.label}: {check.verdict.message}")
            )
        self.stdout.write(self.style.SUCCESS(result.summary))
