"""Fill rules on hand-made ticks (guide step 4.7).

One case per row of the fill table in broker.py, a weekend gap through a
stop, two quotes in the same instant, the next-tick rule, sizing by risk, a
profit checked to the cent, and the vectorised path against the reference.
"""

import random
import unittest

import numpy as np

from simulator.engine.broker import BUY, LIMIT, MARKET, SELL, STOP, Broker, OrderError

S = 1_000_000_000  # one second in ns


def broker(**kw) -> Broker:
    b = Broker(balance=10_000.0, leverage=30.0, **kw)
    b.on_tick(0, 1.10000, 1.10010)  # a quote to validate against
    return b


class MarketOrders(unittest.TestCase):
    def test_market_buy_fills_at_next_ask(self):
        b = broker()
        b.place(BUY, MARKET, 0, lots=1)
        self.assertEqual(b.positions, [])  # not on the tick being displayed
        b.on_tick(1 * S, 1.10020, 1.10030)
        self.assertEqual(b.positions[0].entry_price, 1.10030)

    def test_market_sell_fills_at_next_bid(self):
        b = broker()
        b.place(SELL, MARKET, 0, lots=1)
        b.on_tick(1 * S, 1.10020, 1.10030)
        self.assertEqual(b.positions[0].entry_price, 1.10020)

    def test_order_placed_at_a_tick_time_ignores_that_tick(self):
        b = broker()
        b.place(BUY, MARKET, 5 * S, lots=1)
        b.on_tick(5 * S, 1.2, 1.2001)  # same instant as placement
        self.assertEqual(b.positions, [])
        b.on_tick(6 * S, 1.10000, 1.10010)
        self.assertEqual(b.positions[0].entry_price, 1.10010)

    def test_slippage_is_adverse_on_market_fills(self):
        b = broker(slippage_pips=0.5)
        b.place(BUY, MARKET, 0, lots=1)
        b.on_tick(1 * S, 1.10000, 1.10010)
        self.assertAlmostEqual(b.positions[0].entry_price, 1.10015, places=6)


class PendingOrders(unittest.TestCase):
    def test_buy_limit_triggers_on_ask_at_or_below(self):
        b = broker()
        b.place(BUY, LIMIT, 0, lots=1, price=1.09950)
        b.on_tick(1 * S, 1.09945, 1.09955)  # ask above P: nothing
        self.assertEqual(b.positions, [])
        b.on_tick(2 * S, 1.09940, 1.09950)  # ask == P
        self.assertEqual(b.positions[0].entry_price, 1.09950)

    def test_buy_limit_gapped_below_fills_at_ask(self):
        b = broker()
        b.place(BUY, LIMIT, 0, lots=1, price=1.09950)
        b.on_tick(1 * S, 1.09900, 1.09910)
        self.assertEqual(b.positions[0].entry_price, 1.09910)

    def test_sell_limit_triggers_on_bid_at_or_above(self):
        b = broker()
        b.place(SELL, LIMIT, 0, lots=1, price=1.10100)
        b.on_tick(1 * S, 1.10095, 1.10105)
        self.assertEqual(b.positions, [])
        b.on_tick(2 * S, 1.10100, 1.10110)
        self.assertEqual(b.positions[0].entry_price, 1.10100)

    def test_sell_limit_gapped_above_fills_at_bid(self):
        b = broker()
        b.place(SELL, LIMIT, 0, lots=1, price=1.10100)
        b.on_tick(1 * S, 1.10150, 1.10160)
        self.assertEqual(b.positions[0].entry_price, 1.10150)

    def test_buy_stop_fills_at_ask_which_can_be_worse(self):
        b = broker()
        b.place(BUY, STOP, 0, lots=1, price=1.10100)
        b.on_tick(1 * S, 1.10080, 1.10090)
        self.assertEqual(b.positions, [])
        b.on_tick(2 * S, 1.10120, 1.10130)  # jumped past P
        self.assertEqual(b.positions[0].entry_price, 1.10130)

    def test_sell_stop_fills_at_bid_which_can_be_worse(self):
        b = broker()
        b.place(SELL, STOP, 0, lots=1, price=1.09900)
        b.on_tick(1 * S, 1.09910, 1.09920)
        self.assertEqual(b.positions, [])
        b.on_tick(2 * S, 1.09870, 1.09880)
        self.assertEqual(b.positions[0].entry_price, 1.09870)

    def test_orders_on_the_wrong_side_are_refused(self):
        b = broker()
        with self.assertRaises(OrderError):
            b.place(BUY, LIMIT, 0, lots=1, price=1.10050)  # above the ask
        with self.assertRaises(OrderError):
            b.place(BUY, STOP, 0, lots=1, price=1.09990)  # below the ask
        with self.assertRaises(OrderError):
            b.place(SELL, LIMIT, 0, lots=1, price=1.09990)
        with self.assertRaises(OrderError):
            b.place(SELL, STOP, 0, lots=1, price=1.10050)
        with self.assertRaises(OrderError):
            b.place(BUY, MARKET, 0, lots=1, sl=1.10050)  # stop above entry

    def test_cancel(self):
        b = broker()
        o = b.place(BUY, LIMIT, 0, lots=1, price=1.09)
        b.cancel(o.id)
        b.on_tick(1 * S, 1.0, 1.0001)
        self.assertEqual(b.positions, [])


