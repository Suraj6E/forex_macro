"""P0.5 questions 1 and 3, answered from an imported MT5 calendar.

planning.md §11 asks two things of the MT5 export that nothing else in the
project can answer:

* **Q1 — how far back does *your* terminal's calendar reach?**  This is
  genuinely per-install: one user reports ~90,000 events back to January 2007,
  another saw only 2017 onward.  It decides where Mode B can start, because a
  release with no forecast cannot carry a surprise.
* **Q3 — is MT5's `forecast_value` point-in-time, or does the terminal update
  it after the release?**  §6.10 records this as `UNKNOWN`, and §4.3 explains
  why it matters: a forecast quietly revised after the fact contaminates every
  surprise computed from it, and the contamination is invisible.

The plan's suggested Q3 method was to capture a live ForexFactory snapshot
before a release and compare it to MT5's stored forecast afterwards.  The
weekly forward capture (§7.3) has been doing exactly that since it started, so
the comparison can be made from data already held: every release carrying
`forecast_point_in_time` has a consensus recorded *before* the release, and
MT5's own forecast for the same release either matches it or it does not.

Nothing here writes.  Run it after importing the CSV:

    manage.py mt5_audit
"""

from collections import defaultdict

from django.core.management.base import BaseCommand

from calendar_data.models import EventRelease, SourceObservation
from sources.models import Source

MT5_KEY = "mt5_calendar"

NOT_IMPORTED = """No MT5 calendar has been imported yet, so neither question can be answered.

To produce the file:
  1. In MetaTrader 5, tick Tools -> Options -> Server -> "Enable news", or the
     terminal's calendar is empty and the export will be too.
  2. Drop mql5/CalendarExport.mq5 into MQL5\\Scripts, compile it (F7), and run
     it on any chart. It writes fxmacro_calendar.csv into MQL5\\Files.
  3. Either upload that CSV from the Sources console, or set `export_dir` in
     the mt5_calendar source's configuration and press Fetch.

Then run this command again."""


