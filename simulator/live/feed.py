"""Reading a live feed while it is being recorded.

A live feed directory (see `mt5rec.py`) holds closed days as Parquet with
quality flags, exactly like the replay feed, and the open day(s) as an
append-only CSV the recorder is still writing (`live/YYYY-MM-DD.csv`, rows
of `ts_ms,bid,ask`, UTC). `LiveFeedSource` serves both through the ordinary
TickSource interface, tailing the CSV so new ticks appear within a frame.

On an open day only the checks that need no future ticks are applied: bad
prices, and a spread over 10 x the typical spread. The full Layer 0 pass,
whose rolling window is centred and so looks 500 ticks ahead, runs when the
day is closed and compacted.
"""

from __future__ import annotations

import io
import threading
import time
from collections import OrderedDict
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.csv as pacsv

from simulator import config
from simulator.data.bars import TIMEFRAMES, TimeframeBars, bars_from_ticks, concat_bars
from simulator.data.quality import SPREAD_MULTIPLE
from simulator.data.ticks import DayTicks, FeedStore

CSV_COLUMNS = ["ts_ms", "bid", "ask"]


def read_csv_bytes(data: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not data:
        e = np.empty(0)
        return e.astype(np.int64), e, e
    t = pacsv.read_csv(
        io.BytesIO(data),
        read_options=pacsv.ReadOptions(column_names=CSV_COLUMNS),
        convert_options=pacsv.ConvertOptions(column_types={"ts_ms": pa.int64(), "bid": pa.float64(), "ask": pa.float64()}),
    )
    return (
        t.column("ts_ms").to_numpy() * 1_000_000,
        t.column("bid").to_numpy(),
        t.column("ask").to_numpy(),
    )


class CsvTail:
    """Incremental reader of a CSV someone else is appending to."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.offset = 0
        self.parts: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        self._joined = None
        self.count = 0

    def read(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size < self.offset:  # replaced: start over
            self.offset, self.parts, self._joined, self.count = 0, [], None, 0
        if size > self.offset:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read(size - self.offset)
            cut = data.rfind(b"\n")
            if cut >= 0:  # only whole lines; a half-written one waits
                part = read_csv_bytes(data[: cut + 1])
                self.offset += cut + 1
                if len(part[0]):
                    self.parts.append(part)
                    self.count += len(part[0])
                    self._joined = None
        if self._joined is None:
            if self.parts:
                self._joined = tuple(np.concatenate([p[i] for p in self.parts]) for i in range(3))
                self.parts = [self._joined]
            else:
                e = np.empty(0)
                self._joined = (e.astype(np.int64), e, e)
        return self._joined


class LiveFeedSource:
    def __init__(self, feed_dir: Path, keep_flags=config.KEEP_FLAGS) -> None:
        self.dir = Path(feed_dir)
        self.store = FeedStore(feed_dir)
        self.live_dir = self.dir / "live"
        self.keep_flags = tuple(keep_flags)
        self._keep = np.zeros(256, bool)
        self._keep[list(self.keep_flags)] = True
        self._tails: dict[date, CsvTail] = {}
        self._closed: OrderedDict[date, DayTicks] = OrderedDict()
        self._days: list[date] = []
        self._scanned = 0.0
        self._median: dict[date, tuple[int, float]] = {}
        self._lock = threading.RLock()
        self.refresh(force=True)

    # -- days ----------------------------------------------------------------

    def refresh(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._scanned < 0.5:
            return False
        self._scanned = now
        closed = {date.fromisoformat(d) for d, e in self.store.manifest()["days"].items() if e.get("ticks")}
        open_days = set()
        if self.live_dir.exists():
            open_days = {date.fromisoformat(p.stem) for p in self.live_dir.glob("*.csv") if p.stat().st_size}
        days = sorted(closed | open_days)
        changed = days != self._days
        with self._lock:
            self._days = days
            for d in [d for d in self._tails if d not in open_days]:
                self._tails.pop(d)  # compacted since: read the Parquet now
        return changed

    def days(self) -> list[date]:
        self.refresh()
        return self._days

    def is_open(self, day: date) -> bool:
        return (self.live_dir / f"{day.isoformat()}.csv").exists()

    # -- ticks ---------------------------------------------------------------

    def _reference_spread(self, day: date, spread: np.ndarray) -> float:
        n, med = self._median.get(day, (0, 0.0))
        if len(spread) and (n == 0 or len(spread) >= 2 * n or len(spread) - n >= 5000):
            med = float(np.median(spread))
            self._median[day] = (len(spread), med)
        return med

    def load_day(self, day: date) -> DayTicks:
        with self._lock:
            if self.is_open(day):
                tail = self._tails.get(day)
                if tail is None:
                    tail = self._tails[day] = CsvTail(self.live_dir / f"{day.isoformat()}.csv")
                ts, bid, ask = tail.read()
                spread = ask - bid
                good = np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask >= bid)
                med = self._reference_spread(day, spread[good])
                if med > 0:
                    good &= spread <= SPREAD_MULTIPLE * med
                return DayTicks(day, ts[good], bid[good], ask[good])
            hit = self._closed.get(day)
            if hit is not None:
                return hit
            ts, bid, ask, flag = self.store.read_day(day)
            keep = self._keep[flag]
            ticks = DayTicks(day, ts[keep], bid[keep], ask[keep])
            self._closed[day] = ticks
            while len(self._closed) > 16:
                self._closed.popitem(last=False)
            return ticks

    def latest_ns(self) -> int | None:
        for day in reversed(self.days()[-2:]):
            t = self.load_day(day)
            if len(t):
                return int(t.ts[-1])
        return None


class FeedBars:
    """Candles of a growing feed, computed from its ticks on demand.

    Closed days are computed once; the open day is recomputed only when it
    has new ticks, and only for the timeframe asked for.
    """

    def __init__(self, source: LiveFeedSource) -> None:
        self.source = source
        self._per_day: dict[tuple[date, str], tuple[int, dict]] = {}
        self._joined: dict[str, tuple[tuple, TimeframeBars]] = {}

    def __getitem__(self, tf: str) -> TimeframeBars:
        seconds = TIMEFRAMES[tf][0]
        parts, signature = [], []
        for day in self.source.days():
            t = self.source.load_day(day)
            key = (day, tf)
            cached = self._per_day.get(key)
            if cached is None or cached[0] != len(t):
                cached = (len(t), bars_from_ticks(t.ts, t.bid, t.ask, seconds))
                self._per_day[key] = cached
            parts.append(cached[1])
            signature.append((day, len(t)))
        signature = tuple(signature)
        hit = self._joined.get(tf)
        if hit is None or hit[0] != signature:
            hit = (signature, TimeframeBars.from_arrays(seconds, concat_bars(parts)))
            self._joined[tf] = hit
        return hit[1]
