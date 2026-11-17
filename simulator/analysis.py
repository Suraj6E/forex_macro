"""Currency strength for the Analysis panel.

Which of USD, EUR, GBP and JPY is strongest today, read from the six pairs
between them, and how gold, silver and the two oils are doing on their own.

The day is the FX day. It opens at 17:00 New York (21:00 UTC in US summer
time, 22:00 UTC otherwise), which is when a broker on New York + 7 h starts
its daily candle. Each instrument's move is its Bid now against its first Bid
of the day, the same Bid the chart's candles are drawn from.

Every pair is a match between its two currencies. EUR/USD up means EUR beat
USD today; USD/JPY down means JPY beat USD. A currency plays three matches,
and its score is how many it won: 3 of 3 is strong against everything, 0 of 3
weak against everything. When the scores tie, the average move decides: the
mean of its three pair moves, each taken from its own side (EUR/USD -0.2 %
counts +0.2 % for USD). Together the matches rank all four. USD beating
everyone, and EUR beating everyone but USD, puts USD first and EUR second.

Gold, silver and oil are not part of the matches. Each is shown on its own,
up or down on the day.

The same matches can be scored over other periods, the four the strength
indicator under the chart saves (strength.py), with the same starts:

  * day: since 17:00 New York, as above;
  * session: since the current session opened (Asia 22:00, London 07:00,
    London/New York overlap 12:00, New York 16:00 UTC, the journal's);
  * week: since Sunday 17:00 New York;
  * rolling: the last 24 hours, the Bid now against the last Bid 24 hours
    earlier (Friday's across a weekend), never starting again.

The period is placed at the newest pair tick, so on a Saturday it is still
Friday's day, session and week.

Nothing after `at_ns` is read, so a replay sees the strength as it was at
its clock and never later.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import timedelta

import numpy as np

from simulator.data.ticks import NS_PER_SECOND, day_of_ns, day_start_ns
from simulator.instruments import INSTRUMENTS
from simulator.live.mt5rec import us_dst

CURRENCIES = ("USD", "EUR", "GBP", "JPY")
PAIRS = ("EURUSD", "GBPUSD", "USDJPY", "EURJPY", "GBPJPY", "EURGBP")
ON_THEIR_OWN = ("XAUUSD", "XAGUSD", "WTI", "BRENT")

HOUR_NS = 3600 * NS_PER_SECOND
#: How far back the last tick before a time is looked for (a long weekend,
#: a holiday).
LOOK_BACK_DAYS = 7
#: Feeds whose last ticks are this close count as equally current, and the
#: preferred one (the session's own) is read.
SAME_TIME_NS = 5 * 60 * NS_PER_SECOND

LABELS = {3: "Strong", 2: "Leaning strong", 1: "Leaning weak", 0: "Weak"}

PERIODS = ("day", "session", "week", "rolling")
SESSION_NAMES = {7: "London", 12: "London/New York overlap", 16: "New York", 22: "Asia"}
DAY_NS = 86_400 * NS_PER_SECOND
#: Rolling: a Bid this much older than the instant it stands for is no
#: reference (the indicator's STALE_S).
STALE_NS = 4 * DAY_NS


def fx_day_open_ns(at_ns: int) -> int:
    """The latest 17:00 New York at or before `at_ns`: when the FX day that
    contains it opened."""
    day = day_of_ns(at_ns)
    for back in range(3):
        base = day_start_ns(day - timedelta(days=back))
        hour = 21 if us_dst((base + 21 * HOUR_NS) // 1_000_000) else 22
        opened = base + hour * HOUR_NS
        if opened <= at_ns:
            return opened
    raise AssertionError("unreachable: an FX day opens every calendar day")


def last_tick(source, at_ns: int) -> tuple[int, float] | None:
    """(time, Bid) of the last tick at or before `at_ns`."""
    days = source.days()
    i = bisect_right(days, day_of_ns(max(at_ns, 0))) - 1
    stop = day_of_ns(max(at_ns, 0)) - timedelta(days=LOOK_BACK_DAYS)
    while i >= 0 and days[i] >= stop:
        t = source.load_day(days[i])
        k = int(np.searchsorted(t.ts, at_ns, side="right"))
        if k > 0:
            return int(t.ts[k - 1]), float(t.bid[k - 1])
        i -= 1
    return None


def first_tick(source, from_ns: int, to_ns: int) -> tuple[int, float] | None:
    """(time, Bid) of the first tick in [from_ns, to_ns]."""
    days = source.days()
    i = bisect_right(days, day_of_ns(max(from_ns, 0)) - timedelta(days=1))
    while i < len(days) and day_start_ns(days[i]) <= to_ns:
        t = source.load_day(days[i])
        k = int(np.searchsorted(t.ts, from_ns, side="left"))
        if k < len(t.ts):
            return (int(t.ts[k]), float(t.bid[k])) if t.ts[k] <= to_ns else None
        i += 1
    return None


def _latest(sources: list, at_ns: int):
    """The source with the most recent tick at or before `at_ns`, and that
    tick. Earlier sources in the list win unless a later one is clearly
    more current (Dukascopy history a day behind a live recording). Once
    one has a tick in the current FX day the rest are not read at all, so
    a replay on one feed does not load days of every other."""
    current = fx_day_open_ns(at_ns)
    best = None
    for src in sources:
        tick = last_tick(src, at_ns)
        if tick is not None and (best is None or tick[0] > best[1][0] + SAME_TIME_NS):
            best = (src, tick)
        if best is not None and best[1][0] >= current:
            break
    return best


def period_start_ns(period: str, at_ns: int) -> int:
    """When the period holding `at_ns` started: for the rolling period, the
    instant 24 hours earlier."""
    if period == "rolling":
        return at_ns - DAY_NS
    if period == "day":
        return fx_day_open_ns(at_ns)
    from simulator.strength import PERIOD_START  # strength imports this module

    return int(PERIOD_START[period](np.array([at_ns // NS_PER_SECOND]))[0]) * NS_PER_SECOND


def _move(symbol: str, found, start: int | None, period: str = "day") -> dict:
    """An instrument's move over the period, from `found` = (source, last
    tick): against its first Bid since `start`, or for the rolling period
    against its last Bid at or before `start`."""
    inst = INSTRUMENTS[symbol]
    row = {"symbol": symbol, "label": inst.label, "digits": inst.digits, "pip": inst.pip,
           "base": inst.base, "quote": inst.quote}
    if found is None:
        return {**row, "state": "no data"}
    src, (now_ts, now_bid) = found
    opened = start if start is not None else period_start_ns(period, now_ts)
    quiet = {**row, "state": "no ticks in period", "last_ms": now_ts // 1_000_000, "last": now_bid}
    if period == "rolling":
        first = last_tick(src, opened)
        if first is None or opened - first[0] > STALE_NS or (opened + DAY_NS) - now_ts > STALE_NS:
            return quiet
    else:
        if now_ts < opened:
            return quiet
        first = first_tick(src, opened, now_ts)
        if first is None:
            return quiet
    open_bid = first[1]
    change = now_bid - open_bid
    return {
        **row, "state": "ok", "open": open_bid, "last": now_bid,
        "open_ms": first[0] // 1_000_000, "last_ms": now_ts // 1_000_000,
        "change": round(change, inst.digits),
        "change_pct": 100.0 * change / open_bid if open_bid else 0.0,
        "pips": round(change / inst.pip, 1),
    }


def rank(pairs: list[dict]) -> tuple[list[dict], dict]:
    """Score the four currencies from the pairs' moves (rows from `_move`):
    the ranking, strongest first, and every match as seen from each side."""
    matches: dict[str, dict[str, float]] = {c: {} for c in CURRENCIES}
    for p in pairs:
        if p.get("state") == "ok":
            matches[p["base"]][p["quote"]] = p["change_pct"]
            matches[p["quote"]][p["base"]] = -p["change_pct"]
    out = []
    for c in CURRENCIES:
        moves = matches[c]
        played = len(moves)
        wins = sum(1 for v in moves.values() if v > 0)
        losses = sum(1 for v in moves.values() if v < 0)
        avg = sum(moves.values()) / played if played else None
        if played == 3:
            label = LABELS[wins]
        elif played:
            label = "Strong" if wins == played else "Weak" if losses == played else "Mixed"
        else:
            label = "No data"
        out.append({"currency": c, "wins": wins, "losses": losses, "played": played,
                    "avg_pct": avg, "label": label,
                    "beats": sorted(k for k, v in moves.items() if v > 0),
                    "loses_to": sorted(k for k, v in moves.items() if v < 0)})
    out.sort(key=lambda r: (r["played"] > 0, r["wins"] - r["losses"], r["avg_pct"] or 0.0), reverse=True)
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out, matches


def measure(sources: dict[str, list], at_ns: int, period: str = "day") -> dict:
    """Strength at `at_ns` over `period` (PERIODS). `sources` maps each
    symbol to its tick sources, the preferred first (the session's own feed)."""
    if period not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")
    # The period is the one holding the newest pair tick; a pair with no
    # tick since it started is left out of the matches, not compared with
    # another period.
    found = {s: _latest(sources.get(s, []), at_ns) for s in PAIRS + ON_THEIR_OWN if s in INSTRUMENTS}
    newest = [found[s][1][0] for s in PAIRS if found.get(s) is not None]
    anchor = max(newest) if newest else None
    start = period_start_ns(period, anchor) if anchor is not None else None
    pairs = [_move(s, found[s], start, period) for s in PAIRS]
    others = [_move(s, found[s], start, period) for s in ON_THEIR_OWN if s in INSTRUMENTS]
    currencies, matches = rank(pairs)
    session = None
    if period == "session" and start is not None:
        session = SESSION_NAMES.get(start // (3600 * NS_PER_SECOND) % 24)
    return {
        "period": period,
        "start_ms": start // 1_000_000 if start is not None else None,
        "session": session,
        "day_open_ms": fx_day_open_ns(anchor) // 1_000_000 if anchor is not None else None,
        "currencies": currencies,
        "matrix": matches,
        "pairs": pairs,
        "others": others,
    }
