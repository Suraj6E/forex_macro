"""Currency strength as an indicator: the Analysis panel's numbers at every
minute, worked out in the background, saved, and sent with each candle so a
replay shows them as well as live.

A minute's value is what the Analysis panel would have shown at that
minute's close, from 1-minute Bid candles instead of ticks: each pair's
close against the open of its first candle of the FX day (17:00 New York),
carried forward within the day when a pair has no candle in a minute; USD,
EUR, GBP and JPY scored on the six matches (matches won, average move); and
gold, silver and the two oils on their own (their move since the day
opened). See analysis.py for the method.

A candle of any timeframe gets the value at its last complete minute. The
candle still forming gets the value at the last minute the clock has
completed, so nothing after the clock is ever sent.

Inputs. Every instrument's 1-minute candles, day by day (UTC), from
Dukascopy history or a recorded broker feed, whichever covers the day
later (Dukascopy when they are level). Prices differ a little between the
two, but a move in percent hardly does, so mixing sources across
instruments costs nothing that shows. An open day of a recorded feed is
read from its live ticks.

Saved in store/strength/, moves as whole numbers of 0.001 %:
  m1/YYYY-MM.parquet  a row per minute in which any instrument traded
  h1.parquet          the last row of every hour (1H, 4H and 1D candles)
  state.json          a fingerprint of the candles each month was built
                      from, so only months whose candles changed are
                      built again (new history days, today's minutes)

The first build covers every month with data, newest first, a few seconds
a month. After that only the changed months are built: today's every few
seconds while the market is live, older ones when downloads fill them in.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import numpy as np
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from simulator.analysis import CURRENCIES, ON_THEIR_OWN, PAIRS
from simulator.data.bars import bars_from_ticks
from simulator.data.ticks import NS_PER_SECOND
from simulator.instruments import INSTRUMENTS
from simulator.live.mt5rec import us_dst

#: Bumped when the calculation or the files change: every month is built again.
VERSION = 2
DAY = 86_400
HOUR = 3_600
MINUTE = 60
SYMBOLS = PAIRS + ON_THEIR_OWN
#: What each candle carries, in this order: the currencies' average move,
#: then gold, silver, WTI and Brent's move, all in percent.
KEYS = CURRENCIES + ON_THEIR_OWN
#: A source whose day ends at least this much later wins the day.
LATER_BY_S = 5 * MINUTE
#: How often changed months are looked for (today's minutes, new days).
#: Live, the forming candle's value is at most this much behind its minute.
UPDATE_SECONDS = 20.0
#: Pause between months of a long build, so it stays in the background.
BUILD_PAUSE_SECONDS = 0.05
#: Months of minute rows kept in memory for reading.
CACHED_MONTHS = 3
#: Saved moves are whole numbers of 1/SCALE percent in 16 bits (to
#: +-32.767 %), and EMPTY where there is none.
SCALE = 1000
EMPTY = -32768


def pack(rows: dict) -> dict[str, np.ndarray]:
    """The saved form of computed rows: what the chart shows, and smaller."""
    out = {"time": rows["time"].astype(np.int64)}
    for k in KEYS:
        v = rows[k].astype(np.float64)
        q = np.clip(np.round(np.nan_to_num(v, nan=0.0) * SCALE), -32767, 32767).astype(np.int16)
        q[np.isnan(v)] = EMPTY
        out[k] = q
    for c in CURRENCIES:
        out[c + "_won"] = rows[c + "_won"].astype(np.int8)
        out[c + "_played"] = rows[c + "_played"].astype(np.int8)
    return out

_EPOCH = date(1970, 1, 1)

#: Files are read with the system allocator. Arrow's default one (mimalloc)
#: keeps what it frees: a full build held 665 MB, against 140 MB this way.
_POOL = pa.system_memory_pool()


def _read(path: Path, columns: list[str] | None = None, a: int | None = None, b: int | None = None) -> pa.Table:
    """A Parquet file, or its rows with time in [a, b)."""
    where = None if a is None else (ds.field("time") >= a) & (ds.field("time") < b)
    return ds.dataset(path, format="parquet").to_table(columns=columns, filter=where, memory_pool=_POOL)


def fx_open_s(times_s) -> np.ndarray:
    """For each time (epoch seconds), when its FX day opened: the latest
    17:00 New York at or before it."""
    times_s = np.asarray(times_s, np.int64)
    if not len(times_s):
        return times_s
    day = times_s // DAY
    lo, hi = int(day.min()) - 1, int(day.max())
    opens = np.array([d * DAY + (21 if us_dst((d * DAY + 21 * HOUR) * 1000) else 22) * HOUR
                      for d in range(lo, hi + 1)], np.int64)
    today = opens[day - lo]
    return np.where(times_s >= today, today, opens[day - lo - 1])


def compute(bars: dict, a: int, b: int) -> dict[str, np.ndarray]:
    """Rows for the minutes in [a, b) (epoch seconds, minute starts).

    `bars` maps a symbol to its 1-minute candles (time, open, close), sorted,
    from at least the UTC day before `a`, so every FX day under [a, b) is
    there from its open."""
    per, grid_parts = {}, []
    for s in SYMBOLS:
        t, o, c = bars.get(s, (np.empty(0, np.int64), np.empty(0), np.empty(0)))
        if not len(t):
            continue
        fx = fx_open_s(t)
        starts = np.flatnonzero(np.r_[True, fx[1:] != fx[:-1]])
        day_open = np.repeat(o[starts], np.diff(np.r_[starts, len(t)]))
        per[s] = (t, fx, (c / day_open - 1.0) * 100.0)
        grid_parts.append(t[(t >= a) & (t < b)])
    grid = np.unique(np.concatenate(grid_parts)) if grid_parts else np.empty(0, np.int64)
    grid_fx = fx_open_s(grid)
    moves = {}
    for s in SYMBOLS:
        if s not in per or not len(grid):
            moves[s] = np.full(len(grid), np.nan)
            continue
        t, fx, chg = per[s]
        k = np.searchsorted(t, grid, side="right") - 1
        kk = np.maximum(k, 0)
        same_day = (k >= 0) & (fx[kk] == grid_fx)  # never yesterday's move
        moves[s] = np.where(same_day, chg[kk], np.nan)
    cols: dict[str, np.ndarray] = {"time": grid.astype(np.int64)}
    for cur in CURRENCIES:
        sides = []
        for p in PAIRS:
            inst = INSTRUMENTS[p]
            if inst.base == cur:
                sides.append(moves[p])
            elif inst.quote == cur:
                sides.append(-moves[p])
        m = np.vstack(sides) if len(grid) else np.empty((3, 0))
        played = (~np.isnan(m)).sum(axis=0)
        with np.errstate(invalid="ignore"):
            avg = np.where(played > 0, np.nansum(m, axis=0) / np.maximum(played, 1), np.nan)
        cols[cur] = avg.astype(np.float32)
        cols[cur + "_won"] = (m > 0).sum(axis=0).astype(np.int8)
        cols[cur + "_played"] = played.astype(np.int8)
    for s in SYMBOLS:
        cols[s] = moves[s].astype(np.float32)
    return cols


def _empty_rows() -> dict[str, np.ndarray]:
    return pack(compute({}, 0, 0))


def _concat(parts: list[dict]) -> dict[str, np.ndarray]:
    parts = [p for p in parts if len(p["time"])]
    if not parts:
        return _empty_rows()
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}


def _take(rows: dict, mask) -> dict:
    return {k: v[mask] for k, v in rows.items()}


# -- inputs ----------------------------------------------------------------------


class M1File:
    """A stored 1-minute candle file (`bars/m1.parquet`). Its day index and
    the last window read are kept until the file changes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._index: tuple = (None, {})
        self._window: tuple = (None, None)

    def _mtime(self):
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def days(self) -> dict[int, tuple]:
        """UTC day number -> (candles, ticks, last candle's start)."""
        mtime = self._mtime()
        if mtime is None:
            return {}
        if self._index[0] != mtime:
            t = _read(self.path, ["time", "ticks"])
            tm = t.column("time").to_numpy().astype(np.int64)
            ticks = t.column("ticks").to_numpy().astype(np.int64)
            index = {}
            if len(tm):
                day = tm // DAY
                starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
                ends = np.r_[starts[1:], len(tm)]
                n = np.add.reduceat(ticks, starts)
                index = {int(day[s]): (int(e - s), int(k), int(tm[e - 1])) for s, e, k in zip(starts, ends, n)}
            self._index = (mtime, index)
        return self._index[1]

    def read(self, a: int, b: int):
        key = (self._mtime(), a, b)
        if self._window[0] == key:
            return self._window[1]
        t = _read(self.path, ["time", "open", "close"], a, b)
        out = (t.column("time").to_numpy().astype(np.int64), t.column("open").to_numpy().astype(np.float64),
               t.column("close").to_numpy().astype(np.float64))
        self._window = (key, out)
        return out


