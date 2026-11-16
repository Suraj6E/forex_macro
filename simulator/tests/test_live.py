"""Live data: the MT5 recorder against a fake terminal, and live sessions.

The fake terminal keeps tick times in server time (UTC+2 here), as the
MetaTrader5 package does, so the tests check the conversion to UTC, the
same-millisecond bookkeeping, day rollover and compaction, and the backfill
after the recorder was off.
"""

import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from simulator.data.ticks import NS_PER_SECOND, day_of_ns
from simulator.engine.broker import Broker
from simulator.engine.session import ReplaySession, SessionError
from simulator.live import mt5rec
from simulator.live.feed import FeedBars, LiveFeedSource
from simulator.live.mt5rec import MT5Recorder, measure_offset_ms

OFFSET_MS = 2 * 3600 * 1000
TICK_DTYPE = np.dtype([("time", "<i8"), ("bid", "<f8"), ("ask", "<f8"), ("last", "<f8"), ("volume", "<u8"),
                       ("time_msc", "<i8"), ("flags", "<u4"), ("volume_real", "<f8")])
RATES_DTYPE = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                        ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])


class FakeMT5:
    COPY_TICKS_ALL = -1
    TIMEFRAME_H1 = 16385

    def __init__(self, clock):
        self.clock = clock
        self.ticks = []  # (server msc, bid, ask), ascending
        self.lock = threading.Lock()
        self.calls = []

    def add_utc(self, utc_ms, bid, ask):
        with self.lock:
            self.ticks.append((utc_ms + OFFSET_MS, bid, ask))
            self.ticks.sort(key=lambda t: t[0])

    def _visible(self):
        now_server = int(self.clock() * 1000) + OFFSET_MS
        return [t for t in self.ticks if t[0] <= now_server]

    def _array(self, rows):
        a = np.zeros(len(rows), TICK_DTYPE)
        for i, (msc, bid, ask) in enumerate(rows):
            a[i]["time"], a[i]["time_msc"], a[i]["bid"], a[i]["ask"] = msc // 1000, msc, bid, ask
        return a

    def initialize(self, path=None):
        self.calls.append(("initialize", path))
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (0, "ok")

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def account_info(self):
        return SimpleNamespace(server="Test-Demo", company="Test Co", trade_mode=0, login=1)

    def symbol_info(self, name):
        return SimpleNamespace(digits=5) if name == "EURUSD.r" else None

    def symbols_get(self, group):
        return [SimpleNamespace(name="EURUSD.raw", currency_base="EUR", currency_profit="USD"),
                SimpleNamespace(name="EURUSD.r", currency_base="EUR", currency_profit="USD"),
                SimpleNamespace(name="EURGBP", currency_base="EUR", currency_profit="GBP")]

    def symbol_select(self, name, on):
        return True

    def symbol_info_tick(self, name):
        with self.lock:
            v = self._visible()
        if not v:
            return None
        msc, bid, ask = v[-1]
        return SimpleNamespace(time_msc=msc, bid=bid, ask=ask)

    def copy_ticks_from(self, name, from_s, count, flags):
        with self.lock:
            rows = [t for t in self._visible() if t[0] >= from_s * 1000][:count]
        return self._array(rows)

    def copy_ticks_range(self, name, from_s, to_s, flags):
        with self.lock:
            rows = [t for t in self._visible() if from_s * 1000 <= t[0] < to_s * 1000]
        return self._array(rows)

    def copy_rates_range(self, name, timeframe, from_s, to_s):
        """Hourly candles built from the quotes, opening from `from_s` to
        `to_s` inclusive, as the terminal returns them."""
        assert timeframe == self.TIMEFRAME_H1
        counts = {}
        with self.lock:
            for msc, _bid, _ask in self._visible():
                h = msc // 3_600_000 * 3600
                if from_s <= h <= to_s:
                    counts[h] = counts.get(h, 0) + 1
        a = np.zeros(len(counts), RATES_DTYPE)
        for i, h in enumerate(sorted(counts)):
            a[i]["time"], a[i]["tick_volume"] = h, counts[h]
        return a


