"""Dukascopy decoding, Layer 0 cleaning rules, and the resumable updater.

The updater is run against a fake datafeed: hours that answer, hours not
yet published, hours that fail, and a hole in the middle of the history.
"""

import lzma
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from simulator.data import dukascopy as dk
from simulator.data.bars import BarStore
from simulator.data.quality import flag_day
from simulator.data.ticks import NS_PER_SECOND, FeedStore, ParquetTickSource, day_entry, day_start_ns

S = NS_PER_SECOND
HOUR = 3600 * S


def bi5(rows):
    """Encode (ms, ask_points, bid_points) rows the way Dukascopy does."""
    rec = np.array([(ms, a, b, 1.0, 1.0) for ms, a, b in rows], dtype=dk.RECORD)
    return lzma.compress(rec.tobytes(), format=lzma.FORMAT_ALONE)


class Decode(unittest.TestCase):
    def test_round_trip(self):
        hour = day_start_ns(date(2025, 3, 4)) + 13 * HOUR
        ts, bid, ask = dk.decode(bi5([(5, 108_012, 108_010), (1500, 108_020, 108_017)]), hour)
        self.assertEqual(ts.tolist(), [hour + 5_000_000, hour + 1_500_000_000])
        self.assertEqual(bid.tolist(), [1.0801, 1.08017])
        self.assertEqual(ask.tolist(), [1.08012, 1.0802])

    def test_empty_file_is_a_closed_hour(self):
        ts, bid, ask = dk.decode(b"", 0)
        self.assertEqual(len(ts), 0)

    def test_url_months_are_zero_based(self):
        hour = day_start_ns(date(2025, 1, 2)) + 7 * HOUR
        self.assertTrue(dk.hour_url("EURUSD", hour).endswith("/EURUSD/2025/00/02/07h_ticks.bi5"))


class Cleaning(unittest.TestCase):
    def test_layer0_rules(self):
        day0 = day_start_ns(date(2025, 3, 4))  # a Tuesday
        n = 3000
        ts = day0 + np.arange(n, dtype=np.int64) * S
        bid = 1.1 + np.sin(np.arange(n) / 50) * 0.0005
        ask = bid + 0.00002
        ts[2000:] += 10 * S                       # a 10 s pause before tick 2000
        bid[100], ask[100] = 1.1, 1.0999          # Ask < Bid
        ask[200] = bid[200] + 0.001               # spread 50x the median
        bid[1500] += 0.01                         # a 100-pip spike
        ask[1500] = bid[1500] + 0.00002
        later = round(bid[50] + 0.00001, 5)
        ts = np.r_[ts, ts[50]]                     # repeated timestamp, later quote wins
        bid = np.r_[bid, later]
        ask = np.r_[ask, later + 0.00002]
        out_ts, out_bid, _ask, flag = flag_day(ts, bid, ask)
        self.assertEqual(len(out_ts), n)
        self.assertEqual(out_bid[50], later)
        self.assertEqual((flag[100], flag[200], flag[2000], flag[1500]), (2, 1, 3, 4))
        self.assertEqual(int((flag == 0).sum()), n - 4)

    def test_weekend_gaps_are_not_flagged(self):
        sat = day_start_ns(date(2025, 3, 8))
        ts = sat + np.array([0, 60, 120], dtype=np.int64) * S
        _t, _b, _a, flag = flag_day(ts, np.full(3, 1.1), np.full(3, 1.10002))
        self.assertEqual(flag.tolist(), [0, 0, 0])


class FakeFeed:
    """Every hour has 3 ticks, except: `missing` hours 404 (no data) and
    `failing` hours raise until `heal()`."""

    def __init__(self, missing=(), failing=()):
        self.missing, self.failing, self.calls = set(missing), set(failing), []

    def heal(self):
        self.failing = set()

    def __call__(self, url):
        self.calls.append(url)
        parts = url.split("/")
        y, m, d, h = int(parts[-4]), int(parts[-3]) + 1, int(parts[-2]), int(parts[-1][:2])
        key = (date(y, m, d), h)
        if key in self.failing:
            raise TimeoutError("throttled")
        if key in self.missing:
            return None
        base = 110_000 + h
        return bi5([(1000, base + 2, base), (2000, base + 3, base + 1), (3000, base + 2, base)])


