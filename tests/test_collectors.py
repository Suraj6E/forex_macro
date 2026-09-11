"""Parsing tests for the collectors — no network, no database.

Each test here pins down a decision that would otherwise be discovered as
"the numbers look odd" three phases later:

* the MT5 ×1,000,000 scaling and its LONG_MIN null sentinel (§4.2, §12)
* HistData's fixed UTC−5 with no DST (§4.1)
* Dukascopy's zero-based month in the URL path and its integer price scaling
* that a month's expected bar count follows the New York-anchored FX week
"""

import json
import lzma
import struct
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from analytics.timeutils import expected_minutes, fx_sessions
from collectors import dukascopy as duka
from collectors import histdata as hd
from normalisers import dbnomics as dbn
from normalisers import forexfactory_pages as ffp
from normalisers import mt5_calendar as mt5


class DukascopyWireFormatTests(unittest.TestCase):
    def test_month_in_the_url_is_zero_based(self):
        # September is /08/. Getting this wrong returns a valid file for the
        # wrong month, which is worse than a 404 because it looks like data.
        url = duka.hour_url("EURUSD", datetime(2024, 9, 6, 12, tzinfo=timezone.utc))
        self.assertIn("/2024/08/06/12h_ticks.bi5", url)

    def test_january_is_month_zero(self):
        url = duka.hour_url("EURUSD", datetime(2024, 1, 2, 3, tzinfo=timezone.utc))
        self.assertIn("/2024/00/02/03h_ticks.bi5", url)

    def test_jpy_pairs_scale_by_a_thousand(self):
        self.assertEqual(duka.point_scale("USDJPY"), 1_000.0)
        self.assertEqual(duka.point_scale("EURUSD"), 100_000.0)

    def _payload(self, records) -> bytes:
        raw = b"".join(duka.TICK_RECORD.pack(*r) for r in records)
        return lzma.compress(raw, format=lzma.FORMAT_ALONE)

    def test_decode_ticks_scales_and_places_in_time(self):
        hour = datetime(2024, 9, 6, 12, tzinfo=timezone.utc)
        payload = self._payload(
            [(0, 111071, 111070, 0.9, 4.5), (60_000, 111080, 111078, 1.0, 2.0)]
        )
        ticks = duka.decode_ticks(payload, hour, duka.point_scale("EURUSD"))

        self.assertEqual(len(ticks), 2)
        self.assertEqual(ticks[0][0], hour)
        self.assertAlmostEqual(ticks[0][1], 1.11071, places=6)  # ask
        self.assertAlmostEqual(ticks[0][2], 1.11070, places=6)  # bid
        self.assertEqual(ticks[1][0], datetime(2024, 9, 6, 12, 1, tzinfo=timezone.utc))

    def test_empty_hour_is_data_not_an_error(self):
        # A zero-length body means the market was shut.
        self.assertEqual(duka.decode_ticks(b"", datetime.now(timezone.utc), 1e5), [])

    def test_ticks_aggregate_to_bid_ohlc_with_spread(self):
        hour = datetime(2024, 9, 6, 12, tzinfo=timezone.utc)
        ticks = [
            (hour, 1.1002, 1.1000, 1.0, 1.0),
            (hour.replace(second=20), 1.1006, 1.1004, 1.0, 1.0),
            (hour.replace(second=40), 1.1003, 1.1001, 1.0, 1.0),
        ]
        bars = duka.ticks_to_m1(ticks)

        self.assertEqual(len(bars), 1)
        row = bars.iloc[0]
        self.assertAlmostEqual(row["open"], 1.1000, places=6)
        self.assertAlmostEqual(row["high"], 1.1004, places=6)
        self.assertAlmostEqual(row["low"], 1.1000, places=6)
        self.assertAlmostEqual(row["close"], 1.1001, places=6)
        self.assertEqual(row["tick_count"], 3)
        # OHLC is the bid; the spread is carried separately, because a mid
        # price overstates what was actually capturable.
        self.assertAlmostEqual(row["spread_mean"], 0.0002, places=6)


