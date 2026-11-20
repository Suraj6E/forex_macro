"""The six concepts of "Six Trading Concepts on Real EURUSD" as written
rules, worked out at a session's clock for the Strategies tab.

Only candles complete at the clock are read (the price now is the last
tick), and everything found carries the time it became known: a swing once
the candles after it have closed, a zone once the move away from it has, a
sweep at the 15-minute close back. A replay therefore shows what could have
been seen then, and live runs the same rules.

The PDF's routine, from the daily chart down to the 1-minute chart:

  1 Side    EMA 20 of FX-day closes (17:00 New York): sells below a falling
            one, buys above a rising one. Flat (it moved less than a fifth
            of a day's average range in five days), or price on the other
            side: the halves of the 20-day range, sells in the upper half
            (premium), buys in the lower (discount).
  2 Level   1-hour: yesterday's high and low, equal highs and lows, the
            20-day range's edges (liquidity); supply and demand zones;
            order blocks and fair value gaps (Smart Money Concepts).
  3 Room    volume profile (tick volume per price) of this session and the
            one before: from above the value area there is room down to it,
            inside it to the point of control; below the point of control
            none (two-way); below the value area it is open (price left).
  4 Failure after price takes a level (a sweep: through it, then a
            15-minute close back) or enters a zone, a 1-minute close below
            the last small low (a change of character) within the hour;
            with price action on, a wide candle that closes near its low.
            The small low is the last swing low before the push's high.
  5 Entry   a limit at the level or the zone's near edge (at market when
            price is already past it), the stop 1.5 pips beyond the push's
            extreme or the zone's far edge, whichever is farther ("let the
            zone set the stop"), a fixed 10-pip target; skipped when the stop
            would need more than 10 pips or a check of steps 1 to 3 fails.

Buys are the mirror image. Each concept can be switched off; a setup needs a
level or a zone, so with liquidity, SMC and zones all off there are none.

A setup's result is what would have happened on the recorded prices: a sell
fills when the Bid reaches the entry and closes at the Ask (Bid plus the
candle's last spread); a limit waits an hour, a candle reaching the stop
and the target counts as the stop, and a candle that fills counts no win.
It is a way to test the
rules over many setups, as the PDF asks, never a trade.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from simulator.data.bars import FIELDS
from simulator.data.ticks import NS_PER_SECOND
from simulator.strength import day_start_s, session_start_s

CONCEPTS = ("liquidity", "smc", "zones", "price_action", "ema", "profile")
LABELS = {
    "liquidity": "Liquidity sweep",
    "smc": "Smart Money Concepts",
    "zones": "Supply and demand",
    "price_action": "Price action",
    "ema": "EMA",
    "profile": "Volume profile",
}
PROFILE_MODES = ("session", "day", "rolling")

DAY = 86_400
HOUR = 3_600
#: A touch waits this long for the 1-minute failure.
WATCH_S = HOUR
#: A limit not filled within this long is dropped.
EXPIRE_S = HOUR
#: The "last small low" is looked for this far before the push began.
SMALL_BACK_S = 30 * 60
#: How far back setups are looked for: since the previous FX day opened.
SCAN_DAYS = 1
#: Candles read: 1-hour for the daily and 1-hour concepts, 15-minute for
#: sweeps, 1-minute for the routine and its profile and volume.
H1_COUNT = 90 * 24
M15_COUNT = 4 * 96
M1_COUNT = 6 * 1440
#: Times of day compared for tick volume: the same minute +-2 on 5 days.
RVOL_DAYS, RVOL_SPAN = 5, 2


@dataclass(frozen=True)
class Settings:
    concepts: frozenset = frozenset(CONCEPTS)
    target_pips: float = 10.0
    max_stop_pips: float = 10.0
    stop_buffer_pips: float = 1.5
    ema_period: int = 20
    range_days: int = 20
    profile: str = "session"

    def on(self, concept: str) -> bool:
        return concept in self.concepts


# -- candles -----------------------------------------------------------------------


@dataclass
class Bars:
    """Candles as arrays: start time (epoch s), Bid OHLC, tick volume and the
    last spread."""
    seconds: int
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    sp: np.ndarray

    def __len__(self) -> int:
        return len(self.t)

    @property
    def end(self) -> np.ndarray:
        return self.t + self.seconds

    @classmethod
    def from_arrays(cls, seconds: int, a: dict) -> "Bars":
        return cls(seconds, np.asarray(a["time"], np.int64), np.asarray(a["open"], float), np.asarray(a["high"], float),
                   np.asarray(a["low"], float), np.asarray(a["close"], float), np.asarray(a["ticks"], np.int64),
                   np.asarray(a["spread"], float))

    @classmethod
    def empty(cls, seconds: int) -> "Bars":
        z = np.empty(0)
        return cls(seconds, np.empty(0, np.int64), z, z, z, z, np.empty(0, np.int64), z)

    def cut(self, a: int, b: int) -> "Bars":
        return Bars(self.seconds, self.t[a:b], self.o[a:b], self.h[a:b], self.l[a:b], self.c[a:b], self.v[a:b], self.sp[a:b])

    def since(self, start_s: int) -> "Bars":
        return self.cut(int(np.searchsorted(self.t, start_s, side="left")), len(self))


def _rows(store, a: int, b: int) -> dict:
    """Candles a..b of a store as arrays, without copying stored history: a
    live feed's candles are its stored days followed by today's."""
    if hasattr(store, "closed") and hasattr(store, "open"):
        k = len(store.closed)
        parts = [_rows(store.closed, a, min(b, k)), _rows(store.open, max(a - k, 0), max(b - k, 0))]
        return {f: np.concatenate([p[f] for p in parts]) for f in FIELDS}
    arr = store.arrays
    return {f: arr[f][a:b] for f in FIELDS}


def recent(store, sim_ns: int, count: int) -> Bars:
    """The last `count` candles complete at `sim_ns`."""
    n = store.completed_count(sim_ns)
    return Bars.from_arrays(store.seconds, _rows(store, max(n - count, 0), n))


def fx_days(h1: Bars) -> Bars:
    """FX days (17:00 New York to 17:00) from 1-hour candles, which divide
    them exactly; the last may still be open."""
    if not len(h1):
        return Bars.empty(DAY)
    start = day_start_s(h1.t)
    idx = np.flatnonzero(np.r_[True, start[1:] != start[:-1]])
    last = np.r_[idx[1:] - 1, len(h1) - 1]
    return Bars(DAY, start[idx], h1.o[idx], np.maximum.reduceat(h1.h, idx), np.minimum.reduceat(h1.l, idx),
                h1.c[last], np.add.reduceat(h1.v, idx), h1.sp[last])


# -- arithmetic --------------------------------------------------------------------


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """EMA_t = k * x_t + (1 - k) * EMA_t-1, k = 2 / (n + 1), started from the
    average of the first n values; NaN before."""
    out = np.full(len(x), np.nan)
    if len(x) < n or n < 1:
        return out
    k = 2.0 / (n + 1)
    out[n - 1] = float(np.mean(x[:n]))
    for i in range(n, len(x)):
        out[i] = k * x[i] + (1 - k) * out[i - 1]
    return out


def atr(b: Bars, n: int = 14) -> np.ndarray:
    """Average true range, Wilder's smoothing; early values average what
    there is."""
    if not len(b):
        return np.empty(0)
    prev = np.r_[b.c[0], b.c[:-1]]
    tr = np.maximum(b.h - b.l, np.maximum(np.abs(b.h - prev), np.abs(b.l - prev)))
    out = np.empty(len(tr))
    acc = 0.0
    for i, x in enumerate(tr):
        if i < n:
            acc += x
            out[i] = acc / (i + 1)
        else:
            out[i] = (out[i - 1] * (n - 1) + x) / n
    return out


@dataclass
class Swing:
    i: int          # candle index
    t: int          # candle start, epoch s
    price: float
    known: int      # when it is confirmed: the close of the `right`-th candle after


def swings(b: Bars, left: int = 2, right: int = 2) -> tuple[list[Swing], list[Swing]]:
    """Swing highs and lows (fractals): a high above the `left` candles
    before it and not below the `right` after it; known only once those
    have closed."""
    n = len(b)
    if n < left + right + 1:
        return [], []
    out = []
    for arr, sign in ((b.h, 1.0), (b.l, -1.0)):
        x = arr * sign
        lm = sliding_window_view(x, left).max(axis=1)[: n - left - right]
        rm = sliding_window_view(x, right).max(axis=1)[left + 1: n - right + 1]
        mid = x[left: n - right]
        idx = np.flatnonzero((mid > lm) & (mid >= rm)) + left
        out.append([Swing(int(i), int(b.t[i]), float(arr[i]), int(b.end[i + right])) for i in idx])
    return out[0], out[1]


def rel_volume(b: Bars) -> np.ndarray:
    """Tick volume against the same time of day: each candle's against the
    average of the same minute +-2 candles on the previous 5 days (NaN with
    fewer than 5 to compare). The London and New York opens are always
    busy; this tells a busy candle from a busy hour."""
    if not len(b) or b.seconds >= DAY:
        return np.full(len(b), np.nan)
    step = b.seconds
    slot = (b.t - b.t[0]) // step
    dense = np.full(int(slot[-1]) + 1, np.nan)
    dense[slot] = b.v
    per_day = DAY // step
    offs = np.array([-k * per_day + m for k in range(1, RVOL_DAYS + 1) for m in range(-RVOL_SPAN, RVOL_SPAN + 1)])
    ix = slot[:, None] + offs[None, :]
    ok = (ix >= 0) & (ix < len(dense))
    vals = np.where(ok, dense[np.clip(ix, 0, len(dense) - 1)], np.nan)
    count = np.sum(~np.isnan(vals), axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        ref = np.nanmean(np.where(count[:, None] > 0, vals, 0.0), axis=1)
        return np.where((count >= 5) & (ref > 0), b.v / ref, np.nan)


# -- step 2: levels and areas ------------------------------------------------------


@dataclass
class Area:
    """A level (lo == hi) or a band price may react at. `side` is the trade
    it offers: "sell" for a high or an area above, "buy" for a low or an
    area below."""
    kind: str               # pdh pdl eqh eql rh rl | supply demand | ob fvg
    concept: str            # liquidity | zones | smc
    side: str
    lo: float
    hi: float
    known: int              # epoch s it became known
    origin: int             # epoch s of the candle it is drawn from
    label: str
    until: int | None = None
    end: str = ""           # broken | filled | expired
    expires: int | None = None
    visits: list[tuple[int, int]] = field(default_factory=list)  # (start, known) of each return

    def active(self, t: int) -> bool:
        return self.known <= t and (self.until is None or t < self.until)

    def returns_known(self, t: int) -> int:
        return sum(1 for _s, k in self.visits if k <= t)

    @property
    def price(self) -> float:
        return self.hi if self.side == "sell" else self.lo

    def near_edge(self) -> float:
        """Where a limit goes: a supply's bottom, a demand's top, the level."""
        return self.lo if self.side == "sell" else self.hi

    def to_json(self, digits: int) -> dict:
        return {"kind": self.kind, "concept": self.concept, "side": self.side, "lo": round(self.lo, digits),
                "hi": round(self.hi, digits), "known": self.known, "origin": self.origin, "until": self.until,
                "end": self.end, "label": self.label, "visits": len(self.visits)}


def track(area: Area, b: Bars) -> None:
    """Visits and the end of an area, from candles closed after it was
    known: a visit starts when price comes back into it from outside; a
    close through the far side breaks it (a gap: once price has crossed it
    all, it is filled)."""
    i0 = int(np.searchsorted(b.t, area.known, side="left"))
    outside = True
    for j in range(i0, len(b)):
        if area.expires is not None and b.t[j] >= area.expires:
            area.until, area.end = area.expires, "expired"
            return
        if area.side == "sell":
            inside = b.h[j] >= area.lo
            if area.kind == "fvg" and b.h[j] >= area.hi or area.kind != "fvg" and b.c[j] > area.hi:
                area.until, area.end = int(b.end[j]), "filled" if area.kind == "fvg" else "broken"
                return
        else:
            inside = b.l[j] <= area.hi
            if area.kind == "fvg" and b.l[j] <= area.lo or area.kind != "fvg" and b.c[j] < area.lo:
                area.until, area.end = int(b.end[j]), "filled" if area.kind == "fvg" else "broken"
                return
        if inside and outside:
            area.visits.append((int(b.t[j]), int(b.end[j])))
        outside = not inside


@dataclass
class Sweep:
    level: Area
    start: int      # the 15-minute candle's start
    time: int       # its close: when the sweep is known
    extreme: float  # how far it went through
    broke: bool = False

    def to_json(self, digits: int, pip: float) -> dict:
        return {"time": self.time, "start": self.start, "side": self.level.side, "level": round(self.level.price, digits),
                "extreme": round(self.extreme, digits), "label": self.level.label, "broke": self.broke,
                "through_pips": round(abs(self.extreme - self.level.price) / pip, 1)}


def track_level(level: Area, m15: Bars) -> list[Sweep]:
    """A level's sweeps and its break, from 15-minute closes: through it and
    closed back is a sweep; closed beyond is a real break, after which it is
    no longer a level."""
    out = []
    i0 = int(np.searchsorted(m15.t, level.known, side="left"))
    p = level.price
    for j in range(i0, len(m15)):
        if level.expires is not None and m15.t[j] >= level.expires:
            level.until, level.end = level.expires, "expired"
            break
        if level.side == "sell":
            through, beyond = m15.h[j] > p, m15.c[j] > p
            extreme = m15.h[j]
        else:
            through, beyond = m15.l[j] < p, m15.c[j] < p
            extreme = m15.l[j]
        if beyond:
            level.until, level.end = int(m15.end[j]), "broken"
            out.append(Sweep(level, int(m15.t[j]), int(m15.end[j]), float(extreme), broke=True))
            break
        if through:
            out.append(Sweep(level, int(m15.t[j]), int(m15.end[j]), float(extreme)))
    return out


def liquidity_levels(days: Bars, h1: Bars, h1_highs: list[Swing], h1_lows: list[Swing], now_s: int, from_s: int,
                     pip: float, tol: float, range_days: int) -> list[Area]:
    """Yesterday's high and low and the 20-day range's edges for each FX day
    since `from_s` (each for that day only), and equal highs and lows on the
    1-hour chart (for two days)."""
    out: list[Area] = []
    done = days.cut(0, int(np.searchsorted(days.t + DAY, now_s, side="right")))  # complete days
    today = int(day_start_s(np.array([now_s]))[0])
    starts = sorted({int(s) for s in days.t if s + DAY > from_s} | {today})
    for s in starts:
        k = int(np.searchsorted(done.t, s, side="left")) - 1
        if k < 0:
            continue
        ends = s + DAY
        y = [Area("pdh", "liquidity", "sell", float(done.h[k]), float(done.h[k]), s, int(done.t[k]), "Yesterday's high", expires=ends),
             Area("pdl", "liquidity", "buy", float(done.l[k]), float(done.l[k]), s, int(done.t[k]), "Yesterday's low", expires=ends)]
        out += y
        a = max(0, k - range_days + 1)
        if k - a + 1 >= min(range_days, 5):
            hi_i = a + int(np.argmax(done.h[a: k + 1]))
            lo_i = a + int(np.argmin(done.l[a: k + 1]))
            rh, rl = float(done.h[hi_i]), float(done.l[lo_i])
            if abs(rh - y[0].price) > tol:
                out.append(Area("rh", "liquidity", "sell", rh, rh, s, int(done.t[hi_i]), f"{range_days}-day high", expires=ends))
            if abs(rl - y[1].price) > tol:
                out.append(Area("rl", "liquidity", "buy", rl, rl, s, int(done.t[lo_i]), f"{range_days}-day low", expires=ends))
    for group, side, kind in ((h1_highs, "sell", "eqh"), (h1_lows, "buy", "eql")):
        for bi, b in enumerate(group):
            if b.known < from_s - DAY:
                continue
            for a in reversed(group[:bi]):
                if b.t - a.t > 2 * DAY:
                    break
                if abs(a.price - b.price) > tol:
                    continue
                lvl = max(a.price, b.price) if side == "sell" else min(a.price, b.price)
                between = slice(a.i + 1, b.i)
                if side == "sell" and len(h1.h[between]) and h1.h[between].max() > lvl:
                    continue
                if side == "buy" and len(h1.l[between]) and h1.l[between].min() < lvl:
                    continue
                if any(x.kind == kind and abs(x.price - lvl) <= tol and x.known >= b.known - DAY for x in out):
                    break
                out.append(Area(kind, "liquidity", side, lvl, lvl, b.known, a.t,
                                "Equal highs" if side == "sell" else "Equal lows", expires=b.known + 2 * DAY))
                break
    return out


def zones(b: Bars, at: np.ndarray, from_s: int) -> list[Area]:
    """Supply and demand zones on 1-hour candles. A strong move: one to three
    candles the same way, each with a body at least half its range, moving
    1.5 average ranges. The zone is its base, the candle before it (with up
    to two small ones before that): from the extreme wick to the body of the
    last base candle; at most one average range tall."""
    out: list[Area] = []
    n = len(b)
    for k in range(3, n):
        if b.end[k] < from_s:
            continue
        a_ = at[k - 1]
        d = 1 if b.c[k] > b.o[k] else -1 if b.c[k] < b.o[k] else 0
        if not d:
            continue
        m_end = None
        for m in range(k, min(k + 3, n)):
            body, rng = abs(b.c[m] - b.o[m]), b.h[m] - b.l[m]
            if (b.c[m] - b.o[m]) * d <= 0 or rng <= 0 or body < 0.5 * rng:
                break
            if (b.c[m] - b.o[k]) * d >= 1.5 * a_:
                m_end = m
                break
        if m_end is None:
            continue
        base = [k - 1]
        for j in range(k - 2, max(k - 4, -1), -1):
            if abs(b.c[j] - b.o[j]) > 0.5 * a_:
                break
            base.append(j)
        last = k - 1
        if d > 0:
            lo, hi, side, kind = float(b.l[base].min()), float(max(b.o[last], b.c[last])), "buy", "demand"
        else:
            lo, hi, side, kind = float(min(b.o[last], b.c[last])), float(b.h[base].max()), "sell", "supply"
        if hi - lo > a_ or hi <= lo:
            continue
        known = int(b.end[m_end])
        if any(z.kind == kind and z.active(known) and min(z.hi, hi) - max(z.lo, lo) > 0.5 * (hi - lo) for z in out):
            continue
        z = Area(kind, "zones", side, lo, hi, known, int(b.t[min(base)]), "Demand zone" if d > 0 else "Supply zone")
        track(z, b)
        out.append(z)
    return out


@dataclass
class Break:
    kind: str       # BOS | CHoCH
    side: str       # "buy": broke a high, "sell": broke a low
    level: float
    swing_t: int
    time: int       # the close that broke it

    def to_json(self, digits: int) -> dict:
        return {"kind": self.kind, "side": self.side, "level": round(self.level, digits), "swing": self.swing_t, "time": self.time}


def structure(b: Bars, highs: list[Swing], lows: list[Swing], right: int, from_s: int) -> tuple[list[Break], list[Area]]:
    """Breaks of structure and changes of character, and the order block of
    each: walking the candles, a close beyond the latest unbroken swing is a
    break; against the trend so far a change of character. The order block
    is the last candle the other way before the break (the last up-candle
    before a fall), all of its range."""
    events, blocks = [], []
    trend = 0
    hi_i = lo_i = 0
    last_h = last_l = None
    for j in range(len(b)):
        while hi_i < len(highs) and highs[hi_i].i + right < j:
            last_h = highs[hi_i]
            hi_i += 1
        while lo_i < len(lows) and lows[lo_i].i + right < j:
            last_l = lows[lo_i]
            lo_i += 1
        for sw, side in ((last_h, "buy"), (last_l, "sell")):
            if sw is None:
                continue
            if (side == "buy" and b.c[j] > sw.price) or (side == "sell" and b.c[j] < sw.price):
                d = 1 if side == "buy" else -1
                kind = "CHoCH" if trend == -d else "BOS"
                trend = d
                if side == "buy":
                    last_h = None
                else:
                    last_l = None
                if b.end[j] < from_s:
                    continue
                events.append(Break(kind, side, sw.price, sw.t, int(b.end[j])))
                for k in range(j - 1, max(j - 30, -1), -1):
                    if (b.c[k] - b.o[k]) * d < 0:
                        blocks.append(Area("ob", "smc", side, float(b.l[k]), float(b.h[k]), int(b.end[j]), int(b.t[k]),
                                           ("Bullish" if d > 0 else "Bearish") + " order block", expires=int(b.end[j]) + 5 * DAY))
                        break
    return events, blocks


def fair_value_gaps(b: Bars, at: np.ndarray, from_s: int) -> list[Area]:
    """Three candles whose first and third do not overlap: the gap between
    them, at least 0.3 average ranges (smaller ones are everywhere)."""
    out = []
    for i in range(2, len(b)):
        if b.end[i] < from_s:
            continue
        if b.l[i] > b.h[i - 2] and b.l[i] - b.h[i - 2] >= 0.3 * at[i]:
            out.append(Area("fvg", "smc", "buy", float(b.h[i - 2]), float(b.l[i]), int(b.end[i]), int(b.t[i - 1]),
                            "Fair value gap", expires=int(b.end[i]) + 5 * DAY))
        elif b.h[i] < b.l[i - 2] and b.l[i - 2] - b.h[i] >= 0.3 * at[i]:
            out.append(Area("fvg", "smc", "sell", float(b.h[i]), float(b.l[i - 2]), int(b.end[i]), int(b.t[i - 1]),
                            "Fair value gap", expires=int(b.end[i]) + 5 * DAY))
    return out


# -- step 1: side ------------------------------------------------------------------


@dataclass
class Daily:
    """Daily EMA and range, from complete FX days."""
    days: Bars
    ema: np.ndarray
    atr: np.ndarray

    def at(self, t: int) -> int:
        """Complete days before the one holding t."""
        start = int(day_start_s(np.array([t]))[0])
        return int(np.searchsorted(self.days.t, start, side="left"))


def side_at(daily: Daily, t: int, price: float, s: Settings, pip: float) -> dict:
    """Which side step 1 allows on the FX day holding t, decided once a day
    from that day's opening price `price`, as the PDF's routine does."""
    k = daily.at(t)
    out = {"sell": True, "buy": True, "rule": "none", "text": "Both sides: EMA and SMC off."}
    use_ema, use_range = s.on("ema"), s.on("ema") or s.on("smc")
    if not (use_ema or use_range):
        return out
    e_text = ""
    if use_ema:
        if k < s.ema_period + 5 or math.isnan(daily.ema[k - 1]) or math.isnan(daily.ema[k - 6]):
            e_text = "too little daily history for the EMA"
        else:
            e, slope, a = float(daily.ema[k - 1]), float(daily.ema[k - 1] - daily.ema[k - 6]), float(daily.atr[k - 1])
            out.update(ema=e, slope_pips=round(slope / pip, 1), flat=abs(slope) < 0.2 * a)
            if not out["flat"] and slope < 0 and price < e:
                out.update(sell=True, buy=False, rule="ema",
                           text=f"Sells only: the daily EMA {s.ema_period} is falling ({slope / pip:+.0f} pips in 5 days) and the day opened {(e - price) / pip:.0f} pips below it.")
                return out
            if not out["flat"] and slope > 0 and price > e:
                out.update(sell=False, buy=True, rule="ema",
                           text=f"Buys only: the daily EMA {s.ema_period} is rising ({slope / pip:+.0f} pips in 5 days) and the day opened {(price - e) / pip:.0f} pips above it.")
                return out
            e_text = (f"the daily EMA {s.ema_period} is flat ({slope / pip:+.0f} pips in 5 days)" if out["flat"] else
                      f"the day opened on the other side of the {'falling' if slope < 0 else 'rising'} daily EMA {s.ema_period}")
    a = max(0, k - s.range_days)
    if k - a < 5:
        out.update(text=("Both sides: " + e_text + "; " if e_text else "Both sides: ") + "too little history for the range.")
        return out
    hi, lo = float(daily.days.h[a:k].max()), float(daily.days.l[a:k].min())
    mid = (hi + lo) / 2
    out.update(range_hi=hi, range_lo=lo, mid=mid)
    why = (e_text[0].upper() + e_text[1:] + ", so the range decides: ") if e_text else ""
    out["open"] = price
    if price > mid:
        out.update(sell=True, buy=False, rule="range",
                   text=f"{why}sells only, the day opened in the upper half (premium) of the {s.range_days}-day range, {(price - mid) / pip:.0f} pips above its middle {mid:.5g}.")
    else:
        out.update(sell=False, buy=True, rule="range",
                   text=f"{why}buys only, the day opened in the lower half (discount) of the {s.range_days}-day range, {(mid - price) / pip:.0f} pips below its middle {mid:.5g}.")
    return out


# -- step 3: room ------------------------------------------------------------------


@dataclass
class Profile:
    start: int
    end: int
    base: float
    step: float
    vol: np.ndarray
    poc: float
    vah: float
    val: float

    def to_json(self, digits: int) -> dict:
        top = float(self.vol.max()) if len(self.vol) else 0.0
        return {"start": self.start, "end": self.end, "step": self.step, "base": round(self.base, digits + 1),
                "bins": [round(float(v) / top, 3) if top else 0 for v in self.vol],
                "poc": round(self.poc, digits), "vah": round(self.vah, digits), "val": round(self.val, digits)}


def profile_start(mode: str, t: int) -> int:
    if mode == "day":
        return int(day_start_s(np.array([t]))[0])
    if mode == "rolling":
        return t - DAY
    cur = int(session_start_s(np.array([t]))[0])
    return int(session_start_s(np.array([cur - 1]))[0])


def volume_profile(m1: Bars, start: int, end: int, pip: float) -> Profile | None:
    """Tick volume per price from 1-minute candles between start and end,
    each candle's spread evenly over its range; at most about 80 rows of
    whole pips. The value area grows from the point of control, two rows at
    a time on the busier side, until it holds 70 % of the volume (the
    Market Profile rule)."""
    a = int(np.searchsorted(m1.t, start, side="left"))
    b_ = int(np.searchsorted(m1.end, end, side="right"))
    if b_ - a < 5:
        return None
    lo, hi = float(m1.l[a:b_].min()), float(m1.h[a:b_].max())
    step = pip * max(1, math.ceil((hi - lo) / pip / 80))
    base = math.floor(lo / step + 1e-9) * step
    nb = int(math.floor((hi - base) / step + 1e-9)) + 1
    vol = np.zeros(nb)
    ia = np.floor((m1.l[a:b_] - base) / step + 1e-9).astype(int)
    iz = np.floor((m1.h[a:b_] - base) / step + 1e-9).astype(int)
    for x, z, v in zip(ia, iz, m1.v[a:b_]):
        vol[x: z + 1] += v / (z - x + 1)
    p = int(np.argmax(vol))
    lo_i = hi_i = p
    acc, want = vol[p], 0.7 * vol.sum()
    while acc < want and (lo_i > 0 or hi_i < nb - 1):
        up = vol[hi_i + 1: hi_i + 3].sum() if hi_i < nb - 1 else -1.0
        dn = vol[max(lo_i - 2, 0): lo_i].sum() if lo_i > 0 else -1.0
        if up >= dn:
            hi_i, acc = min(hi_i + 2, nb - 1), acc + up
        else:
            lo_i, acc = max(lo_i - 2, 0), acc + dn
    return Profile(start, end, base, step, vol, base + (p + 0.5) * step, base + (hi_i + 1) * step, base + lo_i * step)


def room(pf: Profile | None, price: float, side: str, pip: float) -> dict:
    """Pips price can travel toward the target before the busy middle."""
    if pf is None:
        return {"pips": None, "open": False, "text": "no volume profile yet"}
    if side == "sell":
        if price > pf.vah:
            return {"pips": round((price - pf.vah) / pip, 1), "open": False, "text": "down to the value area"}
        if price > pf.poc:
            return {"pips": round((price - pf.poc) / pip, 1), "open": False, "text": "down to the point of control"}
        if price >= pf.val:
            return {"pips": 0.0, "open": False, "text": "at or under the point of control: two-way"}
        return {"pips": None, "open": True, "text": "below the value area: price left the profile"}
    if price < pf.val:
        return {"pips": round((pf.val - price) / pip, 1), "open": False, "text": "up to the value area"}
    if price < pf.poc:
        return {"pips": round((pf.poc - price) / pip, 1), "open": False, "text": "up to the point of control"}
    if price <= pf.vah:
        return {"pips": 0.0, "open": False, "text": "at or over the point of control: two-way"}
    return {"pips": None, "open": True, "text": "above the value area: price left the profile"}


def room_ok(r: dict, target: float) -> bool:
    return bool(r["open"] or (r["pips"] is not None and r["pips"] >= target))


# -- step 4: price action ----------------------------------------------------------


def candle_read(b: Bars, j: int, pip: float, n: int = 10) -> dict:
    """The PDF's count over the n candles up to j: sellers are stronger when
    the average up-body is under half the average down-body and at least 3
    up-candles have an upper wick longer than the body; buyers the mirror."""
    a = max(0, j - n + 1)
    o, h, l, c = b.o[a: j + 1], b.h[a: j + 1], b.l[a: j + 1], b.c[a: j + 1]
    body = c - o
    up, dn = body > 0, body < 0
    up_body = float(body[up].mean() / pip) if up.any() else 0.0
    dn_body = float(-body[dn].mean() / pip) if dn.any() else 0.0
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    up_rej = int(np.sum(up & (upper > np.abs(body))))
    dn_rej = int(np.sum(dn & (lower > np.abs(body))))
    sellers = up_body < 0.5 * dn_body and up_rej >= 3
    buyers = dn_body < 0.5 * up_body and dn_rej >= 3
    return {"candles": int(j + 1 - a), "up_body": round(up_body, 1), "down_body": round(dn_body, 1),
            "up_rejections": up_rej, "down_rejections": dn_rej,
            "reading": "sellers stronger" if sellers else "buyers stronger" if buyers else "no clear side"}


def wide(b: Bars, k: int, side: str) -> bool:
    """A wide candle closing near its end: body 1.2 times the average of the
    20 before, closing in the last 40 % of its range the trade's way."""
    if k < 1:
        return False
    a = max(0, k - 20)
    avg = float(np.mean(np.abs(b.c[a:k] - b.o[a:k]))) if k > a else 0.0
    body, rng = b.c[k] - b.o[k], b.h[k] - b.l[k]
    if rng <= 0 or abs(body) < 1.2 * avg:
        return False
    if side == "sell":
        return body < 0 and (b.c[k] - b.l[k]) <= 0.4 * rng
    return body > 0 and (b.h[k] - b.c[k]) <= 0.4 * rng


