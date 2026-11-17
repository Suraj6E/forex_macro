"""One replay: clock, candles and broker tied together (guide steps 2.3-2.6).

The rule everything rests on: `sim_ns` decides what exists. Every tick with a
timestamp <= sim_ns has been given to the broker, and nothing later has been
read for any purpose other than finding where the next tick is after the
market has already been silent for DEAD_GAP (weekends, holidays).

What the screen receives at sim_ns:
  * complete candles (end <= sim_ns) from the BarStore, and
  * the forming candle, built here from ticks in [candle start, sim_ns].
The stored version of the forming candle is never read: at 10:17 the stored
10:00 hourly candle already contains 10:18-10:59.
"""

from __future__ import annotations

import random
from bisect import bisect_left, bisect_right
from datetime import date

import numpy as np

from simulator.data.bars import BarStore, tf_seconds
from simulator.data.ticks import (
    NS_PER_SECOND,
    TickSource,
    day_of_ns,
    day_start_ns,
    now_ns,
)
from simulator.engine.broker import Broker
from simulator.engine.clock import Clock

#: After this much simulated time without a tick, the clock jumps to the next
#: tick. Waiting first means the jump reveals nothing the trader has not
#: already seen: the market was silent for this long.
DEAD_GAP_NS = 3 * 60 * NS_PER_SECOND

#: Complete candles sent with a full history message.
HISTORY_LIMIT = 1500

#: A frame that completes more candles than this sends a full history
#: instead of a list of updates.
MAX_FRAME_BARS = 400


#: Live sessions have no end of data.
OPEN_END = 2**62

#: In a quiet live market the candle clock trails this computer's clock by
#: this much, so a tick stamped just before a candle boundary that reaches
#: the disk a moment late (recording delay, or this clock running ahead of
#: the broker's) still lands in its own candle. A tick moves it on at once.
LIVE_LAG_NS = 2 * NS_PER_SECOND


class SessionError(ValueError):
    """A request the session refuses; the message is shown as is."""