class Exits(unittest.TestCase):
    def open_long(self, b, sl=None, tp=None):
        b.place(BUY, MARKET, 0, lots=1, sl=sl, tp=tp)
        b.on_tick(1 * S, 1.10000, 1.10010)
        return b.positions[0]

    def open_short(self, b, sl=None, tp=None):
        b.place(SELL, MARKET, 0, lots=1, sl=sl, tp=tp)
        b.on_tick(1 * S, 1.10000, 1.10010)
        return b.positions[0]

    def test_long_stop_loss_checked_against_bid(self):
        b = broker()
        self.open_long(b, sl=1.09800)
        b.on_tick(2 * S, 1.09801, 1.09811)  # Bid a tenth of a pip above: nothing
        self.assertEqual(len(b.positions), 1)
        b.on_tick(3 * S, 1.09790, 1.09800)
        t = b.drain_events()[-1]["trade"]
        self.assertEqual((t["exit_reason"], t["exit_price"]), ("sl", 1.09790))

    def test_long_take_profit_checked_against_bid(self):
        b = broker()
        self.open_long(b, tp=1.10200)
        b.on_tick(2 * S, 1.10195, 1.10205)  # ask above TP does not matter
        self.assertEqual(len(b.positions), 1)
        b.on_tick(3 * S, 1.10210, 1.10220)
        t = b.drain_events()[-1]["trade"]
        self.assertEqual((t["exit_reason"], t["exit_price"]), ("tp", 1.10210))

    def test_short_stop_loss_checked_against_ask(self):
        b = broker()
        self.open_short(b, sl=1.10200)
        b.on_tick(2 * S, 1.10195, 1.10199)
        self.assertEqual(len(b.positions), 1)
        b.on_tick(3 * S, 1.10195, 1.10205)
        t = b.drain_events()[-1]["trade"]
        self.assertEqual((t["exit_reason"], t["exit_price"]), ("sl", 1.10205))

    def test_short_take_profit_checked_against_ask(self):
        b = broker()
        self.open_short(b, tp=1.09800)
        b.on_tick(2 * S, 1.09795, 1.09805)  # bid below TP does not matter
        self.assertEqual(len(b.positions), 1)
        b.on_tick(3 * S, 1.09790, 1.09800)
        t = b.drain_events()[-1]["trade"]
        self.assertEqual((t["exit_reason"], t["exit_price"]), ("tp", 1.09800))

    def test_weekend_gap_through_a_stop_fills_at_the_gap_price(self):
        b = broker()
        self.open_long(b, sl=1.09800)
        friday = 2 * S
        monday = friday + 48 * 3600 * S
        b.on_tick(friday, 1.09900, 1.09910)
        b.on_tick(monday, 1.09500, 1.09520)  # opens 30 pips below the stop
        t = b.drain_events()[-1]["trade"]
        self.assertEqual((t["exit_reason"], t["exit_price"]), ("sl", 1.09500))
        self.assertEqual(t["pips"], -51.0)  # 1.10010 -> 1.09500, not the planned -21
        self.assertEqual(t["net_usd"], -510.0)

    def test_two_quotes_in_the_same_instant_apply_in_file_order(self):
        # Two ticks share a timestamp; the first reaches the target, so the
        # second (which would hit the stop) finds no position left.
        b = broker()
        self.open_long(b, sl=1.09800, tp=1.10200)
        b.on_ticks(
            np.array([5 * S, 5 * S], np.int64),
            np.array([1.10200, 1.09790]),
            np.array([1.10210, 1.09800]),
        )
        t = b.drain_events()[-1]["trade"]
        self.assertEqual(t["exit_reason"], "tp")
        self.assertEqual(b.positions, [])

    def test_stop_loss_wins_when_one_tick_satisfies_both(self):
        # The broker refuses crossed levels, so set them directly to pin
        # down which exit wins if both ever hold on one quote.
        b = broker()
        pos = self.open_long(b)
        pos.sl, pos.tp = 1.10100, 1.09900  # crossed on purpose
        b.on_tick(2 * S, 1.10000, 1.10010)
        t = b.drain_events()[-1]["trade"]
        self.assertEqual(t["exit_reason"], "sl")

    def test_position_opened_by_a_tick_can_be_stopped_by_the_same_tick(self):
        # Buy limit gaps down through its own stop: filled, then stopped.
        b = broker()
        b.place(BUY, LIMIT, 0, lots=1, price=1.09950, sl=1.09900)
        b.on_tick(1 * S, 1.09850, 1.09860)
        kinds = [e["kind"] for e in b.drain_events()]
        self.assertEqual(kinds[-2:], ["filled", "closed"])
        self.assertEqual(b.positions, [])

    def test_manual_close_waits_for_the_next_tick(self):
        b = broker()
        pos = self.open_long(b)
        b.close(pos.id, 1 * S)
        self.assertEqual(len(b.positions), 1)
        b.on_tick(2 * S, 1.10050, 1.10060)
        t = b.drain_events()[-1]["trade"]
        self.assertEqual((t["exit_reason"], t["exit_price"]), ("manual", 1.10050))


