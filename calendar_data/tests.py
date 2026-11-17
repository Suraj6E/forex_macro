"""The weekly feed and the calendar pages name the same events
(consolidation.merge.SAME_TITLES): one release each, never two.

Run: python manage.py test calendar_data
"""

from datetime import datetime, timezone
from decimal import Decimal

from django.test import TestCase

from calendar_data.models import EventRelease, Indicator, IndicatorAlias, SourceObservation
from consolidation.merge import apply_observation, merge_title_twins
from normalisers.base import CanonicalEvent
from sources.models import FetchRun, Source

AT = datetime(2026, 11, 18, 7, 0, tzinfo=timezone.utc)
PERIOD = "release:2026-11-18T07:00"


def pages_row(**kw):
    """UK CPI as the pages carry it: the series id, the actual, the
    displayed forecast."""
    return CanonicalEvent(source_key="forexfactory_pages", currency="GBP", source_event_key="ff:180",
                          source_event_name="CPI y/y", reference_period=PERIOD, release_time_utc=AT,
                          timestamp_confidence="minute", importance_source=3, **kw)


def weekly_row(**kw):
    """The same release as the weekly feed carries it: keyed by its title,
    with the forecast held before the release."""
    return CanonicalEvent(source_key="forexfactory_weekly", currency="GBP", source_event_key="GBP|CPI y/y",
                          source_event_name="CPI y/y", reference_period=PERIOD, scheduled_time_utc=AT,
                          timestamp_confidence="minute", importance_source=3,
                          forecast_target="forecast_point_in_time", **kw)


class SameTitles(TestCase):
    def setUp(self):
        self.pages = Source.objects.create(key="forexfactory_pages", name="pages", kind="calendar")
        self.weekly = Source.objects.create(key="forexfactory_weekly", name="weekly", kind="calendar")
        self.run_p = FetchRun.objects.create(source=self.pages)
        self.run_w = FetchRun.objects.create(source=self.weekly)

    def test_the_weekly_feed_files_its_title_under_the_pages_series(self):
        apply_observation(self.pages, self.run_p, pages_row(actual=Decimal("3.8"), forecast=Decimal("3.6")))
        outcome = apply_observation(self.weekly, self.run_w, weekly_row(forecast=Decimal("3.7")))
        self.assertFalse(outcome.created)
        self.assertFalse(outcome.newly_unmapped)
        self.assertEqual(EventRelease.objects.count(), 1)
        rel = EventRelease.objects.get()
        self.assertEqual((rel.actual_current, rel.forecast_point_in_time, rel.forecast_stored),
                         (Decimal("3.8"), Decimal("3.7"), Decimal("3.6")))

    def test_and_the_other_way_round(self):
        apply_observation(self.weekly, self.run_w, weekly_row(forecast=Decimal("3.7")))
        apply_observation(self.pages, self.run_p, pages_row(actual=Decimal("3.8")))
        self.assertEqual(EventRelease.objects.count(), 1)
        self.assertEqual(Indicator.objects.count(), 1)

    def test_a_different_title_or_currency_is_a_different_series(self):
        apply_observation(self.pages, self.run_p, pages_row())
        other = weekly_row()
        other.source_event_key, other.source_event_name = "GBP|Core CPI y/y", "Core CPI y/y"
        apply_observation(self.weekly, self.run_w, other)
        self.assertEqual(Indicator.objects.count(), 2)

    def test_duplicates_already_stored_are_folded_into_one(self):
        # As the merge used to leave them: the weekly alias on its own indicator.
        apply_observation(self.pages, self.run_p, pages_row(actual=Decimal("3.8"), forecast=Decimal("3.6")))
        dup = Indicator.objects.create(currency="GBP", name="CPI y/y", importance=3)
        IndicatorAlias.objects.create(source=self.weekly, source_key="GBP|CPI y/y", source_name="CPI y/y",
                                      currency="GBP", indicator=dup)
        old = EventRelease.objects.create(indicator=dup, reference_period=PERIOD, scheduled_time_utc=AT,
                                          forecast_point_in_time=Decimal("3.7"), timestamp_confidence="minute",
                                          source_map_json={"scheduled_time_utc": "forexfactory_weekly",
                                                           "forecast_point_in_time": "forexfactory_weekly"})
        SourceObservation.objects.create(event_release=old, source=self.weekly, fetch_run=self.run_w, raw_json={})
        moved = EventRelease.objects.create(indicator=dup, reference_period="release:2026-11-25T07:00",
                                            scheduled_time_utc=AT.replace(day=25), timestamp_confidence="minute")

        out = merge_title_twins()
        self.assertEqual((out["indicators"], out["merged"], out["moved"]), (1, 1, 1))
        self.assertFalse(Indicator.objects.filter(pk=dup.pk).exists())
        rel = EventRelease.objects.get(reference_period=PERIOD)
        self.assertEqual((rel.actual_current, rel.forecast_point_in_time, rel.forecast_stored),
                         (Decimal("3.8"), Decimal("3.7"), Decimal("3.6")))
        self.assertEqual(rel.forecast_provenance, "point_in_time")
        self.assertEqual(rel.source_map_json["forecast_point_in_time"], "forexfactory_weekly")
        self.assertEqual(SourceObservation.objects.filter(event_release=rel, source=self.weekly).count(), 1)
        moved.refresh_from_db()
        self.assertEqual(moved.indicator_id, rel.indicator_id)
        alias = IndicatorAlias.objects.get(source=self.weekly)
        self.assertEqual(alias.indicator_id, rel.indicator_id)
        self.assertEqual(merge_title_twins()["indicators"], 0)  # nothing left to do
