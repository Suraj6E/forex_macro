"""Tick access, one UTC day at a time (guide step 1).

The Layer 0 file holds ~46M EUR/USD ticks in 44 row groups of ~1M rows each,
so any day filter reads a whole ~9-day block. `build_tick_cache` rewrites it
once into one small file per day under `cache/ticks/`, and `load_day` reads
that file. Without the cache, `load_day` falls back to a filtered read of the
source, which is correct but slower.

Times are int64 nanoseconds since the epoch, UTC, throughout the engine.
Layer 0 writes naive timestamps that are UTC; nothing here localises them.
"""

from __future__ import annotations

import json
import threading
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Protocol

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from simulator import config

NS_PER_SECOND = 1_000_000_000
NS_PER_DAY = 86_400 * NS_PER_SECOND
_EPOCH = date(1970, 1, 1)


@dataclass(frozen=True)
class DayTicks:
    """Every kept tick of one UTC day, sorted by time."""

    day: date
    ts: np.ndarray  # int64 ns
    bid: np.ndarray  # float64
    ask: np.ndarray  # float64

    def __len__(self) -> int:
        return len(self.ts)

    @classmethod
    def empty(cls, day: date) -> "DayTicks":
        return cls(day, np.empty(0, np.int64), np.empty(0, np.float64), np.empty(0, np.float64))


class TickSource(Protocol):
    def days(self) -> list[date]: ...
    def load_day(self, day: date) -> DayTicks: ...


def day_start_ns(day: date) -> int:
    return (day - _EPOCH).days * NS_PER_DAY


