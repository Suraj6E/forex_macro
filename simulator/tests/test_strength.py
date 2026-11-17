"""Currency strength as an indicator: the same numbers as the Analysis panel
at every minute, carried within the FX day only, the source chosen per day,
months built again only when their candles change, and nothing after the
clock given to a candle."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from simulator import analysis, strength
from simulator.data.ticks import NS_PER_SECOND, MemoryTickSource

START = {"EURUSD": 1.1, "GBPUSD": 1.3, "USDJPY": 150.0, "EURJPY": 165.0, "GBPJPY": 195.0, "EURGBP": 0.85,
         "XAUUSD": 2650.0, "XAGUSD": 31.0, "WTI": 70.0, "BRENT": 74.0}


def s_(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def market(a: int, b: int, seed: int = 7, holes=None):
    """Random-walk 1-minute candles for every instrument over [a, b). `holes`
    maps a symbol to a (from, to) range with no candles."""
    rng = np.random.default_rng(seed)
    out = {}
    for sym, p0 in START.items():
        t = np.arange(a, b, 60, dtype=np.int64)
        t = t[rng.random(len(t)) > 0.15]  # minutes without a tick
        if holes and sym in holes:
            h0, h1 = holes[sym]
            t = t[(t < h0) | (t >= h1)]
        steps = rng.normal(0, 0.0004, len(t))
        close = p0 * np.exp(np.cumsum(steps))
        open_ = close * np.exp(rng.normal(0, 0.0001, len(t)))
        out[sym] = (t, open_, close)
    return out


def tick_sources(bars):
    """The same market as ticks: the open at the minute's start, the close
    50 s in, as the Analysis panel reads it."""
    out = {}
    for sym, (t, o, c) in bars.items():
        ts = np.r_[t, t + 50] * NS_PER_SECOND
        bid = np.r_[o, c]
        out[sym] = [MemoryTickSource(ts, bid, bid * 1.00001)]
    return out


class SameAsThePanel(unittest.TestCase):
    def test_every_minute_matches_the_analysis_panel(self):
        a, b = s_("2026-11-16T19:00"), s_("2026-11-17T03:00")  # the FX day turns at 22:00 UTC
        bars = market(a - 86_400, b, holes={"GBPJPY": (s_("2026-11-16T22:00"), s_("2026-11-16T22:40"))})
        rows = strength.compute(bars, a, b)
        ticks = tick_sources(bars)
        pair_minutes = np.unique(np.concatenate([bars[p][0] for p in analysis.PAIRS]))
        checked = 0
        for i in range(0, len(rows["time"]), 7):
            g = int(rows["time"][i])
            if g not in pair_minutes:
                continue
            panel = analysis.measure(ticks, (g + 59) * NS_PER_SECOND)
            for r in panel["currencies"]:
                c = r["currency"]
                self.assertEqual((int(rows[c + "_won"][i]), int(rows[c + "_played"][i])), (r["wins"], r["played"]), (g, c))
                if r["avg_pct"] is not None:
                    self.assertAlmostEqual(float(rows[c][i]), r["avg_pct"], places=4)
            for r in panel["others"]:
                if r["state"] == "ok":
                    self.assertAlmostEqual(float(rows[r["symbol"]][i]), r["change_pct"], places=4)
            checked += 1
        self.assertGreater(checked, 40)

    def test_a_pair_carries_its_move_within_the_day_only(self):
        a, b = s_("2026-11-16T21:00"), s_("2026-11-16T23:00")
        bars = market(a - 86_400, b, holes={"GBPJPY": (s_("2026-11-16T21:50"), s_("2026-11-16T22:30"))})
        rows = strength.compute(bars, a, b)
        t = rows["time"]
        before = (t >= s_("2026-11-16T21:51")) & (t < s_("2026-11-16T22:00"))
        after = (t >= s_("2026-11-16T22:00")) & (t < s_("2026-11-16T22:30"))
        self.assertFalse(np.isnan(rows["GBPJPY"][before]).any())   # carried from 21:49, same FX day
        self.assertTrue(np.isnan(rows["GBPJPY"][after]).all())     # a new day: no move yet
        self.assertTrue((rows["JPY_played"][after] == 2).all())


class FakePart:
    def __init__(self, bars_of_symbol):
        self.t, self.o, self.c = bars_of_symbol
        self.reads = 0

    def days(self):
        out = {}
        for d in np.unique(self.t // 86_400):
            m = self.t // 86_400 == d
            out[int(d)] = (int(m.sum()), int(m.sum()), int(self.t[m][-1]))
        return out

    def read(self, a, b):
        self.reads += 1
        m = (self.t >= a) & (self.t < b)
        return self.t[m], self.o[m], self.c[m]


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bars = market(s_("2026-10-29T00:00"), s_("2026-11-03T00:00"))
        self.parts = {sym: FakePart(b) for sym, b in self.bars.items()}
        self.store = strength.StrengthStore(Path(self.tmp.name),
                                            lambda sym: [strength.Source("dukascopy", [self.parts[sym]])])

    def tearDown(self):
        self.tmp.cleanup()

    def test_builds_every_month_then_only_changed_ones(self):
        self.assertEqual(self.store.update(), 2)  # October and November
        self.assertTrue((Path(self.tmp.name) / "m1" / "2026-10.parquet").exists())
        self.assertEqual(self.store.status["state"], "ready")
        self.assertEqual(self.store.update(), 0)
        # A day in November changes (its evening re-read): only November again.
        t, o, c = self.bars["EURUSD"]
        keep = t < s_("2026-11-02T20:00")
        self.parts["EURUSD"].t = np.r_[t[keep], s_("2026-11-02T23:59")]
        self.parts["EURUSD"].o = np.r_[o[keep], 1.2]
        self.parts["EURUSD"].c = np.r_[c[keep], 1.2]
        self.assertEqual(self.store.update(), 1)
        # A fresh store reads what was saved and has nothing to do.
        again = strength.StrengthStore(Path(self.tmp.name), lambda sym: [strength.Source("dukascopy", [self.parts[sym]])])
        self.assertEqual(again.update(), 0)

    def test_a_candle_gets_its_last_minute_and_the_forming_one_the_clock(self):
        self.store.update()
        rows = strength.compute(self.bars, s_("2026-11-02T00:00"), s_("2026-11-03T00:00"))
        sim = s_("2026-11-02T10:37") + 30  # mid-minute: 10:36 is the last minute closed
        bars = [{"time": s_("2026-11-02T09:00")}, {"time": s_("2026-11-02T10:00")}]
        self.store.annotate(bars, 3600, sim * NS_PER_SECOND)

        def row_at(last_start):
            j = int(np.searchsorted(rows["time"], last_start, side="right")) - 1
            return [None if np.isnan(rows[k][j]) else round(round(float(rows[k][j]) * 1000) / 1000, 3)
                    for k in strength.KEYS], int(rows["time"][j])

        done, done_t = row_at(s_("2026-11-02T09:59"))
        forming, forming_t = row_at(s_("2026-11-02T10:36"))
        self.assertEqual(bars[0]["s"], done)
        self.assertEqual(bars[1]["s"], forming)
        self.assertLessEqual(forming_t + 60, sim)  # nothing after the clock
        self.assertEqual(len(bars[1]["w"]), 8)

    def test_a_minute_still_open_is_not_used(self):
        self.store.update()
        sim = s_("2026-11-02T10:37") - 1
        bars = [{"time": s_("2026-11-02T10:30")}]
        self.store.annotate(bars, 900, sim * NS_PER_SECOND)
        found = self.store.rows_before([sim], [s_("2026-11-02T10:30")])[0]
        rows, j = found
        self.assertLessEqual(int(rows["time"][j]) + 60, sim)


class Choose(unittest.TestCase):
    def test_the_source_covering_the_day_later_wins(self):
        class P:
            def __init__(self, last):
                self.last = last

            def days(self):
                return {100: (10, 10, self.last)}
        duka_short = strength.Source("dukascopy", [P(100 * 86_400 + 10 * 3600)])
        broker_full = strength.Source("mt5:x", [P(100 * 86_400 + 23 * 3600)])
        self.assertEqual(strength.choose([duka_short, broker_full])[100][0].key, "mt5:x")
        duka_full = strength.Source("dukascopy", [P(100 * 86_400 + 23 * 3600 - 60)])
        self.assertEqual(strength.choose([duka_full, broker_full])[100][0].key, "dukascopy")


if __name__ == "__main__":
    unittest.main()