class Money(unittest.TestCase):
    def test_guide_example_risk_sizing(self):
        # 10,000 USD, 1% risk, 20 pip stop -> 0.5 lot.
        b = broker()
        self.assertEqual(b.lots_for_risk(1.0, 1.10000, 1.09800), 0.5)
        o = b.place(BUY, LIMIT, 0, risk_pct=1.0, price=1.09900, sl=1.09700)
        self.assertEqual(o.lots, 0.5)

    def test_risk_sizing_rounds_down(self):
        b = broker()
        # 100 USD / (13 pips x 10) = 0.769 -> 0.76
        self.assertEqual(b.lots_for_risk(1.0, 1.10000, 1.09870), 0.76)

    def test_guide_example_loss(self):
        # 0.5 lot, 20 pip stop: loss = 0.5 x 20 x 10 = 100 USD.
        b = broker()
        b.place(BUY, LIMIT, 0, lots=0.5, price=1.09900, sl=1.09700)
        b.on_tick(1 * S, 1.09890, 1.09900)
        b.on_tick(2 * S, 1.09700, 1.09710)
        t = b.drain_events()[-1]["trade"]
        self.assertEqual(t["net_usd"], -100.0)
        self.assertEqual(t["r_multiple"], -1.0)
        self.assertEqual(b.balance, 9_900.0)

    def test_profit_to_the_cent_with_commission(self):
        # 0.37 lot long 1.08431 -> 1.08797 = 36.6 pips.
        # gross = 0.00366 x 0.37 x 100,000 = 135.42; commission 0.37 x 7 = 2.59.
        b = Broker(balance=10_000.0, commission_per_lot=7.0)
        b.on_tick(0, 1.08420, 1.08431)
        b.place(BUY, MARKET, 0, lots=0.37, sl=1.08231)
        b.on_tick(1 * S, 1.08420, 1.08431)
        pos = b.positions[0]
        b.close(pos.id, 1 * S)
        b.on_tick(2 * S, 1.08797, 1.08805)
        t = b.drain_events()[-1]["trade"]
        self.assertEqual(t["gross_usd"], 135.42)
        self.assertEqual(t["commission_usd"], 2.59)
        self.assertEqual(t["net_usd"], 132.83)
        self.assertEqual(t["risk_usd"], 74.0)  # 20 pips x 0.37 x 10
        self.assertEqual(t["r_multiple"], round(132.83 / 74.0, 3))
        self.assertEqual(b.balance, 10_132.83)

    def test_margin_is_checked_when_placing(self):
        b = Broker(balance=1_000.0, leverage=30.0)
        b.on_tick(0, 1.10000, 1.10010)
        with self.assertRaises(OrderError):
            b.place(BUY, MARKET, 0, lots=1)  # needs ~3,667 USD

    def test_margin_is_checked_again_at_fill(self):
        # Each order fits alone; once the first fills, the second does not.
        b = Broker(balance=5_000.0, leverage=30.0)
        b.on_tick(0, 1.10000, 1.10010)
        b.place(BUY, MARKET, 0, lots=1)
        b.place(BUY, MARKET, 0, lots=1)
        b.on_tick(1 * S, 1.10000, 1.10010)
        self.assertEqual(len(b.positions), 1)
        self.assertEqual(b.drain_events()[-1]["kind"], "rejected")

    def test_state_round_trip(self):
        b = broker()
        b.place(BUY, LIMIT, 0, lots=1, price=1.09, sl=1.08)
        b.place(SELL, MARKET, 0, lots=0.2, tp=1.0)
        b.on_tick(1 * S, 1.10000, 1.10010)
        c = Broker.from_state(b.to_state())
        self.assertEqual(c.to_state(), b.to_state())