def wait_until(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class Offset(unittest.TestCase):
    def test_measured_from_a_fresh_tick(self):
        now = 1_700_000_000_000
        self.assertEqual(measure_offset_ms(now + OFFSET_MS - 1500, now), OFFSET_MS)
        self.assertEqual(measure_offset_ms(now - 400, now), 0)
        self.assertEqual(measure_offset_ms(now + 3 * 3600_000 - 20_000, now), 3 * 3600_000)

    def test_a_stale_tick_is_never_trusted(self):
        # The last tick of Friday, read an hour after the close, would look
        # like a fresh tick under a +1h offset. The recorder waits for the
        # latest tick to change instead, and a closed market never does.
        api = FakeMT5(time.time)
        api.add_utc(int(time.time() * 1000) - 3600_000, 1.1, 1.10008)
        rec = MT5Recorder("x", api=api, offset_wait=0.6, terminals=lambda: [])
        rec.broker_symbol = "EURUSD.r"
        self.assertIsNone(rec._measure_offset())

    def test_measured_once_a_new_tick_arrives(self):
        api = FakeMT5(time.time)
        api.add_utc(int(time.time() * 1000) - 3600_000, 1.1, 1.10008)
        rec = MT5Recorder("x", api=api, offset_wait=3, terminals=lambda: [])
        rec.broker_symbol = "EURUSD.r"
        threading.Timer(0.5, lambda: api.add_utc(int(time.time() * 1000), 1.2, 1.20008)).start()
        self.assertEqual(rec._measure_offset(), OFFSET_MS)


class Recorder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.api = FakeMT5(time.time)
        now = int(time.time() * 1000)
        # Two days of history every 20 s, plus three quotes in one millisecond.
        t = now - 2 * 86_400_000
        price = 1.1
        while t < now - 5000:
            price += 0.00001 * ((t // 20_000) % 7 - 3)
            self.api.add_utc(t, round(price, 5), round(price + 0.00008, 5))
            t += 20_000
        self.same_ms = now - 3000
        for k in range(3):
            self.api.add_utc(self.same_ms, 1.2 + k * 0.00001, 1.20008 + k * 0.00001)

    def tearDown(self):
        self.stop_ticker()
        self.tmp.cleanup()

    def start_ticker(self):
        """A moving market: a new quote every 100 ms."""
        self._ticking = threading.Event()

        def run():
            while not self._ticking.wait(0.1):
                self.api.add_utc(int(time.time() * 1000), 1.21, 1.21008)
        threading.Thread(target=run, daemon=True).start()

    def stop_ticker(self):
        if getattr(self, "_ticking", None):
            self._ticking.set()
            time.sleep(0.15)
            self._ticking = None

    def recorder(self):
        return MT5Recorder("C:/fake/terminal64.exe", root=self.root, backfill_days=3, api=self.api,
                           terminals=lambda: ["C:/fake/terminal64.exe"], offset_wait=3)

    def recorded(self, feed):
        src = LiveFeedSource(feed, keep_flags=(0, 1, 2, 3, 4))
        days = src.days()
        ts = np.concatenate([src.load_day(d).ts for d in days])
        bid = np.concatenate([src.load_day(d).bid for d in days])
        return ts // 1_000_000, bid

    def test_records_backfills_converts_and_compacts(self):
        self.start_ticker()
        rec = self.recorder()
        rec.start()
        self.assertTrue(wait_until(lambda: rec.status.get("state") == "recording"), rec.status)
        live_tick = int(time.time() * 1000) + 300
        self.api.add_utc(live_tick, 1.3, 1.30008)
        time.sleep(1.0)
        self.stop_ticker()
        time.sleep(1.0)  # let the last poll pick up the last quote
        rec.stop()

        feed = self.root / "test-demo" / "EURUSD"
        self.assertEqual(rec.broker_symbol, "EURUSD.r")
        self.assertEqual(rec.offset_ms, OFFSET_MS)
        self.assertEqual(rec.status["account_type"], "demo")
        # Only the terminal the trader opened was attached to; none was started.
        self.assertEqual(self.api.calls, [("initialize", "C:/fake/terminal64.exe")])

        ms, bid = self.recorded(feed)
        expected = sorted(t[0] - OFFSET_MS for t in self.api.ticks)
        self.assertEqual(ms.tolist(), expected)  # UTC, every tick, no duplicates
        self.assertEqual(int((ms == self.same_ms).sum()), 3)
        self.assertIn(live_tick, ms.tolist())
        # Closed days are cleaned into Parquet; only today stays a CSV.
        today = day_of_ns(int(time.time() * 1e9))
        self.assertEqual([p.stem for p in (feed / "live").glob("*.csv")], [today.isoformat()])
        self.assertTrue((feed / "ticks" / f"{(today - timedelta(days=1)).isoformat()}.parquet").exists())

    def test_a_restart_fills_the_gap_without_duplicates(self):
        self.start_ticker()
        rec = self.recorder()
        rec.start()
        self.assertTrue(wait_until(lambda: rec.status.get("state") == "recording"))
        self.stop_ticker()
        time.sleep(1.0)
        rec.stop()
        # While no recorder runs: one more quote in the millisecond of the last
        # recorded tick (it must be kept, the others there not repeated), then
        # five new ones.
        last_utc = max(t[0] for t in self.api.ticks) - OFFSET_MS
        self.api.add_utc(last_utc, 1.20003, 1.20011)
        now = int(time.time() * 1000)
        for k in range(5):
            self.api.add_utc(now + k, 1.25, 1.25008)
        self.start_ticker()
        time.sleep(0.1)
        rec2 = self.recorder()
        rec2.start()
        self.assertTrue(wait_until(lambda: rec2.status.get("state") == "recording"))
        self.stop_ticker()
        time.sleep(1.0)
        rec2.stop()
        ms, _bid = self.recorded(self.root / "test-demo" / "EURUSD")
        self.assertEqual(ms.tolist(), sorted(t[0] - OFFSET_MS for t in self.api.ticks))

    def test_waits_for_a_terminal_and_never_starts_one(self):
        rec = MT5Recorder("C:/fake/terminal64.exe", root=self.root, api=self.api, terminals=lambda: [])
        rec.start()
        self.assertTrue(wait_until(lambda: rec.status.get("state") == "waiting"))
        rec.stop()
        self.assertEqual(self.api.calls, [])

    def test_a_half_written_line_is_cut_before_appending(self):
        feed = self.root / "test-demo" / "EURUSD" / "live"
        feed.mkdir(parents=True)
        today = day_of_ns(int(time.time() * 1e9)).isoformat()
        (feed / f"{today}.csv").write_bytes(b"1,1.1,1.10008\n1700000000123,1.1")
        mt5rec._trim_partial_line(feed / f"{today}.csv")
        self.assertEqual((feed / f"{today}.csv").read_bytes(), b"1,1.1,1.10008\n")


def utc_ms(y, m, d, h=0, mi=0):
    from datetime import datetime, timezone
    return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


class BrokerClock(unittest.TestCase):
    def test_new_york_summer_time_edges(self):
        # 2024: from 10 March 07:00 UTC to 3 November 06:00 UTC.
        self.assertFalse(mt5rec.us_dst(utc_ms(2024, 3, 10, 6, 59)))
        self.assertTrue(mt5rec.us_dst(utc_ms(2024, 3, 10, 7, 0)))
        self.assertTrue(mt5rec.us_dst(utc_ms(2024, 11, 3, 5, 59)))
        self.assertFalse(mt5rec.us_dst(utc_ms(2024, 11, 3, 6, 0)))
        # Before 2007: first Sunday of April to last Sunday of October.
        self.assertFalse(mt5rec.us_dst(utc_ms(2005, 3, 20)))
        self.assertTrue(mt5rec.us_dst(utc_ms(2005, 4, 3, 7)))
        self.assertFalse(mt5rec.us_dst(utc_ms(2005, 10, 30, 6)))

    def test_history_uses_the_offset_of_its_own_day(self):
        winter = utc_ms(2026, 1, 14, 12)
        clock = mt5rec.ServerClock.measured(2 * 3600_000, winter)  # measured +2h in winter
        self.assertEqual(clock.rule, "ny+7")
        summer = utc_ms(2025, 7, 16, 12)
        self.assertEqual(clock.offset(summer), 3 * 3600_000)
        self.assertEqual(clock.to_utc(np.array([summer + 3 * 3600_000])).tolist(), [summer])
        self.assertEqual(clock.to_utc(np.array([winter + 2 * 3600_000])).tolist(), [winter])

    def test_a_broker_on_another_clock_keeps_its_measured_offset(self):
        clock = mt5rec.ServerClock.measured(0, utc_ms(2026, 1, 14, 12))
        self.assertEqual(clock.rule, "fixed")
        self.assertEqual(clock.offset(utc_ms(2025, 7, 16, 12)), 0)

    def test_the_fx_week_opens_at_five_in_new_york(self):
        # 17:00 EST is 22:00 UTC; 17:00 EDT is 21:00 UTC. A weekend belongs
        # to the week before it.
        self.assertEqual(mt5rec.week_open_ms(utc_ms(2026, 11, 18, 9)), utc_ms(2026, 11, 15, 22))
        self.assertEqual(mt5rec.week_open_ms(utc_ms(2026, 11, 15, 21, 59)), utc_ms(2026, 11, 8, 22))
        self.assertEqual(mt5rec.week_open_ms(utc_ms(2026, 7, 15, 9)), utc_ms(2026, 7, 12, 21))
        # US clocks went back on 1 November 2026, before that Sunday's open.
        self.assertEqual(mt5rec.week_open_ms(utc_ms(2026, 11, 2)), utc_ms(2026, 11, 1, 22))
        # A quote just before the Sunday open counts for the coming week.
        self.assertEqual(mt5rec.quote_week(utc_ms(2026, 11, 15, 21, 58)), utc_ms(2026, 11, 15, 22))
        self.assertTrue(mt5rec.eu_summer(utc_ms(2026, 3, 29, 1)))
        self.assertFalse(mt5rec.eu_summer(utc_ms(2026, 3, 29, 0, 59)))
        self.assertFalse(mt5rec.eu_summer(utc_ms(2026, 10, 25, 1)))

    def test_reads_each_weeks_offset_from_hourly_candles(self):
        def candles(offset_of, first=date(2024, 1, 7), last=date(2026, 11, 8), early_close=None, holiday=None):
            out = []
            d = first
            while d <= last:
                opened = mt5rec._sunday_open_ms(d)
                off = offset_of(opened)
                closes = opened + 5 * 86_400_000 - (3 * 3600_000 if d == early_close else 0)
                h = opened
                while h < closes:
                    gone = holiday and holiday[0] <= h < holiday[1]
                    if not gone:
                        out.append(h + off)
                    h += 3600_000
                d += timedelta(days=7)
            return np.array(out, np.int64)

        ny7 = mt5rec.week_offsets(candles(mt5rec.ny7_offset_ms))
        # Every week but the first (its open is not in) and the last (nor its close).
        self.assertEqual(len(ny7), 147)
        clock = mt5rec.ServerClock(ny7)
        self.assertEqual(clock.rule, "ny+7")
        self.assertIn("fits all 147 weeks", clock.describe())
        self.assertEqual(mt5rec.ServerClock(mt5rec.week_offsets(candles(mt5rec.eet_offset_ms))).rule, "eu")
        self.assertEqual(mt5rec.ServerClock(mt5rec.week_offsets(candles(lambda w: 3 * 3600_000))).rule, "fixed")

        # A week that closed early for a holiday, and a weekday holiday long
        # enough to look like a weekend, are not read; the rest still are.
        christmas = (utc_ms(2024, 12, 24, 18), utc_ms(2024, 12, 26, 12))
        weeks = mt5rec.week_offsets(candles(mt5rec.ny7_offset_ms, early_close=date(2025, 3, 2), holiday=christmas))
        self.assertNotIn(mt5rec._sunday_open_ms(date(2025, 3, 2)), weeks)
        self.assertEqual(len(weeks), 146)
        self.assertEqual(mt5rec.ServerClock(weeks).rule, "ny+7")

        # A broker that moved from a fixed UTC+2 to New York + 7 in 2025:
        # no rule fits, so every week keeps its own offset.
        moved = utc_ms(2025, 6, 1)
        weeks = mt5rec.week_offsets(candles(lambda w: mt5rec.ny7_offset_ms(w) if w >= moved else 2 * 3600_000))
        clock = mt5rec.ServerClock(weeks)
        self.assertEqual(clock.rule, "weeks")
        july24, july25 = utc_ms(2024, 7, 17, 12), utc_ms(2025, 7, 16, 12)
        self.assertEqual(clock.offset(july24), 2 * 3600_000)
        self.assertEqual(clock.offset(july25), 3 * 3600_000)
        self.assertEqual(clock.to_utc([july24 + 2 * 3600_000, july25 + 3 * 3600_000]).tolist(), [july24, july25])

    def test_converts_across_a_clock_change(self):
        clock = mt5rec.ServerClock.measured(2 * 3600_000, utc_ms(2026, 11, 18, 12))
        friday = utc_ms(2026, 10, 30, 20)   # last quotes on UTC+3
        sunday = utc_ms(2026, 11, 1, 22, 5)  # first quotes on UTC+2
        server = [friday + 3 * 3600_000, sunday + 2 * 3600_000]
        self.assertEqual(clock.to_utc(server).tolist(), [friday, sunday])
        self.assertEqual([clock.to_server(friday), clock.to_server(sunday)], server)


class NY7FakeMT5(FakeMT5):
    """A broker whose clock is New York + 7 h: UTC+2 in winter, +3 in summer."""

    def add_utc(self, utc_ms_, bid, ask):
        with self.lock:
            self.ticks.append((utc_ms_ + mt5rec.ny7_offset_ms(utc_ms_), bid, ask))

    def sort(self):
        self.ticks.sort(key=lambda t: t[0])

    def _visible(self):
        now = int(self.clock() * 1000)
        now_server = now + mt5rec.ny7_offset_ms(now)
        return [t for t in self.ticks if t[0] <= now_server]

    def symbol_info(self, name):
        return SimpleNamespace(digits=5) if name in ("EURUSD.r",) else SimpleNamespace(digits=2) if name == "USOIL.cash" else None

    def symbols_get(self, group):
        return [SimpleNamespace(name="EURUSD.r", currency_base="EUR", currency_profit="USD"),
                SimpleNamespace(name="USOIL.cash", currency_base="USD", currency_profit="USD"),
                SimpleNamespace(name="UKOIL.cash", currency_base="USD", currency_profit="USD")]


class History(unittest.TestCase):
    """The broker's tick history, fetched day by day, newest first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._waits = mt5rec.HISTORY_SETTLE_WAIT, mt5rec.HISTORY_RECHECK_WAIT
        mt5rec.HISTORY_SETTLE_WAIT = mt5rec.HISTORY_RECHECK_WAIT = 0.0
        self._ticking = threading.Event()
        self.api = NY7FakeMT5(time.time)

    def tearDown(self):
        mt5rec.HISTORY_SETTLE_WAIT, mt5rec.HISTORY_RECHECK_WAIT = self._waits
        self._ticking.set()
        self.tmp.cleanup()

    def add_weeks(self, until_ms):
        """The broker's history starts with the week that opened ~10 weeks
        back: a quote every 30 minutes from Sunday 17:00 to Friday 17:00 New
        York, summer and winter alike, up to `until_ms`."""
        now = int(time.time() * 1000)
        self.today = day_of_ns(now * 1_000_000)
        sunday = self.today - timedelta(days=70 + (self.today.weekday() + 1) % 7)
        self.first = sunday
        self.added = []
        while True:
            opened = mt5rec._sunday_open_ms(sunday)
            if opened >= until_ms:
                break
            t = opened
            while t < min(opened + 5 * 86_400_000, until_ms):
                self.api.add_utc(t, 1.1 + (t // 1_800_000 % 50) * 1e-5, 1.10008 + (t // 1_800_000 % 50) * 1e-5)
                self.added.append(t)
                t += 1_800_000
            sunday += timedelta(days=7)
        self.api.sort()

    def run_recorder(self):
        start = self.first - timedelta(days=60)  # earlier than the broker has
        rec = MT5Recorder("C:/fake/terminal64.exe", root=self.root, backfill_days=1, api=self.api,
                          terminals=lambda: ["C:/fake/terminal64.exe"], offset_wait=1, symbols=["EURUSD"],
                          history_from=lambda s: start)
        rec.start()
        self.assertTrue(wait_until(lambda: rec.track("EURUSD") and rec.track("EURUSD").history["state"] == "done", 60),
                        rec.status)
        rec.stop()
        self.assertEqual(rec.server_clock.rule, "ny+7")
        self.assertIn("weeks read from the broker's candles", rec.server_clock.describe())
        feed = self.root / "test-demo" / "EURUSD"
        src = LiveFeedSource(feed, keep_flags=(0, 1, 2, 3, 4))
        got = set()
        yesterday = self.today - timedelta(days=1)
        for d in src.days():
            if d < yesterday:
                got |= set((src.load_day(d).ts // 1_000_000).tolist())
        expected = {t for t in self.added if t < utc_ms(yesterday.year, yesterday.month, yesterday.day)}
        # Every quote, at its true UTC time, whatever the season.
        self.assertEqual(got, expected)
        manifest = src.store.manifest()
        # The day after the last empty weekday: the Saturday before the
        # broker's first Sunday. Nothing earlier exists at this broker.
        self.assertEqual(manifest["history_floor"], (self.first - timedelta(days=1)).isoformat())
        # Nothing older than the floor is asked for again.
        self.assertTrue(all(d >= start.isoformat() for d in manifest["days"]))
        return rec

    def test_fetches_history_in_utc_back_to_where_the_broker_ends(self):
        self.add_weeks(int(time.time() * 1000))

        def tick():
            while not self._ticking.wait(0.1):
                with self.api.lock:
                    now = int(time.time() * 1000)
                    self.api.ticks.append((now + mt5rec.ny7_offset_ms(now), 1.2, 1.20008))
        threading.Thread(target=tick, daemon=True).start()
        self.run_recorder()

    def test_with_the_market_shut_the_clock_comes_from_the_candles(self):
        # The weekend: the last quote was Friday's close, and none comes.
        self.add_weeks(mt5rec.week_open_ms(int(time.time() * 1000)) - 2 * 86_400_000)
        rec = self.run_recorder()
        self.assertIsNone(rec._measure_offset())  # no live quote to read it from

    def test_a_day_missing_hours_is_read_again_before_it_is_kept(self):
        self.add_weeks(mt5rec.week_open_ms(int(time.time() * 1000)) - 2 * 86_400_000)
        # The terminal is still downloading one day: its afternoon is missing
        # on the first two reads.
        day = date(1970, 1, 1) + timedelta(days=self.added[len(self.added) // 2] // 86_400_000)
        if day.weekday() >= 5:
            day -= timedelta(days=day.weekday() - 3)
        afternoon = utc_ms(day.year, day.month, day.day, 12) + mt5rec.ny7_offset_ms(utc_ms(day.year, day.month, day.day))
        hidden = {"left": 2}
        real = self.api.copy_ticks_range

        def copy_ticks_range(name, from_s, to_s, flags):
            ticks = real(name, from_s, to_s, flags)
            if from_s * 1000 <= afternoon < to_s * 1000 and hidden["left"]:
                hidden["left"] -= 1
                return ticks[ticks["time_msc"] < afternoon]
            return ticks
        self.api.copy_ticks_range = copy_ticks_range
        rec = self.run_recorder()
        self.assertEqual(hidden["left"], 0)
        self.assertNotIn("missing_hours", rec.track("EURUSD").store.manifest()["days"][day.isoformat()])

    def test_finds_the_brokers_name_for_oil(self):
        rec = MT5Recorder("x", api=self.api, terminals=lambda: [])
        from simulator.instruments import INSTRUMENTS
        self.assertEqual(rec._find_symbol(INSTRUMENTS["WTI"]), "USOIL.cash")
        self.assertEqual(rec._find_symbol(INSTRUMENTS["BRENT"]), "UKOIL.cash")
        self.assertEqual(rec._find_symbol(INSTRUMENTS["EURUSD"]), "EURUSD.r")
        self.assertIsNone(rec._find_symbol(INSTRUMENTS["XAGUSD"]))


class Streaming(unittest.TestCase):
    """Live ticks reach listeners (live sessions, the quote board) within a
    poll or two, also while broker history is being fetched with its real
    waits between reads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.api = NY7FakeMT5(time.time)
        self._ticking = threading.Event()

    def tearDown(self):
        self._ticking.set()
        self.tmp.cleanup()

    def test_ticks_reach_listeners_at_once_while_history_is_fetched(self):
        History.add_weeks(self, mt5rec.week_open_ms(int(time.time() * 1000)) - 2 * 86_400_000)
        start = self.first - timedelta(days=60)
        arrivals = []

        def on_ticks(symbol):
            t = rec.track(symbol)
            arrivals.append(time.time() * 1000 - t.last_ms)

        rec = MT5Recorder("C:/fake/terminal64.exe", root=self.root, backfill_days=1, api=self.api,
                          terminals=lambda: ["C:/fake/terminal64.exe"], offset_wait=1, symbols=["EURUSD"],
                          history_from=lambda s: start, on_ticks=on_ticks)
        rec.start()
        self.assertTrue(wait_until(lambda: rec.status.get("state") == "recording", 20), rec.status)

        def tick():  # a quote every 50 ms, stamped now
            while not self._ticking.wait(0.05):
                with self.api.lock:
                    now = int(time.time() * 1000)
                    self.api.ticks.append((now + mt5rec.ny7_offset_ms(now), 1.2, 1.20008))
        threading.Thread(target=tick, daemon=True).start()
        time.sleep(3.0)
        self._ticking.set()
        history = dict(rec.track("EURUSD").history)
        quotes = rec.quotes()
        stats = rec.live_stats()
        rec.stop()

        self.assertEqual(history["state"], "fetching")  # history ran all along
        self.assertGreater(len(arrivals), 30)
        delays = np.array(arrivals)
        self.assertLess(np.median(delays), 60, delays)
        self.assertLess(np.percentile(delays, 90), 150, delays)
        self.assertEqual(quotes[0]["symbol"], "EURUSD")
        self.assertEqual((quotes[0]["bid"], quotes[0]["ask"]), (1.2, 1.20008))
        self.assertLess(stats["delay_ms_median"], 60)
        self.assertEqual(stats["poll_ms"], 25)


class StoredFeedBars(unittest.TestCase):
    def test_stored_candles_match_a_full_build_and_follow_new_days(self):
        from simulator.data.bars import BarStore
        from simulator.data.ticks import FeedStore
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "s" / "EURUSD"
            store = FeedStore(feed, scale=10**5)
            rng = np.random.default_rng(3)
            base = utc_ms(2025, 3, 3)
            manifest = {"days": {}}
            for k in range(4):
                n = 3000
                ts = (base + k * 86_400_000 + np.sort(rng.integers(0, 86_400_000, n))) * 1_000_000
                bid = np.round(1.1 + np.cumsum(rng.normal(0, 1e-5, n)), 5)
                store.write_day(day_of_ns(int(ts[0])), ts, bid, bid + 0.0001, np.zeros(n, np.int8))
                manifest["days"][day_of_ns(int(ts[0])).isoformat()] = {"ticks": n}
            store.save_manifest(manifest)
            src = LiveFeedSource(feed)
            bars = FeedBars(src)
            full = BarStore.build(src)
            for tf in ("m1", "h1", "d1"):
                self.assertEqual(bars[tf].slice(0, 10**9), full[tf].slice(0, 10**9))
            self.assertTrue((feed / "bars" / "m1.parquet").exists())
            # A fresh reader uses the stored candles; a new day is added to them.
            ts = (base + 4 * 86_400_000 + np.arange(100) * 60_000) * 1_000_000
            store.write_day(day_of_ns(int(ts[0])), ts, np.full(100, 1.2), np.full(100, 1.2001), np.zeros(100, np.int8))
            manifest["days"][day_of_ns(int(ts[0])).isoformat()] = {"ticks": 100}
            time.sleep(0.02)
            store.save_manifest(manifest)
            src2 = LiveFeedSource(feed)
            again = FeedBars(src2)
            self.assertEqual(again["h1"].slice(0, 10**9), BarStore.build(src2)["h1"].slice(0, 10**9))


class LiveSession(unittest.TestCase):
    def test_live_session_follows_the_feed_and_fills_on_the_next_tick(self):
        with tempfile.TemporaryDirectory() as d:
            feed = Path(d) / "x" / "EURUSD"
            (feed / "live").mkdir(parents=True)
            now = int(time.time() * 1000)
            day = (feed / "live" / f"{day_of_ns(now * 1_000_000).isoformat()}.csv")
            start = now - (now % 86_400_000) + 1000  # just after midnight UTC
            with open(day, "w", newline="\n") as fh:
                for k in range(200):
                    fh.write(f"{start + k * 1000},{1.1 + k * 1e-5:.5f},{1.10008 + k * 1e-5:.5f}\n")
            src = LiveFeedSource(feed)
            bars = FeedBars(src)
            s = ReplaySession(src, bars, Broker(), (start + 199_000) * 1_000_000, "m1", live=True)
            with self.assertRaises(SessionError):
                s.step("tick")
            with self.assertRaises(SessionError):
                s.jump(start * 1_000_000)
            s.broker.place("buy", "market", s.sim_ns, lots=0.1)
            # A tick arrives; the session advances to it and fills there.
            with open(day, "a", newline="\n") as fh:
                fh.write(f"{start + 200_000},1.10300,1.10308\n")
            s.advance((start + 200_000) * 1_000_000)
            self.assertEqual(s.broker.positions[0].entry_price, 1.10308)
            hist = s.history()
            self.assertEqual(hist[-1]["close"], 1.103)
            self.assertFalse(s.finished)


if __name__ == "__main__":
    unittest.main()
