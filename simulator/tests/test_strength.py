"""Currency strength as an indicator, over a day, a session, a week and a
rolling day: every period scored on the full sequence of matches, each
checked minute by minute against the same thing worked out by hand (and the
day against the Analysis panel itself); periods starting again where they
should and nowhere else; the source chosen per day; months built again only
when their candles change; and nothing after the clock given to a candle."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from simulator import analysis, strength
from simulator.data.ticks import NS_PER_SECOND, MemoryTickSource
from simulator.instruments import INSTRUMENTS
from simulator.journal import stats
from simulator.live.mt5rec import us_dst

START = {"EURUSD": 1.1, "GBPUSD": 1.3, "USDJPY": 150.0, "EURJPY": 165.0, "GBPJPY": 195.0, "EURGBP": 0.85,
         "XAUUSD": 2650.0, "XAGUSD": 31.0, "WTI": 70.0, "BRENT": 74.0}
DAY = 86_400


def s_(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def market(a: int, b: int, seed: int = 7, holes=None):
    """Random-walk 1-minute candles for every instrument over [a, b). `holes`
    maps a symbol (or "*" for all) to a (from, to) range with no candles."""
    rng = np.random.default_rng(seed)
    out = {}
    for sym, p0 in START.items():
        t = np.arange(a, b, 60, dtype=np.int64)
        t = t[rng.random(len(t)) > 0.15]  # minutes without a tick
        for key in (sym, "*"):
            if holes and key in holes:
                h0, h1 = holes[key]
                t = t[(t < h0) | (t >= h1)]
        steps = rng.normal(0, 0.0004, len(t))
        close = p0 * np.exp(np.cumsum(steps))
        open_ = close * np.exp(rng.normal(0, 0.0001, len(t)))
        out[sym] = (t, open_, close)
    return out


# -- the periods worked out by hand, without the module's helpers -------------


def new_york_five_pm(day_start: int) -> int:
    return day_start + (21 if us_dst((day_start + 21 * 3600) * 1000) else 22) * 3600


def start_by_hand(mode: str, g: int) -> int:
    d0 = g // DAY * DAY
    if mode == "day":
        opened = new_york_five_pm(d0)
        return opened if opened <= g else new_york_five_pm(d0 - DAY)
    if mode == "session":
        starts = [d0 - DAY + 22 * 3600] + [d0 + h * 3600 for h in (7, 12, 16, 22)]
        return max(x for x in starts if x <= g)
    if mode == "week":
        d = datetime.fromtimestamp(d0, timezone.utc)
        for back in range(9):
            day = d - timedelta(days=back)
            if day.weekday() == 6:  # Sunday
                opened = new_york_five_pm(int(day.timestamp()))
                if opened <= g:
                    return opened
    raise AssertionError(mode)


def by_hand(bars, g, mode):
    """One minute plainly: per instrument its move over the period (or the
    rolling day), then the panel's own ranking of the six matches."""
    rows = []
    for sym in analysis.PAIRS + analysis.ON_THEIR_OWN:
        t, o, c = bars[sym]
        inst = INSTRUMENTS[sym]
        row = {"symbol": sym, "base": inst.base, "quote": inst.quote, "state": "no data"}
        if mode == "rolling":
            k, j = np.flatnonzero(t <= g), np.flatnonzero(t <= g - DAY)
            if len(k) and len(j):
                row.update(state="ok", change_pct=(c[k[-1]] / c[j[-1]] - 1) * 100)
        else:
            inside = np.flatnonzero((t >= start_by_hand(mode, g)) & (t <= g))
            if len(inside):
                row.update(state="ok", change_pct=(c[inside[-1]] / o[inside[0]] - 1) * 100)
        rows.append(row)
    ranking, _ = analysis.rank(rows[:6])
    return {r["currency"]: r for r in ranking}, {r["symbol"]: r for r in rows}


# Thursday 12 Nov 2026 to Tuesday 17 Nov, the market shut from Friday 22:00
# to Sunday 22:00 UTC.
WEEKEND = (s_("2026-11-13T22:00"), s_("2026-11-15T22:00"))