class ReplaySession:
    """A replay, or with `live=True` a session whose clock is the wall clock.

    Live is the same engine on a growing feed: every tick recorded since the
    last one is given to the broker, whatever its stamp says about this
    computer's clock (`follow`), and there is nothing to step into or jump
    to, because the future does not exist yet. Orders still fill on the
    tick after the one on screen: they are stamped with that tick's time
    (`order_ns`), not with this computer's.
    """

    def __init__(
        self,
        source: TickSource,
        bars: BarStore,
        broker: Broker,
        start_ns: int,
        timeframe: str = "m15",
        session_id: str = "",
        live: bool = False,
    ) -> None:
        self.id = session_id
        self.source = source
        self.bars = bars
        self.broker = broker
        self.live = live
        if not self.days:
            raise SessionError("No tick data available")
        tf_seconds(timeframe)
        self.timeframe = timeframe
        self.clock = Clock(speed=1.0, playing=True) if live else Clock()
        self.first_ns = int(source.load_day(self.days[0]).ts[0])
        self.end_ns = OPEN_END if live else self._last_tick_ns()
        self.sim_ns = start_ns
        self.last_tick: tuple[int, float, float] | None = None
        self.finished = False
        self._last_sent_time: int | None = None
        self._reset_to(start_ns)

    @property
    def days(self) -> list[date]:
        return self.source.days()

    def _last_tick_ns(self) -> int:
        for day in reversed(self.days[-5:]):
            t = self.source.load_day(day)
            if len(t):
                return int(t.ts[-1])
        raise SessionError("No tick data available")

    def refresh(self) -> bool:
        """Pick up data added since the session started (an update ran).
        A replay that had reached the end can then play on."""
        if self.live or not self.source.refresh():
            return False
        self.end_ns = self._last_tick_ns()
        if self.sim_ns < self.end_ns:
            self.finished = False
        return True

    # -- time --------------------------------------------------------------

    def _reset_to(self, ns: int) -> None:
        ns = max(min(int(ns), self.end_ns), self.first_ns - 1)
        self.sim_ns = ns
        self.last_tick = self._last_tick_at_or_before(ns)
        if self.last_tick is not None:
            # The last quote before the clock is the past, so the broker may
            # know it; it is what an order placed now is validated against.
            self.broker.bid = self.last_tick[1]
            self.broker.ask = self.last_tick[2]
            self.broker.quote_ns = self.last_tick[0]
        self.finished = ns >= self.end_ns
        self._last_sent_time = None
        self._skip_dead_time()

    def jump(self, ns: int) -> None:
        """Move the clock to `ns` (guide step 2.5).

        Backwards only when flat: an open position cannot be carried into its
        own past. Forwards with orders or positions, every tick on the way is
        given to the broker, as fast as it will go. Forwards when flat, the
        clock simply moves and history reloads strictly before the new time.
        """
        ns = int(ns)
        if self.live:
            raise SessionError("A live session follows the clock; there is nowhere to jump")
        busy = bool(self.broker.pending or self.broker.positions)
        if ns < self.sim_ns and busy:
            raise SessionError("Close positions and cancel orders before jumping back in time")
        if ns > self.sim_ns and busy:
            self.advance(ns)
            self._last_sent_time = None
            return
        self._reset_to(ns)

    def advance(self, target_ns: int) -> bool:
        """Give the broker every tick in (sim_ns, target_ns], then move there."""
        if self.finished:
            return False
        target_ns = min(int(target_ns), self.end_ns)
        if target_ns <= self.sim_ns:
            return False
        a = self.sim_ns
        i0 = bisect_left(self.days, day_of_ns(max(a, 0)))
        i1 = bisect_right(self.days, day_of_ns(target_ns))
        for day in self.days[i0:i1]:
            t = self.source.load_day(day)
            lo = int(np.searchsorted(t.ts, a, side="right"))
            hi = int(np.searchsorted(t.ts, target_ns, side="right"))
            if hi > lo:
                self.broker.on_ticks(t.ts[lo:hi], t.bid[lo:hi], t.ask[lo:hi])
                self.last_tick = (int(t.ts[hi - 1]), float(t.bid[hi - 1]), float(t.ask[hi - 1]))
        self.sim_ns = target_ns
        if self.sim_ns >= self.end_ns:
            self.finished = True
            self.clock.playing = False
        self._skip_dead_time()
        return True

    def follow(self, now_ns: int, after_ns: int | None = None) -> bool:
        """Live: give the broker every tick recorded after the last one it
        was given (or after `after_ns`), then move the clock to the later of
        now less LIVE_LAG_NS and that tick.

        Ticks are taken in the order they reach the disk, not by comparing
        their stamps with this computer's clock: a clock running half a
        second ahead of the broker's would otherwise find every new tick
        already in its past and skip it, leaving the quote, and every order,
        stuck on the last tick before."""
        if after_ns is None:
            after_ns = self.last_tick[0] if self.last_tick is not None else self.sim_ns
        moved = False
        for day in self.days[bisect_left(self.days, day_of_ns(max(after_ns, 0))):]:
            t = self.source.load_day(day)
            lo = int(np.searchsorted(t.ts, after_ns, side="right"))
            if len(t.ts) > lo:
                self.broker.on_ticks(t.ts[lo:], t.bid[lo:], t.ask[lo:])
                self.last_tick = (int(t.ts[-1]), float(t.bid[-1]), float(t.ask[-1]))
                after_ns, moved = self.last_tick[0], True
        clock = int(now_ns) - LIVE_LAG_NS
        if self.last_tick is not None:
            clock = max(clock, self.last_tick[0])
        self.sim_ns = max(self.sim_ns, clock)
        return moved

    @property
    def order_ns(self) -> int:
        """The time an order, or a close, placed now is stamped with. In a
        replay, the clock. Live, the broker's stamp of the quote on screen,
        so it fills on the next tick the broker sends."""
        if self.live and self.last_tick is not None:
            return self.last_tick[0]
        return self.sim_ns

    def _skip_dead_time(self) -> None:
        if self.finished or self.live:
            return
        if self.last_tick is not None and self.sim_ns - self.last_tick[0] < DEAD_GAP_NS:
            return
        nxt = self._next_tick_after(self.sim_ns)
        if nxt is None:
            self.finished = True
            self.clock.playing = False
            return
        self.advance(nxt)

    def step(self, kind: str) -> None:
        """'tick': to the next tick. 'candle': to the next candle of the
        visible timeframe, so the forming one completes."""
        if self.live:
            raise SessionError("A live session cannot step ahead of the market")
        if kind == "tick":
            nxt = self._next_tick_after(self.sim_ns)
            if nxt is None:
                self.finished = True
                return
            self.advance(nxt)
        elif kind == "candle":
            width = tf_seconds(self.timeframe) * NS_PER_SECOND
            self.advance((self.sim_ns // width + 1) * width)
        else:
            raise SessionError(f"Unknown step {kind!r}")

    def _last_tick_at_or_before(self, ns: int) -> tuple[int, float, float] | None:
        i = bisect_right(self.days, day_of_ns(max(ns, 0))) - 1
        for _ in range(14):
            if i < 0:
                return None
            t = self.source.load_day(self.days[i])
            k = int(np.searchsorted(t.ts, ns, side="right"))
            if k > 0:
                return int(t.ts[k - 1]), float(t.bid[k - 1]), float(t.ask[k - 1])
            i -= 1
        return None

    def _next_tick_after(self, ns: int) -> int | None:
        i = bisect_left(self.days, day_of_ns(max(ns, 0)))
        while i < len(self.days):
            t = self.source.load_day(self.days[i])
            k = int(np.searchsorted(t.ts, ns, side="right"))
            if k < len(t.ts):
                return int(t.ts[k])
            i += 1
        return None

    # -- what the screen sees ---------------------------------------------

    def forming(self, tf: str) -> dict | None:
        """The candle of `tf` containing sim_ns, from ticks up to sim_ns only."""
        width = tf_seconds(tf) * NS_PER_SECOND
        start_ns = (self.sim_ns // width) * width
        t = self.source.load_day(day_of_ns(self.sim_ns))
        lo = int(np.searchsorted(t.ts, start_ns, side="left"))
        hi = int(np.searchsorted(t.ts, self.sim_ns, side="right"))
        if hi <= lo:
            return None
        seg = t.bid[lo:hi]
        return {
            "time": int(start_ns // NS_PER_SECOND),
            "open": float(seg[0]),
            "high": float(seg.max()),
            "low": float(seg.min()),
            "close": float(seg[-1]),
            "spread": float(t.ask[hi - 1] - t.bid[hi - 1]),
            "volume": hi - lo,  # ticks so far, never the candle's full count
        }

    def history(self, tf: str | None = None, limit: int = HISTORY_LIMIT, before: int | None = None) -> list[dict]:
        """Complete candles up to sim_ns, then the forming one.

        With `before` (epoch seconds), returns up to `limit` complete candles
        starting before that time instead, for scrolling back; no forming
        candle, and the frame cursor is left alone.
        """
        tf = tf or self.timeframe
        store = self.bars[tf]
        n = store.completed_count(self.sim_ns)
        if before is not None:
            n = min(n, store.index_of_time(int(before)))
            return store.slice(n - limit, n)
        out = store.slice(n - limit, n)
        f = self.forming(tf)
        if f:
            out.append(f)
        if tf == self.timeframe:
            self._last_sent_time = out[-1]["time"] if out else None
        return out

    def frame_bars(self) -> list[dict] | None:
        """Candles of the visible timeframe changed since the last send.

        The previously forming candle is resent in its completed form, then
        any candles that completed in between, then the new forming candle.
        None means too many changed: send a full history instead.
        """
        store = self.bars[self.timeframe]
        n = store.completed_count(self.sim_ns)
        if self._last_sent_time is None:
            return None
        a = store.index_of_time(self._last_sent_time)
        if n - a > MAX_FRAME_BARS:
            return None
        out = store.slice(a, n)
        f = self.forming(self.timeframe)
        if f:
            out.append(f)
        if out:
            self._last_sent_time = out[-1]["time"]
        return out

    def quote(self) -> dict | None:
        if self.last_tick is None:
            return None
        ns, bid, ask = self.last_tick
        return {"time_ms": ns // 1_000_000, "bid": bid, "ask": ask}

    def set_timeframe(self, tf: str) -> None:
        tf_seconds(tf)
        self.timeframe = tf
        self._last_sent_time = None

    def status(self) -> dict:
        return {
            "id": self.id,
            "sim_ms": self.sim_ns // 1_000_000,
            "timeframe": self.timeframe,
            "speed": self.clock.speed_label(),
            "playing": self.clock.playing,
            "finished": self.finished,
            "first_ms": self.first_ns // 1_000_000,
            "end_ms": None if self.live else self.end_ns // 1_000_000,
            "live": self.live,
            # Live, the clock on screen is this computer's; the candles
            # follow the ticks (sim_ms, up to LIVE_LAG_NS behind).
            "now_ms": now_ns() // 1_000_000 if self.live else None,
        }

    # -- persistence -------------------------------------------------------

    def to_state(self) -> dict:
        return {
            "sim_ns": self.sim_ns,
            "timeframe": self.timeframe,
            "speed": self.clock.speed,
            "broker": self.broker.to_state(),
        }

    @classmethod
    def from_state(cls, source: TickSource, bars: BarStore, state: dict, session_id: str,
                   live: bool = False, now: int | None = None, rate_fn=None) -> "ReplaySession":
        broker = Broker.from_state(state["broker"])
        broker.rate_fn = rate_fn
        seen = broker.quote_ns  # the last tick its orders and positions saw
        session = cls(source, bars, broker, state["sim_ns"], state["timeframe"], session_id, live=live)
        if live:
            # Everything recorded after that tick, including what arrived
            # while the session was closed, is given to its orders and
            # positions, in order, before it is shown.
            session.follow(now if now is not None else state["sim_ns"], after_ns=seen)
        else:
            session.clock.speed = state.get("speed", 60.0)
        return session


def random_start_ns(days: list[date], hour_utc: int = 6, rng: random.Random | None = None) -> int:
    """A weekday, away from the edges of the data, at `hour_utc`."""
    rng = rng or random.Random()
    candidates = [d for d in days[10:-2] if d.weekday() < 5]
    day = rng.choice(candidates or days)
    return day_start_ns(day) + hour_utc * 3600 * NS_PER_SECOND
