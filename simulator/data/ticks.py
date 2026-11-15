"""Tick storage, one UTC day per file (guide step 1).

A feed directory holds `ticks/YYYY-MM-DD.parquet` with every tick of the day
and a `manifest.json` describing each day: where it came from, how many
ticks, whether it is complete. Quality flags are stored, not applied;
`KEEP_FLAGS` filters when a day is read, so changing it never means fetching
anything again.

The file layout is compact, because nine instruments over years add up:
`ms` (int64 milliseconds), `bid` (int32 points), `spr` (int32 points, Ask -
Bid) and `flag` (int8), the integers delta-encoded and zstd-compressed, with
the points-per-unit scale in the file's metadata. That is about 2.1 bytes a
tick against 10 for float columns. Every source quotes whole points and
whole milliseconds, so nothing is lost, and a price read back is the double
nearest its decimal, which is the same double a typed-in stop of that price
becomes. (Some 2023 days of the Layer 0 file hold prices one unit in the
last place off, from float arithmetic; stored as points they compare equal
to the level they stand for.) Files in the first layout (`ts`, `bid`, `ask`
as floats) are still read, and `migrate_feed` rewrites them.

The Dukascopy feed is filled from two places: the Layer 0 file of
trading-research (2023-2024, imported once by `import_layer0`) and the
Dukascopy datafeed itself for everything after (`dukascopy.update`).

Times are int64 nanoseconds since the epoch, UTC, throughout the engine.
Layer 0 writes naive timestamps that are UTC; nothing here localises them.
"""

from __future__ import annotations

import json
import os
import threading
import time
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
    """The kept ticks of one UTC day, sorted by time."""

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


def now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1e9)


_UNIT_NS = {"s": NS_PER_SECOND, "ms": 1_000_000, "us": 1_000, "ns": 1}


def ts_to_ns(column: pa.ChunkedArray | pa.Array) -> np.ndarray:
    """Arrow timestamp column -> int64 ns since the epoch.

    Reads the stored integers directly, so neither a naive nor a UTC-aware
    column can be shifted by a local timezone on the way.
    """
    raw = pc.cast(column, pa.int64())
    if isinstance(raw, pa.ChunkedArray):
        raw = raw.combine_chunks()
    return raw.to_numpy(zero_copy_only=False).astype(np.int64) * _UNIT_NS[column.type.unit]


def _retry(fn, tries: int = 20, wait: float = 0.05):
    """Run `fn`, retrying on the sharing errors Windows raises when one
    thread replaces a file another is reading at that instant (a download
    writing a day while a session reads it). Each lasts milliseconds."""
    for attempt in range(tries):
        try:
            return fn()
        except FileNotFoundError:
            raise
        except OSError:
            # PermissionError from os.replace, or pyarrow's own OSError
            # ("used by another process"), which carries no winerror.
            if attempt == tries - 1:
                raise
            time.sleep(wait)


def _atomic_write_table(table: pa.Table, path: Path, **options) -> None:
    tmp = path.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd", **options)
    _retry(lambda: os.replace(tmp, path))


#: Delta encoding suits ticks: times, prices and spreads move in small steps.
_ENCODING = {"ms": "DELTA_BINARY_PACKED", "bid": "DELTA_BINARY_PACKED", "spr": "DELTA_BINARY_PACKED", "flag": "PLAIN"}


def infer_scale(*prices: np.ndarray, max_digits: int = 6) -> int:
    """Smallest 10**d at which every price is a whole number of points."""
    joined = np.concatenate([np.asarray(p, np.float64).ravel() for p in prices]) if prices else np.empty(0)
    for d in range(max_digits + 1):
        scaled = joined * 10**d
        if np.all(np.abs(scaled - np.round(scaled)) < 1e-6):
            return 10**d
    return 10**max_digits