def absorbing(b: Bars, rv: np.ndarray, k: int) -> bool:
    """Busy for the time of day (1.5 times usual) with little progress (a
    body under 35 % of the range): the push was absorbed."""
    rng = b.h[k] - b.l[k]
    return bool(not math.isnan(rv[k]) and rv[k] >= 1.5 and rng > 0 and abs(b.c[k] - b.o[k]) <= 0.35 * rng)


# -- step 4 and 5: setups ----------------------------------------------------------


@dataclass
class Touch:
    side: str
    start: int          # the push's first candle
    time: int           # when it counts: the 1-minute close inside, or the 15-minute close back for a sweep
    area: Area
    what: str


@dataclass
class Setup:
    side: str
    time: int           # the failure's close: when it is known
    push_start: int
    entry: float
    stop: float
    target: float
    market: bool
    areas: list[str]
    checks: list[dict]
    status: str = "waiting"   # waiting active won lost expired skipped
    filled: int | None = None
    closed: int | None = None
    pips: float | None = None

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks if c["required"])

    def to_json(self, digits: int, pip: float) -> dict:
        return {"id": f"{self.side}-{self.time}", "side": self.side, "time": self.time, "push_start": self.push_start,
                "entry": round(self.entry, digits), "stop": round(self.stop, digits), "target": round(self.target, digits),
                "market": self.market, "risk_pips": round(abs(self.stop - self.entry) / pip, 1),
                "areas": self.areas, "checks": self.checks, "status": self.status,
                "filled": self.filled, "closed": self.closed, "pips": self.pips}