class BatchMatchesReference(unittest.TestCase):
    """on_ticks must give exactly what on_tick on every quote gives."""

    def test_random_paths(self):
        rng = random.Random(7)
        for trial in range(60):
            n = 3_000
            steps = np.array([rng.gauss(0, 0.00008) for _ in range(n)])
            bid = np.round(1.1 + np.cumsum(steps), 5)
            ask = np.round(bid + rng.choice([0.00002, 0.00008, 0.0002]), 5)
            ts = np.cumsum(np.array([rng.choice([0, 1, 2, 30]) for _ in range(n)])) * S + S
            ref, fast = broker(), broker()
            for b in (ref, fast):
                r = random.Random(trial)
                for _ in range(6):
                    side = r.choice([BUY, SELL])
                    typ = r.choice([MARKET, LIMIT, STOP])
                    dist = r.uniform(0.0005, 0.003)
                    ref_px = b.ask if side == BUY else b.bid
                    price = None
                    if typ == LIMIT:
                        price = ref_px - dist if side == BUY else ref_px + dist
                    elif typ == STOP:
                        price = ref_px + dist if side == BUY else ref_px - dist
                    entry = price if price is not None else ref_px
                    sl = entry - 0.002 if side == BUY else entry + 0.002
                    tp = entry + 0.003 if side == BUY else entry - 0.003
                    b.place(side, typ, 0, lots=0.1, price=price, sl=sl, tp=tp)
            for i in range(n):
                ref.on_tick(int(ts[i]), float(bid[i]), float(ask[i]))
            fast.on_ticks(ts, bid, ask)
            self.assertEqual(ref.to_state(), fast.to_state(), f"trial {trial}")
            self.assertEqual(ref.drain_events(), fast.drain_events(), f"trial {trial}")


if __name__ == "__main__":
    unittest.main()
