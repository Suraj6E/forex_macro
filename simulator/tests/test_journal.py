"""Journal store and statistics (guide step 5).

The statistics are checked against numbers worked out by hand from the
trades, which is the step's own done-criterion.
"""

import tempfile
import unittest
from pathlib import Path

from simulator.engine.broker import BUY, MARKET, Broker
from simulator.journal import stats
from simulator.journal.store import Journal

S = 1_000_000_000
HOUR = 3600 * S
# Monday 2024-03-11 00:00 UTC
MON = 1_710_115_200 * S


def trade(i, net, r, entry_ns, hold_h=1.0, tags=""):
    return {"id": i, "net_usd": net, "r_multiple": r, "entry_ns": entry_ns,
            "exit_ns": entry_ns + int(hold_h * HOUR), "exit_reason": "tp" if net > 0 else "sl", "tags": tags}


class Stats(unittest.TestCase):
    def setUp(self):
        self.trades = [
            trade(1, 200.0, 2.0, MON + 8 * HOUR, tags="breakout"),            # Mon London
            trade(2, -100.0, -1.0, MON + 13 * HOUR, tags="breakout,fomo"),    # Mon overlap
            trade(3, 50.0, 0.5, MON + 24 * HOUR + 17 * HOUR),                 # Tue New York
            trade(4, -100.0, -1.0, MON + 48 * HOUR + 2 * HOUR, hold_h=3),     # Wed Asia
            trade(5, 0.0, None, MON + 48 * HOUR + 9 * HOUR),                  # Wed London, no stop
        ]

    def test_headline_numbers_match_hand_arithmetic(self):
        out = stats.compute(self.trades, starting_balance=10_000.0)
        self.assertEqual(out["trades"], 5)
        self.assertEqual((out["wins"], out["losses"], out["breakeven"]), (2, 2, 1))
        self.assertEqual(out["win_rate"], 0.4)
        self.assertEqual(out["net_usd"], 50.0)
        self.assertEqual(out["profit_factor"], 1.25)          # 250 / 200
        self.assertEqual(out["expectancy_r"], 0.125)          # (2 - 1 + 0.5 - 1) / 4
        self.assertEqual(out["trades_with_r"], 4)
        self.assertEqual(out["avg_win_r"], 1.25)              # (2 + 0.5) / 2
        self.assertEqual(out["avg_loss_r"], -1.0)
        # equity 10000 -> 10200 -> 10100 -> 10150 -> 10050 -> 10050
        self.assertEqual(out["max_drawdown_usd"], 150.0)
        self.assertEqual(out["max_drawdown_pct"], round(100 * 150 / 10_200, 2))
        self.assertEqual([p["equity"] for p in out["equity_curve"]], [10_000, 10_200, 10_100, 10_150, 10_050, 10_050])
        self.assertEqual(out["avg_hold_seconds"], round((1 + 1 + 1 + 3 + 1) * 3600 / 5))
        self.assertTrue(out["small_sample"])

    def test_splits(self):
        out = stats.compute(self.trades)
        by_session = {r["label"].split(" (")[0]: r["trades"] for r in out["by_session"]}
        self.assertEqual(by_session, {"Asia": 1, "London": 2, "London/NY overlap": 1, "New York": 1})
        by_day = {r["label"]: r["trades"] for r in out["by_weekday"]}
        self.assertEqual(by_day, {"Mon": 2, "Tue": 1, "Wed": 2, "Thu": 0, "Fri": 0})
        by_tag = {r["label"]: (r["trades"], r["net_usd"]) for r in out["by_tag"]}
        self.assertEqual(by_tag, {"breakout": (2, 100.0), "fomo": (1, -100.0)})
        self.assertEqual(out["curve_kind"], "cumulative profit")

    def test_win_rate_range(self):
        # Guide: 30 trades near 50% -> about +-17 points; 100 trades -> about +-10.
        lo, hi = stats.wilson(15, 30)
        self.assertAlmostEqual(lo, 0.3315, places=3)
        self.assertAlmostEqual(hi, 0.6685, places=3)
        lo, hi = stats.wilson(50, 100)
        self.assertAlmostEqual((hi - lo) / 2, 0.096, places=2)
        lo, hi = stats.wilson(0, 5)  # does not collapse at 0%
        self.assertEqual(lo, 0.0)
        self.assertGreater(hi, 0.4)
        self.assertIsNone(stats.wilson(0, 0))

    def test_empty(self):
        out = stats.compute([], starting_balance=10_000.0)
        self.assertEqual(out["trades"], 0)
        self.assertIsNone(out["win_rate"])
        self.assertIsNone(out["profit_factor"])


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.j = Journal(Path(self.tmp.name) / "j.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_orders_trades_round_trip(self):
        b = Broker(balance=10_000.0, commission_per_lot=7.0)
        sid = self.j.create_session(start_ns=MON, timeframe="m15", broker_state=b.to_state(), hide_dates=True, name="t")
        b.on_tick(MON, 1.08000, 1.08010)
        b.place(BUY, MARKET, MON, lots=0.5, sl=1.07800)
        b.on_tick(MON + S, 1.08000, 1.08010)
        b.close(b.positions[0].id, MON + S)
        b.on_tick(MON + 2 * S, 1.08210, 1.08220)
        for e in b.drain_events():
            if "order" in e:
                self.j.upsert_order(sid, e["order"])
            if e["kind"] == "closed":
                self.j.insert_trade(sid, e["trade"])
        self.j.save_state(sid, {"sim_ns": MON + 2 * S, "timeframe": "h1", "speed": 60.0, "broker": b.to_state()})

        row = self.j.get_session(sid)
        self.assertEqual((row["sim_ns"], row["timeframe"], row["hide_dates"]), (MON + 2 * S, "h1", 1))
        self.assertEqual(Broker.from_state(row["state"]["broker"]).balance, b.balance)
        self.assertEqual([o["status"] for o in self.j.orders(sid)], ["filled"])
        [t] = self.j.trades(sid)
        # 0.5 lot, 1.08010 -> 1.08210 = 20 pips = 100 USD gross, 3.50 commission.
        self.assertEqual((t["gross_usd"], t["commission_usd"], t["net_usd"]), (100.0, 3.5, 96.5))

        self.j.annotate_trade(sid, t["id"], tags=" Breakout, ,Plan ", note="clean retest")
        [t] = self.j.trades(sid)
        self.assertEqual((t["tags"], t["note"]), ("breakout,plan", "clean retest"))

        sessions = self.j.list_sessions()
        self.assertEqual((sessions[0]["trades"], sessions[0]["net_usd"]), (1, 96.5))

    def test_drawings_hidden_before_they_were_drawn(self):
        sid = self.j.create_session(start_ns=MON, timeframe="m15", broker_state=Broker().to_state(), hide_dates=False)
        self.j.save_drawing(sid, {"id": "a", "kind": "hline", "points": [{"time": 1, "price": 1.1}], "created_sim_ns": MON + 14 * HOUR})
        self.j.save_drawing(sid, {"id": "b", "kind": "hline", "points": [{"time": 1, "price": 1.2}], "created_sim_ns": MON + 8 * HOUR})
        self.assertEqual([d["id"] for d in self.j.drawings(sid, visible_at_ns=MON + 9 * HOUR)], ["b"])
        self.assertEqual([d["id"] for d in self.j.drawings(sid, visible_at_ns=MON + 15 * HOUR)], ["b", "a"])
        # Editing a drawing keeps the time it was first drawn.
        self.j.save_drawing(sid, {"id": "b", "kind": "hline", "points": [{"time": 1, "price": 1.3}], "created_sim_ns": MON + 20 * HOUR})
        self.assertEqual(self.j.drawings(sid, visible_at_ns=MON + 9 * HOUR)[0]["points"][0]["price"], 1.3)

    def test_sessions_the_screen_opened_and_whether_anything_was_done(self):
        form = self.j.create_session(start_ns=MON, timeframe="m15", broker_state=Broker().to_state(), hide_dates=False)
        screen = self.j.create_session(start_ns=MON, timeframe="m15", broker_state=Broker().to_state(), hide_dates=False,
                                       mode="live", feed="mt5:ftmo-demo", origin="screen")
        self.assertEqual((self.j.get_session(form)["origin"], self.j.get_session(screen)["origin"]), ("form", "screen"))
        self.assertFalse(self.j.has_records(screen))
        self.j.save_drawing(screen, {"id": "a", "kind": "hline", "points": [{"time": 1, "price": 1.1}], "created_sim_ns": MON})
        self.assertTrue(self.j.has_records(screen))
        self.assertFalse(self.j.has_records(form))
        self.j.upsert_order(form, {"id": 1, "side": "buy", "type": "market", "lots": 0.1, "price": None, "sl": None,
                                   "tp": None, "risk_pct": None, "placed_ns": MON, "status": "pending", "filled_ns": None,
                                   "fill_price": None, "position_id": None, "reason": None})
        self.assertTrue(self.j.has_records(form))

    def test_a_journal_from_before_screen_sessions_gains_the_column(self):
        import sqlite3
        from contextlib import closing
        path = Path(self.tmp.name) / "old.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:  # the sessions table as it was
            db.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', symbol TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, start_ns INTEGER NOT NULL, sim_ns INTEGER NOT NULL,
                timeframe TEXT NOT NULL, starting_balance REAL NOT NULL, leverage REAL NOT NULL,
                commission_per_lot REAL NOT NULL, slippage_pips REAL NOT NULL, hide_dates INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active', mode TEXT NOT NULL DEFAULT 'replay',
                feed TEXT NOT NULL DEFAULT 'dukascopy', state TEXT NOT NULL)""")
            db.execute("""INSERT INTO sessions (id, symbol, created_at, updated_at, start_ns, sim_ns, timeframe,
                starting_balance, leverage, commission_per_lot, slippage_pips, state)
                VALUES ('old1', 'EURUSD', 'x', 'x', 1, 1, 'm15', 10000, 30, 0, 0, '{}')""")
        j = Journal(path)
        self.assertEqual(j.get_session("old1")["origin"], "form")  # never removed by the screen
        self.assertEqual([s["origin"] for s in j.list_sessions()], ["form"])

    def test_deleting_a_session_removes_its_rows(self):
        sid = self.j.create_session(start_ns=MON, timeframe="m15", broker_state=Broker().to_state(), hide_dates=False)
        self.j.add_note(sid, MON, "hello")
        self.j.save_snapshot(sid, 1, "entry", b"\x89PNG....")
        self.j.delete_session(sid)
        self.assertIsNone(self.j.get_session(sid))
        self.assertEqual(self.j.notes(sid), [])
        self.assertIsNone(self.j.snapshot(sid, 1, "entry"))


if __name__ == "__main__":
    unittest.main()
