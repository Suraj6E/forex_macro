"""The simulator's web server (guide step 3).

FastAPI serves the pages and one WebSocket per browser tab. The engine runs
here and the browser only draws. Each session is driven by its own clock
task, which advances time every 50 ms and sends at most one screen update
per frame (20 per second); the broker still sees every tick.

A session belongs to one tab at a time. Opening it in a second tab moves it
there and tells the first tab, so there is never a second copy advancing the
same account.

Every instrument has its own market data: replay history from Dukascopy
(plus the Layer 0 file for EUR/USD), kept current by a background download,
and live feeds recorded from an MT5 terminal, one per trade server. A live
session runs on a live feed with the wall clock. Economic calendar events
come read-only from the fxmacro database.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from simulator import config, instruments
from simulator.data import dukascopy
from simulator.data.bars import TIMEFRAMES, BarStore, tf_seconds
from simulator.data.calendar import Calendar
from simulator.data.ticks import (
    NS_PER_SECOND,
    FeedStore,
    ParquetTickSource,
    day_start_ns,
    import_layer0,
    migrate_feed,
    needs_migration,
    now_ns,
)
from simulator.engine.broker import UNSET, Broker, OrderError
from simulator.engine.clock import FRAME_SECONDS, SPEEDS
from simulator.engine.session import ReplaySession, SessionError, random_start_ns
from simulator.instruments import CONVERSION, INSTRUMENTS, Instrument
from simulator.journal import stats
from simulator.journal.store import Journal
from simulator.live import mt5rec
from simulator.live.feed import FeedBars, LiveFeedSource
from simulator.live.winproc import installed_terminals

SAVE_EVERY_SECONDS = 5.0
MAX_SNAPSHOT_BYTES = 6_000_000
REPLAY_FEED = "dukascopy"
#: Days fetched per instrument before moving to the next one. Downloads go
#: round the instruments, newest days first, so after one round every
#: instrument has its latest weeks, and older history fills in behind.
UPDATE_CHUNK_DAYS = 15
#: Candles for downloaded days are built at least this often, so a slow
#: download shows its days without waiting for the whole chunk.
CANDLES_EVERY_SECONDS = 60.0
#: A live session sends a frame as soon as the recorder has new ticks, and
#: at least this often without (the clock moves on).
LIVE_IDLE_SECONDS = 0.1
#: The quote board gets at most one message per this many seconds.
QUOTES_MIN_SECONDS = 0.02
#: When Dukascopy stops answering altogether, the update tries again after this long.
RETRY_UPDATE_SECONDS = 30 * 60.0
#: Seconds per hour file at the datafeed's pace, for the time estimate.
SECONDS_PER_HOUR_FILE = 1.05
#: Bytes per stored tick in the compact layout, for estimates before an
#: instrument has any data of its own.
BYTES_PER_DAY_GUESS = 250_000
#: How far ahead the chart is told about scheduled events.
CALENDAR_AHEAD_NS = 3 * 86_400 * NS_PER_SECOND


class Feed:
    """A source of ticks and its candles. `bars` may be a function, so
    listing feeds never loads candles into memory."""

    def __init__(self, key: str, label: str, source, bars, live: bool) -> None:
        self.key, self.label, self.source, self._bars, self.live = key, label, source, bars, live

    @property
    def bars(self):
        return self._bars() if callable(self._bars) else self._bars


class Market:
    """One instrument's replay history: its ticks and its candles.

    Candles are loaded into memory only while something uses them (a
    session on this instrument, or one that converts its profit through
    it): nine instruments over years would otherwise hold close to a
    gigabyte. Downloads update the stored candles either way.
    """

    def __init__(self, inst: Instrument) -> None:
        self.inst = inst
        self.dir = config.duka_dir(inst.symbol)
        self.bars_dir = self.dir / "bars"
        self.source = ParquetTickSource(self.dir)
        self._bars: BarStore | None = None
        self.has_bars = False
        self.lock = threading.RLock()
        self._size: tuple[float, int] = (0.0, 0)

    def load(self) -> bool:
        """Whether stored candles match the ticks' flags. True when the
        market can be replayed."""
        self.source.refresh()
        self.has_bars = BarStore.exists(self.bars_dir, config.KEEP_FLAGS)
        return self.ready

    @property
    def bars(self) -> BarStore | None:
        with self.lock:
            if self._bars is None and self.has_bars:
                self._bars = BarStore.load(self.bars_dir)
            return self._bars

    def release(self) -> None:
        with self.lock:
            self._bars = None

    @property
    def ready(self) -> bool:
        return self.has_bars and self.source.ready

    @property
    def needs_build(self) -> bool:
        return self.source.ready and not self.has_bars

    def stale_days(self) -> list[date]:
        """Days whose stored candles do not hold exactly the day's kept
        ticks: a download wrote more of a day after its candles were built
        (stopped between the two, or an older version of this code). The
        1-minute candles count their ticks, and the day index counts each
        day's ticks by quality flag, so the check reads no tick file."""
        if not self.has_bars:
            return []
        entries = self.source.store.manifest().get("days", {})
        t = pq.read_table(self.bars_dir / "m1.parquet", columns=["time", "ticks"])
        day_idx = t.column("time").to_numpy() // 86_400
        uniq, inverse = np.unique(day_idx, return_inverse=True)
        counted = dict(zip(uniq.tolist(), np.bincount(inverse, weights=t.column("ticks").to_numpy()).astype(np.int64).tolist()))
        out = set()
        for d, e in entries.items():
            flags = e.get("flags") or []
            kept = sum(flags[f] for f in config.KEEP_FLAGS if f < len(flags))
            idx = (date.fromisoformat(d) - date(1970, 1, 1)).days
            if counted.pop(idx, 0) != kept:
                out.add(date.fromisoformat(d))
        out |= {date(1970, 1, 1) + timedelta(days=int(k)) for k, n in counted.items() if n}
        return sorted(out)

    def build(self, progress=None) -> None:
        bars = BarStore.build(self.source, progress=progress)
        bars.save(self.bars_dir, config.KEEP_FLAGS)
        with self.lock:
            self.has_bars = True
            self._bars = None

    def add_days(self, days: list[date]) -> None:
        """New or changed days: candles for exactly those, then saved. A
        store in use is updated in place, so open sessions see the days."""
        with self.lock:
            self.source.refresh()
            bars = self._bars or (BarStore.load(self.bars_dir) if self.has_bars else BarStore.empty())
            bars.replace_days(self.source, days)
            bars.save(self.bars_dir, config.KEEP_FLAGS)
            self.has_bars = True

    def size_bytes(self) -> int:
        at, size = self._size
        if time.monotonic() - at > 60:
            size = self.source.store.size_bytes()
            self._size = (time.monotonic(), size)
        return size

    def info(self, settings: dict, now: int) -> dict:
        days = self.source.days()
        start = date.fromisoformat(settings["from"])
        pending = dukascopy.pending_days(self.dir, start_day=start, now=now) if settings["enabled"] else []
        size = self.size_bytes()
        per_day = size / len(days) if len(days) >= 20 else BYTES_PER_DAY_GUESS
        # The last tick from the day index, not the file: the newest day is
        # the one a download may be rewriting at this very moment.
        entry = self.source._entries.get(days[-1].isoformat(), {}) if days else {}
        last_ns = entry.get("last_ns")
        return {
            **self.inst.to_json(),
            "enabled": settings["enabled"],
            "from": settings["from"],
            "ready": self.ready,
            "first_day": days[0].isoformat() if days else None,
            "last_day": days[-1].isoformat() if days else None,
            "days": len(days),
            "last_tick_ms": last_ns // 1_000_000 if last_ns else None,
            "size_bytes": size,
            "pending_days": len(pending),
            "pending_bytes": int(len(pending) * per_day),
            "pending_seconds": int(len(pending) * 23 * SECONDS_PER_HOUR_FILE),
        }