def day_of_ns(ns: int) -> date:
    return _EPOCH + timedelta(days=int(ns // NS_PER_DAY))


def ns_to_datetime(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / NS_PER_SECOND, tz=timezone.utc)


def datetime_to_ns(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * NS_PER_SECOND + delta.microseconds * 1000


_UNIT_NS = {"s": NS_PER_SECOND, "ms": 1_000_000, "us": 1_000, "ns": 1}


def _ts_to_ns(column: pa.ChunkedArray | pa.Array) -> np.ndarray:
    """Arrow timestamp column -> int64 ns since the epoch.

    Reads the stored integers directly, so neither a naive nor a UTC-aware
    column can be shifted by a local timezone on the way.
    """
    raw = pc.cast(column, pa.int64())
    if isinstance(raw, pa.ChunkedArray):
        raw = raw.combine_chunks()
    return raw.to_numpy(zero_copy_only=False).astype(np.int64) * _UNIT_NS[column.type.unit]


class ParquetTickSource:
    """Ticks from the per-day cache, or straight from the Layer 0 file.

    Keeps a few recently used days in memory, shared by every session: a
    replay touches the current day and its neighbour, and a forming candle is
    recomputed from the current day many times per second.
    """

    def __init__(
        self,
        tick_file: Path = config.TICK_FILE,
        cache_dir: Path = config.TICK_CACHE_DIR,
        keep_flags: Iterable[int] = config.KEEP_FLAGS,
        memory_days: int = 8,
    ) -> None:
        self.tick_file = Path(tick_file)
        self.cache_dir = Path(cache_dir)
        self.keep_flags = tuple(sorted(set(keep_flags)))
        self._memory: OrderedDict[date, DayTicks] = OrderedDict()
        self._memory_days = memory_days
        self._lock = threading.Lock()
        self._days: list[date] | None = None

    # -- discovery ---------------------------------------------------------

    @property
    def cached(self) -> bool:
        return (self.cache_dir / "manifest.json").exists()

    def manifest(self) -> dict | None:
        path = self.cache_dir / "manifest.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def days(self) -> list[date]:
        """Every UTC day that holds at least one kept tick, ascending."""
        if self._days is None:
            manifest = self.manifest()
            if manifest is not None:
                self._days = [date.fromisoformat(d) for d in manifest["days"]]
            else:
                ts = pq.read_table(self.tick_file, columns=["timestamp"]).column("timestamp")
                day_index = np.unique(_ts_to_ns(ts) // NS_PER_DAY)
                self._days = [_EPOCH + timedelta(days=int(i)) for i in day_index]
        return self._days

    # -- loading -----------------------------------------------------------

    def load_day(self, day: date) -> DayTicks:
        with self._lock:
            hit = self._memory.get(day)
            if hit is not None:
                self._memory.move_to_end(day)
                return hit
        ticks = self._read_day(day)
        with self._lock:
            self._memory[day] = ticks
            self._memory.move_to_end(day)
            while len(self._memory) > self._memory_days:
                self._memory.popitem(last=False)
        return ticks

    def _read_day(self, day: date) -> DayTicks:
        if self.cached:
            path = self.cache_dir / f"{day.isoformat()}.parquet"
            if not path.exists():
                return DayTicks.empty(day)  # weekend, holiday, or outside the data
            table = pq.read_table(path)
            return DayTicks(
                day,
                _ts_to_ns(table.column("ts")),
                table.column("bid").to_numpy().astype(np.float64),
                table.column("ask").to_numpy().astype(np.float64),
            )
        return self._read_day_from_source(day)

    def _read_day_from_source(self, day: date) -> DayTicks:
        start = datetime(day.year, day.month, day.day)
        end = start + timedelta(days=1)
        table = pq.read_table(
            self.tick_file,
            columns=["timestamp", "Bid", "Ask", "quality_flag"],
            filters=[("timestamp", ">=", start), ("timestamp", "<", end)],
        )
        table = table.filter(pc.is_in(table.column("quality_flag"), pa.array(self.keep_flags, pa.int64())))
        return _sorted_day(
            day,
            _ts_to_ns(table.column("timestamp")),
            table.column("Bid").to_numpy().astype(np.float64),
            table.column("Ask").to_numpy().astype(np.float64),
        )


def _sorted_day(day: date, ts: np.ndarray, bid: np.ndarray, ask: np.ndarray) -> DayTicks:
    good = np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask >= bid)
    ts, bid, ask = ts[good], bid[good], ask[good]
    if len(ts) and np.any(np.diff(ts) < 0):
        order = np.argsort(ts, kind="stable")
        ts, bid, ask = ts[order], bid[order], ask[order]
    return DayTicks(day, ts, bid, ask)


def build_tick_cache(
    tick_file: Path = config.TICK_FILE,
    cache_dir: Path = config.TICK_CACHE_DIR,
    keep_flags: Iterable[int] = config.KEEP_FLAGS,
    progress=print,
) -> dict:
    """Split the Layer 0 file into one Parquet file per UTC day.

    Streams row group by row group, so memory holds one ~1M-row block plus
    the day that straddles two blocks. Rerunning replaces the cache.
    """
    tick_file, cache_dir = Path(tick_file), Path(cache_dir)
    keep_flags = tuple(sorted(set(keep_flags)))
    cache_dir.mkdir(parents=True, exist_ok=True)
    for old in cache_dir.glob("*.parquet"):
        old.unlink()
    manifest_path = cache_dir / "manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()

    source = pq.ParquetFile(tick_file)
    keep = pa.array(keep_flags, pa.int64())
    flag_counts: dict[int, int] = {}
    day_counts: dict[str, int] = {}

    pending_day: int | None = None
    pending: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []

    def flush(day_idx: int, parts) -> None:
        ts = np.concatenate([p[0] for p in parts])
        bid = np.concatenate([p[1] for p in parts])
        ask = np.concatenate([p[2] for p in parts])
        day = _EPOCH + timedelta(days=day_idx)
        ticks = _sorted_day(day, ts, bid, ask)
        if not len(ticks):
            return
        table = pa.table(
            {
                "ts": pa.array(ticks.ts.view("datetime64[ns]"), pa.timestamp("ns", tz="UTC")),
                "bid": ticks.bid,
                "ask": ticks.ask,
            }
        )
        pq.write_table(table, cache_dir / f"{day.isoformat()}.parquet", compression="zstd")
        day_counts[day.isoformat()] = len(ticks)

    for group in range(source.metadata.num_row_groups):
        table = source.read_row_group(group, columns=["timestamp", "Bid", "Ask", "quality_flag"])
        flags, counts = np.unique(table.column("quality_flag").to_numpy(), return_counts=True)
        for f, c in zip(flags, counts):
            flag_counts[int(f)] = flag_counts.get(int(f), 0) + int(c)
        table = table.filter(pc.is_in(table.column("quality_flag"), keep))
        ts = _ts_to_ns(table.column("timestamp"))
        bid = table.column("Bid").to_numpy().astype(np.float64)
        ask = table.column("Ask").to_numpy().astype(np.float64)
        if not len(ts):
            continue
        day_idx = ts // NS_PER_DAY
        cuts = np.flatnonzero(np.diff(day_idx)) + 1
        bounds = [0, *cuts.tolist(), len(ts)]
        for a, b in zip(bounds[:-1], bounds[1:]):
            d = int(day_idx[a])
            if pending_day is not None and d != pending_day:
                flush(pending_day, pending)
                pending = []
            pending_day = d
            pending.append((ts[a:b], bid[a:b], ask[a:b]))
        progress(f"  row group {group + 1}/{source.metadata.num_row_groups}")
    if pending_day is not None:
        flush(pending_day, pending)

    stat = tick_file.stat()
    manifest = {
        "source": str(tick_file),
        "source_size": stat.st_size,
        "source_mtime": stat.st_mtime,
        "keep_flags": list(keep_flags),
        "flag_counts": {str(k): v for k, v in sorted(flag_counts.items())},
        "days": sorted(day_counts),
        "ticks_per_day": day_counts,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


class MemoryTickSource:
    """In-memory ticks for tests: same interface as the Parquet source."""

    def __init__(self, ts: np.ndarray, bid: np.ndarray, ask: np.ndarray) -> None:
        ts = np.asarray(ts, np.int64)
        bid = np.asarray(bid, np.float64)
        ask = np.asarray(ask, np.float64)
        order = np.argsort(ts, kind="stable")
        ts, bid, ask = ts[order], bid[order], ask[order]
        self._by_day: dict[date, DayTicks] = {}
        day_idx = ts // NS_PER_DAY
        for d in np.unique(day_idx):
            m = day_idx == d
            day = _EPOCH + timedelta(days=int(d))
            self._by_day[day] = DayTicks(day, ts[m], bid[m], ask[m])
        self._days = sorted(self._by_day)

    def days(self) -> list[date]:
        return self._days

    def load_day(self, day: date) -> DayTicks:
        return self._by_day.get(day) or DayTicks.empty(day)


def trading_day_index(days: list[date], day: date) -> int:
    """Index of the first trading day on or after `day`."""
    return bisect_left(days, day)
