"""Candle and marker feed for the price chart.

The failure modes worth pinning are the quiet ones: a marker whose timestamp
does not land on a candle is silently dropped by the charting library, and a
long range that thins candles instead of aggregating them draws gaps that
never happened.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
from django.test import TestCase, override_settings

from calendar_data.models import EventRelease, Indicator
from prices import series
from prices.models import Instrument
from prices.store import write_month
from sources.models import Source, SourceKind

START = datetime(2024, 9, 2, 0, 0, tzinfo=timezone.utc)


def hourly(count: int, first: float = 1.10) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": pd.date_range(START, periods=count, freq="1h", tz="UTC"),
            "open": [first + i * 1e-4 for i in range(count)],
            "high": [first + i * 1e-4 + 5e-4 for i in range(count)],
            "low": [first + i * 1e-4 - 5e-4 for i in range(count)],
            "close": [first + i * 1e-4 + 1e-4 for i in range(count)],
            "volume": [100.0] * count,
            "spread_mean": [None] * count,
            "tick_count": [None] * count,
        }
    )


class ChartFeedTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parquet = Path(self.tmp.name)

        self.instrument = Instrument.objects.create(
            symbol="EURUSD", base_ccy="EUR", quote_ccy="USD", pip_size=Decimal("0.0001")
        )
        Instrument.objects.create(
            symbol="USDJPY", base_ccy="USD", quote_ccy="JPY", pip_size=Decimal("0.01")
        )
        Instrument.objects.create(
            symbol="AUDUSD", base_ccy="AUD", quote_ccy="USD", pip_size=Decimal("0.0001")
        )
        self.source = Source.objects.create(
            key="dukascopy", name="Dukascopy", kind=SourceKind.PRICE
        )
        self.indicator = Indicator.objects.create(
            currency="USD", name="Non-Farm Employment Change"
        )

    def _store(self, count=200):
        with override_settings(PARQUET_DIR=self.parquet):
            write_month(
                self.instrument, self.source, hourly(count),
                START.date(), timeframe="h1",
            )

    def _release(self, when, actual=None, forecast=None):
        return EventRelease.objects.create(
            indicator=self.indicator,
            reference_period=f"release:{when:%Y-%m-%dT%H:%M}",
            release_time_utc=when,
            actual_current=None if actual is None else Decimal(str(actual)),
            forecast_stored=None if forecast is None else Decimal(str(forecast)),
        )

    def test_candles_come_back_in_chart_shape(self):
        self._store(48)
        with override_settings(PARQUET_DIR=self.parquet):
            rows, label = series.candles(
                "EURUSD", "dukascopy", "h1", START, START + timedelta(hours=47)
            )
        self.assertEqual(len(rows), 48)
        self.assertEqual(label, "1 hour")
        self.assertEqual(set(rows[0]), {"time", "open", "high", "low", "close"})
        self.assertIsInstance(rows[0]["time"], int)

    def test_candle_times_are_unix_seconds_and_ordered(self):
        self._store(24)
        with override_settings(PARQUET_DIR=self.parquet):
            rows, _ = series.candles(
                "EURUSD", "dukascopy", "h1", START, START + timedelta(hours=23)
            )
        self.assertEqual(rows[0]["time"], int(START.timestamp()))
        times = [r["time"] for r in rows]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(times), len(set(times)))

    def test_ohlc_stays_internally_consistent(self):
        self._store(60)
        with override_settings(PARQUET_DIR=self.parquet):
            rows, _ = series.candles(
                "EURUSD", "dukascopy", "h1", START, START + timedelta(hours=59)
            )
        for row in rows:
            self.assertLessEqual(row["low"], min(row["open"], row["close"]))
            self.assertGreaterEqual(row["high"], max(row["open"], row["close"]))

    def test_a_long_range_aggregates_rather_than_thinning(self):
        # Thinning would draw gaps that never happened; aggregating keeps every
        # minute of trading represented.
        self._store(400)
        with override_settings(PARQUET_DIR=self.parquet):
            with self.settings():
                original = series.MAX_CANDLES
                series.MAX_CANDLES = 100
                try:
                    rows, label = series.candles(
                        "EURUSD", "dukascopy", "h1", START, START + timedelta(hours=399)
                    )
                finally:
                    series.MAX_CANDLES = original

        self.assertLessEqual(len(rows), 100)
        self.assertNotEqual(label, "1 hour", "the chart must say it coarsened the bars")
        # Aggregation preserves the extremes; thinning would lose them.
        self.assertGreater(rows[-1]["time"], rows[0]["time"])

    def test_markers_carry_the_release_time_not_the_bar_time(self):
        release = self._release(START + timedelta(hours=12, minutes=30), 142000, 164000)
        markers, _legend = series.event_markers(
            [self.indicator.pk], START, START + timedelta(days=2)
        )
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["time"], int(release.release_time_utc.timestamp()))
        self.assertEqual(markers[0]["id"], release.pk)

    def test_marker_says_beat_or_missed_only_when_both_numbers_exist(self):
        self._release(START + timedelta(hours=1), 142000, 100000)
        self._release(START + timedelta(hours=2), 100000, 142000)
        self._release(START + timedelta(hours=3), 142000, None)
        markers, _ = series.event_markers(
            [self.indicator.pk], START, START + timedelta(days=1)
        )
        self.assertEqual([m["beat"] for m in markers], [True, False, None])

    def test_marker_text_has_no_decimal_tail(self):
        # Decimal keeps its stored scale, so this would read "142000.000000".
        self._release(START + timedelta(hours=1), 142000, 164000)
        markers, _ = series.event_markers(
            [self.indicator.pk], START, START + timedelta(days=1)
        )
        self.assertIn("actual 142K", markers[0]["text"])
        self.assertNotIn("142000.000000", markers[0]["text"])

    def test_small_values_keep_their_precision(self):
        # A 4.1% unemployment rate must not be compacted into something coarser.
        self._release(START + timedelta(hours=1), "4.1", "4.3")
        markers, _ = series.event_markers(
            [self.indicator.pk], START, START + timedelta(days=1)
        )
        self.assertIn("actual 4.1", markers[0]["text"])

    def test_markers_outside_the_window_are_excluded(self):
        self._release(START - timedelta(days=5), 1, 2)
        self._release(START + timedelta(hours=5), 1, 2)
        self._release(START + timedelta(days=40), 1, 2)
        markers, _ = series.event_markers(
            [self.indicator.pk], START, START + timedelta(days=1)
        )
        self.assertEqual(len(markers), 1)

    def test_several_indicators_overlay_in_one_query(self):
        other = Indicator.objects.create(currency="USD", name="Consumer Price Index")
        self._release(START + timedelta(hours=1), 1, 2)
        EventRelease.objects.create(
            indicator=other, reference_period="2024-09",
            release_time_utc=START + timedelta(hours=2),
            actual_current=Decimal("3"), forecast_stored=Decimal("2"),
        )
        with self.assertNumQueries(1):
            markers, legend = series.event_markers(
                [self.indicator.pk, other.pk], START, START + timedelta(days=1)
            )
        self.assertEqual(len(markers), 2)
        self.assertEqual({l["id"] for l in legend}, {self.indicator.pk, other.pk})
        self.assertEqual({l["count"] for l in legend}, {1})

    def test_each_marker_carries_a_short_code_for_its_indicator(self):
        # Identity is the printed label, not the colour — the candles already
        # use the only two well-separated hues.
        self._release(START + timedelta(hours=1), 1, 2)
        markers, _ = series.event_markers(
            [self.indicator.pk], START, START + timedelta(days=1)
        )
        self.assertTrue(markers[0]["code"])
        self.assertLessEqual(len(markers[0]["code"]), 14)

    def test_an_acronym_in_the_name_beats_initials(self):
        # "ISM Manufacturing PMI" must read "ISM", not the opaque "IMP".
        self.assertEqual(
            series.short_code("USD", "ISM Manufacturing PMI", None, with_currency=False),
            "ISM",
        )
        self.assertEqual(
            series.short_code("USD", "JOLTS Job Openings", None, with_currency=False),
            "JOLTS",
        )
        self.assertEqual(
            series.short_code("USD", "Unemployment Claims", None, with_currency=False),
            "UC",
        )

    def test_currency_prefix_only_when_overlays_span_currencies(self):
        eur = Indicator.objects.create(currency="EUR", name="Retail Sales")
        self._release(START + timedelta(hours=1), 1, 2)
        markers, _ = series.event_markers(
            [self.indicator.pk], START, START + timedelta(days=1)
        )
        self.assertNotIn("USD", markers[0]["code"], "one currency needs no prefix")

        EventRelease.objects.create(
            indicator=eur, reference_period="2024-09",
            release_time_utc=START + timedelta(hours=2),
        )
        markers, _ = series.event_markers(
            [self.indicator.pk, eur.pk], START, START + timedelta(days=1)
        )
        self.assertTrue(all(m["code"].split()[0] in {"USD", "EUR"} for m in markers))

    def test_large_values_stay_readable_in_the_tooltip(self):
        # 7,180,000 must not render as 7.18e+06.
        self._release(START + timedelta(hours=1), 7180000, 7380000)
        markers, _ = series.event_markers(
            [self.indicator.pk], START, START + timedelta(days=1)
        )
        self.assertIn("7.18M", markers[0]["text"])
        self.assertNotIn("e+", markers[0]["text"])

    def test_canonical_code_is_preferred_as_the_marker_label(self):
        self.indicator.canonical_code = "US_NFP"
        self.indicator.save()
        self._release(START + timedelta(hours=1), 1, 2)
        markers, _ = series.event_markers(
            [self.indicator.pk], START, START + timedelta(days=1)
        )
        self.assertEqual(markers[0]["code"], "US_NFP")

    def test_overlay_count_is_capped(self):
        ids = []
        for n in range(series.MAX_OVERLAYS + 3):
            ind = Indicator.objects.create(currency="USD", name=f"Series {n}")
            EventRelease.objects.create(
                indicator=ind, reference_period=f"2024-{n:02d}",
                release_time_utc=START + timedelta(hours=n + 1),
            )
            ids.append(ind.pk)
        markers, legend = series.event_markers(ids, START, START + timedelta(days=1))
        self.assertLessEqual(len(legend), series.MAX_OVERLAYS)
        self.assertLessEqual(len(markers), series.MAX_OVERLAYS)

    def test_no_indicators_means_no_query_and_no_markers(self):
        with self.assertNumQueries(0):
            markers, legend = series.event_markers([], START, START + timedelta(days=1))
        self.assertEqual((markers, legend), ([], []))

    def test_only_pairs_carrying_the_currency_are_offered(self):
        # A USD release cannot move a pair that has no USD leg.
        self._release(START + timedelta(hours=1), 1, 2)
        symbols = set(
            series.relevant_indicators("EURUSD").values_list("currency", flat=True)
        )
        self.assertEqual(symbols, {"USD"})

        eur_only = Indicator.objects.create(currency="JPY", name="Tankan")
        EventRelease.objects.create(
            indicator=eur_only, reference_period="2024-09",
            release_time_utc=START + timedelta(hours=2),
        )
        self.assertNotIn(
            eur_only.pk,
            set(series.relevant_indicators("EURUSD").values_list("id", flat=True)),
        )
        self.assertIn(
            eur_only.pk,
            set(series.relevant_indicators("USDJPY").values_list("id", flat=True)),
        )

    def test_markers_snap_onto_the_bar_that_contains_them(self):
        # A release at 12:30 against hourly bars at 12:00 and 13:00 matches
        # neither. lightweight-charts silently drops such a marker, so the
        # chart looks like "no events here" with no error anywhere.
        candles = [
            {"time": int((START + timedelta(hours=h)).timestamp())} for h in range(4)
        ]
        raw = [{"time": int((START + timedelta(hours=1, minutes=30)).timestamp()),
                "code": "NFP", "beat": True}]

        snapped = series.snap_markers(raw, candles)

        self.assertEqual(len(snapped), 1)
        self.assertEqual(snapped[0]["time"], candles[1]["time"], "snaps to the containing bar")
        self.assertEqual(
            snapped[0]["release_time"], raw[0]["time"], "exact instant is preserved"
        )

    def test_markers_snap_correctly_at_a_coarser_resolution(self):
        # After aggregation the bars are daily, so an intraday release must
        # land on its day, not vanish.
        candles = [
            {"time": int((START + timedelta(days=d)).timestamp())} for d in range(3)
        ]
        raw = [{"time": int((START + timedelta(days=1, hours=14)).timestamp()), "code": "X"}]
        snapped = series.snap_markers(raw, candles)
        self.assertEqual(snapped[0]["time"], candles[1]["time"])

    def test_markers_beyond_the_candles_are_dropped(self):
        candles = [{"time": int((START + timedelta(hours=h)).timestamp())} for h in range(3)]
        raw = [
            {"time": int((START - timedelta(days=1)).timestamp()), "code": "early"},
            {"time": int((START + timedelta(days=9)).timestamp()), "code": "late"},
        ]
        self.assertEqual(series.snap_markers(raw, candles), [])

    def test_snapped_markers_come_back_sorted(self):
        candles = [{"time": int((START + timedelta(hours=h)).timestamp())} for h in range(6)]
        raw = [
            {"time": int((START + timedelta(hours=4)).timestamp()), "code": "b"},
            {"time": int((START + timedelta(hours=1)).timestamp()), "code": "a"},
        ]
        snapped = series.snap_markers(raw, candles)
        self.assertEqual([m["code"] for m in snapped], ["a", "b"])

    def test_snapping_without_candles_is_empty_not_an_error(self):
        self.assertEqual(series.snap_markers([{"time": 1}], []), [])

    def test_window_for_brackets_the_release(self):
        focus = datetime(2024, 9, 6, 12, 30, tzinfo=timezone.utc)
        start, end = series.window_for(focus)
        self.assertLess(start, focus)
        self.assertGreater(end, focus)
        self.assertEqual((end - start).days, 10)


class ChartViewTests(TestCase):
    def setUp(self):
        Instrument.objects.create(
            symbol="EURUSD", base_ccy="EUR", quote_ccy="USD", pip_size=Decimal("0.0001")
        )
        Source.objects.create(key="dukascopy", name="Dukascopy", kind=SourceKind.PRICE)

    def test_chart_page_renders_without_price_data(self):
        response = self.client.get("/instruments/chart/?symbol=EURUSD")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No price data stored")

    def test_api_reports_absence_rather_than_an_empty_chart(self):
        response = self.client.get("/instruments/api/candles/?symbol=EURUSD&tf=h1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["candles"], [])
        self.assertIn("No h1 price data", payload["message"])

    def test_unknown_instrument_is_a_404_not_an_empty_chart(self):
        self.assertEqual(
            self.client.get("/instruments/api/candles/?symbol=XXXYYY").status_code, 404
        )

    def test_before_parameter_pages_further_back(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        instrument = Instrument.objects.get(symbol="EURUSD")
        source = Source.objects.get(key="dukascopy")

        with override_settings(PARQUET_DIR=Path(tmp.name)):
            write_month(instrument, source, hourly(600), START.date(), timeframe="h1")

            first = self.client.get(
                "/instruments/api/candles/?symbol=EURUSD&tf=h1&span=5"
            ).json()
            self.assertTrue(first["candles"])

            earlier = self.client.get(
                "/instruments/api/candles/",
                {"symbol": "EURUSD", "tf": "h1", "span": "5", "before": first["from"]},
            ).json()

        self.assertTrue(earlier["candles"], "paging back must return bars")
        self.assertLess(
            earlier["candles"][0]["time"], first["candles"][0]["time"],
            "the earlier page must precede what is already held",
        )

    def test_an_unencoded_offset_still_pages_back(self):
        # "+00:00" arrives as a space when the caller forgot to encode it.
        # Falling back to the same window would look like a dead scroll.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        instrument = Instrument.objects.get(symbol="EURUSD")
        source = Source.objects.get(key="dukascopy")

        with override_settings(PARQUET_DIR=Path(tmp.name)):
            write_month(instrument, source, hourly(600), START.date(), timeframe="h1")
            first = self.client.get(
                "/instruments/api/candles/?symbol=EURUSD&tf=h1&span=5"
            ).json()
            mangled = first["from"].replace("+", " ")
            earlier = self.client.get(
                f"/instruments/api/candles/?symbol=EURUSD&tf=h1&span=5&before={mangled}"
            ).json()

        self.assertLess(earlier["candles"][0]["time"], first["candles"][0]["time"])
