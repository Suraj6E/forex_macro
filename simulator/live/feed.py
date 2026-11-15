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
import json
import threading
import time
from collections import OrderedDict
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.csv as pacsv

from simulator import config
from simulator.data.bars import TIMEFRAMES, BarStore, TimeframeBars, bars_from_ticks, concat_bars
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
                ts, bid, ask = ts[good], bid[good], ask[good]
                if self.store.day_path(day).exists():
                    # Recorded in two sittings: the stored part comes first.
                    old = self.load_closed(day)
                    later = ts > (old.ts[-1] if len(old) else -1)
                    ts, bid, ask = np.r_[old.ts, ts[later]], np.r_[old.bid, bid[later]], np.r_[old.ask, ask[later]]
                return DayTicks(day, ts, bid, ask)
            return self.load_closed(day)

    def load_closed(self, day: date) -> DayTicks:
        """The stored (Parquet) part of a day only."""
        with self._lock:
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


class _ClosedView:
    """A feed's stored days only, for building their candles."""

    def __init__(self, source: LiveFeedSource) -> None:
        self.source = source

    def days(self) -> list[date]:
        return self.source.days()

    def load_day(self, day: date) -> DayTicks:
        return self.source.load_closed(day)


class JoinedBars:
    """Stored candles followed by the open day's, without copying either.
    Open days are always after every stored candle (they are today's)."""

    def __init__(self, closed: TimeframeBars, open_: TimeframeBars) -> None:
        self.closed, self.open = closed, open_
        self.seconds = closed.seconds

    def __len__(self) -> int:
        return len(self.closed) + len(self.open)

    def completed_count(self, sim_ns: int) -> int:
        return self.closed.completed_count(sim_ns) + self.open.completed_count(sim_ns)

    def index_of_time(self, time_s: int) -> int:
        return self.closed.index_of_time(time_s) + self.open.index_of_time(time_s)

    def slice(self, a: int, b: int) -> list[dict]:
        n = len(self.closed)
        a, b = max(a, 0), max(b, 0)
        return self.closed.slice(a, min(b, n)) + self.open.slice(max(a - n, 0), max(b - n, 0))

    @property
    def arrays(self) -> dict:
        return concat_bars([self.closed.arrays, self.open.arrays])


class FeedBars:
    """Candles of a recorded feed: stored ones for closed days, kept on disk
    in `bars/` like the replay history's and updated when days are added
    (years of broker history are not rebuilt on every frame), plus the open
    day's, recomputed only when it has new ticks."""

    def __init__(self, source: LiveFeedSource) -> None:
        self.source = source
        self.bars_dir = Path(source.dir) / "bars"
        self.closed: BarStore | None = None
        self._built: dict[str, int] = {}  # day -> ticks its candles were built from
        self._version = None
        self._lock = threading.Lock()
        self._open: dict[tuple[date, str], tuple[int, TimeframeBars]] = {}

    def _sync_closed(self) -> None:
        try:
            mtime = self.source.store.manifest_path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        open_days = frozenset(d.isoformat() for d in self.source.days() if self.source.is_open(d))
        version = (mtime, open_days)
        if version == self._version and self.closed is not None:
            return
        with self._lock:
            if self.closed is None:
                self.closed, self._built = BarStore.empty(), {}
                if BarStore.exists(self.bars_dir, self.source.keep_flags):
                    try:
                        self._built = json.loads((self.bars_dir / "days.json").read_text(encoding="utf-8"))
                        self.closed = BarStore.load(self.bars_dir)
                    except (FileNotFoundError, ValueError):
                        self.closed, self._built = BarStore.empty(), {}
            entries = self.source.store.manifest().get("days", {})
            # An open day's candles are all built from its ticks below, even
            # the part already stored, so no candle appears twice.
            wanted = {d: e.get("ticks", 0) for d, e in entries.items() if e.get("ticks") and d not in open_days}
            changed = [d for d, n in wanted.items() if self._built.get(d) != n]
            changed += [d for d in self._built if d not in wanted]
            if changed:
                self.closed.replace_days(_ClosedView(self.source), [date.fromisoformat(d) for d in changed])
                self._built = wanted
                self.closed.save(self.bars_dir, self.source.keep_flags)
                (self.bars_dir / "days.json").write_text(json.dumps(wanted), encoding="utf-8")
            self._version = version

    def __getitem__(self, tf: str) -> JoinedBars:
        seconds = TIMEFRAMES[tf][0]
        self._sync_closed()
        parts = []
        for day in self.source.days():
            if not self.source.is_open(day):
                continue
            t = self.source.load_day(day)
            key = (day, tf)
            cached = self._open.get(key)
            if cached is None or cached[0] != len(t):
                cached = (len(t), TimeframeBars.from_arrays(seconds, bars_from_ticks(t.ts, t.bid, t.ask, seconds)))
                self._open[key] = cached
            parts.append(cached[1])
        for key in [k for k in self._open if not self.source.is_open(k[0])]:
            self._open.pop(key)
        open_bars = TimeframeBars.from_arrays(seconds, concat_bars([p.arrays for p in parts]))
        return JoinedBars(self.closed[tf], open_bars)