class DukascopyCandleTests(unittest.TestCase):
    """The candle endpoint: one file per instrument-month, which is what makes
    an hourly backbone ~1,600 requests instead of ~1.2 million."""

    MONTH = datetime(2024, 9, 1, tzinfo=timezone.utc)

    def _payload(self, records) -> bytes:
        raw = b"".join(duka.CANDLE_RECORD.pack(*r) for r in records)
        return lzma.compress(raw, format=lzma.FORMAT_ALONE)

    def test_month_is_zero_based_in_the_candle_url(self):
        url = duka.candle_url("EURUSD", date(2024, 9, 1), "h1")
        self.assertIn("/2024/08/BID_candles_hour_1.bi5", url)

    def test_daily_candles_live_in_a_year_file(self):
        url = duka.candle_url("EURUSD", date(2024, 9, 1), "d1")
        self.assertIn("/2024/BID_candles_day_1.bi5", url)
        self.assertNotIn("/08/", url)

    def test_field_order_is_open_close_low_high(self):
        # Read as open/high/low/close this record would put the high below the
        # close — which is exactly how the real format was identified.
        record = (3600, 111417, 111365, 111355, 111426, 2686.32)
        rows = duka.decode_candles(self._payload([record]), self.MONTH, 100_000.0)

        self.assertEqual(len(rows), 1)
        when, open_, high, low, close, volume = rows[0]
        self.assertEqual(when, datetime(2024, 9, 1, 1, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(open_, 1.11417, places=6)
        self.assertAlmostEqual(close, 1.11365, places=6)
        self.assertAlmostEqual(low, 1.11355, places=6)
        self.assertAlmostEqual(high, 1.11426, places=6)
        self.assertAlmostEqual(volume, 2686.32, places=2)

    def test_decoded_bars_are_internally_consistent(self):
        rows = duka.decode_candles(
            self._payload(
                [
                    (0, 110456, 110500, 110400, 110550, 10.0),
                    (3600, 110500, 110450, 110430, 110560, 20.0),
                ]
            ),
            self.MONTH,
            100_000.0,
        )
        for _when, open_, high, low, close, _vol in rows:
            self.assertLessEqual(low, min(open_, close))
            self.assertGreaterEqual(high, max(open_, close))

    def test_offsets_are_seconds_from_the_period_start(self):
        rows = duka.decode_candles(
            self._payload([(0, 1, 1, 1, 2, 1.0), (7200, 1, 1, 1, 2, 1.0)]),
            self.MONTH,
            100_000.0,
        )
        self.assertEqual(rows[1][0] - rows[0][0], timedelta(hours=2))

    def test_flat_zero_volume_padding_is_dropped(self):
        # Closed hours arrive as a flat bar carrying the last price. That is
        # padding, not a quote, and counting it would invent liquidity.
        padded = (0, 110456, 110456, 110456, 110456, 0.0)
        real = (3600, 110456, 110500, 110400, 110550, 12.0)
        rows = duka.decode_candles(self._payload([padded, real]), self.MONTH, 100_000.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], datetime(2024, 9, 1, 1, 0, tzinfo=timezone.utc))

    def test_a_genuinely_flat_bar_with_volume_is_kept(self):
        traded = (0, 110456, 110456, 110456, 110456, 5.0)
        rows = duka.decode_candles(self._payload([traded]), self.MONTH, 100_000.0)
        self.assertEqual(len(rows), 1, "zero range with volume is a real bar")

    def test_jpy_pairs_use_the_three_decimal_scale(self):
        rows = duka.decode_candles(
            self._payload([(0, 143250, 143300, 143200, 143400, 9.0)]),
            self.MONTH,
            duka.point_scale("USDJPY"),
        )
        self.assertAlmostEqual(rows[0][1], 143.250, places=4)

    def test_frame_has_no_spread_column_populated(self):
        # Bid candles carry no ask, so spread must stay null rather than be
        # invented (§4.7) — only the tick endpoint can give spread.
        rows = duka.decode_candles(
            self._payload([(0, 110456, 110500, 110400, 110550, 12.0)]),
            self.MONTH, 100_000.0,
        )
        frame = duka.candles_to_frame(rows)
        self.assertTrue(frame["spread_mean"].isna().all())
        self.assertEqual(list(frame["ts_utc"])[0], self.MONTH)

    def test_empty_payload_is_no_bars_not_an_error(self):
        self.assertEqual(duka.decode_candles(b"", self.MONTH, 100_000.0), [])


class HistDataTests(unittest.TestCase):
    def test_filename_yields_instrument_and_month(self):
        self.assertEqual(
            hd.parse_filename("HISTDATA_COM_ASCII_EURUSD_M1_202409.zip"),
            ("EURUSD", date(2024, 9, 1)),
        )
        self.assertEqual(
            hd.parse_filename("DAT_ASCII_USDJPY_M1_200701.csv"),
            ("USDJPY", date(2007, 1, 1)),
        )

    def test_unrecognised_filename_is_rejected_not_guessed(self):
        self.assertIsNone(hd.parse_filename("prices.zip"))
        self.assertIsNone(hd.parse_filename("EURUSD_M1_202413.zip"))  # month 13

    def test_timestamps_use_a_fixed_utc_minus_five_all_year(self):
        # Same wall-clock time in January and July must map to the same UTC
        # offset. Treating this source as America/New_York would put the July
        # row an hour out — for half the history, silently.
        payload = (
            b"20240102 083000;1.1;1.2;1.0;1.15;0\n"
            b"20240702 083000;1.1;1.2;1.0;1.15;0\n"
        )
        frame = hd.parse_csv(payload)
        self.assertEqual(
            frame["ts_utc"].iloc[0].to_pydatetime(),
            datetime(2024, 1, 2, 13, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            frame["ts_utc"].iloc[1].to_pydatetime(),
            datetime(2024, 7, 2, 13, 30, tzinfo=timezone.utc),
        )

    def test_bid_only_bars_carry_no_spread(self):
        frame = hd.parse_csv(b"20240102 083000;1.1;1.2;1.0;1.15;0\n")
        self.assertTrue(frame["spread_mean"].isna().all())


class Mt5ValueTests(unittest.TestCase):
    """§4.2 gotcha 1 and §12: the two traps that quietly destroy a sample."""

    def test_values_are_divided_by_one_million(self):
        self.assertEqual(mt5.decode_value("3200000"), Decimal("3.2"))
        self.assertEqual(mt5.decode_value("227000000000"), Decimal("227000"))

    def test_long_min_is_null_not_a_number(self):
        self.assertIsNone(mt5.decode_value(str(mt5.LONG_MIN)))
        self.assertIsNone(mt5.decode_value(""))
        self.assertIsNone(mt5.decode_value("NULL"))

    def test_negative_values_survive(self):
        self.assertEqual(mt5.decode_value("-100000"), Decimal("-0.1"))

    def test_period_becomes_a_month_label(self):
        label, start = mt5.decode_period("2024.08.01 00:00:00")
        self.assertEqual(label, "2024-08")
        self.assertEqual(start, date(2024, 8, 1))

    def test_absent_period_is_empty_not_1970(self):
        self.assertEqual(mt5.decode_period("1970.01.01 00:00:00"), ("", None))

    def test_parse_converts_with_the_recorded_server_offset(self):
        csv_bytes = (
            "event_id;event_name;country;currency;importance;server_time;"
            "server_gmt_offset;period;revision;actual_value;forecast_value;"
            "prev_value;revised_prev_value;unit\n"
            "840010013;Nonfarm Payrolls;United States;USD;HIGH;"
            "2024.09.06 15:30:00;10800;2024.08.01 00:00:00;0;"
            "142000000000;165000000000;89000000000;-9223372036854775808;persons\n"
        ).encode("utf-8")

        rows = mt5.parse(csv_bytes)
        self.assertEqual(len(rows), 1)
        row = rows[0]

        # 15:30 server time minus a +3h offset is 12:30 UTC — 08:30 New York.
        self.assertEqual(
            row.release_time_utc, datetime(2024, 9, 6, 12, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(row.actual, Decimal("142000"))
        self.assertEqual(row.forecast, Decimal("165000"))
        self.assertEqual(row.previous, Decimal("89000"))
        self.assertIsNone(row.revised_previous, "LONG_MIN must read as null")
        self.assertEqual(row.reference_period, "2024-08")
        self.assertEqual(row.importance_source, 3)

    def test_forecast_never_claims_to_be_point_in_time(self):
        # Whether MT5's stored forecast is the consensus as of release or a
        # later snapshot is undocumented — so it can only be vendor-stored.
        csv_bytes = (
            "event_id;event_name;country;currency;importance;server_time;"
            "server_gmt_offset;period;revision;actual_value;forecast_value;"
            "prev_value;revised_prev_value;unit\n"
            "1;CPI;United States;USD;HIGH;2024.09.06 15:30:00;0;"
            "2024.08.01 00:00:00;0;1;2;3;4;%\n"
        ).encode("utf-8")
        row = mt5.parse(csv_bytes)[0]
        self.assertEqual(row.forecast_target, "forecast_stored")
        self.assertEqual(row.timestamp_confidence, "inferred")

    def test_missing_columns_fail_loudly(self):
        with self.assertRaises(ValueError) as caught:
            mt5.parse(b"event_id;event_name\n1;CPI\n")
        self.assertIn("missing column", str(caught.exception).lower())


class ForexFactoryPagesTests(unittest.TestCase):
    """The historical calendar. Field mapping verified against real releases
    before this parser was written — NFP on 6 Sep 2024 reads 142K actual,
    164K forecast, 114K previous and 89K revised previous, at 12:30 UTC."""

    NFP = {
        "id": 135998,
        "ebaseId": 66,
        "name": "Non-Farm Employment Change",
        "currency": "USD",
        "dateline": 1725625800,
        "impactName": "high",
        "actual": "142K",
        "forecast": "164K",
        "previous": "114K",
        "revision": "89K",
        "timeMasked": False,
    }

    def _page(self, *events) -> bytes:
        days = [{"date": "Fri", "dateline": 1725580800, "events": list(events)}]
        return (
            "<html><script>window.calendarComponentStates[1] = {\n"
            f"days: {json.dumps(days)},\n"
            "other: 1};</script></html>"
        ).encode("utf-8")

    def test_known_release_maps_correctly(self):
        rows = ffp.parse(self._page(self.NFP))
        self.assertEqual(len(rows), 1)
        row = rows[0]

        self.assertEqual(
            row.release_time_utc, datetime(2024, 9, 6, 12, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(row.actual, Decimal("142000"))
        self.assertEqual(row.forecast, Decimal("164000"))
        self.assertEqual(row.previous, Decimal("114000"))
        # The site's `revision` restates the PREVIOUS release, not this one.
        self.assertEqual(row.revised_previous, Decimal("89000"))
        self.assertEqual(row.importance_source, 3)

    def test_the_stable_series_id_is_the_alias_key(self):
        # ebaseId survives title changes across nineteen years; the display
        # title does not, and a changed title silently forks a new indicator.
        row = ffp.parse(self._page(self.NFP))[0]
        self.assertEqual(row.source_event_key, "ff:66")

    def test_a_historical_scrape_is_never_point_in_time(self):
        # §4.3: what the page shows today is the currently displayed forecast.
        # Filing it as point-in-time would be look-ahead contamination.
        row = ffp.parse(self._page(self.NFP))[0]
        self.assertEqual(row.forecast_target, "forecast_stored")
        self.assertEqual(row.actual_target, "actual_current")

    def test_an_event_without_an_actual_is_scheduled_not_released(self):
        pending = {**self.NFP, "actual": ""}
        row = ffp.parse(self._page(pending))[0]
        self.assertIsNone(row.release_time_utc)
        self.assertEqual(
            row.scheduled_time_utc, datetime(2024, 9, 6, 12, 30, tzinfo=timezone.utc)
        )

    def test_masked_times_are_graded_down(self):
        masked = {**self.NFP, "timeMasked": True}
        self.assertEqual(ffp.parse(self._page(masked))[0].timestamp_confidence, "date_only")
        self.assertEqual(ffp.parse(self._page(self.NFP))[0].timestamp_confidence, "minute")

    def test_out_of_scope_currencies_are_dropped(self):
        self.assertEqual(ffp.parse(self._page({**self.NFP, "currency": "CNY"})), [])

    def test_nested_brackets_do_not_truncate_the_calendar(self):
        # A lazy regex would stop at the first "]," inside the data and return
        # a short calendar — a plausible-looking wrong answer, which is worse
        # than an error. The bracket scanner must survive it.
        noisy = {**self.NFP, "name": "Thing [a], [b]", "notice": "see [1], [2]"}
        rows = ffp.parse(self._page(noisy, {**self.NFP, "id": 2, "ebaseId": 67}))
        self.assertEqual(len(rows), 2)

    def test_a_block_page_raises_rather_than_returning_nothing(self):
        with self.assertRaises(ValueError):
            ffp.parse(b"<html><body>Attention Required! Cloudflare</body></html>")

    def test_summarise_counts_what_arrived(self):
        stats = ffp.summarise(ffp.parse(self._page(self.NFP)))
        self.assertEqual(stats["rows"], 1)
        self.assertEqual(stats["with_actual"], 1)
        self.assertEqual(stats["with_revision"], 1)


class DbnomicsTests(unittest.TestCase):
    def test_period_labels_resolve_to_a_start_date(self):
        self.assertEqual(dbn.period_start("2024"), date(2024, 1, 1))
        self.assertEqual(dbn.period_start("2024-03"), date(2024, 3, 1))
        self.assertEqual(dbn.period_start("2024-Q3"), date(2024, 7, 1))
        self.assertEqual(dbn.period_start("2024-03-15"), date(2024, 3, 15))
        self.assertIsNone(dbn.period_start("whenever"))

    def test_observations_become_rows_without_a_release_time(self):
        payload = (
            b'{"series": {"docs": [{"provider_code": "BLS", "dataset_code": "cu", '
            b'"series_code": "X", "series_name": "CPI", '
            b'"period": ["2023-12", "2024-01"], "value": [300.1, 309.8]}]}}'
        )
        config = {"BLS/cu/X": {"currency": "USD", "name": "US CPI"}}
        rows, found, missing = dbn.parse(payload, series_config=config, since=date(2024, 1, 1))

        self.assertEqual(found, ["BLS/cu/X"])
        self.assertEqual(missing, [])
        self.assertEqual(len(rows), 1, "the 2023 observation is before `since`")
        self.assertEqual(rows[0].actual, Decimal("309.8"))
        self.assertIsNone(rows[0].release_time_utc)
        self.assertIsNone(rows[0].scheduled_time_utc)
        self.assertEqual(rows[0].actual_target, "actual_current")

    def test_unresolved_series_are_reported_not_dropped(self):
        payload = b'{"series": {"docs": []}}'
        _rows, _found, missing = dbn.parse(
            payload, series_config={"A/B/C": {"currency": "USD"}}
        )
        self.assertEqual(missing, ["A/B/C"])

    def test_shape_change_raises(self):
        with self.assertRaises(ValueError):
            dbn.parse(b'{"unexpected": 1}', series_config={})


class ExpectedBarCountTests(unittest.TestCase):
    """A month is not "minutes minus weekends" — the week is anchored to
    17:00 New York, so its UTC boundary moves twice a year (§4.1)."""

    def test_sessions_open_sunday_and_close_friday(self):
        sessions = fx_sessions(date(2024, 7, 1), date(2024, 7, 15))
        for open_utc, close_utc in sessions:
            self.assertEqual(open_utc.weekday(), 6, "sessions open on a Sunday")
            self.assertEqual(close_utc.weekday(), 4, "sessions close on a Friday")
            self.assertEqual((close_utc - open_utc).days, 5)

    def test_expected_minutes_is_a_plausible_month(self):
        minutes = expected_minutes(date(2024, 9, 1))
        # Roughly 21 weekdays × 1,440, well under the 43,200 of a raw month.
        self.assertGreater(minutes, 25_000)
        self.assertLess(minutes, 35_000)

    def test_summer_and_winter_months_differ_from_a_naive_count(self):
        for month in (date(2024, 1, 1), date(2024, 7, 1)):
            self.assertNotEqual(expected_minutes(month), 31 * 24 * 60)