def touches(areas: list[Area], sweeps: list[Sweep], m1: Bars, s: Settings) -> list[Touch]:
    """Where a push reached a level or a zone: a sweep (known at its
    15-minute close), or a 1-minute candle reaching an area from outside it.
    A supply or demand zone counts on its first and second return only."""
    out = []
    if s.on("liquidity"):
        out += [Touch(w.level.side, w.start, w.time, w.level, "sweep") for w in sweeps if not w.broke]
    for a in areas:
        if a.concept == "liquidity" or not s.on(a.concept):
            continue
        i0 = int(np.searchsorted(m1.t, a.known, side="left"))
        i1 = len(m1) if a.until is None else int(np.searchsorted(m1.t, a.until, side="left"))
        if i1 <= i0:
            continue
        if a.side == "sell":
            reach = m1.h[i0:i1] >= a.lo
            before = np.r_[m1.h[i0 - 1] if i0 > 0 else -np.inf, m1.h[i0: i1 - 1]] < a.lo
        else:
            reach = m1.l[i0:i1] <= a.hi
            before = np.r_[m1.l[i0 - 1] if i0 > 0 else np.inf, m1.l[i0: i1 - 1]] > a.hi
        for j in np.flatnonzero(reach & before) + i0:
            t = int(m1.t[j])
            if a.kind in ("supply", "demand") and a.returns_known(t) > 1:
                continue
            out.append(Touch(a.side, t, int(m1.end[j]), a, "entered"))
    out.sort(key=lambda x: (x.time, x.side))
    return out


