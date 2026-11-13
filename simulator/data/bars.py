"""Bid candles for every timeframe (guide step 2.1).

Candles take open, high, low and close from the Bid, the usual chart
convention; the Ask is recovered as Bid plus the stored last spread. These
are not the Layer 1 bars, which mix mid, Ask and Bid and are not a tradable
candle.

Every timeframe divides a UTC day, so buckets are aligned to UTC midnight and
no candle ever spans two days: building day by day and concatenating gives
exactly the same bars as building over the whole history at once, and an
update only has to rebuild the days that changed.

A candle is *complete* at time T when its end (start + length) is <= T. Only
complete candles come from this store. The candle still forming at T is built
from ticks up to T by the session, never read from here, because the stored
version already holds the rest of its period.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from simulator.data.ticks import NS_PER_SECOND, TickSource, day_start_ns

#: key -> (seconds, label). Order is the order of the timeframe buttons.
TIMEFRAMES: dict[str, tuple[int, str]] = {
    "m1": (60, "1M"),
    "m5": (300, "5M"),
    "m15": (900, "15M"),
    "h1": (3600, "1H"),
    "h4": (14_400, "4H"),
    "d1": (86_400, "1D"),
}

FIELDS = ("time", "open", "high", "low", "close", "spread", "ticks")


def tf_seconds(tf: str) -> int:
    try:
        return TIMEFRAMES[tf][0]
    except KeyError:
        raise ValueError(f"Unknown timeframe {tf!r}; expected one of {', '.join(TIMEFRAMES)}") from None


def _empty() -> dict[str, np.ndarray]:
    return {f: np.empty(0, np.int64 if f in ("time", "ticks") else np.float64) for f in FIELDS}


def bars_from_ticks(ts: np.ndarray, bid: np.ndarray, ask: np.ndarray, seconds: int) -> dict[str, np.ndarray]:
    """Aggregate sorted ticks into Bid candles of `seconds` length.

    `time` is the candle's start in epoch seconds. `spread` is Ask - Bid of
    the candle's last tick. Buckets without ticks produce no candle.
    """
    if len(ts) == 0:
        return _empty()
    width = seconds * NS_PER_SECOND
    bucket = ts // width
    starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
    ends = np.r_[starts[1:], len(ts)]
    return {
        "time": (bucket[starts] * seconds).astype(np.int64),
        "open": bid[starts],
        "high": np.maximum.reduceat(bid, starts),
        "low": np.minimum.reduceat(bid, starts),
        "close": bid[ends - 1],
        "spread": ask[ends - 1] - bid[ends - 1],
        "ticks": (ends - starts).astype(np.int64),
    }


def concat_bars(parts: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    parts = [p for p in parts if len(p["time"])]
    if not parts:
        return _empty()
    return {f: np.concatenate([p[f] for p in parts]) for f in FIELDS}


def bar_dict(bars: dict[str, np.ndarray], i: int) -> dict:
    """One candle as the JSON the chart receives."""
    return {
        "time": int(bars["time"][i]),
        "open": float(bars["open"][i]),
        "high": float(bars["high"][i]),
        "low": float(bars["low"][i]),
        "close": float(bars["close"][i]),
        "spread": float(bars["spread"][i]),
    }


@dataclass
class TimeframeBars:
    seconds: int
    arrays: dict[str, np.ndarray]
    end_ns: np.ndarray  # start + length, in ns; sorted because starts are

    @classmethod
    def from_arrays(cls, seconds: int, arrays: dict[str, np.ndarray]) -> "TimeframeBars":
        end_ns = (arrays["time"].astype(np.int64) + seconds) * NS_PER_SECOND
        return cls(seconds, arrays, end_ns)

    def __len__(self) -> int:
        return len(self.arrays["time"])

    def completed_count(self, sim_ns: int) -> int:
        """How many candles are complete at `sim_ns` (end <= sim_ns)."""
        return int(np.searchsorted(self.end_ns, sim_ns, side="right"))

    def slice(self, a: int, b: int) -> list[dict]:
        return [bar_dict(self.arrays, i) for i in range(max(a, 0), max(b, 0))]

    def index_of_time(self, time_s: int) -> int:
        """Index of the first candle starting at or after `time_s`."""
        return int(np.searchsorted(self.arrays["time"], time_s, side="left"))


def _day_bars(source: TickSource, days: list[date], progress=None) -> dict[str, list]:
    parts: dict[str, list] = {tf: [] for tf in TIMEFRAMES}
    for n, day in enumerate(days, 1):
        t = source.load_day(day)
        for tf, (seconds, _label) in TIMEFRAMES.items():
            parts[tf].append(bars_from_ticks(t.ts, t.bid, t.ask, seconds))
        if progress and (n % 50 == 0 or n == len(days)):
            progress(f"  candles: {n}/{len(days)} days")
    return parts


class BarStore:
    """Complete candles for every timeframe, held in memory.

    Two years of EUR/USD at one minute is ~740k candles, a few tens of MB
    across all timeframes, so the whole store is loaded once and shared by
    every session read-only. An update swaps in new arrays atomically.
    """

    def __init__(self, by_tf: dict[str, TimeframeBars]) -> None:
        self.by_tf = by_tf

    def __getitem__(self, tf: str) -> TimeframeBars:
        return self.by_tf[tf]

    @classmethod
    def build(cls, source: TickSource, progress=None) -> "BarStore":
        parts = _day_bars(source, source.days(), progress)
        return cls({tf: TimeframeBars.from_arrays(TIMEFRAMES[tf][0], concat_bars(parts[tf])) for tf in TIMEFRAMES})

    def rebuild_from(self, source: TickSource, first_day: date) -> None:
        """Recompute every day from `first_day` on and splice it in."""
        cut_s = day_start_ns(first_day) // NS_PER_SECOND
        days = [d for d in source.days() if d >= first_day]
        parts = _day_bars(source, days)
        by_tf = {}
        for tf, (seconds, _label) in TIMEFRAMES.items():
            old = self.by_tf[tf].arrays
            keep = int(np.searchsorted(old["time"], cut_s, side="left"))
            head = {f: old[f][:keep] for f in FIELDS}
            by_tf[tf] = TimeframeBars.from_arrays(seconds, concat_bars([head, *parts[tf]]))
        self.by_tf = by_tf

    def save(self, directory: Path, keep_flags) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for tf, bars in self.by_tf.items():
            tmp = directory / f"{tf}.tmp"
            pq.write_table(pa.table(bars.arrays), tmp, compression="zstd")
            tmp.replace(directory / f"{tf}.parquet")
        (directory / "meta.json").write_text(json.dumps({"keep_flags": list(keep_flags)}), encoding="utf-8")

    @classmethod
    def load(cls, directory: Path) -> "BarStore":
        directory = Path(directory)
        by_tf = {}
        for tf, (seconds, _label) in TIMEFRAMES.items():
            table = pq.read_table(directory / f"{tf}.parquet")
            arrays = {f: table.column(f).to_numpy() for f in FIELDS}
            by_tf[tf] = TimeframeBars.from_arrays(seconds, arrays)
        return cls(by_tf)

    @staticmethod
    def exists(directory: Path, keep_flags=None) -> bool:
        """Built, and built with these flags (a flag change needs a rebuild)."""
        directory = Path(directory)
        if not all((directory / f"{tf}.parquet").exists() for tf in TIMEFRAMES):
            return False
        if keep_flags is None:
            return True
        try:
            meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return False
        return sorted(meta.get("keep_flags", [])) == sorted(keep_flags)