class Updater(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.feed = Path(self.tmp.name) / "EURUSD"
        store = FeedStore(self.feed)
        # Existing history: Mon 3 Mar and Wed 5 Mar 2025, Tue 4 Mar missing.
        manifest = {"days": {}}
        for d in (date(2025, 3, 3), date(2025, 3, 5)):
            ts = day_start_ns(d) + np.arange(10, dtype=np.int64) * 60 * S
            bid = np.full(10, 1.05)
            flag = np.zeros(10, np.int8)
            store.write_day(d, ts, bid, bid + 0.00002, flag)
            manifest["days"][d.isoformat()] = day_entry("layer0", ts, flag, complete=True)
        store.save_manifest(manifest)
        # "Now" is Friday 7 Mar 2025 10:30 UTC: hours to 09:00 are published.
        self.now = day_start_ns(date(2025, 3, 7)) + 10 * HOUR + 30 * 60 * S

    def tearDown(self):
        self.tmp.cleanup()

    def test_fills_the_hole_and_new_days_up_to_the_last_published_hour(self):
        self.assertEqual(dk.pending_days(self.feed, now=self.now),
                         [date(2025, 3, 4), date(2025, 3, 6), date(2025, 3, 7)])
        summary = dk.update(self.feed, now=self.now, fetch=FakeFeed())
        self.assertEqual(summary["first_changed"], date(2025, 3, 4))
        days = FeedStore(self.feed).manifest()["days"]
        self.assertTrue(days["2025-03-04"]["complete"])
        self.assertEqual(days["2025-03-04"]["ticks"], 72)
        self.assertFalse(days["2025-03-07"]["complete"])         # today: hours 0-9 only
        self.assertEqual(days["2025-03-07"]["hours_done"], list(range(10)))
        self.assertEqual(days["2025-03-03"]["source"], "layer0")  # history untouched

    def test_a_failed_hour_is_retried_next_time_without_refetching_the_rest(self):
        fake = FakeFeed(failing={(date(2025, 3, 4), 13)})
        s1 = dk.update(self.feed, now=self.now, fetch=fake)
        self.assertEqual(s1["failed_hours"], 1)
        entry = FeedStore(self.feed).manifest()["days"]["2025-03-04"]
        self.assertFalse(entry["complete"])
        self.assertNotIn(13, entry["hours_done"])
        fake.heal()
        fake.calls.clear()
        later = self.now + 2 * HOUR
        dk.update(self.feed, now=later, fetch=fake)
        fetched = [u for u in fake.calls if "/2025/02/04/" in u]
        self.assertEqual(fetched, [dk.hour_url("EURUSD", day_start_ns(date(2025, 3, 4)) + 13 * HOUR)])
        self.assertTrue(FeedStore(self.feed).manifest()["days"]["2025-03-04"]["complete"])

    def test_a_recent_404_is_not_yet_published_but_an_old_one_is_no_data(self):
        fake = FakeFeed(missing={(date(2025, 3, 7), 9), (date(2025, 3, 6), 5)})
        dk.update(self.feed, now=self.now, fetch=fake)
        days = FeedStore(self.feed).manifest()["days"]
        self.assertIn(5, days["2025-03-06"]["hours_done"])       # old: empty hour, done
        self.assertNotIn(9, days["2025-03-07"]["hours_done"])    # recent: asked again later

    def test_chunks_and_candles(self):
        s = dk.update(self.feed, now=self.now, fetch=FakeFeed(), max_days=1)
        self.assertEqual((s["days"], s["remaining"], s["through"]), (1, 2, date(2025, 3, 4)))
        source = ParquetTickSource(self.feed)
        bars = BarStore.build(source)
        before = len(bars["h1"])
        dk.update(self.feed, now=self.now, fetch=FakeFeed(), start_day=date(2025, 3, 5))
        source.refresh()
        bars.rebuild_from(source, date(2025, 3, 5))
        self.assertEqual(len(bars["h1"]), before + 24 + 10)
        self.assertEqual(len(bars["h1"]), len(BarStore.build(source)["h1"]))


if __name__ == "__main__":
    unittest.main()