def find_setups(tchs: list[Touch], m1: Bars, small_highs: list[Swing], small_lows: list[Swing], rv: np.ndarray,
                side_fn, room_fn, s: Settings, pip: float) -> list[Setup]:
    """Steps 4 and 5 for each touch, in time order."""
    out: list[Setup] = []
    idx_l = [x.i for x in small_lows]
    idx_h = [x.i for x in small_highs]
    by_bar: dict[tuple[str, int], Setup] = {}
    last_from: dict[int, int] = {}  # area -> its last setup's time: one an hour
    for tc in tchs:
        if tc.time < last_from.get(id(tc.area), -WATCH_S) + WATCH_S:
            continue
        sell = tc.side == "sell"
        j0 = int(np.searchsorted(m1.t, tc.start, side="left"))
        jc = int(np.searchsorted(m1.end, tc.time, side="left"))
        jz = min(int(np.searchsorted(m1.t, tc.time + WATCH_S, side="left")), len(m1))
        fail, small = None, None
        pool, idx = (small_lows, idx_l) if sell else (small_highs, idx_h)
        for j in range(jc, jz):
            # the push's extreme so far, and the last small low before it
            # (known by this candle, formed near the push)
            ext = j0 + int(np.argmax(m1.h[j0: j + 1]) if sell else np.argmin(m1.l[j0: j + 1]))
            k = bisect_left(idx, ext) - 1
            while k >= 0 and pool[k].known > m1.t[j]:
                k -= 1
            if k < 0 or pool[k].t < tc.start - SMALL_BACK_S:
                continue
            small = pool[k]
            if (sell and m1.c[j] < small.price) or (not sell and m1.c[j] > small.price):
                fail = j
                break
        if fail is None:
            continue
        key = (tc.side, fail)
        if key in by_bar:  # the same failure after several touches: one setup
            if tc.area.label not in by_bar[key].areas:
                by_bar[key].areas.append(tc.area.label)
            continue
        j = fail
        close, spread = float(m1.c[j]), float(m1.sp[j])
        if sell:
            extreme = float(m1.h[j0: j + 1].max())
            ref = tc.area.near_edge()
            market = ref <= close
            entry = close if market else ref
            stop = max(extreme, tc.area.hi) + s.stop_buffer_pips * pip   # behind the push and the zone
            target = entry - s.target_pips * pip
        else:
            extreme = float(m1.l[j0: j + 1].min())
            ref = tc.area.near_edge()
            ask = close + spread
            market = ref >= ask
            entry = ask if market else ref
            stop = min(extreme, tc.area.lo) - s.stop_buffer_pips * pip
            target = entry + s.target_pips * pip
        risk = abs(stop - entry) / pip
        t = int(m1.end[j])
        checks = []
        if s.on("ema") or s.on("smc"):
            sd = side_fn(t, close)
            checks.append({"step": 1, "name": "Side", "required": True, "ok": bool(sd[tc.side]), "text": sd["text"]})
        what = f"{tc.area.label} {'swept' if tc.what == 'sweep' else 'reached'}"
        checks.append({"step": 2, "name": "Level", "required": True, "ok": True, "text": what})
        if s.on("profile"):
            r = room_fn(t, entry, tc.side)
            checks.append({"step": 3, "name": "Room", "required": True, "ok": room_ok(r, s.target_pips),
                           "text": (f"{r['pips']:g} pips {r['text']}" if r["pips"] is not None else r["text"])})
        side_word = "below the last small low" if sell else "above the last small high"
        checks.append({"step": 4, "name": "Failure", "required": True, "ok": True,
                       "text": f"1-minute close {side_word} {small.price:.5g}"})
        if s.on("price_action"):
            lo_k = max(j0, j - 15)
            ok = any(wide(m1, k, tc.side) and ((m1.c[k] < small.price) if sell else (m1.c[k] > small.price))
                     for k in range(lo_k, j + 1))
            absorbed = sum(absorbing(m1, rv, k) for k in range(j0, j + 1))
            read = candle_read(m1, j, pip)
            checks.append({"step": 4, "name": "Wide candle", "required": True, "ok": ok,
                           "text": "a wide candle closed through it" if ok else "no wide candle closed through it"})
            checks.append({"step": 4, "name": "Absorption", "required": False, "ok": absorbed > 0,
                           "text": f"{absorbed} busy candle{'s' if absorbed != 1 else ''} with little progress in the push; "
                                   f"last 10: {read['reading']}"})
        checks.append({"step": 5, "name": "Stop", "required": True, "ok": risk <= s.max_stop_pips + 1e-9,
                       "text": f"{risk:.1f} pips, behind the push" + (" and the zone" if tc.area.lo != tc.area.hi else "") +
                               ("" if risk <= s.max_stop_pips + 1e-9 else f": over {s.max_stop_pips:g}")})
        st = Setup(tc.side, t, int(m1.t[j0]), entry, stop, target, market, [tc.area.label], checks)
        if not st.ok:
            st.status = "skipped"
        out.append(st)
        by_bar[key] = st
        last_from[id(tc.area)] = t
    for st in out:
        if st.status != "skipped":
            outcome(st, m1, pip)
    return out


