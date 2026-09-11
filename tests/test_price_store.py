"""The price pipeline end to end, without touching the network.

Proves the part the live feeds depend on: a collector's frame becomes a
Parquet month on disk and a coverage row in SQLite, re-writing merges rather
than duplicating, and naive timestamps are refused outright (§4.1, §4.5, §8).
"""

import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
from django.test import TestCase, override_settings

from prices.models import Instrument, PriceCoverage
from prices.store import normalise_frame, read_month, read_range, write_month
from sources.models import Source, SourceKind


def bars(start: datetime, count: int, first_price: float = 1.1000) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": pd.date_range(start, periods=count, freq="1min", tz="UTC"),
            "open": [first_price + i * 1e-5 for i in range(count)],
            "high": [first_price + i * 1e-5 + 2e-5 for i in range(count)],
            "low": [first_price + i * 1e-5 - 2e-5 for i in range(count)],
            "close": [first_price + i * 1e-5 + 1e-5 for i in range(count)],
            "volume": [10.0] * count,
            "spread_mean": [2e-5] * count,
            "tick_count": [40] * count,
        }
    )


class PriceStoreTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parquet_dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        self.instrument = Instrument.objects.create(
            symbol="EURUSD", base_ccy="EUR", quote_ccy="USD", pip_size=Decimal("0.0001")
        )
        self.source = Source.objects.create(
            key="dukascopy", name="Dukascopy", kind=SourceKind.PRICE
        )

    def test_writing_a_month_creates_parquet_and_a_coverage_row(self):
        frame = bars(datetime(2024, 9, 6, 12, tzinfo=timezone.utc), 60)

        with override_settings(PARQUET_DIR=self.parquet_dir):
            coverage = write_month(self.instrument, self.source, frame, date(2024, 9, 1))
            stored = read_month("EURUSD", "dukascopy", date(2024, 9, 1))

        self.assertEqual(coverage.bar_count, 60)
        self.assertEqual(len(stored), 60)
        self.assertEqual(
            coverage.first_ts_utc, datetime(2024, 9, 6, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(
            coverage.last_ts_utc, datetime(2024, 9, 6, 12, 59, tzinfo=timezone.utc)
        )
        self.assertTrue((self.parquet_dir / coverage.parquet_path).exists())

    def test_coverage_records_the_gap_against_a_full_month(self):
        frame = bars(datetime(2024, 9, 6, 12, tzinfo=timezone.utc), 60)
        with override_settings(PARQUET_DIR=self.parquet_dir):
            coverage = write_month(self.instrument, self.source, frame, date(2024, 9, 1))

        # One hour of a month is almost all gap. Long horizons need unbroken
        # bars across weekends and holidays, so the shortfall is the point.
        self.assertGreater(coverage.expected_bar_count, 25_000)
        self.assertEqual(
            coverage.gap_count, coverage.expected_bar_count - coverage.bar_count
        )

    def test_refetching_merges_rather_than_duplicating(self):
        start = datetime(2024, 9, 6, 12, tzinfo=timezone.utc)
        with override_settings(PARQUET_DIR=self.parquet_dir):
            write_month(self.instrument, self.source, bars(start, 60), date(2024, 9, 1))
            # An overlapping second fetch: 30 minutes already held, 30 new.
            coverage = write_month(
                self.instrument,
                self.source,
                bars(start + pd.Timedelta(minutes=30), 60),
                date(2024, 9, 1),
            )
            stored = read_month("EURUSD", "dukascopy", date(2024, 9, 1))

        self.assertEqual(coverage.bar_count, 90)
        self.assertEqual(len(stored), 90)
        self.assertEqual(stored["ts_utc"].is_unique, True)
        self.assertEqual(PriceCoverage.objects.count(), 1)

    def test_a_later_write_corrects_an_earlier_bar(self):
        start = datetime(2024, 9, 6, 12, tzinfo=timezone.utc)
        corrected = bars(start, 1, first_price=1.2000)

        with override_settings(PARQUET_DIR=self.parquet_dir):
            write_month(self.instrument, self.source, bars(start, 1), date(2024, 9, 1))
            write_month(self.instrument, self.source, corrected, date(2024, 9, 1))
            stored = read_month("EURUSD", "dukascopy", date(2024, 9, 1))

        self.assertEqual(len(stored), 1)
        self.assertAlmostEqual(stored["open"].iloc[0], 1.2000, places=6)

    def test_naive_timestamps_are_refused(self):
        frame = bars(datetime(2024, 9, 6, 12, tzinfo=timezone.utc), 5)
        frame["ts_utc"] = frame["ts_utc"].dt.tz_localize(None)
        with self.assertRaises(ValueError) as caught:
            normalise_frame(frame)
        self.assertIn("naive", str(caught.exception).lower())

    def test_read_range_stitches_across_months(self):
        with override_settings(PARQUET_DIR=self.parquet_dir):
            write_month(
                self.instrument,
                self.source,
                bars(datetime(2024, 8, 30, 23, 30, tzinfo=timezone.utc), 30),
                date(2024, 8, 1),
            )
            write_month(
                self.instrument,
                self.source,
                bars(datetime(2024, 9, 1, 0, 0, tzinfo=timezone.utc), 30),
                date(2024, 9, 1),
            )
            window = read_range(
                "EURUSD",
                "dukascopy",
                datetime(2024, 8, 30, 23, 45, tzinfo=timezone.utc),
                datetime(2024, 9, 1, 0, 15, tzinfo=timezone.utc),
            )

        self.assertEqual(len(window), 31)
        self.assertEqual(PriceCoverage.objects.count(), 2)
