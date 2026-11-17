"""Currency strength (the Analysis panel): the FX day, the matches between
USD, EUR, GBP and JPY, gold and oil on their own, and nothing read after
the clock."""

import unittest
from datetime import datetime, timezone

from simulator import analysis
from simulator.data.ticks import NS_PER_SECOND, MemoryTickSource

MIN = 60 * NS_PER_SECOND


def ns(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * NS_PER_SECOND


def source(points):
    """Ticks at (time ns, Bid); Ask a little above."""
    ts = [t for t, _ in points]
    bid = [b for _, b in points]
    return MemoryTickSource(ts, bid, [b * 1.00001 for b in bid])


class FxDay(unittest.TestCase):
    def test_opens_at_five_in_new_york(self):
        # Winter: 17:00 New York is 22:00 UTC; summer: 21:00 UTC.
        self.assertEqual(analysis.fx_day_open_ns(ns("2026-11-17T10:00")), ns("2026-11-16T22:00"))
        self.assertEqual(analysis.fx_day_open_ns(ns("2026-11-17T22:00")), ns("2026-11-17T22:00"))
        self.assertEqual(analysis.fx_day_open_ns(ns("2026-07-15T10:00")), ns("2026-07-14T21:00"))
        self.assertEqual(analysis.fx_day_open_ns(ns("2026-07-15T21:30")), ns("2026-07-15T21:00"))

    def test_on_the_days_new_york_changes_its_clocks(self):
        self.assertEqual(analysis.fx_day_open_ns(ns("2026-03-08T23:00")), ns("2026-03-08T21:00"))  # EDT from 02:00
        self.assertEqual(analysis.fx_day_open_ns(ns("2026-11-01T23:00")), ns("2026-11-01T22:00"))  # EST from 02:00


# Tuesday 17 November 2026; its FX day opened Monday 22:00 UTC.
OPEN = ns("2026-11-16T22:00")
AT = ns("2026-11-17T10:00")
#: Day moves in percent. USD beats everyone; EUR everyone but USD; GBP
#: beats JPY only.
MOVES = {"EURUSD": -0.30, "GBPUSD": -0.50, "USDJPY": +0.40, "EURJPY": +0.10, "GBPJPY": +0.05, "EURGBP": +0.20,
         "XAUUSD": +1.2, "XAGUSD": -0.4, "WTI": -2.0, "BRENT": -1.5}
START = {"EURUSD": 1.1, "GBPUSD": 1.3, "USDJPY": 150.0, "EURJPY": 165.0, "GBPJPY": 195.0, "EURGBP": 0.85,
         "XAUUSD": 2650.0, "XAGUSD": 31.0, "WTI": 70.0, "BRENT": 74.0}


def market(moves=MOVES, after=None):
    """Each instrument: a tick before the day opened (yesterday), its first
    tick of the day, and its tick now. `after` adds a tick later than AT."""
    out = {}
    for s, pct in moves.items():
        p0 = START[s]
        points = [(OPEN - 30 * MIN, p0 * 0.9), (OPEN + 5 * MIN, p0), (AT - MIN, p0 * (1 + pct / 100))]
        if after:
            points.append((AT + MIN, p0 * (1 + after / 100)))
        out[s] = [source(points)]
    return out


class Strength(unittest.TestCase):
    def test_every_match_ranks_the_four_currencies(self):
        out = analysis.measure(market(), AT)
        self.assertEqual(out["day_open_ms"], OPEN // 1_000_000)
        ranking = [(r["currency"], r["wins"], r["played"], r["label"]) for r in out["currencies"]]
        self.assertEqual(ranking, [("USD", 3, 3, "Strong"), ("EUR", 2, 3, "Leaning strong"),
                                   ("GBP", 1, 3, "Leaning weak"), ("JPY", 0, 3, "Weak")])
        usd = out["currencies"][0]
        self.assertEqual(usd["beats"], ["EUR", "GBP", "JPY"])
        # From USD's side: +0.30 against EUR, +0.50 against GBP, +0.40 against JPY.
        self.assertAlmostEqual(usd["avg_pct"], 0.4, places=6)
        self.assertAlmostEqual(out["matrix"]["JPY"]["USD"], -0.4, places=6)
        eurusd = out["pairs"][0]
        self.assertEqual((eurusd["symbol"], eurusd["open"]), ("EURUSD", 1.1))  # the first tick of the day, not yesterday's
        self.assertAlmostEqual(eurusd["pips"], -33.0, places=1)

    def test_gold_silver_and_oil_stand_on_their_own(self):
        out = analysis.measure(market(), AT)
        own = {r["symbol"]: round(r["change_pct"], 6) for r in out["others"]}
        self.assertEqual(own, {"XAUUSD": 1.2, "XAGUSD": -0.4, "WTI": -2.0, "BRENT": -1.5})
        self.assertEqual(sum(r["played"] for r in out["currencies"]), 12)  # six pairs, two sides each

    def test_nothing_after_the_clock_is_read(self):
        # A tick a minute after the clock reverses everything; it must not count.
        later = analysis.measure(market(after=-5.0), AT)
        self.assertEqual([r["currency"] for r in later["currencies"]], ["USD", "EUR", "GBP", "JPY"])
        self.assertEqual(later["pairs"][0]["last_ms"], (AT - MIN) // 1_000_000)

    def test_ties_in_wins_go_to_the_bigger_average_move(self):
        moves = dict(MOVES, EURUSD=+0.30)  # EUR now beats USD too: EUR 3/3, USD 2/3
        out = analysis.measure(market(moves), AT)
        self.assertEqual([r["currency"] for r in out["currencies"]][:2], ["EUR", "USD"])

    def test_a_pair_with_no_tick_today_is_left_out_of_the_matches(self):
        m = market()
        m["GBPJPY"] = [source([(OPEN - 30 * MIN, 190.0)])]  # nothing since the day opened
        out = analysis.measure(m, AT)
        gbpjpy = [p for p in out["pairs"] if p["symbol"] == "GBPJPY"][0]
        self.assertEqual(gbpjpy["state"], "no ticks today")
        jpy = [r for r in out["currencies"] if r["currency"] == "JPY"][0]
        self.assertEqual((jpy["played"], jpy["wins"]), (2, 0))

    def test_on_a_saturday_it_is_fridays_day(self):
        friday_open, friday_late = ns("2026-11-12T22:00"), ns("2026-11-13T21:50")
        m = {s: [source([(friday_open + MIN, START[s]), (friday_late, START[s] * (1 + p / 100))])]
             for s, p in MOVES.items()}
        out = analysis.measure(m, ns("2026-11-14T12:00"))
        self.assertEqual(out["day_open_ms"], friday_open // 1_000_000)
        self.assertEqual(out["currencies"][0]["currency"], "USD")

    def test_a_more_current_feed_wins_over_a_stale_one(self):
        stale = source([(OPEN - 26 * 60 * MIN, 1.0)])  # Dukascopy a day behind
        live = market()["EURUSD"][0]
        m = market()
        m["EURUSD"] = [stale, live]
        out = analysis.measure(m, AT)
        self.assertEqual(out["pairs"][0]["state"], "ok")
        self.assertEqual(out["pairs"][0]["open"], 1.1)


if __name__ == "__main__":
    unittest.main()