class Hub:
    """Market data, the background jobs that keep it current, the recorder,
    the calendar, and the sessions in memory.

    Nothing here needs a command. On start the server prepares what is
    missing (imports the Layer 0 file, rewrites old tick files into the
    compact layout, builds candles), then downloads every instrument in the
    background, newest days first, rebuilding candles as days arrive. The
    recorder starts again if it was on last time. The page shows all of it.
    """

    def __init__(self) -> None:
        self.journal = Journal()
        self.markets: dict[str, Market] = {s: Market(i) for s, i in INSTRUMENTS.items()}
        self.calendar = Calendar()
        self.problem: str | None = None
        self.live: dict[str, Live] = {}
        self._mt5_feeds: dict[str, Feed] = {}
        self.job: dict = {"kind": None, "running": False}
        self._job_thread: threading.Thread | None = None
        self._job_stop = threading.Event()
        self._retry_token = None  # a pending retry after Dukascopy stopped answering
        self.recorder: mt5rec.MT5Recorder | None = None
        #: Called (from the recorder's thread) with an instrument's symbol
        #: whenever new live ticks of it are on disk: live sessions and quote
        #: boards wake on it instead of waiting for their next frame.
        self.tick_listeners: set = set()
        self.prepared = False

    # -- replay history --------------------------------------------------------

    @property
    def ready(self) -> bool:
        """At least one instrument can be replayed or watched live."""
        return self.prepared and (any(m.ready for m in self.markets.values())
                                  or any(self.feeds(symbol) for symbol in INSTRUMENTS))

    def market(self, symbol: str | None) -> Market:
        return self.markets[instruments.get(symbol).symbol]

    def _start_job(self, kind: str, target) -> bool:
        if self._job_thread and self._job_thread.is_alive():
            return False
        self._job_stop.clear()
        self.job = {"kind": kind, "running": True, "started_ms": int(time.time() * 1000),
                    "done": 0, "total": 0, "message": "Starting"}
        self._job_thread = threading.Thread(target=self._guard(target), name=kind, daemon=True)
        self._job_thread.start()
        return True

    def _guard(self, target):
        def run():
            try:
                target()
            except Exception as exc:
                self.job.update(running=False, error=f"{type(exc).__name__}: {exc}",
                                message=f"Failed: {exc}", finished_ms=int(time.time() * 1000))
        return run

    def start(self) -> None:
        """On server start: load what is there, prepare what is missing in the
        background, then download (if enabled)."""
        for m in self.markets.values():
            m.load()
        eur = self.markets[config.SYMBOL]
        layer0_pending = not FeedStore(eur.dir).manifest().get("layer0") and config.TICK_FILE.exists()
        migrations = self._migrations()
        builds = [m for m in self.markets.values() if m.needs_build]
        stale = [m for m in self.markets.values() if m.stale_days()]
        if not (layer0_pending or migrations or builds or stale):
            self.prepared = True
            if config.AUTO_UPDATE:
                self.start_update()
            return
        self._start_job("prepare", self._prepare)

    def _migrations(self) -> list[tuple[Path, int | None]]:
        out = [(m.dir, m.inst.scale) for m in self.markets.values() if needs_migration(m.dir)]
        for feed_dir in sorted(config.MT5_DIR.glob("*/*")):
            if feed_dir.is_dir() and needs_migration(feed_dir):
                out.append((feed_dir, None))
        return out

    def _prepare(self) -> None:
        job = self.job
        eur = self.markets[config.SYMBOL]
        if not FeedStore(eur.dir).manifest().get("layer0") and config.TICK_FILE.exists():
            groups = pq.ParquetFile(config.TICK_FILE).metadata.num_row_groups
            job.update(total=groups, message="Importing the 2023-2024 EUR/USD tick file")

            def on_group(msg: str) -> None:
                job["done"] = min(job["done"] + 1, groups)
                job["message"] = f"Importing the 2023-2024 EUR/USD tick file ({job['done']}/{groups})"
            import_layer0(progress=on_group)
            eur.source.refresh()
            eur.bars = None  # rebuilt below from the imported days
        for feed_dir, scale in self._migrations():
            def on_file(n: int, total: int, name=feed_dir.name) -> None:
                job.update(done=n, total=total, message=f"Compacting {name} tick files ({n}/{total})")
            migrate_feed(feed_dir, scale, progress=on_file, stop=self._job_stop)
        for m in self.markets.values():
            m.source.refresh()
            stale = m.stale_days()
            if stale:
                job.update(done=0, total=len(stale), message=f"Updating {m.inst.label} candles for {len(stale)} changed days")
                m.add_days(stale)
            if m.needs_build:
                days = len(m.source.days())

                def on_days(msg: str, m=m, days=days) -> None:
                    n = int(msg.split(":")[1].split("/")[0])
                    job.update(done=n, total=days, message=f"Building {m.inst.label} candles ({n}/{days} days)")
                m.build(progress=on_days)
        self.prepared = True
        job.update(running=False, finished_ms=int(time.time() * 1000), message="Market data ready")
        if config.AUTO_UPDATE and not self._job_stop.is_set():
            self._job_thread = None
            self.start_update()

    def start_update(self) -> bool:
        if not self.prepared:
            return False
        return self._start_job("update", self._update)

    def _retry_later(self, seconds: float) -> None:
        """Start the update again after a while, unless something else runs."""
        token = self._retry_token = object()

        def retry():
            if self._retry_token is token:  # not stopped from the page since
                self.start_update()
        timer = threading.Timer(seconds, retry)
        timer.daemon = True
        timer.start()

    def stop_job(self) -> None:
        self._retry_token = None
        self._job_stop.set()

    # -- downloads ---------------------------------------------------------------

    def data_settings(self) -> dict[str, dict]:
        try:
            saved = json.loads(config.DATA_SETTINGS.read_text(encoding="utf-8")).get("instruments", {})
        except (FileNotFoundError, ValueError):
            saved = {}
        out = {}
        for symbol in INSTRUMENTS:
            s = saved.get(symbol, {})
            out[symbol] = {"enabled": bool(s.get("enabled", True)), "from": s.get("from") or config.DEFAULT_FROM}
        return out

    def save_data_settings(self, symbol: str, enabled: bool | None = None, start: str | None = None) -> dict:
        settings = self.data_settings()
        s = settings[instruments.get(symbol).symbol]
        if enabled is not None:
            s["enabled"] = bool(enabled)
        if start:
            day = date.fromisoformat(start)
            if day < date(2003, 1, 1) or day > date.today():
                raise ValueError("Pick a start day between 2003 and today")
            s["from"] = day.isoformat()
        config.DATA_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        config.DATA_SETTINGS.write_text(json.dumps({"instruments": settings}, indent=1), encoding="utf-8")
        return settings

    @staticmethod
    def disk() -> dict:
        usage = shutil.disk_usage(config.STORE_DIR if config.STORE_DIR.exists() else config.SIM_DIR)
        return {"free_bytes": usage.free, "total_bytes": usage.total, "min_free_bytes": config.MIN_FREE_BYTES}

    def _update(self) -> None:
        """Round the enabled instruments, newest missing days first, a chunk
        of days each, until nothing is missing. Settings are re-read every
        round, so a start day changed in the page applies straight away. A
        day that fails is left for the next run rather than retried here."""
        job = self.job
        job.update(message="Checking Dukascopy for new hours", days=0, ticks=0, failed_hours=0, instruments={})
        fetcher = dukascopy.Fetcher(
            stop=self._job_stop,
            on_wait=lambda s: job.update(message=f"Dukascopy is not answering; pausing {s:.0f} s, then trying again"),
        )
        attempted: dict[str, set[date]] = {s: set() for s in INSTRUMENTS}
        done_days = 0
        stopped_for = None
        while not self._job_stop.is_set() and stopped_for is None:
            settings = self.data_settings()
            now = now_ns()
            queues = {}
            for symbol, m in self.markets.items():
                if not settings[symbol]["enabled"]:
                    continue
                start = date.fromisoformat(settings[symbol]["from"])
                pending = [d for d in dukascopy.pending_days(m.dir, start_day=start, now=now) if d not in attempted[symbol]]
                if pending:
                    queues[symbol] = pending
            job["instruments"] = {s: {"pending": len(q)} for s, q in queues.items()}
            job["total"] = done_days + sum(len(q) for q in queues.values())
            if not queues:
                break
            for symbol, pending in queues.items():
                if self._job_stop.is_set():
                    break
                if self.disk()["free_bytes"] < config.MIN_FREE_BYTES:
                    stopped_for = (f"Stopped: less than {config.MIN_FREE_BYTES / 1024**3:.0f} GB free on the "
                                   f"store's disk. Free some space, or start later days in Market data.")
                    break
                m = self.markets[symbol]
                chunk = pending[-UPDATE_CHUNK_DAYS:]
                attempted[symbol].update(chunk)

                def progress(day, done, total, m=m, base=done_days):
                    job.update(done=min(base + done, job["total"]), day=day.isoformat(), symbol=m.inst.symbol,
                               message=f"Fetching {m.inst.label} {day.isoformat()} from Dukascopy")

                built: set[date] = set()
                waiting: list[date] = []
                last_build = [time.monotonic()]

                def on_day(day, m=m, built=built, waiting=waiting, last_build=last_build):
                    waiting.append(day)
                    if time.monotonic() - last_build[0] >= CANDLES_EVERY_SECONDS:
                        m.add_days(list(waiting))
                        built.update(waiting)
                        waiting.clear()
                        last_build[0] = time.monotonic()
                summary = dukascopy.update(m.dir, m.inst.duka, days=chunk, scale=m.inst.scale, now=now,
                                           progress=progress, stop=self._job_stop, fetch=fetcher, on_day=on_day)
                summary["changed"] = [d for d in summary["changed"] if d not in built]
                done_days += len(chunk)
                job["done"] = min(done_days, job["total"])
                job["days"] += summary["days"]
                job["ticks"] += summary["ticks"]
                job["failed_hours"] += summary["failed_hours"]
                job["instruments"][symbol] = {"pending": len(pending) - len(chunk)}
                if summary["changed"]:
                    job["message"] = f"Building {m.inst.label} candles for the new days"
                    m.add_days(summary["changed"])
                    self.trim_memory()
                if summary["unreachable"]:
                    at = datetime.now().astimezone() + timedelta(seconds=RETRY_UPDATE_SECONDS)
                    stopped_for = (f"Dukascopy has not answered for {dukascopy.GIVE_UP_SECONDS / 60:.0f} minutes: "
                                   f"it is down, or refusing this connection for a while after a lot of "
                                   f"downloading. Trying again at {at:%H:%M}.")
                    self._retry_later(RETRY_UPDATE_SECONDS)
                    break
                if summary["stopped"]:
                    break
        gaps = f"; {job['failed_hours']} hours did not answer and are retried next time" if job["failed_hours"] else ""
        if stopped_for:
            message = stopped_for
        elif self._job_stop.is_set():
            message = "Stopped; it resumes next time"
        else:
            message = f"Up to date: {job['days']} days fetched, {job['ticks']:,} new ticks{gaps}"
        job.update(running=False, done=job.get("total", 0), finished_ms=int(time.time() * 1000), message=message,
                   error=stopped_for)

    # -- feeds -------------------------------------------------------------------

    def feeds(self, symbol: str) -> dict[str, Feed]:
        inst = instruments.get(symbol)
        out = {}
        m = self.markets[inst.symbol]
        if m.ready:
            out[REPLAY_FEED] = Feed(REPLAY_FEED, "Dukascopy history", m.source, lambda m=m: m.bars, live=False)
        for feed_dir in sorted(config.MT5_DIR.glob(f"*/{inst.symbol}")):
            key = f"mt5:{feed_dir.parent.name}"
            cache_key = f"{key}/{inst.symbol}"
            if cache_key not in self._mt5_feeds:
                if not ((feed_dir / "manifest.json").exists() or any((feed_dir / "live").glob("*.csv"))):
                    continue
                src = LiveFeedSource(feed_dir)
                manifest = src.store.manifest()
                label = f"MT5 {manifest.get('server', feed_dir.parent.name)}"
                self._mt5_feeds[cache_key] = Feed(key, label, src, FeedBars(src), live=True)
            out[key] = self._mt5_feeds[cache_key]
        return out

    def feed(self, key: str | None, symbol: str | None) -> Feed:
        inst = instruments.get(symbol)
        feeds = self.feeds(inst.symbol)
        key = key or REPLAY_FEED
        if key not in feeds:
            if key == REPLAY_FEED:
                raise SessionError(f"{inst.label} has no history yet: it is downloading (see Market data)")
            raise SessionError(f"No {inst.label} data from {key} yet")
        if not feeds[key].source.days():
            raise SessionError(f"{feeds[key].label} has no {inst.label} ticks recorded yet")
        return feeds[key]

    def rate_fn(self, inst: Instrument):
        """USD per unit of the instrument's quote currency at a time, from
        the converting instrument's last complete minute. Before that
        instrument's first candle, its first candle: an approximation of
        the USD value only, never of a price on the chart. None when no
        conversion is needed."""
        if inst.quote == "USD" or inst.base == "USD":
            return None
        conv_symbol, invert = CONVERSION[inst.quote]
        market = self.markets[conv_symbol]

        def rate(ns: int) -> float | None:
            bars = market.bars
            if bars is None or not len(bars["m1"]):
                return None
            m1 = bars["m1"]
            i = max(m1.completed_count(ns) - 1, 0)
            close = float(m1.arrays["close"][i])
            return 1.0 / close if invert else close
        return rate

    # -- live recording ------------------------------------------------------------

    def live_settings(self) -> dict:
        try:
            return json.loads(config.LIVE_SETTINGS.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def save_live_settings(self, settings: dict) -> None:
        config.LIVE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        config.LIVE_SETTINGS.write_text(json.dumps(settings, indent=1), encoding="utf-8")

    def in_use(self) -> set[str]:
        """Instruments whose candles open sessions need, with the ones
        their profit is converted through."""
        out = set()
        for live in self.live.values():
            inst = instruments.get(live.meta.get("symbol"))
            out.add(inst.symbol)
            if inst.quote in CONVERSION and inst.base != "USD":
                out.add(CONVERSION[inst.quote][0])
        return out

    def trim_memory(self) -> None:
        keep = self.in_use()
        for symbol, m in self.markets.items():
            if symbol not in keep:
                m.release()

    def history_from(self, symbol: str) -> date | None:
        """Where the recorder's broker history starts for an instrument:
        the same start day as its downloads, or none when it is switched off."""
        s = self.data_settings().get(symbol)
        return date.fromisoformat(s["from"]) if s and s["enabled"] else None

    def make_recorder(self, terminal: str) -> mt5rec.MT5Recorder:
        return mt5rec.MT5Recorder(terminal, history_from=self.history_from, on_ticks=self._on_ticks)

    def streaming_feed(self, symbol: str) -> Feed | None:
        """The live feed the recorder is streaming this instrument into now."""
        rec = self.recorder
        if rec is None or rec.status.get("state") != "recording" or rec.track(symbol) is None:
            return None
        return self.feeds(symbol).get(f"mt5:{mt5rec.slug(rec.status.get('server') or '')}")

    def live_feed(self, symbol: str) -> Feed | None:
        """The streaming feed, else the recorded feed with the newest tick."""
        feed = self.streaming_feed(symbol)
        if feed is not None:
            return feed
        recorded = [f for f in self.feeds(symbol).values() if f.live and f.source.days()]
        return max(recorded, key=lambda f: f.source.latest_ns() or 0, default=None)

    def replay_feed(self, symbol: str) -> Feed | None:
        """Dukascopy history when there is some, else a recorded feed."""
        feeds = self.feeds(symbol)
        if REPLAY_FEED in feeds and feeds[REPLAY_FEED].source.days():
            return feeds[REPLAY_FEED]
        return self.live_feed(symbol)

    def _on_ticks(self, symbol: str) -> None:
        for listener in list(self.tick_listeners):
            listener(symbol)

    def quotes(self) -> dict:
        """The quote board: each recorded instrument's latest tick."""
        rec = self.recorder
        status = rec.status if rec is not None else {}
        return {
            "type": "quotes",
            "now_ms": int(time.time() * 1000),
            "state": status.get("state", "stopped"),
            "server": status.get("server"),
            "quotes": rec.quotes() if rec is not None else [],
            "live": status.get("live"),
        }

    def start_recorder(self, terminal: str) -> None:
        if self.recorder is not None:
            self.recorder.stop()
        self.recorder = self.make_recorder(terminal)
        self.recorder.start()
        self.save_live_settings({"terminal": terminal, "autostart": True})

    def stop_recorder(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()
        self.recorder = None
        settings = self.live_settings()
        settings["autostart"] = False
        self.save_live_settings(settings)


hub = Hub()


def _quiet_resets(loop, context) -> None:
    """Windows reports every browser tab closed mid-request as a
    ConnectionResetError from asyncio's proactor. It is harmless noise."""
    if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)


async def _keep_recorded_candles_current() -> None:
    """Recorded feeds gain days while the recorder fetches broker history.
    Their stored candles are brought up to date here, in a worker thread,
    so the first session on a feed with years of history does not build
    them inside a request."""
    while True:
        await asyncio.sleep(30)
        if not hub.prepared:
            continue
        for symbol in INSTRUMENTS:
            for feed in list(hub.feeds(symbol).values()):
                if feed.live:
                    try:
                        await asyncio.to_thread(feed.bars._sync_closed)
                    except Exception:
                        pass  # the next pass, or the session itself, tries again


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.get_running_loop().set_exception_handler(_quiet_resets)
    maintenance = asyncio.create_task(_keep_recorded_candles_current())
    hub.start()
    settings = hub.live_settings()
    if settings.get("autostart") and settings.get("terminal"):
        # Waits harmlessly until the trader opens that terminal.
        hub.recorder = hub.make_recorder(settings["terminal"])
        hub.recorder.start()
    yield
    maintenance.cancel()
    for live in list(hub.live.values()):
        live.save()
    hub.stop_job()
    if hub.recorder is not None:
        hub.recorder.stop(wait=3)


app = FastAPI(title="FX Replay Simulator", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


@app.middleware("http")
async def revalidate_static(request, call_next):
    # Local tool, local files: always revalidate, so an updated app.js is
    # never shadowed by a cached one. A 304 costs nothing on localhost.
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# -- live sessions -------------------------------------------------------------


class Live:
    """A session in memory, the tab it is attached to, and its clock task."""

    def __init__(self, session: ReplaySession, meta: dict) -> None:
        self.session = session
        self.meta = meta  # the journal's session row, minus state
        self.ws: WebSocket | None = None
        self.task: asyncio.Task | None = None
        self.last_save = time.monotonic()
        self.wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _poke(self, symbol: str) -> None:
        """New ticks recorded (called from the recorder's thread)."""
        if symbol == self.meta.get("symbol") and self._loop is not None and self.wake is not None:
            self._loop.call_soon_threadsafe(self.wake.set)

    @property
    def id(self) -> str:
        return self.session.id

    def save(self) -> None:
        hub.journal.save_state(self.id, self.session.to_state())
        self.last_save = time.monotonic()

    async def send(self, message: dict) -> None:
        if self.ws is not None:
            try:
                await self.ws.send_json(message)
            except Exception:  # the tab went away mid-send
                self.ws = None

    def book(self) -> dict:
        b = self.session.broker
        positions = []
        for p in b.positions:
            mark = b.bid if p.side == "buy" else b.ask
            gross = b._gross(p.side, p.entry_price, mark, p.lots) if mark is not None else 0.0
            positions.append({
                "id": p.id, "side": p.side, "lots": p.lots, "entry_price": p.entry_price,
                "entry_ms": p.entry_ns // 1_000_000, "sl": p.sl, "tp": p.tp,
                "closing": p.close_requested_ns is not None,
                "pnl": round(gross - p.commission, 2),
                "pips": round((mark - p.entry_price if p.side == "buy" else p.entry_price - mark) / b.pip, 1)
                if mark is not None else 0.0,
            })
        pending = [{
            "id": o.id, "side": o.side, "type": o.type, "lots": o.lots, "price": o.price,
            "sl": o.sl, "tp": o.tp, "placed_ms": o.placed_ns // 1_000_000,
        } for o in b.pending]
        return {"positions": positions, "pending": pending, "account": b.account()}

    async def send_history(self) -> None:
        s = self.session
        await self.send({
            "type": "history",
            "status": s.status(),
            "bars": s.history(),
            "quote": s.quote(),
            "book": self.book(),
            "drawings": hub.journal.drawings(self.id, visible_at_ns=s.sim_ns),
        })

    async def send_frame(self) -> None:
        s = self.session
        bars = s.frame_bars()
        if bars is None:
            await self.send_history()
        else:
            await self.send({
                "type": "frame",
                "status": s.status(),
                "bars": bars,
                "quote": s.quote(),
                "book": self.book(),
            })
        await self.flush_events()

    async def flush_events(self) -> None:
        events = self.session.broker.drain_events()
        if not events:
            return
        for e in events:
            if "order" in e:
                hub.journal.upsert_order(self.id, e["order"])
            if e["kind"] == "closed":
                hub.journal.insert_trade(self.id, e["trade"])
        self.save()
        await self.send({"type": "events", "events": [_event_for_screen(e) for e in events], "book": self.book()})

    async def clock_loop(self) -> None:
        last = time.perf_counter()
        while True:
            if self.session.live:
                # Straight away when the recorder has new ticks.
                try:
                    await asyncio.wait_for(self.wake.wait(), LIVE_IDLE_SECONDS)
                except asyncio.TimeoutError:
                    pass
                self.wake.clear()
            else:
                await asyncio.sleep(FRAME_SECONDS)
            now = time.perf_counter()
            dt, last = now - last, now
            s = self.session
            if s.live:
                # The clock is the wall clock. A tick stamped a moment ahead
                # of this machine's clock still exists, so it is shown.
                latest = s.source.latest_ns() or 0
                s.advance(max(now_ns(), latest))
                if self.ws is not None:
                    await self.send_frame()
                if now - self.last_save > SAVE_EVERY_SECONDS:
                    self.save()
                continue
            if not s.clock.playing or self.ws is None:
                continue
            s.advance(s.sim_ns + s.clock.market_ns(dt))
            if s.finished and s.refresh():  # new data arrived: play on
                s.clock.playing = True
            await self.send_frame()
            if s.finished:
                s.clock.playing = False
                await self.send({"type": "status", "status": s.status(),
                                 "message": "End of the data. Newer hours appear here as the update fetches them."})
                self.save()
            elif now - self.last_save > SAVE_EVERY_SECONDS:
                self.save()

    def start_clock(self) -> None:
        if self.task is None or self.task.done():
            self._loop = asyncio.get_running_loop()
            self.wake = asyncio.Event()
            hub.tick_listeners.add(self._poke)
            self.task = asyncio.create_task(self.clock_loop())

    def stop_clock(self) -> None:
        hub.tick_listeners.discard(self._poke)
        if self.task is not None:
            self.task.cancel()
            self.task = None


def _event_for_screen(e: dict) -> dict:
    out = {"kind": e["kind"]}
    if "order" in e:
        o = e["order"]
        out["order"] = {k: o[k] for k in ("id", "side", "type", "lots", "price", "sl", "tp", "status", "fill_price", "position_id", "reason")}
    if "position" in e:
        out["position_id"] = e["position"]["id"]
    if "trade" in e:
        t = dict(e["trade"])
        t["entry_ms"] = t.pop("entry_ns") // 1_000_000
        t["exit_ms"] = t.pop("exit_ns") // 1_000_000
        out["trade"] = t
    return out


def _restore(sid: str) -> Live | None:
    if sid in hub.live:
        return hub.live[sid]
    row = hub.journal.get_session(sid)
    if row is None or row["status"] != "active":
        return None
    inst = instruments.get(row.get("symbol"))
    feed = hub.feed(row.get("feed") or REPLAY_FEED, inst.symbol)
    is_live = row.get("mode") == "live"
    session = ReplaySession.from_state(feed.source, feed.bars, row["state"], sid,
                                       live=is_live, now=now_ns() if is_live else None, rate_fn=hub.rate_fn(inst))
    meta = {k: v for k, v in row.items() if k != "state"}
    live = Live(session, meta)
    hub.live[sid] = live
    return live


async def _attach(live: Live, ws: WebSocket) -> None:
    if live.ws is not None and live.ws is not ws:
        old = live.ws
        live.ws = None
        try:
            await old.send_json({"type": "taken_over", "message": "This session was opened in another tab."})
            await old.close()
        except Exception:
            pass
    live.ws = ws
    if not live.session.live:
        live.session.clock.playing = False
        live.session.refresh()
    live.start_clock()
    await live.send({"type": "session", "session": _session_info(live)})
    await live.send_history()


def _unused(sid: str) -> bool:
    """A session the main screen opened in which nothing was done."""
    row = hub.journal.get_session(sid)
    return (row is not None and row.get("origin") == "screen" and not row.get("name")
            and row.get("status") == "active" and not hub.journal.has_records(sid))


def _discard(sid: str) -> None:
    live = hub.live.pop(sid, None)
    if live is not None:
        live.stop_clock()
    hub.journal.delete_session(sid)


def _leave(live: Live | None, ws: WebSocket, going_to: Live | None) -> None:
    """The tab moves from `live` to another session. One the screen opened
    and nothing was done in is deleted, so switching symbols and modes does
    not fill the journal with empty sessions."""
    if live is None or live is going_to or live.ws is not ws:
        return
    live.ws = None
    live.stop_clock()
    live.save()
    if _unused(live.id):
        _discard(live.id)


def _latest_start_ns(feed: Feed) -> int:
    """Where "the latest date" starts: 00:00 UTC of the last weekday with
    ticks, so there is a day to play."""
    days = feed.source.days()
    weekdays = [d for d in days[-4:] if d.weekday() < 5]
    return day_start_ns(weekdays[-1] if weekdays else days[-1])


def _last_tick_ns(feed: Feed) -> int:
    src = feed.source
    if hasattr(src, "latest_ns") and src.latest_ns():
        return src.latest_ns()
    days = src.days()
    entry = getattr(src, "_entries", {}).get(days[-1].isoformat(), {}) if days else {}
    return entry.get("last_ns") or (day_start_ns(days[-1]) + 86_400 * NS_PER_SECOND if days else 0)


def _usable_symbol(symbol: str | None) -> Instrument:
    """The instrument asked for, or the first with any data."""
    wanted = instruments.get(symbol) if symbol in INSTRUMENTS else instruments.get(config.SYMBOL)
    for inst in [wanted, *INSTRUMENTS.values()]:
        if hub.replay_feed(inst.symbol) is not None:
            return inst
    raise SessionError("No market data yet: it is downloading (see Market data)")


async def _open(ws: WebSocket, live: Live | None, msg: dict) -> Live:
    """The main screen: show an instrument live, or replay it.

    Without a mode: live when the recorder is streaming the instrument now,
    else a replay at the latest date. Live resumes the instrument's live
    session (one per feed). Replay starts at `at`, else at the time the tab
    was replaying (switching instruments keeps the date) when that
    instrument has data then, else at the latest date; but an instrument
    with a replay holding open positions or orders resumes that one."""
    inst = _usable_symbol(msg.get("symbol") or (live.meta.get("symbol") if live is not None else None))
    mode = msg.get("mode") if msg.get("mode") in ("live", "replay") else None
    if mode is None:
        mode = "live" if hub.streaming_feed(inst.symbol) is not None else "replay"
    tf = msg.get("timeframe") or (live.session.timeframe if live is not None else config.DEFAULT_TIMEFRAME)
    tf_seconds(tf)
    sessions = [r for r in hub.journal.list_sessions()
                if r["status"] == "active" and r["symbol"] == inst.symbol and r["mode"] == mode]

    target = None
    if mode == "live":
        feed = hub.live_feed(inst.symbol)
        if feed is None:
            raise SessionError(f"No live {inst.label} yet: open your MT5 terminal, then press Record in Market data")
        target = next((r["id"] for r in sessions if r["feed"] == feed.key), None)
        start_ns = max(now_ns(), feed.source.latest_ns() or 0)
    else:
        feed = hub.replay_feed(inst.symbol)
        if feed is None:
            raise SessionError(f"{inst.label} has no history yet: it is downloading (see Market data)")
        at = _parse_time(msg["at"]) if msg.get("at") else None
        if at is None:
            busy = [r["id"] for r in sessions if hub.journal.has_records(r["id"])
                    and _has_open_book(r["id"])]
            target = busy[0] if busy else None
            if live is not None and not live.session.live:
                at = live.session.sim_ns
        days = feed.source.days()
        if at is not None and day_start_ns(days[0]) + 86_400 * NS_PER_SECOND <= at < _last_tick_ns(feed):
            start_ns = at
        else:
            if msg.get("at"):
                await ws.send_json({"type": "notice", "message": f"{inst.label} has no data then; "
                                                                 f"showing its latest date instead."})
            start_ns = _latest_start_ns(feed)

    if target is not None:
        found = _restore(target)
        if found is not None:
            if found is live and live.ws is ws:
                await _attach(live, ws)
                return live
            _leave(live, ws, found)
            await _attach(found, ws)
            return found

    rate_fn = hub.rate_fn(inst)
    if rate_fn is not None and rate_fn(start_ns) is None:
        conv = instruments.get(CONVERSION[inst.quote][0])
        raise SessionError(f"{inst.label} profits are valued in USD through {conv.label}, which has no data yet. "
                           f"It downloads before {inst.label}; try again in a few minutes.")
    # Empty sessions the screen opened for this instrument before, in no tab
    # now (the tab was closed): replaced by this one.
    for r in sessions:
        other = hub.live.get(r["id"])
        if r.get("origin") == "screen" and (other is None or other.ws is None) and _unused(r["id"]):
            _discard(r["id"])
    broker = Broker.for_instrument(inst, balance=config.DEFAULT_BALANCE, leverage=config.DEFAULT_LEVERAGE,
                                   rate_fn=rate_fn)
    sid = hub.journal.create_session(start_ns=start_ns, timeframe=tf, broker_state=broker.to_state(), hide_dates=False,
                                     mode=mode, feed=feed.key, symbol=inst.symbol, origin="screen")
    session = ReplaySession(feed.source, feed.bars, broker, start_ns, tf, sid, live=mode == "live")
    row = hub.journal.get_session(sid)
    new_live = Live(session, {k: v for k, v in row.items() if k != "state"})
    hub.live[sid] = new_live
    new_live.save()
    _leave(live, ws, new_live)
    await _attach(new_live, ws)
    return new_live


def _has_open_book(sid: str) -> bool:
    """Open positions or pending orders in a session, in memory or saved."""
    if sid in hub.live:
        b = hub.live[sid].session.broker
        return bool(b.positions or b.pending)
    row = hub.journal.get_session(sid)
    broker = (row or {}).get("state", {}).get("broker", {})
    return bool(broker.get("positions") or broker.get("pending"))


def _session_info(live: Live) -> dict:
    m = live.meta
    inst = instruments.get(m.get("symbol"))
    feed_key = m.get("feed") or REPLAY_FEED
    feed = hub.feeds(inst.symbol).get(feed_key)
    return {
        "id": live.id,
        "name": m.get("name", ""),
        "start_ms": m["start_ns"] // 1_000_000,
        "hide_dates": bool(m.get("hide_dates")),
        "mode": m.get("mode") or "replay",
        "feed": feed_key,
        "feed_label": feed.label if feed else feed_key,
        "instrument": inst.to_json(),
        "timeframes": [{"key": k, "label": v[1], "seconds": v[0]} for k, v in TIMEFRAMES.items()],
        "speeds": list(SPEEDS),
    }


def _parse_time(value) -> int:
    """'2024-03-14T07:00' (UTC) or epoch milliseconds -> ns."""
    if isinstance(value, (int, float)):
        return int(value) * 1_000_000
    text = str(value).strip().replace("Z", "")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * NS_PER_SECOND


DRAWING_POINTS = {"hline": 1, "trend": 2, "rect": 2, "position": 3}


def _valid_drawing(raw) -> dict:
    if not isinstance(raw, dict) or raw.get("kind") not in DRAWING_POINTS:
        raise ValueError("Unknown drawing")
    points = raw.get("points")
    if not isinstance(points, list) or len(points) != DRAWING_POINTS[raw["kind"]]:
        raise ValueError("Drawing has the wrong number of points")
    clean = [{"time": int(p["time"]), "price": round(float(p["price"]), 5)} for p in points]
    style = raw.get("style") if isinstance(raw.get("style"), dict) else {}
    return {"id": str(raw.get("id", ""))[:64] or "d", "kind": raw["kind"], "timeframe": str(raw.get("timeframe", ""))[:8],
            "points": clean, "style": style}


def _num(msg: dict, key: str, default=None):
    value = msg.get(key, default)
    if value is None or value == "":
        return default
    return float(value)


# -- websocket -----------------------------------------------------------------


@app.websocket("/ws/quotes")
async def ws_quotes(ws: WebSocket) -> None:
    """The quote board: every recorded instrument's latest tick, sent the
    moment the recorder has new ticks (at most every 20 ms), and once a
    second without."""
    await ws.accept()
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()

    def poke(_symbol: str) -> None:
        loop.call_soon_threadsafe(wake.set)
    hub.tick_listeners.add(poke)
    try:
        while True:
            await ws.send_json(hub.quotes())
            try:
                await asyncio.wait_for(wake.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
            wake.clear()
            await asyncio.sleep(QUOTES_MIN_SECONDS)
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        pass
    finally:
        hub.tick_listeners.discard(poke)


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    live: Live | None = None
    try:
        if not hub.ready:
            # Data is still being prepared (or cannot be): the page shows the
            # progress from /api/data and reconnects when it is ready.
            await ws.send_json({"type": "preparing", "problem": hub.problem, "job": hub.job})
            return
        while True:
            msg = await ws.receive_json()
            try:
                live = await _handle(ws, live, msg)
            except (OrderError, SessionError, ValueError, KeyError, TypeError) as exc:
                await ws.send_json({"type": "error", "message": str(exc) or exc.__class__.__name__})
                if live is not None:
                    await live.flush_events()
    except WebSocketDisconnect:
        pass
    finally:
        if live is not None and live.ws is ws:
            live.ws = None
            live.session.clock.playing = False
            live.stop_clock()
            live.save()


async def _handle(ws: WebSocket, live: Live | None, msg: dict) -> Live | None:
    kind = msg.get("type")

    if kind == "hello":
        sid = msg.get("session")
        found = _restore(sid) if sid else None
        if found is None:
            await ws.send_json({"type": "no_session", "meta": _meta()})
            return live
        await _attach(found, ws)
        return found

    if kind == "new":
        mode = "live" if msg.get("mode") == "live" else "replay"
        inst = instruments.get(msg.get("symbol"))
        feed = hub.feed(msg.get("feed") or REPLAY_FEED, inst.symbol)
        if mode == "live" and not feed.live:
            raise SessionError("Live sessions need a live feed: start the MT5 recorder in Data")
        rate_fn = hub.rate_fn(inst)
        if rate_fn is not None and rate_fn(now_ns()) is None:
            conv = instruments.get(CONVERSION[inst.quote][0])
            raise SessionError(f"{inst.label} profits are valued in USD through {conv.label}, which has no data yet. "
                               f"It downloads before {inst.label}; try again in a few minutes.")
        if mode == "live":
            start_ns = max(now_ns(), feed.source.latest_ns() or 0)
        elif msg.get("random") or not msg.get("start"):
            start_ns = random_start_ns(feed.source.days())
        else:
            start_ns = _parse_time(msg["start"])
        tf = msg.get("timeframe") or config.DEFAULT_TIMEFRAME
        tf_seconds(tf)
        broker = Broker.for_instrument(
            inst,
            balance=_num(msg, "balance", config.DEFAULT_BALANCE),
            leverage=_num(msg, "leverage", config.DEFAULT_LEVERAGE),
            commission_per_lot=_num(msg, "commission", 0.0),
            slippage_pips=_num(msg, "slippage", 0.0),
            rate_fn=rate_fn,
        )
        sid = hub.journal.create_session(
            start_ns=start_ns, timeframe=tf, broker_state=broker.to_state(),
            hide_dates=bool(msg.get("hide_dates")) and mode == "replay", name=str(msg.get("name") or "")[:120],
            mode=mode, feed=feed.key, symbol=inst.symbol,
        )
        session = ReplaySession(feed.source, feed.bars, broker, start_ns, tf, sid, live=mode == "live")
        row = hub.journal.get_session(sid)
        new_live = Live(session, {k: v for k, v in row.items() if k != "state"})
        hub.live[sid] = new_live
        new_live.save()
        _leave(live, ws, new_live)
        await _attach(new_live, ws)
        return new_live

    if kind == "open":
        return await _open(ws, live, msg)

    if live is None or live.ws is not ws:
        raise SessionError("No session is open in this tab")
    s = live.session

    if kind in ("play", "pause", "speed", "step", "jump") and s.live:
        raise SessionError("A live session follows the market's own clock")

    if kind == "play":
        s.refresh()
        if s.finished:
            raise SessionError("The replay has reached the end of the data. Newer hours appear as the update fetches them.")
        s.clock.playing = True
        await live.send({"type": "status", "status": s.status()})
    elif kind == "pause":
        s.clock.playing = False
        live.save()
        await live.send({"type": "status", "status": s.status()})
    elif kind == "speed":
        s.clock.set_speed(msg["value"])
        await live.send({"type": "status", "status": s.status()})
    elif kind == "step":
        s.clock.playing = False
        s.step(msg.get("kind", "candle"))
        await live.send_frame()
    elif kind == "timeframe":
        s.set_timeframe(msg["timeframe"])
        live.save()
        await live.send_history()
    elif kind == "jump":
        s.clock.playing = False
        s.jump(_parse_time(msg["time"]))
        live.save()
        await live.send_history()
        await live.flush_events()
    elif kind == "more_history":
        bars = s.history(msg.get("timeframe") or s.timeframe, limit=1500, before=int(msg["before"]))
        await live.send({"type": "older", "timeframe": msg.get("timeframe") or s.timeframe, "bars": bars})
    elif kind == "order":
        s.broker.place(
            msg["side"], msg.get("order_type", "market"), s.sim_ns,
            lots=_num(msg, "lots"), risk_pct=_num(msg, "risk_pct"),
            price=_num(msg, "price"), sl=_num(msg, "sl"), tp=_num(msg, "tp"),
        )
        await live.flush_events()
    elif kind == "cancel":
        s.broker.cancel(int(msg["id"]))
        await live.flush_events()
    elif kind == "modify":
        changes = {k: (msg[k] if msg[k] not in ("", None) else None) for k in ("price", "sl", "tp") if k in msg}
        if msg.get("target") == "position":
            changes.pop("price", None)
            s.broker.modify_position(int(msg["id"]), sl=changes.get("sl", UNSET), tp=changes.get("tp", UNSET))
        else:
            s.broker.modify_order(
                int(msg["id"]), price=changes.get("price", UNSET),
                sl=changes.get("sl", UNSET), tp=changes.get("tp", UNSET),
            )
        await live.flush_events()
    elif kind == "close":
        s.broker.close(int(msg["id"]), s.sim_ns)
        await live.flush_events()
    elif kind == "end_session":
        s.clock.playing = False
        s.broker.close_all_now("end")
        await live.flush_events()
        live.save()
        hub.journal.end_session(live.id)
        live.stop_clock()
        hub.live.pop(live.id, None)
        hub.trim_memory()
        await live.send({"type": "ended", "session": live.id})
        live.ws = None
        return None
    elif kind == "snapshot":
        data = str(msg["png"]).split(",", 1)[-1]
        png = base64.b64decode(data, validate=True)
        if len(png) > MAX_SNAPSHOT_BYTES or not png.startswith(b"\x89PNG"):
            raise ValueError("Snapshot is not a PNG under 6 MB")
        if msg.get("kind") not in ("entry", "exit"):
            raise ValueError("Snapshot kind must be entry or exit")
        hub.journal.save_snapshot(live.id, int(msg["position_id"]), msg["kind"], png)
    elif kind == "drawing_save":
        d = _valid_drawing(msg["drawing"])
        existing = {x["id"]: x for x in hub.journal.drawings(live.id)}
        d["created_sim_ns"] = existing[d["id"]]["created_sim_ns"] if d.get("id") in existing else s.sim_ns
        hub.journal.save_drawing(live.id, d)
        await live.send({"type": "drawing_saved", "id": d["id"], "created_ms": d["created_sim_ns"] // 1_000_000})
    elif kind == "drawing_delete":
        hub.journal.delete_drawing(live.id, str(msg["id"]))
    elif kind == "annotate":
        hub.journal.annotate_trade(live.id, int(msg["trade_id"]), tags=msg.get("tags"), note=msg.get("note"))
        await live.send({"type": "annotated", "trade_id": int(msg["trade_id"])})
    elif kind == "note":
        body = str(msg.get("body", "")).strip()
        if body:
            hub.journal.add_note(live.id, s.sim_ns, body)
            await live.send({"type": "noted"})
    else:
        raise ValueError(f"Unknown message type {kind!r}")
    return live


# -- pages and JSON ----------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/journal")
def journal_page() -> FileResponse:
    return FileResponse(config.WEB_DIR / "journal.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict:
    return {"app": config.APP_NAME, "ready": hub.ready, "problem": hub.problem}


def _meta() -> dict:
    if not hub.ready:
        return {"ready": False, "problem": hub.problem}
    return {
        "ready": True,
        "symbol": config.SYMBOL,
        "timeframes": [{"key": k, "label": v[1]} for k, v in TIMEFRAMES.items()],
        "speeds": list(SPEEDS),
        "defaults": {
            "balance": config.DEFAULT_BALANCE,
            "leverage": config.DEFAULT_LEVERAGE,
            "timeframe": config.DEFAULT_TIMEFRAME,
            "symbol": config.SYMBOL,
        },
        "keep_flags": list(config.KEEP_FLAGS),
        "instruments": [dict(inst.to_json(), feeds=_feed_list(symbol), streaming=hub.streaming_feed(symbol) is not None)
                        for symbol, inst in INSTRUMENTS.items()],
        "calendar": hub.calendar.status(),
    }


def _feed_list(symbol: str) -> list[dict]:
    out = []
    for key, f in hub.feeds(symbol).items():
        days = f.source.days()
        if not days:
            continue
        last = f.source.latest_ns() if f.live else None
        out.append({
            "key": key, "label": f.label, "live": f.live, "symbol": symbol,
            "first_day": days[0].isoformat(), "last_day": days[-1].isoformat(), "days": len(days),
            "last_tick_ms": last // 1_000_000 if last else None,
        })
    return out


@app.get("/api/data")
def data_status(terminals: int = 0) -> dict:
    """Everything the Data panel shows: preparation and download progress,
    each instrument's history and settings, disk space, the calendar and
    the live recorder."""
    live = {
        "settings": hub.live_settings(),
        "recorder": dict(hub.recorder.status) if hub.recorder else None,
        "feeds": [f for s in INSTRUMENTS for f in _feed_list(s) if f["live"]] if hub.prepared else [],
    }
    if terminals:
        live["terminals"] = installed_terminals()
    settings = hub.data_settings()
    now = now_ns()
    job = dict(hub.job)
    job["instruments"] = dict(job.get("instruments") or {})
    return {
        "ready": hub.ready,
        "prepared": hub.prepared,
        "problem": hub.problem,
        "job": job,
        "auto_update": config.AUTO_UPDATE,
        "instruments": [dict(m.info(settings[s], now), broker=[f for f in _feed_list(s) if f["live"]])
                        for s, m in hub.markets.items()] if hub.prepared else [],
        "disk": hub.disk(),
        "calendar": hub.calendar.status(),
        "live": live,
    }


class DataSettings(BaseModel):
    symbol: str
    enabled: bool | None = None
    start: str | None = None


@app.post("/api/data/settings")
def data_settings(body: DataSettings) -> dict:
    try:
        settings = hub.save_data_settings(body.symbol, body.enabled, body.start)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True, "settings": settings}


@app.post("/api/data/update")
def data_update() -> dict:
    return {"started": hub.start_update(), "job": dict(hub.job)}


@app.post("/api/data/stop")
def data_stop() -> dict:
    hub.stop_job()
    return {"ok": True}


class RecorderStart(BaseModel):
    terminal: str


@app.post("/api/live/start")
def live_start(body: RecorderStart) -> dict:
    path = Path(body.terminal)
    if path.name.lower() != "terminal64.exe" or not path.exists():
        raise HTTPException(400, "Pick an installed MT5 terminal (terminal64.exe)")
    hub.start_recorder(str(path))
    return {"ok": True}


@app.post("/api/live/stop")
def live_stop() -> dict:
    hub.stop_recorder()
    return {"ok": True}


@app.get("/api/meta")
def meta() -> dict:
    return _meta()


@app.get("/api/sessions")
def sessions() -> list[dict]:
    out = hub.journal.list_sessions()
    for s in out:
        s["start_ms"] = s.pop("start_ns") // 1_000_000
        s["sim_ms"] = s.pop("sim_ns") // 1_000_000
        s["net_usd"] = round(s["net_usd"], 2)
    return out


class SessionPatch(BaseModel):
    name: str


@app.patch("/api/sessions/{sid}")
def rename(sid: str, body: SessionPatch) -> dict:
    hub.journal.rename_session(sid, body.name)
    if sid in hub.live:
        hub.live[sid].meta["name"] = body.name
    return {"ok": True}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str) -> dict:
    live = hub.live.pop(sid, None)
    if live is not None:
        live.stop_clock()
        hub.trim_memory()
    hub.journal.delete_session(sid)
    return {"ok": True}


def _trade_json(t: dict) -> dict:
    t = dict(t)
    t["entry_ms"] = t.pop("entry_ns") // 1_000_000
    t["exit_ms"] = t.pop("exit_ns") // 1_000_000
    if t.get("symbol"):
        inst = instruments.get(t["symbol"])
        t["label"], t["digits"] = inst.label, inst.digits
    return t


@app.get("/api/trades")
def trades(session: str | None = None) -> list[dict]:
    return [_trade_json(t) for t in hub.journal.trades(session if session not in (None, "", "all") else None)]


@app.get("/api/stats")
def session_stats(session: str | None = None) -> dict:
    sid = session if session not in (None, "", "all") else None
    rows = hub.journal.trades(sid)
    start = None
    if sid:
        row = hub.journal.get_session(sid)
        if row is None:
            raise HTTPException(404, "No such session")
        start = row["starting_balance"]
    out = stats.compute(rows, start)
    if sid:
        out["notes"] = [
            {"sim_ms": n["sim_ns"] // 1_000_000, "body": n["body"], "created_at": n["created_at"]}
            for n in hub.journal.notes(sid)
        ]
    return out


class TradePatch(BaseModel):
    tags: str | None = None
    note: str | None = None


@app.patch("/api/trades/{sid}/{trade_id}")
def annotate(sid: str, trade_id: int, body: TradePatch) -> dict:
    hub.journal.annotate_trade(sid, trade_id, tags=body.tags, note=body.note)
    return {"ok": True}


@app.get("/api/snapshot/{sid}/{position_id}/{kind}.png")
def snapshot(sid: str, position_id: int, kind: str) -> Response:
    png = hub.journal.snapshot(sid, position_id, kind)
    if png is None:
        raise HTTPException(404, "No snapshot")
    return Response(png, media_type="image/png")


@app.get("/api/review/{sid}/{trade_id}")
def review(sid: str, trade_id: int, tf: str = "m15") -> dict:
    """Candles around a closed trade, for the review chart.

    Review shows what happened after the exit too. For a session still in
    play it stops at that session's clock, so reviewing a trade can never
    show the live replay its own future.
    """
    if not hub.ready:
        raise HTTPException(503, hub.problem)
    trade = hub.journal.trade(sid, trade_id)
    if trade is None:
        raise HTTPException(404, "No such trade")
    row = hub.journal.get_session(sid)
    seconds = tf_seconds(tf)
    inst = instruments.get((row or {}).get("symbol"))
    feeds = hub.feeds(inst.symbol)
    feed_key = (row or {}).get("feed") or REPLAY_FEED
    if feed_key not in feeds:
        raise HTTPException(404, "The data this trade was made on is not available")
    store = feeds[feed_key].bars[tf]
    a = store.index_of_time(trade["entry_ns"] // NS_PER_SECOND) - 200
    b = store.index_of_time(trade["exit_ns"] // NS_PER_SECOND) + 80
    clock = _session_clock(sid, row)
    if clock is not None:
        b = min(b, store.completed_count(clock))
    t = _trade_json(trade)
    t["symbol"] = inst.symbol
    return {
        "trade": t,
        "timeframe": tf,
        "seconds": seconds,
        "bars": store.slice(a, b),
        "session_active": bool(row and row["status"] == "active"),
        "instrument": inst.to_json(),
        "hide_dates": bool(row and row.get("hide_dates")),
    }


def _session_clock(sid: str, row: dict | None) -> int | None:
    """The clock that limits what a session may see; None once it ended."""
    if row is None or row["status"] != "active":
        return None
    return hub.live[sid].session.sim_ns if sid in hub.live else row["sim_ns"]


# -- economic calendar -----------------------------------------------------------


@app.get("/api/calendar")
def calendar_window(session: str, start: int, end: int) -> dict:
    """Events for the session's instrument between two times (epoch ms),
    up to a few days past its clock: the schedule is known ahead, the
    numbers are not."""
    row = hub.journal.get_session(session)
    if row is None:
        raise HTTPException(404, "No such session")
    inst = instruments.get(row.get("symbol"))
    clock = _session_clock(session, row)
    a = int(start) * 1_000_000
    b = int(end) * 1_000_000
    if clock is not None:
        b = min(b, clock + CALENDAR_AHEAD_NS)
    events = hub.calendar.window(a, b, inst.currencies, clock) if b >= a else []
    return {"events": events, "clock_ms": clock // 1_000_000 if clock is not None else None,
            "currencies": list(inst.currencies), "status": hub.calendar.status()}


@app.get("/api/calendar/event/{event_id}")
def calendar_event(event_id: int, session: str) -> dict:
    row = hub.journal.get_session(session)
    if row is None:
        raise HTTPException(404, "No such session")
    clock = _session_clock(session, row)
    detail = hub.calendar.detail(event_id, clock, hide_dates=bool(row.get("hide_dates")))
    if detail is None:
        raise HTTPException(404, "No such event")
    if clock is not None and detail["ms"] * 1_000_000 > clock + CALENDAR_AHEAD_NS:
        raise HTTPException(404, "No such event")
    return detail