class Command(BaseCommand):
    help = "P0.5 Q1 and Q3: how far back the MT5 calendar reaches, and whether its forecast is point-in-time."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tolerance",
            type=float,
            default=0.0,
            help="Treat two forecasts as agreeing if they differ by no more than this. "
            "Default 0 — an exact match, which is what 'point in time' has to mean.",
        )

    def handle(self, *args, **options):
        try:
            source = Source.objects.get(key=MT5_KEY)
        except Source.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"No source registered under {MT5_KEY!r}."))
            return

        observations = SourceObservation.objects.filter(source=source)
        if not observations.exists():
            self.stdout.write(NOT_IMPORTED)
            return

        self._reach(observations)
        self.stdout.write("")
        self._forecast_provenance(observations, options["tolerance"])

    # -- Q1 ---------------------------------------------------------------

    def _reach(self, observations):
        """How far back, and — the part that actually gates Mode B — how far
        back with a forecast attached."""
        release_ids = observations.values_list("event_release_id", flat=True)
        releases = EventRelease.objects.filter(pk__in=release_ids)

        total = releases.count()
        earliest = releases.order_by("release_time_utc").exclude(
            release_time_utc__isnull=True
        ).first()

        self.stdout.write(self.style.MIGRATE_HEADING("Q1 — how far back the MT5 calendar reaches"))
        self.stdout.write(f"{total:,} events imported from the terminal.")
        if earliest is None:
            self.stdout.write(
                self.style.WARNING(
                    "None of them carries a release time, so none can anchor a study."
                )
            )
            return
        self.stdout.write(f"Earliest timestamped event: {earliest.release_time_utc:%Y-%m-%d %H:%M} UTC.")

        by_year = defaultdict(lambda: [0, 0])
        rows = releases.exclude(release_time_utc__isnull=True).values_list(
            "release_time_utc", "forecast_stored"
        )
        for stamp, forecast in rows.iterator():
            bucket = by_year[stamp.year]
            bucket[0] += 1
            if forecast is not None:
                bucket[1] += 1

        self.stdout.write("")
        self.stdout.write(f"{'year':>6}  {'events':>8}  {'with forecast':>14}")
        for year in sorted(by_year):
            events, forecast = by_year[year]
            self.stdout.write(f"{year:>6}  {events:>8,}  {forecast:>14,}")

        with_forecast = [y for y in sorted(by_year) if by_year[y][1] > 0]
        self.stdout.write("")
        if with_forecast:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Mode B can start in {with_forecast[0]} — the first year carrying forecasts. "
                    f"Mode A is unaffected and runs from {min(by_year)}."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "No imported event carries a forecast, so this export cannot start Mode B "
                    "at all. §6.7's modelled expectation is the fallback."
                )
            )

    # -- Q3 ---------------------------------------------------------------

    def _forecast_provenance(self, observations, tolerance):
        """Compare MT5's forecast against a consensus captured *before* the
        release.  A mismatch is the finding: it means the terminal's number
        moved after the fact, and §4.3's look-ahead problem applies to it."""
        self.stdout.write(
            self.style.MIGRATE_HEADING("Q3 — is MT5's forecast point-in-time?")
        )

        anchors = EventRelease.objects.filter(
            forecast_point_in_time__isnull=False
        ).select_related("indicator")
        if not anchors.exists():
            self.stdout.write(
                "Nothing to compare against yet: no release carries a point-in-time "
                "forecast. The weekly forward capture (§7.3) is what produces those, "
                "and it earns one release at a time."
            )
            return

        observed = {
            obs.event_release_id: obs
            for obs in observations.filter(event_release__in=anchors)
        }
        if not observed:
            self.stdout.write(
                f"{anchors.count()} release(s) carry a point-in-time forecast, but the MT5 "
                "import covers none of them. The comparison needs the same release in both, "
                "so it will start working once a forward-captured week is also in the export."
            )
            return

        agree, differ, absent = 0, 0, 0
        for release in anchors:
            obs = observed.get(release.pk)
            if obs is None:
                continue
            mt5_forecast = _forecast_of(obs)
            if mt5_forecast is None:
                absent += 1
                continue
            captured = float(release.forecast_point_in_time)
            if abs(mt5_forecast - captured) <= tolerance:
                agree += 1
            else:
                differ += 1
                self.stdout.write(
                    f"  {release.indicator.currency} {release.indicator.name} "
                    f"{release.release_time_utc:%Y-%m-%d %H:%M}: "
                    f"captured before release {captured:g}, MT5 now says {mt5_forecast:g}"
                )

        compared = agree + differ
        self.stdout.write("")
        self.stdout.write(
            f"{compared} release(s) compared — {agree} agree, {differ} differ"
            + (f", {absent} carry no MT5 forecast" if absent else "")
        )
        if compared == 0:
            self.stdout.write("Not enough overlap to say anything yet.")
        elif differ == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "Every MT5 forecast still matches what was captured before the release. "
                    "Consistent with point-in-time — but this is the weaker half of the "
                    "inference: a forecast that was never going to be revised looks "
                    "identical to one that cannot be. Say `point_in_time` only once the "
                    "sample covers releases whose consensus is known to have moved."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"{differ} of {compared} MT5 forecasts no longer match the consensus "
                    "captured before the release. The terminal updates its forecast after "
                    "the fact, so it is `vendor_stored`, not `point_in_time`, and §4.3's "
                    "look-ahead problem applies to every surprise computed from it."
                )
            )


def _forecast_of(observation) -> float | None:
    """MT5's own forecast for this release, as the normaliser stored it."""
    payload = (observation.raw_json or {}).get("payload") or {}
    for key in ("forecast", "forecast_value"):
        if key in payload and payload[key] not in (None, ""):
            try:
                return float(str(payload[key]).rstrip("%").replace(",", ""))
            except ValueError:
                return None
    return None
