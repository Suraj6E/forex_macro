"""Several instruments: compact tick files, candles kept in step with their
ticks, profit in a USD account, and the economic calendar's clock rule."""

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from simulator import instruments
from simulator.data import dukascopy as dk
from simulator.data.bars import BarStore
from simulator.data.calendar import Calendar, fmt_value
from simulator.data.ticks import NS_PER_SECOND, FeedStore, ParquetTickSource, day_entry, day_start_ns, migrate_feed
from simulator.engine.broker import Broker, OrderError
from simulator.tests.test_data import FakeFeed

S = NS_PER_SECOND


def ns(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp()) * S


class CompactTicks(unittest.TestCase):
    def test_round_trip_is_exact_for_every_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            for scale, prices in ((10**5, [1.08123, 1.08124, 1.0812]), (10**3, [156.161, 156.2, 29.878])):
                store = FeedStore(Path(tmp) / str(scale), scale=scale)
                ts = ns(2025, 3, 4) + np.array([0, 1_000_000, 1_000_000], np.int64)
                bid = np.array(prices)
                ask = np.round(bid + 2 / scale, 6)
                store.write_day(date(2025, 3, 4), ts, bid, ask, np.array([0, 3, 1], np.int8))
                r = store.read_day(date(2025, 3, 4))
                self.assertEqual(r[0].tolist(), ts.tolist())
                self.assertEqual(r[1].tolist(), bid.tolist())
                self.assertEqual(r[2].tolist(), np.round(ask, 5).tolist())
                self.assertEqual(r[3].tolist(), [0, 3, 1])

    def test_old_files_are_rewritten_and_read_the_same(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp)
            (feed / "ticks").mkdir()
            ts = ns(2024, 6, 12) + np.arange(5, dtype=np.int64) * S
            bid = np.array([1.08001, 1.08002, 1.08003, 1.08001, 1.08])
            old = pa.table({"ts": pa.array(ts.view("datetime64[ns]"), pa.timestamp("ns", tz="UTC")),
                            "bid": bid, "ask": bid + 0.00002, "flag": np.zeros(5, np.int8)})
            pq.write_table(old, feed / "ticks" / "2024-06-12.parquet")
            FeedStore(feed).save_manifest({"days": {"2024-06-12": day_entry("layer0", ts, np.zeros(5, np.int8))}})
            before = FeedStore(feed).read_day(date(2024, 6, 12))
            self.assertEqual(migrate_feed(feed, 10**5), 1)
            after = FeedStore(feed).read_day(date(2024, 6, 12))
            self.assertIn("ms", pq.read_schema(feed / "ticks" / "2024-06-12.parquet").names)
            self.assertEqual(after[0].tolist(), before[0].tolist())
            self.assertEqual(after[1].tolist(), np.round(before[1], 5).tolist())
            self.assertEqual(migrate_feed(feed, 10**5), 0)  # nothing left to do

    def test_updater_writes_the_instruments_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = ns(2025, 3, 6, 12)
            summary = dk.update(Path(tmp), "USDJPY", days=[date(2025, 3, 5)], scale=10**3, now=now, fetch=FakeFeed())
            self.assertEqual(summary["changed"], [date(2025, 3, 5)])
            ts, bid, ask, flag = FeedStore(Path(tmp)).read_day(date(2025, 3, 5))
            # FakeFeed sends 110_000 + hour points: 110.000 and up on the yen scale.
            self.assertEqual(bid[0], 110.0)
            self.assertEqual(FeedStore(Path(tmp)).manifest()["scale"], 10**3)