def outcome(st: Setup, m1: Bars, pip: float) -> None:
    """What the setup would have done on the candles closed since: fill,
    then stop or target (see the module notes)."""
    j = int(np.searchsorted(m1.t, st.time, side="left"))
    sell = st.side == "sell"
    if st.market:
        st.status, st.filled = "active", st.time
    for k in range(j, len(m1)):
        hi, lo, sp, t = m1.h[k], m1.l[k], m1.sp[k], int(m1.t[k])
        fresh = False
        if st.status == "waiting":
            if t >= st.time + EXPIRE_S:
                st.status, st.closed = "expired", t
                return
            if (sell and hi >= st.entry) or (not sell and lo + sp <= st.entry):
                st.status, st.filled, fresh = "active", t, True
        if st.status == "active":
            if (sell and hi + sp >= st.stop) or (not sell and lo <= st.stop):
                st.status, st.closed = "lost", int(m1.end[k])
                st.pips = -round(abs(st.stop - st.entry) / pip, 1)
                return
            if not fresh and ((sell and lo + sp <= st.target) or (not sell and hi >= st.target)):
                st.status, st.closed = "won", int(m1.end[k])
                st.pips = round(abs(st.entry - st.target) / pip, 1)
                return


# -- everything at a clock ---------------------------------------------------------


