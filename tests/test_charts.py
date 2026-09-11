"""Chart rendering and series queries.

The charts are strings of SVG, so these assert on structure and on the things
that would silently mislead: a wrong axis, a dropped point, a series that
renders on top of a missing baseline.
"""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.test import TestCase

from calendar_data import series
from calendar_data.models import EventRelease, Indicator
from dashboard import charts

T0 = datetime(2007, 1, 5, 13, 30, tzinfo=timezone.utc)


class CompactNumberTests(unittest.TestCase):
    def test_large_values_get_a_suffix(self):
        self.assertEqual(charts.compact(142000), "142K")
        self.assertEqual(charts.compact(-20537000), "-20.5M")
        self.assertEqual(charts.compact(2480000000000), "2.5T")

    def test_small_values_keep_their_precision(self):
        # A 4.1% unemployment rate must not render as "4".
        self.assertEqual(charts.compact(4.1), "4.1")
        self.assertEqual(charts.compact(-0.25), "-0.25")
        self.assertEqual(charts.compact(0), "0")

    def test_none_is_an_em_dash_not_a_zero(self):
        self.assertEqual(charts.compact(None), "—")


class LineChartTests(unittest.TestCase):
    def _points(self, values):
        return [(T0 + timedelta(days=30 * i), v) for i, v in enumerate(values)]

    def test_empty_input_says_so_rather_than_drawing_axes(self):
        svg = charts.line([])
        self.assertNotIn("<svg", svg)
        self.assertIn("No values", svg)

    def test_every_point_becomes_a_hover_target(self):
        svg = charts.line(self._points([4.5, 5.0, 4.8]))
        self.assertEqual(svg.count("<title>"), 3)
        self.assertIn('class="series"', svg)

    def test_extremes_are_labelled(self):
        svg = charts.line(self._points([4.5, 14.7, 3.4]))
        self.assertIn("14.7", svg)
        self.assertIn("3.4", svg)

    def test_a_second_series_is_dashed_not_recoloured(self):
        # Identity by weight and dash, so the chart survives CVD and print.
        points = self._points([4.5, 5.0])
        svg = charts.line(points, secondary=points)
        self.assertIn("series--secondary", svg)

    def test_a_zero_line_appears_only_when_the_data_crosses_zero(self):
        self.assertIn('class="zero"', charts.line(self._points([-1.0, 2.0])))
        self.assertNotIn('class="zero"', charts.line(self._points([1.0, 2.0])))

    def test_values_are_escaped(self):
        svg = charts.line(self._points([1.0]), label='<script>"x"')
        self.assertNotIn("<script>", svg)


class SurpriseChartTests(unittest.TestCase):
    def test_polarity_is_positional_and_labelled_in_words(self):
        points = [(T0, 1.5), (T0 + timedelta(days=30), -2.0)]
        svg = charts.surprise(points)
        self.assertIn("beat", svg)
        self.assertIn("missed", svg)
        self.assertIn('class="zero"', svg)

    def test_empty_says_why(self):
        self.assertIn("No forecast", charts.surprise([]))


class SparklineTests(unittest.TestCase):
    def test_needs_at_least_two_points(self):
        self.assertNotIn("<svg", charts.sparkline([1.0]))
        self.assertIn("<svg", charts.sparkline([1.0, 2.0]))

    def test_flat_series_does_not_divide_by_zero(self):
        self.assertIn("<svg", charts.sparkline([3.0, 3.0, 3.0]))


class ThinningTests(unittest.TestCase):
    def test_short_series_is_untouched(self):
        points = list(range(10))
        self.assertEqual(series.thin(points, limit=50), points)

    def test_long_series_keeps_its_endpoints(self):
        points = list(range(5000))
        thinned = series.thin(points, limit=100)
        self.assertEqual(len(thinned), 100)
        self.assertEqual(thinned[0], 0)
        self.assertEqual(thinned[-1], 4999, "the latest value must never be dropped")


class SeriesQueryTests(TestCase):
    def setUp(self):
        self.indicator = Indicator.objects.create(currency="USD", name="Unemployment Rate")
        self.other = Indicator.objects.create(currency="EUR", name="Something Else")
        for index, value in enumerate([4.5, 14.7, 3.4]):
            EventRelease.objects.create(
                indicator=self.indicator,
                reference_period=f"2007-{index + 1:02d}",
                release_time_utc=T0 + timedelta(days=30 * index),
                actual_current=Decimal(str(value)),
                forecast_stored=Decimal("4.0"),
            )
        # No timestamp and no value: must never reach a chart.
        EventRelease.objects.create(
            indicator=self.indicator, reference_period="2007-09", actual_current=None
        )

    def test_series_excludes_rows_that_cannot_be_plotted(self):
        points = series.indicator_series(self.indicator.pk)
        self.assertEqual(len(points), 3)
        self.assertEqual([float(p[1]) for p in points], [4.5, 14.7, 3.4])

    def test_series_is_oldest_first(self):
        points = series.indicator_series(self.indicator.pk)
        self.assertEqual([p[0] for p in points], sorted(p[0] for p in points))

    def test_surprise_is_actual_minus_forecast(self):
        surprises = series.surprise_series(self.indicator.pk)
        self.assertEqual([round(s[1], 1) for s in surprises], [0.5, 10.7, -0.6])

    def test_stats_are_computed_in_sql(self):
        stats = series.indicator_stats(self.indicator.pk)
        self.assertEqual(stats["releases"], 4)
        self.assertEqual(stats["with_actual"], 3)
        self.assertEqual(float(stats["highest"]), 14.7)

    def test_sparklines_for_many_indicators_take_one_query(self):
        with self.assertNumQueries(1):
            values = series.sparkline_values([self.indicator.pk, self.other.pk])
        self.assertEqual(values[self.indicator.pk], [4.5, 14.7, 3.4])
        self.assertNotIn(self.other.pk, values)

    def test_sparkline_values_are_oldest_first(self):
        values = series.sparkline_values([self.indicator.pk])[self.indicator.pk]
        self.assertEqual(values[-1], 3.4, "the most recent value belongs at the end")