class CandlesFollowTicks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.feed = Path(self.tmp.name) / "EURUSD"
        self.store = FeedStore(self.feed, scale=10**5)
        rng = np.random.default_rng(5)
        manifest = {"days": {}}
        for k, day in enumerate([date(2025, 3, 3), date(2025, 3, 4), date(2025, 3, 5)]):
            n = 2000
            ts = day_start_ns(day) + np.sort(rng.integers(0, 86_400 * S, n))
            bid = np.round(1.1 + np.cumsum(rng.normal(0, 1e-5, n)), 5)
            flag = np.where(np.arange(n) % 97 == 0, 1, 0).astype(np.int8)  # some dropped ticks
            self.store.write_day(day, ts, bid, bid + 0.0001, flag)
            manifest["days"][day.isoformat()] = day_entry("dukascopy", ts, flag, complete=True)
        self.store.save_manifest(manifest)

    def tearDown(self):
        self.tmp.cleanup()

    def market(self):
        from simulator import server
        with mock.patch.object(server.config, "duka_dir", lambda s: self.feed):
            m = server.Market(instruments.get("EURUSD"))
        return m

    def test_a_day_that_grew_after_its_candles_is_found_and_rebuilt(self):
        m = self.market()
        m.build()
        self.assertEqual(m.stale_days(), [])
        # A download adds the day's last hours but stops before rebuilding.
        day = date(2025, 3, 4)
        ts, bid, ask, flag = self.store.read_day(day)
        extra = day_start_ns(day) + 86_399 * S + np.arange(3, dtype=np.int64)
        ts2 = np.r_[ts, extra]
        self.store.write_day(day, ts2, np.r_[bid, [1.2] * 3], np.r_[ask, [1.2001] * 3], np.r_[flag, [0, 0, 0]].astype(np.int8))
        manifest = self.store.manifest()
        manifest["days"][day.isoformat()] = day_entry("dukascopy", ts2, np.r_[flag, [0, 0, 0]].astype(np.int8), complete=True)
        self.store.save_manifest(manifest)
        self.assertEqual(m.stale_days(), [day])
        m.add_days(m.stale_days())
        self.assertEqual(m.stale_days(), [])
        fresh = BarStore.build(ParquetTickSource(self.feed))
        for tf in ("m1", "h1", "d1"):
            self.assertEqual(m.bars[tf].slice(0, 10**9), fresh[tf].slice(0, 10**9))

    def test_days_arriving_newest_first_give_the_same_candles(self):
        source = ParquetTickSource(self.feed)
        bars = BarStore.empty()
        for day in reversed(source.days()):
            bars.replace_days(source, [day])
        full = BarStore.build(source)
        for tf in ("m1", "m15", "d1"):
            self.assertEqual(bars[tf].slice(0, 10**9), full[tf].slice(0, 10**9))


class Money(unittest.TestCase):
    """Profit in a USD account, checked by hand."""

    def fill(self, broker, bid, ask, t=1):
        broker.on_tick(t * S, bid, ask)

    def test_usd_quoted_silver_and_oil_use_their_contract(self):
        b = Broker.for_instrument(instruments.get("XAGUSD"), balance=100_000.0)
        self.fill(b, 29.870, 29.900)
        b.place("buy", "market", 1 * S, lots=0.1)
        self.fill(b, 29.950, 29.980, 2)            # bought 0.1 lot = 500 oz at 29.980
        self.fill(b, 30.100, 30.130, 3)
        b.close(1, 3 * S)
        self.fill(b, 30.080, 30.110, 4)            # sold at the Bid, 30.080
        t = b.drain_events()[-1]["trade"]
        self.assertEqual(t["gross_usd"], round((30.080 - 29.980) * 500, 2))  # 50.00
        self.assertEqual(t["pips"], 10.0)                                     # pip 0.01
        wti = Broker.for_instrument(instruments.get("WTI"), balance=100_000.0)
        self.fill(wti, 78.00, 78.04)
        self.assertEqual(wti.pip_value, 10.0)  # 0.01 x 1,000 barrels

    def test_usdjpy_converts_at_the_exit_price(self):
        b = Broker.for_instrument(instruments.get("USDJPY"), balance=10_000.0)
        self.fill(b, 150.000, 150.010)
        b.place("buy", "market", 1 * S, lots=1)
        self.fill(b, 150.000, 150.010, 2)          # long at 150.010
        b.close(1, 2 * S)
        self.fill(b, 151.010, 151.020, 3)          # out at 151.010: +1.000 yen x 100,000
        t = b.drain_events()[-1]["trade"]
        self.assertEqual(t["gross_usd"], round(100_000 / 151.010, 2))
        # Margin on USD/JPY is the USD notional over the leverage.
        self.assertEqual(round(b._margin(1, 150.0), 2), round(100_000 / 30.0, 2))

    def test_crosses_convert_through_another_instrument(self):
        rate = mock.Mock(return_value=1 / 150.0)   # USD per yen, from USD/JPY
        b = Broker.for_instrument(instruments.get("EURJPY"), balance=10_000.0, rate_fn=rate)
        self.fill(b, 162.000, 162.020)
        self.assertAlmostEqual(b.pip_value, 0.01 * 100_000 / 150.0)  # 6.67 USD a pip a lot
        b.place("sell", "market", 1 * S, lots=0.5)
        self.fill(b, 162.000, 162.020, 2)          # short at 162.000
        b.close(1, 2 * S)
        self.fill(b, 161.480, 161.500, 3)          # out at the Ask, 161.500
        t = b.drain_events()[-1]["trade"]
        self.assertEqual(t["gross_usd"], round(0.5 * 50_000 / 150.0, 2))
        rate.assert_called()

    def test_stock_indices_are_money_a_point_in_their_own_currency(self):
        sp = Broker.for_instrument(instruments.get("US500"), balance=100_000.0)
        self.fill(sp, 6700.00, 6700.40)
        self.assertEqual(sp.pip_value, 1.0)                       # 1 USD a point a lot
        self.assertEqual(round(sp._margin(1, 6700.0), 2), round(6700.0 / 30.0, 2))
        # The DAX in euros, through EUR/USD at 1.08.
        dax = Broker.for_instrument(instruments.get("GER40"), balance=100_000.0, rate_fn=lambda ns: 1.08)
        self.fill(dax, 24000.0, 24001.0)
        self.assertAlmostEqual(dax.pip_value, 1.08)
        dax.place("buy", "market", 1 * S, lots=2)
        self.fill(dax, 24000.0, 24001.0, 2)                       # long 2 lots at 24,001
        dax.close(1, 2 * S)
        self.fill(dax, 24051.0, 24052.0, 3)                       # out at the Bid, +50 points
        self.assertEqual(dax.drain_events()[-1]["trade"]["gross_usd"], round(50 * 2 * 1.08, 2))
        # The Nikkei: 100 yen a point, through USD/JPY at 155.
        nk = Broker.for_instrument(instruments.get("JP225"), balance=100_000.0, rate_fn=lambda ns: 1 / 155.0)
        self.fill(nk, 50000.0, 50010.0)
        self.assertAlmostEqual(nk.pip_value, 100 / 155.0)

    def test_every_instrument_can_be_valued_in_usd(self):
        for inst in instruments.INSTRUMENTS.values():
            self.assertTrue(inst.quote == "USD" or inst.base == "USD" or inst.quote in instruments.CONVERSION, inst.symbol)
            conv = instruments.CONVERSION.get(inst.quote)
            if conv and inst.base != "USD":
                self.assertIn(conv[0], instruments.INSTRUMENTS)

    def test_a_cross_without_a_rate_is_refused_not_guessed(self):
        b = Broker.for_instrument(instruments.get("EURGBP"), balance=10_000.0, rate_fn=lambda ns: None)
        self.fill(b, 0.84000, 0.84010)
        with self.assertRaises(OrderError):
            b.place("buy", "market", 1 * S, lots=0.1)

    def test_an_old_session_is_still_eurusd(self):
        state = Broker(balance=5000.0).to_state()
        for k in ("symbol", "pip", "contract_size", "digits", "base_ccy", "quote_ccy", "last_rate"):
            state.pop(k)
        b = Broker.from_state(state)
        self.assertEqual((b.symbol, b.pip, b.contract_size, b.quote_ccy), ("EURUSD", 0.0001, 100_000, "USD"))