class Periods(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = market(s_("2026-11-04T00:00"), s_("2026-11-17T06:00"), holes={"*": WEEKEND})
        cls.rows = strength.compute(cls.bars, s_("2026-11-12T00:00"), s_("2026-11-17T06:00"))

    def at(self, g):
        i = int(np.searchsorted(self.rows["time"], g))
        self.assertEqual(int(self.rows["time"][i]), g)
        return i

    def test_every_period_matches_working_it_out_by_hand(self):
        for mode in strength.MODES:
            checked = 0
            for i in range(0, len(self.rows["time"]), 61):
                g = int(self.rows["time"][i])
                cur, inst = by_hand(self.bars, g, mode)
                for c, r in cur.items():
                    got = (int(self.rows[f"{mode}_{c}_won"][i]), int(self.rows[f"{mode}_{c}_played"][i]))
                    self.assertEqual(got, (r["wins"], r["played"]), (mode, g, c))
                    if r["avg_pct"] is not None:
                        self.assertAlmostEqual(float(self.rows[f"{mode}_{c}"][i]), r["avg_pct"], places=4)
                for sym, r in inst.items():
                    if r["state"] == "ok":
                        self.assertAlmostEqual(float(self.rows[f"{mode}_{sym}"][i]), r["change_pct"], places=4)
                    else:
                        self.assertTrue(np.isnan(self.rows[f"{mode}_{sym}"][i]), (mode, g, sym))
                checked += 1
            self.assertGreater(checked, 60, mode)

    def test_the_day_is_the_analysis_panel_minute_by_minute(self):
        ticks = {}
        for sym, (t, o, c) in self.bars.items():  # the open at the minute's start, the close 50 s in
            ticks[sym] = [MemoryTickSource(np.r_[t, t + 50] * NS_PER_SECOND, np.r_[o, c], np.r_[o, c] * 1.00001)]
        pair_minutes = np.unique(np.concatenate([self.bars[p][0] for p in analysis.PAIRS]))
        checked = 0
        for i in range(0, len(self.rows["time"]), 89):
            g = int(self.rows["time"][i])
            if g not in pair_minutes:
                continue
            panel = analysis.measure(ticks, (g + 59) * NS_PER_SECOND)
            for r in panel["currencies"]:
                c = r["currency"]
                self.assertEqual((int(self.rows[f"day_{c}_won"][i]), int(self.rows[f"day_{c}_played"][i])),
                                 (r["wins"], r["played"]), (g, c))
                if r["avg_pct"] is not None:
                    self.assertAlmostEqual(float(self.rows[f"day_{c}"][i]), r["avg_pct"], places=4)
            checked += 1
        self.assertGreater(checked, 40)

    def test_sessions_are_the_journals(self):
        label = [next(name for name, test in stats.SESSIONS if test(h)) for h in range(24)]
        changes = tuple(h for h in range(24) if label[h] != label[h - 1])
        self.assertEqual(changes, strength.SESSION_STARTS_H)

    def test_each_period_starts_again_where_it_should_and_nowhere_else(self):
        t = self.bars["EURUSD"][0]

        def jump(mode, boundary):
            """EUR/USD's value at its last candle before a boundary and at
            its first after it."""
            i = self.at(int(t[t < boundary][-1]))
            j = self.at(int(t[t >= boundary][0]))
            return float(self.rows[f"{mode}_EURUSD"][i]), float(self.rows[f"{mode}_EURUSD"][j])

        london = s_("2026-11-16T07:00")
        new_day = s_("2026-11-16T22:00")  # 17:00 New York in November
        for mode, boundary, resets in (("session", london, True), ("day", london, False),
                                       ("day", new_day, True), ("week", new_day, False), ("rolling", new_day, False)):
            before, after = jump(mode, boundary)
            if resets:
                self.assertLess(abs(after), 0.06, (mode, boundary))   # one candle's move from its open
            else:
                self.assertLess(abs(after - before), 0.15, (mode, boundary))
        # The week starts again on Sunday evening, after the weekend.
        before, after = jump("week", WEEKEND[1])
        self.assertLess(abs(after), 0.06)
        self.assertGreater(abs(before), 0.06)

    def test_a_rolling_day_after_the_weekend_starts_from_fridays_close(self):
        g = int(self.rows["time"][self.rows["time"] >= s_("2026-11-15T22:30")][0])
        t, _o, c = self.bars["EURUSD"]
        friday_close = c[t < WEEKEND[0]][-1]
        now = c[t <= g][-1]
        self.assertAlmostEqual(float(self.rows["rolling_EURUSD"][self.at(g)]), (now / friday_close - 1) * 100, places=4)

    def test_no_rolling_value_without_a_day_before(self):
        rows = strength.compute(self.bars, s_("2026-11-04T00:00"), s_("2026-11-04T06:00"))
        self.assertTrue(np.isnan(rows["rolling_EURUSD"]).all())
        self.assertTrue((rows["rolling_USD_played"] == 0).all())
        self.assertFalse(np.isnan(rows["session_EURUSD"]).all())


class FakePart:
    def __init__(self, bars_of_symbol):
        self.t, self.o, self.c = bars_of_symbol

    def days(self):
        out = {}
        for d in np.unique(self.t // DAY):
            m = self.t // DAY == d
            out[int(d)] = (int(m.sum()), int(m.sum()), int(self.t[m][-1]))
        return out

    def read(self, a, b):
        m = (self.t >= a) & (self.t < b)
        return self.t[m], self.o[m], self.c[m]


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bars = market(s_("2026-10-27T00:00"), s_("2026-11-03T00:00"))
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

    def test_every_period_is_saved_and_a_candle_gets_its_last_minute(self):
        self.store.update()
        rows = strength.compute(self.bars, s_("2026-11-02T00:00"), s_("2026-11-03T00:00"))
        sim = s_("2026-11-02T10:37") + 30  # mid-minute: 10:36 is the last minute closed

        def row_at(mode, last_start):
            j = int(np.searchsorted(rows["time"], last_start, side="right")) - 1
            vals = [rows[f"{mode}_{k}"][j] for k in strength.KEYS]
            return [None if np.isnan(v) else round(round(float(v) * 1000) / 1000, 3) for v in vals], int(rows["time"][j])

        seen = {}
        for mode in strength.MODES:
            bars = [{"time": s_("2026-11-02T09:00")}, {"time": s_("2026-11-02T10:00")}]
            self.store.annotate(bars, 3600, sim * NS_PER_SECOND, mode)
            done, _ = row_at(mode, s_("2026-11-02T09:59"))
            forming, forming_t = row_at(mode, s_("2026-11-02T10:36"))
            self.assertEqual(bars[0]["s"], done, mode)
            self.assertEqual(bars[1]["s"], forming, mode)
            self.assertLessEqual(forming_t + 60, sim)  # nothing after the clock
            self.assertEqual(len(bars[1]["w"]), 8)
            seen[mode] = bars[1]["s"]
        # A Monday: the day and the week both opened on Sunday evening.
        self.assertEqual(seen["day"], seen["week"])
        self.assertEqual(len({tuple(v) for v in seen.values()}), 3)

    def test_a_minute_still_open_is_not_used(self):
        self.store.update()
        sim = s_("2026-11-02T10:37") - 1
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
        duka_short = strength.Source("dukascopy", [P(100 * DAY + 10 * 3600)])
        broker_full = strength.Source("mt5:x", [P(100 * DAY + 23 * 3600)])
        self.assertEqual(strength.choose([duka_short, broker_full])[100][0].key, "mt5:x")
        duka_full = strength.Source("dukascopy", [P(100 * DAY + 23 * 3600 - 60)])
        self.assertEqual(strength.choose([duka_full, broker_full])[100][0].key, "dukascopy")


if __name__ == "__main__":
    unittest.main()