def evaluate(stores: dict, sim_ns: int, bid: float | None, ask: float | None, inst, s: Settings,
             chart_tf: str | None = None) -> dict:
    """All six concepts and the routine at `sim_ns`, for the panel and the
    chart. `stores` maps timeframe keys to the session's candle stores."""
    pip, digits = inst.pip, inst.digits
    now = sim_ns // NS_PER_SECOND
    h1 = recent(stores["h1"], sim_ns, H1_COUNT)
    m15 = recent(stores["m15"], sim_ns, M15_COUNT)
    m1 = recent(stores["m1"], sim_ns, M1_COUNT)
    out: dict = {"at": now, "concepts": sorted(s.concepts), "levels": [], "sweeps": [], "areas": [], "breaks": [],
                 "ema": [], "profile": None, "marks": [], "setups": [], "now": {}}
    if len(h1) < 48 or len(m1) < 30:
        out["problem"] = "Not enough history before the clock yet (2 days of 1-hour candles are needed)."
        return out
    price_b = bid if bid is not None else float(m1.c[-1])
    price_a = ask if ask is not None else price_b + float(m1.sp[-1])
    out["price"] = round(price_b, digits)
    days_all = fx_days(h1)
    done = int(np.searchsorted(days_all.t + DAY, now, side="right"))
    days = days_all.cut(0, done)
    daily = Daily(days, ema(days.c, s.ema_period), atr(days))
    scan_from = int(day_start_s(np.array([now]))[0]) - SCAN_DAYS * DAY
    a_h1 = atr(h1)
    tol = max(2 * pip, 0.25 * float(a_h1[-1]))

    # step 2
    h1_hi, h1_lo = swings(h1, 2, 2)
    areas: list[Area] = []
    sweeps: list[Sweep] = []
    if s.on("liquidity"):
        lv = liquidity_levels(days_all, h1, h1_hi, h1_lo, now, scan_from, pip, tol, s.range_days)
        for x in lv:
            sweeps += track_level(x, m15)
        areas += lv
    if s.on("zones"):
        areas += zones(h1, a_h1, now - 30 * DAY)
    breaks: list[Break] = []
    if s.on("smc"):
        breaks, obs = structure(h1, h1_hi, h1_lo, 2, now - 10 * DAY)
        gaps = fair_value_gaps(h1, a_h1, now - 10 * DAY)
        for x in obs + gaps:
            track(x, h1)
        areas += obs + gaps

    # steps 1 and 3 as functions of time, for the setups and for now
    def day_open(t: int) -> float:
        start = int(day_start_s(np.array([t]))[0])
        i = int(np.searchsorted(m1.t, start, side="left"))
        return float(m1.o[i]) if i < len(m1) and m1.t[i] < t else price_b

    def side_fn(t: int, _price: float = 0.0) -> dict:
        return side_at(daily, t, day_open(t), s, pip)

    profiles: dict[int, Profile | None] = {}

    def profile_at(t: int) -> Profile | None:
        start = profile_start(s.profile, t)
        key = (start, t // 60)
        if key not in profiles:
            profiles[key] = volume_profile(m1, start, t, pip)
        return profiles[key]

    def room_fn(t: int, price: float, side: str) -> dict:
        return room(profile_at(t), price, side, pip)

    # steps 4 and 5
    m1_scan = m1.since(scan_from - 2 * HOUR)
    small_hi, small_lo = swings(m1_scan, 2, 2)
    rv_all = rel_volume(m1) if s.on("price_action") else np.full(len(m1), np.nan)
    rv = rv_all[len(m1) - len(m1_scan):]
    tchs = [t for t in touches(areas, sweeps, m1_scan, s) if t.time >= scan_from]
    setups = find_setups(tchs, m1_scan, small_hi, small_lo, rv, side_fn, room_fn, s, pip)

    # what is drawn: what is still active, or ended since the scan began
    shown = [a for a in areas if a.known <= now and (a.until is None or a.until >= scan_from)]
    out["levels"] = [a.to_json(digits) for a in shown if a.concept == "liquidity"]
    out["areas"] = [a.to_json(digits) for a in shown if a.concept != "liquidity"]
    out["sweeps"] = [w.to_json(digits, pip) for w in sweeps if w.time >= scan_from]
    out["breaks"] = [b.to_json(digits) for b in breaks if b.time >= scan_from - DAY]
    if s.on("ema") and len(days):
        # each day's closing EMA, drawn over the next FX day (when it is the
        # value the filter uses); the last over today
        today = int(day_start_s(np.array([now]))[0])
        steps = []
        for i in range(max(0, len(days) - 30), len(days)):
            if math.isnan(daily.ema[i]):
                continue
            start = int(days.t[i + 1]) if i + 1 < len(days) else today
            steps.append({"start": start, "end": start + DAY, "value": round(float(daily.ema[i]), digits + 1)})
        out["ema"] = steps
    pf = profile_at(now) if s.on("profile") else None
    out["profile"] = pf.to_json(digits) if pf is not None else None
    if s.on("price_action") and chart_tf in stores and chart_tf != "d1":
        out["marks"] = chart_marks(stores[chart_tf], sim_ns)
    out["setups"] = [x.to_json(digits, pip) for x in sorted(setups, key=lambda x: x.time)]

    # now, at the last tick
    sd = side_fn(now, price_b) if (s.on("ema") or s.on("smc")) else {"sell": True, "buy": True, "rule": "none",
                                                                     "text": "Both sides: EMA and SMC are off."}
    if sd.get("ema") is not None and s.on("ema"):
        k = 2.0 / (s.ema_period + 1)
        sd["ema_now"] = round(k * price_b + (1 - k) * sd["ema"], digits + 1)
    active = [a for a in areas if a.active(now)]
    above = sorted((a for a in active if a.side == "sell" and a.lo >= price_b - tol), key=lambda a: a.lo)[:3]
    below = sorted((a for a in active if a.side == "buy" and a.hi <= price_a + tol), key=lambda a: -a.hi)[:3]
    now_out = {
        "side": sd,
        "above": [dict(a.to_json(digits), pips=round((a.lo - price_b) / pip, 1)) for a in above],
        "below": [dict(a.to_json(digits), pips=round((price_a - a.hi) / pip, 1)) for a in below],
        "room": {"sell": room_fn(now, price_b, "sell"), "buy": room_fn(now, price_a, "buy")} if s.on("profile") else None,
        "read": candle_read(m1, len(m1) - 1, pip),
        "watching": [],
    }
    watching = {}
    for tc in tchs:  # touches still waiting for their failure
        if now - tc.time < WATCH_S and not any(x.side == tc.side and x.time >= tc.time for x in setups):
            watching[(tc.side, tc.area.label)] = {"side": tc.side, "time": tc.time, "label": tc.area.label,
                                                  "what": "swept" if tc.what == "sweep" else "reached"}
    now_out["watching"] = sorted(watching.values(), key=lambda w: -w["time"])
    out["now"] = now_out
    return out


def chart_marks(store, sim_ns: int, count: int = 400) -> list[dict]:
    """Absorption candles of the chart's own timeframe (busy for the time
    of day, little progress), for dots on the chart."""
    per_day = DAY // store.seconds
    b = recent(store, sim_ns, count + (RVOL_DAYS + 1) * per_day)
    rv = rel_volume(b)
    out = []
    for k in range(max(0, len(b) - count), len(b)):
        if absorbing(b, rv, k):
            up = b.c[k] >= b.o[k]
            out.append({"time": int(b.t[k]), "price": float(b.h[k] if up else b.l[k]), "above": bool(up),
                        "rvol": round(float(rv[k]), 1)})
    return out


def settings_from(params: dict) -> Settings:
    """Settings from the page's query: unknown concepts are ignored and
    numbers are kept in sensible bounds."""
    raw = params.get("on")
    concepts = frozenset(CONCEPTS) if raw is None else frozenset(c for c in str(raw).split(",") if c in CONCEPTS)

    def num(key, default, lo, hi):
        try:
            return min(max(float(params.get(key, default)), lo), hi)
        except (TypeError, ValueError):
            return default

    mode = params.get("profile") if params.get("profile") in PROFILE_MODES else "session"
    return Settings(concepts, num("target", 10.0, 1, 500), num("max_stop", 10.0, 1, 500),
                    num("buffer", 1.5, 0, 50), int(num("ema", 20, 2, 200)), int(num("range", 20, 5, 120)), mode)