SCHEMA = """
CREATE TABLE calendar_data_indicator (id INTEGER PRIMARY KEY, currency TEXT, importance INTEGER, name TEXT,
  concept TEXT, frequency TEXT, unit TEXT, explainer_md TEXT, transmission_channel TEXT, mapping_status TEXT);
CREATE TABLE calendar_data_releasegroup (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE calendar_data_eventrelease (id INTEGER PRIMARY KEY, indicator_id INTEGER, release_group_id INTEGER,
  reference_period TEXT, release_time_utc TEXT, scheduled_time_utc TEXT, actual_first_print REAL, actual_current REAL,
  forecast_point_in_time REAL, forecast_stored REAL, previous REAL, revised_previous REAL,
  timestamp_confidence TEXT, forecast_provenance TEXT, confounded INTEGER, source_map_json TEXT);
"""


class EconomicCalendar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite3"
        db = sqlite3.connect(self.db)
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO calendar_data_indicator VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (1, "USD", 3, "Non-Farm Employment Change", "labour", "", "", "", "", "auto"),
            (2, "USD", 3, "Unemployment Rate", "labour", "", "", "", "", "auto"),
            (3, "JPY", 1, "Leading Indicators", "", "", "", "", "", "auto"),
            (4, "USD", 0, "Bank Holiday", "", "", "", "", "", "auto"),
        ])
        db.executemany("INSERT INTO calendar_data_eventrelease VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (10, 1, None, "release:2024-05-03T12:30", "2024-05-03 12:30:00", None, 175000, 175000, None, 238000, 303000, None, "minute", "", 0, "{}"),
            (11, 1, None, "release:2024-06-07T12:30", "2024-06-07 12:30:00", None, 272000, 290000, None, 182000, 175000, 165000, "minute", "", 0, '{"actual_current": "forexfactory_pages"}'),
            (12, 2, None, "release:2024-06-07T12:30", "2024-06-07 12:30:00", None, 4.0, 4.0, None, 3.9, 3.9, None, "minute", "", 0, "{}"),
            (13, 1, None, "release:2024-07-05T12:30", "2024-07-05 12:30:00", None, 206000, 206000, None, 190000, 272000, None, "minute", "", 0, "{}"),
            (14, 3, None, "release:2024-06-07T05:00", "2024-06-07 05:00:00", None, 111.6, 111.6, None, 111.4, 112.2, None, "minute", "", 0, "{}"),
            (15, 4, None, "x", "2024-06-07 00:00:00", None, None, None, None, None, None, None, "minute", "", 0, "{}"),
            (16, 1, None, "2024-08", None, "2024-08-02 00:00:00", None, None, None, None, None, None, "date_only", "", 0, "{}"),
        ])
        db.commit()
        db.close()
        self.cal = Calendar(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_rated_timed_events_of_the_wanted_currencies(self):
        events = self.cal.window(ns(2024, 6, 7), ns(2024, 6, 8), ("EUR", "USD"), None)
        self.assertEqual([e["id"] for e in events], [11, 12])  # no JPY, no holiday, no date-only

    def test_the_actual_exists_only_after_the_clock_passes_it(self):
        before = self.cal.detail(11, ns(2024, 6, 7, 12, 29))
        self.assertFalse(before["released"])
        self.assertIsNone(before["actual"])
        self.assertIsNone(before["revised_previous"])
        self.assertEqual((before["forecast"], before["previous"]), ("182K", "175K"))  # known ahead
        after = self.cal.detail(11, ns(2024, 6, 7, 12, 30))
        # The first print, not the later revision (290K) the database also holds.
        self.assertEqual((after["actual"], after["revised_previous"], after["surprise"]), ("272K", "165K", "above forecast"))
        self.assertEqual([x["id"] for x in after["same_time"]], [12])

    def test_history_stops_at_the_clock(self):
        d = self.cal.detail(11, ns(2024, 6, 7, 13))
        self.assertEqual([h["actual"] for h in d["history"]], ["175K"])  # not July's
        window = self.cal.window(ns(2024, 7, 1), ns(2024, 7, 6), ("USD",), ns(2024, 6, 7, 13))
        self.assertEqual([(e["id"], e["released"]) for e in window], [(13, False)])

    def test_numbers_print_as_the_calendar_sites_do(self):
        self.assertEqual(fmt_value(22_100_000_000), "22.1B")
        self.assertEqual(fmt_value(-7_600_000_000), "-7.6B")
        self.assertEqual(fmt_value(0.4, "Average Hourly Earnings m/m"), "0.4%")
        self.assertEqual(fmt_value(4.0, "Unemployment Rate"), "4%")
        self.assertEqual(fmt_value(111.6, "Leading Indicators"), "111.6")


class ReplayData(unittest.TestCase):
    """The replay data a tab chose: played when it has the instrument's
    ticks, else Dukascopy, saying so."""

    @staticmethod
    def feed(key, label, days):
        from simulator import server
        src = mock.Mock()
        src.days.return_value = days
        return server.Feed(key, label, src, None, live=key != "dukascopy")

    def test_the_chosen_data_or_dukascopy_with_a_notice(self):
        from simulator import server
        day = [date(2025, 3, 4)]
        duka = self.feed("dukascopy", "Dukascopy history", day)
        ftmo = self.feed("mt5:ftmo-demo", "MT5 FTMO-Demo", day)
        quiet = self.feed("mt5:ft-server", "MT5 FT-Server", [])  # nothing recorded yet
        feeds = {"EURUSD": {"dukascopy": duka, "mt5:ftmo-demo": ftmo, "mt5:ft-server": quiet},
                 "JP225": {"dukascopy": duka}}
        eurusd, nikkei = instruments.get("EURUSD"), instruments.get("JP225")
        with mock.patch.object(server.hub, "feeds", lambda s: feeds.get(s, {})), \
                mock.patch.object(server.hub, "replay_feed", lambda s: duka):
            self.assertEqual(server.chosen_replay_feed(eurusd, "mt5:ftmo-demo"), (ftmo, None))
            self.assertEqual(server.chosen_replay_feed(eurusd, "dukascopy"), (duka, None))
            self.assertEqual(server.chosen_replay_feed(eurusd, None), (duka, None))
            self.assertEqual(server.chosen_replay_feed(nikkei, "mt5:ftmo-demo"),
                             (duka, "MT5 FTMO-Demo has no Nikkei 225 (Japan) ticks; replaying Dukascopy history instead."))
            self.assertEqual(server.chosen_replay_feed(eurusd, "mt5:ft-server"),
                             (duka, "MT5 FT-Server has no EUR/USD ticks; replaying Dukascopy history instead."))
            self.assertEqual(server.chosen_replay_feed(eurusd, "mt5:gone")[1],
                             "mt5:gone has no EUR/USD ticks; replaying Dukascopy history instead.")


if __name__ == "__main__":
    unittest.main()
