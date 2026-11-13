"""Nothing the screen receives may depend on a tick after the clock.

The direct check (guide step 2): rebuild every candle from raw ticks with
timestamp <= sim_time and require that what the session sends is identical,
for the full history message and for every incremental frame during play.

Runs on synthetic ticks always, and on the real cache when it is built.
"""

import random
import time
import unittest
from datetime import date, datetime, timezone

import numpy as np

from simulator.data.bars import TIMEFRAMES, BarStore, bar_dict, bars_from_ticks
from simulator.data.ticks import NS_PER_SECOND, MemoryTickSource, ParquetTickSource, day_start_ns
from simulator.engine.broker import Broker
from simulator.engine.clock import MAX_SPEED, FRAME_SECONDS
from simulator.engine.session import DEAD_GAP_NS, ReplaySession
from simulator import config

S = NS_PER_SECOND


def synthetic_source(seed: int = 1) -> MemoryTickSource:
    """Mon-Fri ticks for two weeks, with a weekend gap and a quiet hour."""
    rng = np.random.default_rng(seed)
    ts_parts, bid_parts = [], []
    price = 1.1
    for d in range(14):
        day = date(2024, 3, 4 + d)  # Monday 4 March 2024
        if day.weekday() >= 5:
            continue
        start = day_start_ns(day) + 30 * 60 * S
        n = 6000
        gaps = rng.exponential(12.0, n) * S
        ts = start + np.cumsum(gaps).astype(np.int64)
        ts = ts[ts < day_start_ns(day) + 86_400 * S]
        if d == 2:  # an hour of silence mid-day, longer than DEAD_GAP
            mid = len(ts) // 2
            ts[mid:] += 3600 * S
            ts = ts[ts < day_start_ns(day) + 86_400 * S]
        steps = rng.normal(0, 0.00005, len(ts))
        bid = np.round(price + np.cumsum(steps), 5)
        price = float(bid[-1])
        ts_parts.append(ts)
        bid_parts.append(bid)
    ts = np.concatenate(ts_parts)
    bid = np.concatenate(bid_parts)
    ask = np.round(bid + 0.00008, 5)
    return MemoryTickSource(ts, bid, ask)


def all_ticks(source):
    days = [source.load_day(d) for d in source.days()]
    return (
        np.concatenate([d.ts for d in days]),
        np.concatenate([d.bid for d in days]),
        np.concatenate([d.ask for d in days]),
    )


def rebuilt(ts, bid, ask, sim_ns: int, tf: str) -> list[dict]:
    k = int(np.searchsorted(ts, sim_ns, side="right"))
    bars = bars_from_ticks(ts[:k], bid[:k], ask[:k], TIMEFRAMES[tf][0])
    return [bar_dict(bars, i) for i in range(len(bars["time"]))]