def _atomic_write_json(data: dict, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    _retry(lambda: os.replace(tmp, path))


class FeedStore:
    """Read and write one feed directory: day files plus the manifest.

    `scale` is points per price unit (10**digits). Without one, the
    manifest's is used, and failing that it is read off the prices.
    """

    def __init__(self, feed_dir: Path, scale: int | None = None) -> None:
        self.dir = Path(feed_dir)
        self.ticks_dir = self.dir / "ticks"
        self.manifest_path = self.dir / "manifest.json"
        self.lock = threading.Lock()
        self.scale = int(scale) if scale else None

    def exists(self) -> bool:
        return self.manifest_path.exists()

    def manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {"days": {}}
        return json.loads(_retry(lambda: self.manifest_path.read_text(encoding="utf-8")))

    def save_manifest(self, manifest: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        manifest["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _atomic_write_json(manifest, self.manifest_path)

    def day_path(self, day: date) -> Path:
        return self.ticks_dir / f"{day.isoformat()}.parquet"

    def _scale_for(self, bid: np.ndarray, ask: np.ndarray) -> int:
        if self.scale is None:
            self.scale = int(self.manifest().get("scale") or 0) or None
        return self.scale or infer_scale(bid, ask)

    def write_day(self, day: date, ts: np.ndarray, bid: np.ndarray, ask: np.ndarray, flag: np.ndarray) -> None:
        self.ticks_dir.mkdir(parents=True, exist_ok=True)
        ts = np.asarray(ts, np.int64)
        bid = np.asarray(bid, np.float64)
        ask = np.asarray(ask, np.float64)
        scale = self._scale_for(bid, ask)
        bid_pts = np.round(bid * scale).astype(np.int64)
        spr_pts = np.round(ask * scale).astype(np.int64) - bid_pts
        table = pa.table(
            {
                "ms": ts // 1_000_000,
                "bid": bid_pts.astype(np.int32),
                "spr": spr_pts.astype(np.int32),
                "flag": np.asarray(flag, np.int8),
            },
            metadata={"scale": str(scale), "layout": "2"},
        )
        _atomic_write_table(table, self.day_path(day), use_dictionary=False, column_encoding=_ENCODING)

    def read_day(self, day: date) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Every stored tick of the day, flags included."""
        path = self.day_path(day)
        if not path.exists():
            e = np.empty(0)
            return e.astype(np.int64), e, e, e.astype(np.int8)
        t = _retry(lambda: pq.read_table(path))
        flag = t.column("flag").to_numpy().astype(np.int8)
        if "ms" not in t.column_names:  # first layout: float columns
            return (
                ts_to_ns(t.column("ts")),
                t.column("bid").to_numpy().astype(np.float64),
                t.column("ask").to_numpy().astype(np.float64),
                flag,
            )
        scale = float(t.schema.metadata[b"scale"])
        bid_pts = t.column("bid").to_numpy().astype(np.int64)
        ask_pts = bid_pts + t.column("spr").to_numpy().astype(np.int64)
        return t.column("ms").to_numpy().astype(np.int64) * 1_000_000, bid_pts / scale, ask_pts / scale, flag

    def day_files(self) -> list[Path]:
        return sorted(self.ticks_dir.glob("*.parquet")) if self.ticks_dir.exists() else []

    def size_bytes(self) -> int:
        total = 0
        for p in self.day_files():
            try:
                total += p.stat().st_size
            except OSError:
                pass  # replaced or being replaced this instant
        return total


def day_entry(source: str, ts: np.ndarray, flag: np.ndarray, **extra) -> dict:
    counts = np.bincount(flag.astype(np.int64), minlength=5) if len(flag) else np.zeros(5, np.int64)
    return {
        "source": source,
        "ticks": int(len(ts)),
        "flags": [int(c) for c in counts[:5]],
        "first_ns": int(ts[0]) if len(ts) else None,
        "last_ns": int(ts[-1]) if len(ts) else None,
        **extra,
    }


class ParquetTickSource:
    """Kept ticks of a feed, a day at a time.

    Keeps a few recently used days in memory, shared by every session: a
    replay touches the current day and its neighbour, and a forming candle is
    recomputed from the current day many times per second. `refresh()` picks
    up days added by an update while the server runs.
    """

    def __init__(
        self,
        feed_dir: Path = config.DUKA_DIR,
        keep_flags: Iterable[int] = config.KEEP_FLAGS,
        memory_days: int = 8,
    ) -> None:
        self.dir = Path(feed_dir)
        self.store = FeedStore(feed_dir)
        self.keep_flags = tuple(sorted(set(keep_flags)))
        self._keep = np.zeros(256, bool)
        self._keep[list(self.keep_flags)] = True
        self._memory: OrderedDict[date, DayTicks] = OrderedDict()
        self._memory_days = memory_days
        self._lock = threading.Lock()
        self._days: list[date] = []
        self._entries: dict = {}
        self._version = None
        self.refresh()

    @property
    def ready(self) -> bool:
        return bool(self._days)

    def refresh(self) -> bool:
        """Reload the manifest if it changed. True when it did."""
        try:
            version = self.store.manifest_path.stat().st_mtime_ns
        except FileNotFoundError:
            version = None
        if version == self._version:
            return False
        entries = self.store.manifest()["days"]
        days = sorted(date.fromisoformat(d) for d, e in entries.items() if e.get("ticks"))
        with self._lock:
            # A day whose entry changed (a partial day that grew) is re-read.
            for d in list(self._memory):
                if self._entries.get(d.isoformat()) != entries.get(d.isoformat()):
                    self._memory.pop(d, None)
            self._entries = entries
            self._days = days
            self._version = version
        return True

    def days(self) -> list[date]:
        """Every UTC day that holds at least one tick, ascending."""
        return self._days

    def load_day(self, day: date) -> DayTicks:
        with self._lock:
            hit = self._memory.get(day)
            if hit is not None:
                self._memory.move_to_end(day)
                return hit
        ts, bid, ask, flag = self.store.read_day(day)
        keep = self._keep[flag]
        ticks = DayTicks(day, ts[keep], bid[keep], ask[keep])
        with self._lock:
            self._memory[day] = ticks
            self._memory.move_to_end(day)
            while len(self._memory) > self._memory_days:
                self._memory.popitem(last=False)
        return ticks


def import_layer0(
    tick_file: Path = config.TICK_FILE,
    feed_dir: Path = config.DUKA_DIR,
    progress=print,
) -> dict:
    """Copy the Layer 0 file into the feed, one file per UTC day.

    Every tick is copied with its Layer 0 quality flag. Streams row group by
    row group, so memory holds one ~1M-row block plus the day that straddles
    two blocks. Days already in the feed from Dukascopy downloads are left
    alone; days from a previous import are replaced.
    """
    tick_file = Path(tick_file)
    store = FeedStore(feed_dir, scale=10**5)
    manifest = store.manifest()
    source = pq.ParquetFile(tick_file)
    flag_counts: dict[int, int] = {}
    imported: dict[str, dict] = {}

    pending_day: int | None = None
    pending: list[tuple[np.ndarray, ...]] = []

    def flush(day_idx: int, parts) -> None:
        ts = np.concatenate([p[0] for p in parts])
        bid = np.concatenate([p[1] for p in parts])
        ask = np.concatenate([p[2] for p in parts])
        flag = np.concatenate([p[3] for p in parts])
        order = np.argsort(ts, kind="stable")
        ts, bid, ask, flag = ts[order], bid[order], ask[order], flag[order]
        day = _EPOCH + timedelta(days=day_idx)
        if manifest["days"].get(day.isoformat(), {}).get("source") == "dukascopy":
            return
        store.write_day(day, ts, bid, ask, flag)
        imported[day.isoformat()] = day_entry("layer0", ts, flag, complete=True)

    for group in range(source.metadata.num_row_groups):
        table = source.read_row_group(group, columns=["timestamp", "Bid", "Ask", "quality_flag"])
        ts = ts_to_ns(table.column("timestamp"))
        bid = table.column("Bid").to_numpy().astype(np.float64)
        ask = table.column("Ask").to_numpy().astype(np.float64)
        flag = table.column("quality_flag").to_numpy().astype(np.int8)
        for f, c in zip(*np.unique(flag, return_counts=True)):
            flag_counts[int(f)] = flag_counts.get(int(f), 0) + int(c)
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
            pending.append((ts[a:b], bid[a:b], ask[a:b], flag[a:b]))
        progress(f"  row group {group + 1}/{source.metadata.num_row_groups}")
    if pending_day is not None:
        flush(pending_day, pending)

    stat = tick_file.stat()
    manifest = store.manifest()  # re-read: an update may have run meanwhile
    manifest["symbol"] = config.SYMBOL
    manifest["scale"] = 10**5
    manifest["layout"] = 2
    manifest["layer0"] = {
        "source": str(tick_file),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "flag_counts": {str(k): v for k, v in sorted(flag_counts.items())},
        "days": len(imported),
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    manifest["days"].update(imported)
    manifest["days"] = dict(sorted(manifest["days"].items()))
    store.save_manifest(manifest)
    return manifest


def migrate_feed(feed_dir: Path, scale: int | None = None, progress=None, stop=None) -> int:
    """Rewrite day files still in the first layout into the compact one.

    One file at a time, each replaced atomically, so stopping half way
    leaves a feed that reads correctly and finishes next time. Without a
    scale (a recorded broker feed), each day's is read off its prices.
    Returns how many files were rewritten.
    """
    store = FeedStore(feed_dir, scale=scale)
    old = [p for p in store.day_files() if "ms" not in pq.read_schema(p).names]
    for n, path in enumerate(old, 1):
        if stop is not None and stop.is_set():
            return n - 1
        day = date.fromisoformat(path.stem)
        with store.lock:
            store.write_day(day, *store.read_day(day))
        if progress:
            progress(n, len(old))
    if store.exists():
        with store.lock:
            manifest = store.manifest()
            if scale:
                manifest["scale"] = scale
            manifest["layout"] = 2
            store.save_manifest(manifest)
    return len(old)


def needs_migration(feed_dir: Path) -> bool:
    store = FeedStore(feed_dir)
    return store.exists() and store.manifest().get("layout") != 2 and bool(store.day_files())


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

    def refresh(self) -> bool:
        return False
