"""Layer 0's tick checks, for ticks that did not come through Layer 0.

Downloaded and recorded ticks must be cleaned exactly as the 2023-2024 file
was, or the replay would change character on 1 January 2025. trading-research
`layer0/SchemaValidation.py` runs these steps on one UTC day at a time:

  2  bad price: missing field, Ask < Bid, or a price <= 0
  1  spread > 10 x the median spread of the day's unflagged ticks
     -- then: sort by time, drop repeated timestamps keeping the last --
  3  more than 5 s since the previous tick, outside the weekend
     (Friday from 22:00, all Saturday, all Sunday, in UTC)
  4  mid price > 5 rolling standard deviations from the rolling median,
     centred window of 1000 ticks, only for ticks still unflagged

One deliberate difference: Layer 0 then drops every row with a missing
value, which removes the first tick of each day (it has no previous tick, so
no gap) and the rare tick whose window is perfectly flat. Those are valid
quotes; they are kept here with their flag.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

NS_PER_SECOND = 1_000_000_000
NS_PER_DAY = 86_400 * NS_PER_SECOND

FLAG_CLEAN, FLAG_SPREAD, FLAG_PRICE, FLAG_GAP, FLAG_OUTLIER = 0, 1, 2, 3, 4

GAP_NS = 5 * NS_PER_SECOND
WINDOW = 1000
SIGMAS = 5.0
SPREAD_MULTIPLE = 10.0


def _weekend(ts: np.ndarray) -> np.ndarray:
    days = ts // NS_PER_DAY
    weekday = (days + 3) % 7  # 1970-01-01 was a Thursday; 0 = Monday
    hour = (ts % NS_PER_DAY) // (3600 * NS_PER_SECOND)
    return ((weekday == 4) & (hour >= 22)) | (weekday == 5) | (weekday == 6)


def flag_day(ts: np.ndarray, bid: np.ndarray, ask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Clean one UTC day of ticks. Returns (ts, bid, ask, flag), sorted by
    time with repeated timestamps reduced to the last quote."""
    ts = np.asarray(ts, np.int64)
    bid = np.asarray(bid, np.float64)
    ask = np.asarray(ask, np.float64)
    n = len(ts)
    flag = np.zeros(n, np.int8)
    if n == 0:
        return ts, bid, ask, flag

    bad = ~np.isfinite(bid) | ~np.isfinite(ask) | (ask < bid) | (ask <= 0) | (bid <= 0)
    flag[bad] = FLAG_PRICE

    spread = ask - bid
    clean = flag == FLAG_CLEAN
    if clean.any():
        median = float(np.median(spread[clean]))
        flag[clean & (spread > SPREAD_MULTIPLE * median)] = FLAG_SPREAD

    order = np.argsort(ts, kind="stable")
    ts, bid, ask, flag = ts[order], bid[order], ask[order], flag[order]
    last_of_run = np.r_[ts[1:] != ts[:-1], True]
    ts, bid, ask, flag = ts[last_of_run], bid[last_of_run], ask[last_of_run], flag[last_of_run]

    gap = np.r_[False, np.diff(ts) > GAP_NS] & ~_weekend(ts)
    flag[(flag == FLAG_CLEAN) & gap] = FLAG_GAP

    mid = pd.Series((ask + bid) / 2)
    roll = mid.rolling(window=WINDOW, center=True, min_periods=1)
    deviation = (np.abs(mid - roll.median()) / roll.std()).to_numpy()
    with np.errstate(invalid="ignore"):
        outlier = deviation > SIGMAS
    flag[(flag == FLAG_CLEAN) & outlier] = FLAG_OUTLIER
    return ts, bid, ask, flag