class SyntheticNoLookahead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = synthetic_source()
        cls.bars = BarStore.build(cls.source)
        cls.ts, cls.bid, cls.ask = all_ticks(cls.source)

    def session(self, start_ns: int) -> ReplaySession:
        return ReplaySession(self.source, self.bars, Broker(), start_ns, "m15")

    def test_history_matches_ticks_up_to_the_clock_at_100_random_times(self):
        rng = random.Random(3)
        lo, hi = int(self.ts[0]) - 3600 * S, int(self.ts[-1])
        for _ in range(100):
            s = self.session(rng.randrange(lo, hi))
            for tf in TIMEFRAMES:
                got = s.history(tf, limit=10**9)
                self.assertEqual(got, rebuilt(self.ts, self.bid, self.ask, s.sim_ns, tf), tf)
            q = s.quote()
            k = int(np.searchsorted(self.ts, s.sim_ns, side="right"))
            if k:
                self.assertEqual(q["time_ms"], int(self.ts[k - 1]) // 1_000_000)
                self.assertEqual((q["bid"], q["ask"]), (float(self.bid[k - 1]), float(self.ask[k - 1])))

    def test_every_frame_during_play_matches_ticks_up_to_the_clock(self):
        rng = random.Random(5)
        for trial in range(8):
            tf = rng.choice(list(TIMEFRAMES))
            s = self.session(int(self.ts[0]) + rng.randrange(0, 3 * 86_400) * S)
            s.set_timeframe(tf)
            screen = {b["time"]: b for b in s.history()}
            for _ in range(400):
                if s.finished:
                    break
                s.advance(s.sim_ns + int(rng.choice([0.5, 5, 60, 900, 4000]) * S))
                if rng.random() < 0.05:  # a timeframe switch mid-replay
                    tf = rng.choice(list(TIMEFRAMES))
                    s.set_timeframe(tf)
                bars = s.frame_bars()
                if bars is None:
                    bars = s.history()
                    screen = {}
                truth = {b["time"]: b for b in rebuilt(self.ts, self.bid, self.ask, s.sim_ns, tf)}
                for b in bars:
                    self.assertEqual(b, truth[b["time"]], f"trial {trial} tf {tf}")
                    screen[b["time"]] = b
                # What the chart holds now equals the truth at the clock.
                recent = sorted(screen)[-50:]
                self.assertEqual([screen[t] for t in recent], [truth[t] for t in recent])

    def test_dead_time_jumps_only_after_the_market_was_silent(self):
        # Friday's last tick, then the clock is moved past it.
        friday = date(2024, 3, 8)
        t = self.source.load_day(friday)
        last = int(t.ts[-1])
        s = self.session(last)
        s.advance(last + DEAD_GAP_NS - S)  # silent, but not long enough
        self.assertEqual(s.sim_ns, last + DEAD_GAP_NS - S)
        s.advance(last + DEAD_GAP_NS + S)  # now long enough: to Monday
        monday = self.source.load_day(date(2024, 3, 11))
        self.assertEqual(s.sim_ns, int(monday.ts[0]))

    def test_jumping_back_with_a_position_is_refused(self):
        s = self.session(int(self.ts[5000]))
        s.broker.place("buy", "market", s.sim_ns, lots=0.1)
        s.advance(s.sim_ns + 60 * S)
        with self.assertRaises(Exception):
            s.jump(int(self.ts[100]))

    def test_jumping_forward_with_a_position_feeds_every_tick(self):
        s = self.session(int(self.ts[5000]))
        s.broker.place("buy", "market", s.sim_ns, lots=0.1, sl=round(s.broker.bid - 0.0005, 5))
        target = int(self.ts[40_000])
        s.jump(target)
        # The stop is 5 pips away and a random walk crosses that quickly.
        ref = Broker()
        ref.on_tick(int(self.ts[5000]), float(self.bid[5000]), float(self.ask[5000]))
        ref.place("buy", "market", int(self.ts[5000]), lots=0.1, sl=round(float(self.bid[5000]) - 0.0005, 5))
        for i in range(5001, 40_001):
            ref.on_tick(int(self.ts[i]), float(self.bid[i]), float(self.ask[i]))
        self.assertEqual(s.broker.balance, ref.balance)
        self.assertEqual(len(s.broker.positions), len(ref.positions))


@unittest.skipUnless(BarStore.exists() and (config.TICK_CACHE_DIR / "manifest.json").exists(), "cache not built")
class RealDataNoLookahead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = ParquetTickSource()
        cls.bars = BarStore.load()

    def test_100_random_times(self):
        rng = random.Random(11)
        days = self.source.days()
        for _ in range(100):
            day = rng.choice(days)
            start = day_start_ns(day) + rng.randrange(0, 86_400) * S
            s = ReplaySession(self.source, self.bars, Broker(), start, "m15")
            t = self.source.load_day(datetime.fromtimestamp(s.sim_ns / 1e9, timezone.utc).date())
            day0 = day_start_ns(t.day) // S
            for tf in TIMEFRAMES:
                got = [b for b in s.history(tf, limit=1500) if b["time"] >= day0]  # > 1440 one-minute candles
                want = rebuilt(t.ts, t.bid, t.ask, s.sim_ns, tf)
                self.assertEqual(got, want, f"{tf} at {s.sim_ns}")

    def test_a_full_day_at_max_speed_takes_seconds(self):
        day = date(2024, 11, 6)  # US election result day, ~250k ticks
        s = ReplaySession(self.source, self.bars, Broker(), day_start_ns(day), "m1")
        s.history()
        s.broker.place("buy", "market", s.sim_ns, lots=0.1, sl=round(s.broker.bid - 0.003, 5))
        frame_ns = int(FRAME_SECONDS * MAX_SPEED * S)
        t0 = time.perf_counter()
        frames = 0
        while s.sim_ns < day_start_ns(day) + 86_400 * S and not s.finished:
            s.advance(s.sim_ns + frame_ns)
            if s.frame_bars() is None:
                s.history()
            frames += 1
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 3.0, f"{frames} frames took {elapsed:.2f}s of compute")


if __name__ == "__main__":
    unittest.main()
