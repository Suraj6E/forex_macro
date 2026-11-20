"""The six concepts as rules (the Strategies tab): each against the PDF's own
EUR/USD numbers where it gives them, the routine's setups and their
results, and nothing read after the clock."""

import unittest
from datetime import datetime, timezone

import numpy as np

from simulator import instruments, strategies as st
from simulator.data.bars import TimeframeBars, bars_from_ticks
from simulator.data.ticks import NS_PER_SECOND

PIP = 0.0001
EURUSD = instruments.get("EURUSD")


def s_(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def bars(seconds, rows, start=None, spread=0.00002):
    """Candles from (open, high, low, close[, volume]) rows, one `seconds` apart."""
    start = s_("2026-11-02T00:00") if start is None else start
    t = np.array([start + i * seconds for i in range(len(rows))], np.int64)
    a = np.array([r[:4] for r in rows], float)
    v = np.array([r[4] if len(r) > 4 else 100 for r in rows], np.int64)
    return st.Bars(seconds, t, a[:, 0], a[:, 1], a[:, 2], a[:, 3], v, np.full(len(rows), spread))


class Arithmetic(unittest.TestCase):
    def test_ema_is_the_pdfs_formula_started_from_an_average(self):
        x = np.arange(1.0, 31.0)
        e = st.ema(x, 20)
        k = 2 / 21
        self.assertTrue(np.isnan(e[18]))
        self.assertAlmostEqual(e[19], x[:20].mean())
        self.assertAlmostEqual(e[20], k * x[20] + (1 - k) * e[19])
        self.assertAlmostEqual(k, 0.095, places=3)  # "each new close moves the line by 9.5 %"

    def test_a_swing_is_known_only_once_the_candles_after_it_have_closed(self):
        b = bars(3600, [(1, 2, 0.5, 1.5), (1.5, 3, 1, 2), (2, 5, 1.8, 4), (4, 4.5, 3, 3.5), (3.5, 4, 2, 2.5), (2.5, 3, 1, 1.2)])
        highs, _lows = st.swings(b, 2, 2)
        self.assertEqual([(h.i, h.price) for h in highs], [(2, 5.0)])
        self.assertEqual(highs[0].known, int(b.end[4]))  # after candles 3 and 4 closed


class Liquidity(unittest.TestCase):
    def test_a_close_back_is_a_sweep_and_a_close_beyond_a_break(self):
        # The PDF's table: the 14 Oct low, 1.1113, swept on 29 Oct (6 pips
        # through, closed 1.1162) and broken on 11 Nov (closed 1.1093).
        low = st.Area("pdl", "liquidity", "buy", 1.1113, 1.1113, s_("2026-11-02T00:00"), 0, "Yesterday's low")
        m15 = bars(900, [(1.1130, 1.1140, 1.1120, 1.1125),
                         (1.1125, 1.1170, 1.1107, 1.1162),   # sweep
                         (1.1162, 1.1170, 1.1150, 1.1160),
                         (1.1120, 1.1125, 1.1083, 1.1093),   # break
                         (1.1093, 1.1100, 1.1050, 1.1060)])  # no longer a level
        sweeps = st.track_level(low, m15)
        self.assertEqual([(w.broke, round(abs(w.extreme - 1.1113) / PIP)) for w in sweeps], [(False, 6), (True, 30)])
        self.assertEqual(sweeps[0].time, int(m15.end[1]))  # known at the 15-minute close
        self.assertEqual((low.end, low.until), ("broken", int(m15.end[3])))


class SmartMoney(unittest.TestCase):
    def test_a_fair_value_gap_is_where_the_first_and_third_candles_do_not_meet(self):
        # 10 Nov low 1.1146 and 12 Nov high 1.1103: the PDF's gap, 1.1103 to 1.1146.
        b = bars(86400, [(1.1224, 1.1251, 1.1146, 1.1168), (1.1168, 1.1170, 1.1083, 1.1093), (1.1093, 1.1103, 1.1046, 1.1056)])
        gaps = st.fair_value_gaps(b, np.full(3, 60 * PIP), 0)
        self.assertEqual([(g.side, round(g.lo, 4), round(g.hi, 4)) for g in gaps], [("sell", 1.1103, 1.1146)])
        self.assertEqual(gaps[0].known, int(b.end[2]))

    def test_a_close_beyond_the_last_swing_breaks_structure_and_marks_the_order_block(self):
        rows = [(1.10, 1.105, 1.095, 1.102), (1.102, 1.110, 1.100, 1.108), (1.108, 1.120, 1.106, 1.118),  # swing high 1.120
                (1.118, 1.119, 1.110, 1.112), (1.112, 1.114, 1.104, 1.106),                                   # swing low 1.104
                (1.106, 1.112, 1.105, 1.111), (1.111, 1.113, 1.109, 1.112),
                (1.112, 1.1125, 1.095, 1.096)]                                                                # close under 1.104
        b = bars(3600, rows)
        highs, lows = st.swings(b, 2, 2)
        events, blocks = st.structure(b, highs, lows, 2, 0)
        self.assertEqual([(e.kind, e.side, round(e.level, 3)) for e in events], [("BOS", "sell", 1.104)])
        # The last up-candle before the fall, all of its range.
        self.assertEqual([(x.side, x.lo, x.hi) for x in blocks], [("sell", 1.109, 1.113)])


class Zones(unittest.TestCase):
    def test_the_base_of_a_strong_move_from_wick_to_body_weakening_with_each_visit(self):
        # The PDF's demand zone: 14 Oct fell to 1.1113 and closed 1.1158, then
        # rose 111 pips in two days; three visits, the third broke it.
        rows = [(1.1170, 1.1180, 1.1160, 1.1165), (1.1165, 1.1172, 1.1158, 1.1162),
                (1.1150, 1.1165, 1.1113, 1.1158),                  # base
                (1.1158, 1.1228, 1.1155, 1.1221),                  # strong move
                (1.1221, 1.1269, 1.1218, 1.1262),
                (1.1262, 1.1265, 1.1200, 1.1210), (1.1210, 1.1215, 1.1107, 1.1162),   # visit 1
                (1.1162, 1.1273, 1.1160, 1.1250), (1.1250, 1.1255, 1.1132, 1.1150),   # visit 2
                (1.1150, 1.1251, 1.1148, 1.1240), (1.1240, 1.1245, 1.1083, 1.1093)]   # closes under: broken
        b = bars(86400, rows)
        found = st.zones(b, np.full(len(rows), 60 * PIP), 0)
        demand = [z for z in found if z.kind == "demand"]
        self.assertEqual([(round(z.lo, 4), round(z.hi, 4)) for z in demand], [(1.1113, 1.1158)])
        z = demand[0]
        self.assertEqual(z.known, int(b.end[4]))
        self.assertEqual((len(z.visits), z.end), (2, "broken"))


class RoomAndSide(unittest.TestCase):
    def test_the_value_area_holds_seventy_percent_around_the_point_of_control(self):
        rows = []
        for i, (lo, vol) in enumerate([(1.1000, 10), (1.1001, 40), (1.1002, 200), (1.1003, 60), (1.1004, 30), (1.1005, 5)] * 3):
            rows.append((lo, lo + 0.00004, lo, lo + 0.00004, vol))
        m1 = bars(60, rows)
        pf = st.volume_profile(m1, int(m1.t[0]), int(m1.end[-1]), PIP)
        self.assertAlmostEqual(pf.poc, 1.10025)
        inside = pf.vol[int(round((pf.val - pf.base) / pf.step)): int(round((pf.vah - pf.base) / pf.step))].sum()
        self.assertGreaterEqual(inside / pf.vol.sum(), 0.7)
        self.assertLessEqual(pf.val, pf.poc)
        self.assertGreaterEqual(pf.vah, pf.poc)

    def test_room_toward_the_target_before_the_busy_middle(self):
        pf = st.Profile(0, 1, 1.1100, PIP, np.ones(40), poc=1.1120, vah=1.1130, val=1.1110)
        self.assertEqual(st.room(pf, 1.1145, "sell", PIP)["pips"], 15.0)   # thin lane above value
        self.assertEqual(st.room(pf, 1.1126, "sell", PIP)["pips"], 6.0)    # inside value: to the middle
        self.assertEqual(st.room(pf, 1.1115, "sell", PIP)["pips"], 0.0)    # under the middle: two-way
        self.assertTrue(st.room(pf, 1.1100, "sell", PIP)["open"])          # left the profile: no fading
        self.assertTrue(st.room_ok(st.room(pf, 1.1145, "sell", PIP), 10))
        self.assertFalse(st.room_ok(st.room(pf, 1.1126, "sell", PIP), 10))

    def test_the_side_from_a_sloping_ema_else_the_range_halves(self):
        n = 40
        closes = np.linspace(1.16, 1.11, n)                       # falling every day
        days = bars(86400, [(c + 0.0010, c + 0.0030, c - 0.0030, c) for c in closes])
        daily = st.Daily(days, st.ema(days.c, 20), st.atr(days))
        t = int(days.end[-1]) + 3600
        s = st.Settings()
        side = st.side_at(daily, t, 1.1090, s, PIP)
        self.assertEqual((side["sell"], side["buy"], side["rule"]), (True, False, "ema"))
        flat = bars(86400, [(1.12, 1.125, 1.115, 1.12 + (0.0005 if i % 2 else -0.0005)) for i in range(n)])
        daily = st.Daily(flat, st.ema(flat.c, 20), st.atr(flat))
        up = st.side_at(daily, t, 1.1240, s, PIP)
        self.assertEqual((up["sell"], up["buy"], up["rule"]), (True, False, "range"))   # upper half: sells
        self.assertFalse(st.side_at(daily, t, 1.1160, s, PIP)["sell"])                  # lower half: buys
        off = st.side_at(daily, t, 1.1240, st.Settings(frozenset({"zones"})), PIP)
        self.assertEqual((off["sell"], off["buy"]), (True, True))

    def test_the_pdfs_ten_candle_count(self):
        rows = [(1.0, 1.0006, 0.9999, 1.0001)] * 4 + [(1.0010, 1.0011, 0.9990, 0.9991)] * 6  # small up, big down
        r = st.candle_read(bars(60, rows), 9, PIP)
        self.assertEqual(r["reading"], "sellers stronger")
        self.assertEqual(r["up_rejections"], 4)


class Setups(unittest.TestCase):
    """A sweep of yesterday's high, a 1-minute close under the last small
    low, a sell limit back at the level: filled, then the target."""

    def scenario(self, tail):
        base = s_("2026-11-03T10:00")
        p = [(1.1000, 1.1003, 1.0999, 1.1002), (1.1002, 1.1005, 1.1001, 1.1004), (1.1004, 1.1006, 1.1002, 1.1003),
             (1.1003, 1.1004, 1.1000, 1.1002),                                    # the small low, 1.1000
             (1.1002, 1.1006, 1.1002, 1.1005), (1.1005, 1.1009, 1.1004, 1.1008),
             (1.1008, 1.1014, 1.1007, 1.1009),                                    # through 1.1010 to 1.1014
             (1.1009, 1.1010, 1.1004, 1.1006), (1.1006, 1.1007, 1.1003, 1.1004),
             (1.1004, 1.1005, 1.0995, 1.0996)]                                    # wide, closes under it
        m1 = bars(60, p + tail, start=base)
        level = st.Area("pdh", "liquidity", "sell", 1.1010, 1.1010, base - 3600, base - 86400, "Yesterday's high")
        touch = st.Touch("sell", base, base + 900, level, "sweep")             # the 15-minute close back
        highs, lows = st.swings(m1, 2, 2)
        s = st.Settings(frozenset({"liquidity", "price_action"}))
        out = st.find_setups([touch], m1, highs, lows, np.full(len(m1), np.nan), None, None, s, PIP)
        return m1, out

    def test_a_sell_limit_at_the_swept_level_stop_beyond_the_push_ten_pips_target(self):
        # The failure came inside the sweep's 15-minute candle: the setup
        # is known at its close, 10:15, and a new low since does not matter.
        tail = [(1.0996, 1.1000, 1.0994, 1.0999)] * 5 + [(1.0999, 1.1011, 1.0998, 1.1008),   # back up: filled at 1.1010
                                                         (1.1008, 1.1009, 1.0990, 1.0992)]   # down: the target
        m1, out = self.scenario(tail)
        self.assertEqual(len(out), 1)
        x = out[0]
        self.assertEqual((x.side, x.market, x.status), ("sell", False, "won"))
        self.assertAlmostEqual(x.entry, 1.1010)
        self.assertAlmostEqual(x.stop, 1.1014 + 1.5 * PIP)
        self.assertAlmostEqual(x.target, 1.1000)
        self.assertEqual(x.time, int(m1.end[14]))
        self.assertEqual(x.time, s_("2026-11-03T10:15"))
        self.assertEqual(x.pips, 10.0)
        self.assertTrue(all(c["ok"] for c in x.checks if c["required"]))

    def test_a_candle_reaching_stop_and_target_counts_as_the_stop(self):
        tail = [(1.0996, 1.1000, 1.0994, 1.0999)] * 5 + [(1.0999, 1.1011, 1.0998, 1.1008),
                                                         (1.1008, 1.1020, 1.0985, 1.0990)]
        _m1, out = self.scenario(tail)
        self.assertEqual((out[0].status, out[0].pips), ("lost", -5.5))

    def test_a_limit_not_filled_within_the_hour_expires(self):
        _m1, out = self.scenario([(1.0996, 1.1000, 1.0994, 1.0999)] * 70)
        self.assertEqual(out[0].status, "expired")


class NoLookAhead(unittest.TestCase):
    """Everything found at a clock is found the same way later: the same
    levels and zones from the same time, the same setups with the same
    prices and checks; only results still open may change."""

    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(7)
        start = s_("2026-10-19T00:00") * NS_PER_SECOND
        n = 26 * 8640                       # a tick every 10 s for 26 days
        ts = start + np.arange(n, dtype=np.int64) * 10 * NS_PER_SECOND
        bid = np.round(1.1000 + np.cumsum(rng.normal(0, 0.000035, n)), 5)
        ask = bid + 0.00002
        cls.stores = {tf: TimeframeBars.from_arrays(sec, bars_from_ticks(ts, bid, ask, sec))
                      for tf, sec in (("m1", 60), ("m15", 900), ("h1", 3600))}

    def test_a_later_clock_never_changes_what_an_earlier_one_saw(self):
        t2 = s_("2026-11-12T21:41")
        b = st.evaluate(self.stores, t2 * NS_PER_SECOND, None, None, EURUSD, st.Settings(), "m15")
        for t1 in (s_("2026-11-12T08:13"), s_("2026-11-12T11:31"), s_("2026-11-12T13:07"), s_("2026-11-12T16:52")):
            with self.subTest(clock=t1):
                self.compare(t1, b)

    def compare(self, t1, b):
        a = st.evaluate(self.stores, t1 * NS_PER_SECOND, None, None, EURUSD, st.Settings(), "m15")
        self.assertGreater(len(a["setups"]), 3)
        later = {x["id"]: x for x in b["setups"]}
        for x in a["setups"]:
            self.assertLessEqual(x["time"], t1)
            y = later[x["id"]]
            for k in ("entry", "stop", "target", "market", "checks", "areas"):
                self.assertEqual(x[k], y[k], (x["id"], k))
            if x["status"] in ("won", "lost", "expired", "skipped"):
                self.assertEqual((x["status"], x["pips"], x["closed"]), (y["status"], y["pips"], y["closed"]))
            for t in (x["filled"], x["closed"]):
                self.assertTrue(t is None or t <= t1)
        for group in ("levels", "areas"):
            seen = {(z["label"], z["lo"], z["hi"], z["known"]) for z in b[group]}
            for z in a[group]:
                self.assertLessEqual(z["known"], t1)
                self.assertTrue(z["until"] is None or z["until"] <= t1)
                self.assertIn((z["label"], z["lo"], z["hi"], z["known"]), seen)
        for w in a["sweeps"]:
            self.assertLessEqual(w["time"], t1)
        self.assertLessEqual(a["profile"]["end"], t1)

    def test_switching_every_concept_off_leaves_no_setups(self):
        none = st.evaluate(self.stores, s_("2026-11-12T13:07") * NS_PER_SECOND, None, None, EURUSD,
                           st.Settings(frozenset()), "m15")
        self.assertEqual((none["setups"], none["levels"], none["areas"], none["profile"]), ([], [], [], None))


class Query(unittest.TestCase):
    def test_settings_from_the_page(self):
        s = st.settings_from({"on": "ema,profile,bogus", "target": "12", "max_stop": "-3", "profile": "day"})
        self.assertEqual(s.concepts, frozenset({"ema", "profile"}))
        self.assertEqual((s.target_pips, s.max_stop_pips, s.profile), (12.0, 1.0, "day"))
        self.assertEqual(st.settings_from({}).concepts, frozenset(st.CONCEPTS))
        self.assertEqual(st.settings_from({"on": ""}).concepts, frozenset())


if __name__ == "__main__":
    unittest.main()