class OpenDays:
    """A recorded feed's open days (still being written), as 1-minute
    candles from its live ticks."""

    def __init__(self, source) -> None:
        self.source = source
        self._ticks: dict[date, object] = {}  # what days() read, for read() just after
        self._bars: dict[date, tuple] = {}    # day -> (ticks, candles)

    def _open(self) -> list[date]:
        return [d for d in self.source.days()[-3:] if self.source.is_open(d)]

    def days(self) -> dict[int, tuple]:
        out, self._ticks = {}, {}
        for d in self._open():
            t = self._ticks[d] = self.source.load_day(d)
            if len(t):
                out[(d - _EPOCH).days] = (len(t), len(t), int(t.ts[-1] // NS_PER_SECOND))
        return out

    def read(self, a: int, b: int):
        parts = []
        for d in self._open():
            start = (d - _EPOCH).days * DAY
            if start + DAY <= a or start >= b:
                continue
            t = self._ticks.get(d) or self.source.load_day(d)
            cached = self._bars.get(d)
            if cached is None or cached[0] != len(t):
                cached = self._bars[d] = (len(t), bars_from_ticks(t.ts, t.bid, t.ask, MINUTE))
            bars = cached[1]
            keep = (bars["time"] >= a) & (bars["time"] < b)
            parts.append((bars["time"][keep], bars["open"][keep], bars["close"][keep]))
        if not parts:
            return np.empty(0, np.int64), np.empty(0), np.empty(0)
        return tuple(np.concatenate([p[i] for p in parts]) for i in range(3))


class Source:
    """One feed of an instrument: its parts, a later part winning a day
    (open days over the stored file)."""

    def __init__(self, key: str, parts: list) -> None:
        self.key, self.parts = key, parts

    def days(self) -> dict[int, tuple]:
        out = {}
        for i, part in enumerate(self.parts):
            for d, fp in part.days().items():
                out[d] = (i, fp)
        return out


def choose(sources: list[Source]) -> dict[int, tuple]:
    """UTC day -> (source, part index, fingerprint): the source whose day
    ends latest, the earlier listed when within LATER_BY_S."""
    best: dict[int, tuple] = {}
    for src in sources:
        for d, (i, fp) in src.days().items():
            cur = best.get(d)
            if cur is None or fp[2] > cur[2][2] + LATER_BY_S:
                best[d] = (src, i, fp)
    return best


def _month_key(day: int) -> str:
    return (_EPOCH + timedelta(days=day)).strftime("%Y-%m")


def _month_bounds(key: str) -> tuple[int, int]:
    """First UTC day of the month and of the next, as day numbers."""
    y, m = int(key[:4]), int(key[5:7])
    first = (date(y, m, 1) - _EPOCH).days
    nxt = (date(y + (m == 12), m % 12 + 1, 1) - _EPOCH).days
    return first, nxt


# -- the store -------------------------------------------------------------------


class StrengthStore:
    """Builds and keeps the rows, and reads them for candles."""

    def __init__(self, root: Path, sources_for: Callable[[str], list[Source]]) -> None:
        self.root = Path(root)
        self.m1_dir = self.root / "m1"
        self.sources_for = sources_for
        self.status: dict = {"state": "waiting", "message": "Waiting for the market data", "done": 0, "total": 0}
        self._lock = threading.RLock()
        self._months: OrderedDict[str, dict] = OrderedDict()
        self._h1: dict | None = None
        self._state: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Goes up whenever rows are saved, so sessions know to send again.
        self.version = 0

    # -- background ------------------------------------------------------------

    def start(self, ready: Callable[[], bool] = lambda: True) -> None:
        def run():
            while not self._stop.is_set():
                if ready():
                    try:
                        self.update()
                    except Exception as exc:  # shown in the page; tried again next round
                        self.status.update(state="error", message=f"{type(exc).__name__}: {exc}")
                self._stop.wait(UPDATE_SECONDS)
        self._thread = threading.Thread(target=run, name="strength", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _load_state(self) -> dict:
        if self._state is None:
            try:
                self._state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                self._state = {}
            if self._state.get("version") != VERSION:
                self._state = {"version": VERSION, "months": {}}
        return self._state

    def _save_state(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "state.tmp"
        tmp.write_text(json.dumps(self._state, indent=0), encoding="utf-8")
        tmp.replace(self.root / "state.json")

    def plan(self) -> tuple[dict[str, dict], list[tuple[str, str]]]:
        """Each symbol's chosen source per day, and the months whose inputs
        changed since they were built (newest first) with their fingerprints."""
        chosen = {s: choose(self.sources_for(s)) for s in SYMBOLS if s in INSTRUMENTS}
        days = sorted({d for c in chosen.values() for d in c})
        if not days:
            return chosen, []
        months = sorted({_month_key(d) for d in days}, reverse=True)
        built = self._load_state()["months"]
        todo = []
        for key in months:
            first, nxt = _month_bounds(key)
            h = hashlib.sha1(str(VERSION).encode())
            for s in sorted(chosen):
                for d in range(first - 1, nxt):
                    pick = chosen[s].get(d)
                    if pick is not None:
                        h.update(f"{s}|{d}|{pick[0].key}|{pick[1]}|{pick[2]}".encode())
            fp = h.hexdigest()
            if built.get(key) != fp:
                todo.append((key, fp))
        return chosen, todo

    def update(self) -> int:
        """Build every month whose inputs changed. Returns how many."""
        chosen, todo = self.plan()
        if not todo:
            last = self._last_row_time()
            self.status.update(state="ready", done=0, total=0, month=None,
                               message="Up to date", built_to_ms=last * 1000 if last else None)
            return 0
        for n, (key, fp) in enumerate(todo, 1):
            if self._stop.is_set():
                break
            self.status.update(state="building", done=n - 1, total=len(todo), month=key,
                               message=f"Working out {key}" + (f" ({n} of {len(todo)} months)" if len(todo) > 1 else ""))
            self.build_month(key, chosen)
            with self._lock:
                self._state["months"][key] = fp
                self._save_state()
            if len(todo) > 1:
                time.sleep(BUILD_PAUSE_SECONDS)
        last = self._last_row_time()
        self.status.update(state="ready", done=len(todo), total=len(todo), month=None, message="Up to date",
                           built_to_ms=last * 1000 if last else None)
        return len(todo)

    def build_month(self, key: str, chosen: dict[str, dict]) -> dict:
        first, nxt = _month_bounds(key)
        a, b = first * DAY, nxt * DAY
        bars = {}
        for s, picks in chosen.items():
            # The day before the month too, for the FX day that opened then.
            by_part: dict[tuple, set] = {}
            for d in range(first - 1, nxt):
                pick = picks.get(d)
                if pick is not None:
                    by_part.setdefault((id(pick[0]), pick[1]), (pick[0].parts[pick[1]], set()))[1].add(d)
            parts = []
            for part, wanted in by_part.values():
                t, o, c = part.read(a - DAY, b)
                keep = np.isin(t // DAY, np.fromiter(wanted, np.int64))
                parts.append((t[keep], o[keep], c[keep]))
            if parts:
                t = np.concatenate([p[0] for p in parts])
                order = np.argsort(t, kind="stable")
                bars[s] = (t[order], np.concatenate([p[1] for p in parts])[order],
                           np.concatenate([p[2] for p in parts])[order])
        rows = pack(compute(bars, a, b))
        self._write_month(key, rows)
        return rows

    # -- files -------------------------------------------------------------------

    def _write_month(self, key: str, rows: dict) -> None:
        self.m1_dir.mkdir(parents=True, exist_ok=True)
        path = self.m1_dir / f"{key}.parquet"
        if len(rows["time"]):
            tmp = path.with_suffix(".tmp")
            pq.write_table(pa.table(rows), tmp, compression="zstd")
            tmp.replace(path)
        elif path.exists():
            path.unlink()
        first, nxt = _month_bounds(key)
        hour = rows["time"] // HOUR
        last_of_hour = np.flatnonzero(np.r_[hour[1:] != hour[:-1], True]) if len(hour) else np.empty(0, np.int64)
        with self._lock:
            self.version += 1
            self._months[key] = rows
            self._months.move_to_end(key)
            while len(self._months) > CACHED_MONTHS:
                self._months.popitem(last=False)
            h1 = self._hourly()
            outside = (h1["time"] < first * DAY) | (h1["time"] >= nxt * DAY)
            joined = _concat([_take(h1, outside), _take(rows, last_of_hour)])
            order = np.argsort(joined["time"], kind="stable")
            self._h1 = {k: v[order] for k, v in joined.items()}
            tmp = self.root / "h1.tmp"
            pq.write_table(pa.table(self._h1), tmp, compression="zstd")
            tmp.replace(self.root / "h1.parquet")

    def _hourly(self) -> dict:
        with self._lock:
            if self._h1 is None:
                try:
                    t = _read(self.root / "h1.parquet")
                    self._h1 = {k: t.column(k).to_numpy() for k in t.column_names}
                except (FileNotFoundError, OSError, pa.ArrowInvalid):
                    self._h1 = _empty_rows()
            return self._h1

    def _month(self, key: str) -> dict:
        with self._lock:
            hit = self._months.get(key)
            if hit is not None:
                self._months.move_to_end(key)
                return hit
        try:
            t = _read(self.m1_dir / f"{key}.parquet")
            rows = {k: t.column(k).to_numpy() for k in t.column_names}
        except (FileNotFoundError, OSError, pa.ArrowInvalid):
            rows = _empty_rows()
        with self._lock:
            self._months[key] = rows
            while len(self._months) > CACHED_MONTHS:
                self._months.popitem(last=False)
        return rows

    def _last_row_time(self) -> int | None:
        h1 = self._hourly()
        return int(h1["time"][-1]) if len(h1["time"]) else None

    # -- reading -----------------------------------------------------------------

    def rows_before(self, cutoffs, lowers) -> list[tuple[dict, int] | None]:
        """For each (cutoff, lower) in epoch seconds: the last row whose
        minute closed by the cutoff (time + 60 <= cutoff) and started at or
        after `lower`, as (rows, index); None when there is none."""
        cutoffs = np.asarray(cutoffs, np.int64)
        lowers = np.asarray(lowers, np.int64)
        out: list = [None] * len(cutoffs)
        target = cutoffs - MINUTE
        on_hour = cutoffs % HOUR == 0
        groups: dict = {}
        for i in range(len(cutoffs)):
            if target[i] < lowers[i]:
                continue
            key = "h1" if on_hour[i] else _month_key(int(target[i] // DAY))
            groups.setdefault(key, []).append(i)
        for key, idx in groups.items():
            # The last row of the hour before an hour boundary is the last row
            # before it at all, so whole-hour cutoffs read the small table.
            rows = self._hourly() if key == "h1" else self._month(key)
            if not len(rows["time"]):
                continue
            idx = np.asarray(idx)
            k = np.searchsorted(rows["time"], target[idx], side="right") - 1
            for i, j in zip(idx, k):
                if j >= 0 and rows["time"][j] >= lowers[i]:
                    out[i] = (rows, int(j))
        return out

    def annotate(self, bars: list[dict], width_s: int, sim_ns: int) -> None:
        """Give each candle its strength: `s` the values of KEYS (percent to
        0.001, None when unknown) and `w` matches won and played per currency, at
        its last complete minute, or for the forming candle at the last
        minute the clock has completed."""
        if not bars:
            return
        sim_s = sim_ns // NS_PER_SECOND
        starts = np.fromiter((b["time"] for b in bars), np.int64, len(bars))
        found = self.rows_before(np.minimum(starts + width_s, sim_s), starts)
        for bar, hit in zip(bars, found):
            if hit is None:
                bar["s"] = None
                continue
            rows, j = hit
            bar["s"] = [None if rows[k][j] == EMPTY else round(int(rows[k][j]) / SCALE, 3) for k in KEYS]
            bar["w"] = [int(rows[c + f][j]) for c in CURRENCIES for f in ("_won", "_played")]
