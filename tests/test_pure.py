"""Unit tests for the Django-free layer.

Each of these guards a row in the §12 risk table rather than chasing coverage:

* "Timestamp offset wrong for one source — Severe: wrong but plausible output"
* "MT5 server DST shifts across 19 years — High"
* look-ahead contamination from treating a stored forecast as point-in-time

They run without a database because `analytics/`, `collectors/` and
`normalisers/` import nothing from Django (§8).
"""

import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from analytics.horizons import LADDER, POST_HORIZONS, PRE_HORIZONS, seconds_for
from analytics.timeutils import trading_day
from normalisers.base import parse_numeric, unknown_period
from normalisers.forexfactory import parse as ff_parse


class TradingDayTests(unittest.TestCase):
    """§4.1: the trading day is anchored to 17:00 New York, so its UTC
    boundary moves twice a year.  A hardcoded offset silently misaligns
    ~38 stretches of a 19-year sample."""

    def test_summer_boundary_is_2100_utc(self):
        # 20:00Z = 16:00 EDT — still the same trading day.
        self.assertEqual(
            trading_day(datetime(2024, 7, 1, 20, 0, tzinfo=timezone.utc)), date(2024, 7, 1)
        )
        # 21:30Z = 17:30 EDT — past the rollover.
        self.assertEqual(
            trading_day(datetime(2024, 7, 1, 21, 30, tzinfo=timezone.utc)), date(2024, 7, 2)
        )

    def test_winter_boundary_is_2200_utc(self):
        # The same 21:30Z that rolled over in July does *not* roll over in
        # January — this is the whole point of deriving rather than assuming.
        self.assertEqual(
            trading_day(datetime(2024, 1, 2, 21, 30, tzinfo=timezone.utc)), date(2024, 1, 2)
        )
        self.assertEqual(
            trading_day(datetime(2024, 1, 2, 22, 30, tzinfo=timezone.utc)), date(2024, 1, 3)
        )

    def test_friday_rollover_lands_on_monday(self):
        # Friday 17:30 NY opens no session; the week reopens Sunday 17:00 NY.
        self.assertEqual(
            trading_day(datetime(2024, 7, 5, 21, 30, tzinfo=timezone.utc)), date(2024, 7, 8)
        )

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            trading_day(datetime(2024, 7, 1, 20, 0))


class HorizonLadderTests(unittest.TestCase):
    def test_ladder_is_ordered_and_straddles_t0(self):
        offsets = [h.seconds for h in LADDER]
        self.assertEqual(offsets, sorted(offsets))
        self.assertTrue(all(h.is_pre for h in PRE_HORIZONS))
        self.assertTrue(all(not h.is_pre for h in POST_HORIZONS))

    def test_one_day_is_a_fixed_offset_not_a_bar(self):
        # §4.1: `+1d` is t0 + 24h.  Daily bars inherit the rollover ambiguity.
        self.assertEqual(seconds_for("+1d"), 24 * 60 * 60)


class ParseNumericTests(unittest.TestCase):
    def test_suffixes_and_percent(self):
        self.assertEqual(parse_numeric("227K"), Decimal("227000"))
        self.assertEqual(parse_numeric("$52.3B"), Decimal("52300000000.0"))
        self.assertEqual(parse_numeric("1,234"), Decimal("1234"))

    def test_percent_keeps_the_number_shown(self):
        # Rescaling to 0.032 here would make two sources disagree for a reason
        # that is ours, not theirs.  The unit belongs to the indicator.
        self.assertEqual(parse_numeric("3.2%"), Decimal("3.2"))
        self.assertEqual(parse_numeric("-0.1%"), Decimal("-0.1"))

    def test_absence_is_none_never_zero(self):
        for empty in ("", "  ", "-", "--", "N/A", None):
            self.assertIsNone(parse_numeric(empty), empty)

    def test_unparseable_returns_none_rather_than_guessing(self):
        self.assertIsNone(parse_numeric("Tentative"))
        self.assertIsNone(parse_numeric("Bank Holiday"))


class ForexFactoryNormaliserTests(unittest.TestCase):
    """§4.3: capturing a forecast *after* its release and filing it as
    point-in-time is look-ahead contamination — it inflates every downstream
    result and produces clean-looking, wrong output."""

    RELEASE = "2024-09-06T08:30:00-04:00"  # 12:30Z

    def _payload(self, **overrides):
        entry = {
            "title": "Non-Farm Employment Change",
            "country": "USD",
            "date": self.RELEASE,
            "impact": "High",
            "forecast": "165K",
            "previous": "114K",
        }
        entry.update(overrides)
        return json.dumps([entry]).encode("utf-8")

    def test_captured_before_release_is_point_in_time(self):
        rows = ff_parse(
            self._payload(),
            captured_at=datetime(2024, 9, 2, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].forecast_target, "forecast_point_in_time")
        self.assertEqual(rows[0].forecast, Decimal("165000"))
        self.assertEqual(rows[0].previous, Decimal("114000"))

    def test_captured_after_release_is_not_point_in_time(self):
        rows = ff_parse(
            self._payload(),
            captured_at=datetime(2024, 9, 9, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(rows[0].forecast_target, "forecast_stored")

    def test_timestamp_converted_to_utc(self):
        rows = ff_parse(
            self._payload(), captured_at=datetime(2024, 9, 2, tzinfo=timezone.utc)
        )
        self.assertEqual(
            rows[0].scheduled_time_utc, datetime(2024, 9, 6, 12, 30, tzinfo=timezone.utc)
        )
        self.assertIsNone(rows[0].release_time_utc, "the feed schedules, it does not observe")

    def test_naive_timestamp_is_dropped_not_assumed(self):
        rows = ff_parse(
            self._payload(date="2024-09-06T08:30:00"),
            captured_at=datetime(2024, 9, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(rows, [])

    def test_out_of_scope_currency_ignored(self):
        rows = ff_parse(
            self._payload(country="CNY"),
            captured_at=datetime(2024, 9, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(rows, [])

    def test_feed_never_supplies_an_actual(self):
        rows = ff_parse(
            self._payload(actual="142K"),
            captured_at=datetime(2024, 9, 9, tzinfo=timezone.utc),
        )
        self.assertIsNone(rows[0].actual)

    def test_shape_change_raises_rather_than_returning_nothing(self):
        with self.assertRaises(ValueError):
            ff_parse(b'{"events": []}', captured_at=datetime(2024, 9, 2, tzinfo=timezone.utc))

    def test_same_day_repeats_get_distinct_identities(self):
        rows = ff_parse(
            json.dumps(
                [
                    {"title": "ECB President Speaks", "country": "EUR",
                     "date": "2024-09-06T08:00:00-04:00", "impact": "High"},
                    {"title": "ECB President Speaks", "country": "EUR",
                     "date": "2024-09-06T13:00:00-04:00", "impact": "High"},
                ]
            ).encode("utf-8"),
            captured_at=datetime(2024, 9, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(len({r.reference_period for r in rows}), 2)


class UnknownPeriodTests(unittest.TestCase):
    def test_carries_the_minute_so_same_day_repeats_survive(self):
        a = unknown_period(datetime(2024, 9, 6, 12, 0, tzinfo=timezone.utc))
        b = unknown_period(datetime(2024, 9, 6, 17, 0, tzinfo=timezone.utc))
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("release:"))
