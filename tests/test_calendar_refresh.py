"""The calendar refresh (sources/calendar_refresh.py): what it fetches when,
and which events a windowed fetch folds in. No Django, no network."""

import unittest
from datetime import date, datetime, timedelta, timezone

from collectors.forexfactory_pages import in_window
from normalisers.base import CanonicalEvent
from sources.calendar_refresh import MIN_GAP, add_months, plan_pages

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)


class Plan(unittest.TestCase):
    def test_a_long_gap_is_filled_from_the_last_full_fetch_to_next_month(self):
        # The state found on 18 Nov 2026: last fetched by hand on 22 Sep.
        last = datetime(2026, 9, 22, 16, 13, tzinfo=timezone.utc)
        p = plan_pages(NOW, last, last)
        self.assertEqual(p.mode, "full")
        self.assertEqual((p.date_from, p.date_to), (date(2026, 9, 1), date(2026, 12, 1)))
        self.assertEqual(p.rows_from, last - timedelta(days=2))
        self.assertIsNone(p.rows_to)
        self.assertNotIn("rows_to", p.params())  # what marks a full fetch

    def test_never_fetched_starts_this_month(self):
        p = plan_pages(NOW, None, None)
        self.assertEqual((p.mode, p.date_from, p.date_to, p.rows_from), ("full", date(2026, 11, 1), date(2026, 12, 1), None))

    def test_within_the_day_only_the_events_around_today(self):
        p = plan_pages(NOW, NOW - timedelta(hours=5), NOW - timedelta(hours=1))
        self.assertEqual(p.mode, "recent")
        self.assertEqual((p.date_from, p.date_to), (date(2026, 11, 1), date(2026, 11, 1)))
        self.assertEqual((p.rows_from, p.rows_to), (NOW - timedelta(days=1), NOW + timedelta(days=8)))

    def test_across_a_month_end_both_pages(self):
        now = datetime(2026, 11, 28, 9, 0, tzinfo=timezone.utc)
        p = plan_pages(now, now - timedelta(hours=5), now - timedelta(hours=1))
        self.assertEqual((p.date_from, p.date_to), (date(2026, 11, 1), date(2026, 12, 1)))

    def test_never_twice_in_ten_minutes_unless_forced(self):
        recent = NOW - MIN_GAP + timedelta(minutes=1)
        self.assertEqual(plan_pages(NOW, recent, recent).mode, "skip")
        self.assertEqual(plan_pages(NOW, NOW - timedelta(hours=5), recent, force=True).mode, "recent")

    def test_months(self):
        self.assertEqual(add_months(date(2026, 12, 1), 1), date(2027, 1, 1))
        self.assertEqual(add_months(date(2026, 1, 1), -1), date(2025, 12, 1))


class Window(unittest.TestCase):
    def row(self, t, scheduled=False):
        return CanonicalEvent(source_key="forexfactory_pages", currency="USD", source_event_key="ff:1",
                              source_event_name="x", reference_period="r",
                              **({"scheduled_time_utc": t} if scheduled else {"release_time_utc": t}))

    def test_only_the_events_inside_it(self):
        a, b = NOW - timedelta(days=1), NOW + timedelta(days=8)
        self.assertTrue(in_window(self.row(NOW), a, b))
        self.assertTrue(in_window(self.row(a), a, b))
        self.assertFalse(in_window(self.row(a - timedelta(minutes=1)), a, b))
        self.assertFalse(in_window(self.row(b + timedelta(minutes=1), scheduled=True), a, b))
        self.assertTrue(in_window(self.row(NOW - timedelta(days=400)), None, None))  # no window: everything


if __name__ == "__main__":
    unittest.main()
