"""Bring the calendar up to date — sources/calendar_refresh.py.

    python manage.py refresh_calendar          # whatever is due
    python manage.py refresh_calendar --json   # and a JSON summary as the last line

The replay simulator runs this in the background while it is open. It is
also safe to run by hand or from Task Scheduler: it fetches only what is due
and never more often than the sources' policies allow.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from sources.calendar_refresh import refresh


class Command(BaseCommand):
    help = "Fetch the calendar's new actuals and the schedule ahead, as far as due."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print a JSON summary as the last line.")
        parser.add_argument("--force", action="store_true",
                            help="Fetch the pages even if they were fetched in the last ten minutes.")

    def handle(self, *args, **options):
        outcome = refresh(force=options["force"], log=self.stdout.write)
        if options["json"]:
            self.stdout.write(json.dumps(outcome.as_dict()))
        if outcome.errors:
            raise CommandError("; ".join(outcome.errors))
