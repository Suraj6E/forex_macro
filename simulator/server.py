"""The simulator's web server (guide step 3).

FastAPI serves the pages and one WebSocket per browser tab. The engine runs
here and the browser only draws. Each session is driven by its own clock
task, which advances time every 50 ms and sends at most one screen update
per frame (20 per second); the broker still sees every tick.

A session belongs to one tab at a time. Opening it in a second tab moves it
there and tells the first tab, so there is never a second copy advancing the
same account.

Two kinds of feed: the replay feed (Layer 0 history plus Dukascopy
downloads, kept current by a background update) and live feeds recorded
from an MT5 terminal, one per trade server. A live session runs on a live
feed with the wall clock.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from simulator import config
from simulator.data import dukascopy
from simulator.data.bars import TIMEFRAMES, BarStore, tf_seconds
from simulator.data.ticks import NS_PER_SECOND, FeedStore, ParquetTickSource, import_layer0, now_ns
from simulator.engine.broker import UNSET, Broker, OrderError
from simulator.engine.clock import FRAME_SECONDS, SPEEDS
from simulator.engine.session import ReplaySession, SessionError, random_start_ns
from simulator.journal import stats
from simulator.journal.store import Journal
from simulator.live import mt5rec
from simulator.live.feed import FeedBars, LiveFeedSource
from simulator.live.winproc import installed_terminals

SAVE_EVERY_SECONDS = 5.0
MAX_SNAPSHOT_BYTES = 6_000_000
REPLAY_FEED = "dukascopy"
#: Days fetched between candle rebuilds during a long catch-up.
UPDATE_CHUNK_DAYS = 20


class Feed:
    def __init__(self, key: str, label: str, source, bars, live: bool) -> None:
        self.key, self.label, self.source, self.bars, self.live = key, label, source, bars, live


class Hub:
    """Market data, the background jobs that keep it current, the recorder,
    and the sessions in memory.

    Nothing here needs a command. On start the server prepares the replay
    data if it is missing (imports the Layer 0 file, builds candles), then
    fetches new Dukascopy hours in the background, rebuilding candles after
    every chunk so new days become playable as they arrive. The recorder
    starts again if it was on last time. The page shows all of it.
    """

    def __init__(self) -> None:
        self.journal = Journal()
        self.source: ParquetTickSource | None = None
        self.bars: BarStore | None = None
        self.problem: str | None = None
        self.live: dict[str, Live] = {}
        self._mt5_feeds: dict[str, Feed] = {}
        self.job: dict = {"kind": None, "running": False}
        self._job_thread: threading.Thread | None = None
        self._job_stop = threading.Event()
        self.recorder: mt5rec.MT5Recorder | None = None

    # -- the replay feed -------------------------------------------------------

    def load(self) -> bool:
        source = ParquetTickSource()
        if not source.ready or not BarStore.exists(config.DUKA_DIR / "bars", config.KEEP_FLAGS):
            return False
        self.source = source
        self.bars = BarStore.load(config.DUKA_DIR / "bars")
        self.problem = None
        return True

    @property
    def ready(self) -> bool:
        return self.source is not None and self.bars is not None

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
        """On server start: prepare if needed, then update (if enabled)."""
        if self.load():
            if config.AUTO_UPDATE:
                self.start_update()
            return
        if not ParquetTickSource().ready and not config.TICK_FILE.exists():
            self.problem = (f"No market data yet, and the Layer 0 tick file was not found at {config.TICK_FILE}. "
                            "Set SIM_TICK_FILE to its path and start again.")
            return
        self._start_job("prepare", self._prepare)

    def _prepare(self) -> None:
        job = self.job
        if not FeedStore(config.DUKA_DIR).manifest().get("layer0") and config.TICK_FILE.exists():
            groups = pq.ParquetFile(config.TICK_FILE).metadata.num_row_groups
            job.update(total=groups, message="Importing the 2023-2024 tick file")

            def on_group(msg: str) -> None:
                job["done"] = min(job["done"] + 1, groups)
                job["message"] = f"Importing the 2023-2024 tick file ({job['done']}/{groups})"
            import_layer0(progress=on_group)
        source = ParquetTickSource()
        days = len(source.days())
        job.update(done=0, total=days, message="Building candles")

        def on_days(msg: str) -> None:
            n = int(msg.split(":")[1].split("/")[0])
            job.update(done=n, message=f"Building candles ({n}/{days} days)")
        bars = BarStore.build(source, progress=on_days)
        bars.save(config.DUKA_DIR / "bars", config.KEEP_FLAGS)
        self.load()
        job.update(running=False, finished_ms=int(time.time() * 1000), message="Market data ready")
        if config.AUTO_UPDATE:
            self._job_thread = None
            self.start_update()

    def start_update(self) -> bool:
        if not self.ready:
            return False
        return self._start_job("update", self._update)

    def stop_job(self) -> None:
        self._job_stop.set()

    def _update(self) -> None:
        job = self.job
        job["message"] = "Checking Dukascopy for new hours"
        total = len(dukascopy.pending_days())
        job.update(total=total, done=0, days=0, ticks=0, failed_hours=0)
        fetcher = dukascopy.Fetcher(
            stop=self._job_stop,
            on_wait=lambda s: job.update(message=f"Dukascopy is not answering; pausing {s:.0f} s, then trying again"),
        )
        start_day = None
        while not self._job_stop.is_set():
            base = job["done"]

            def progress(day, done, total, base=base, overall=total):
                job.update(done=min(base + done, total), day=day.isoformat(),
                           message=f"Fetching {day.isoformat()} from Dukascopy")
            summary = dukascopy.update(start_day=start_day, max_days=UPDATE_CHUNK_DAYS,
                                       progress=progress, stop=self._job_stop, fetch=fetcher)
            job["days"] += summary["days"]
            job["ticks"] += summary["ticks"]
            job["failed_hours"] += summary["failed_hours"]
            if summary["first_changed"] is not None:
                job["message"] = "Building candles for the new days"
                self.source.refresh()
                self.bars.rebuild_from(self.source, summary["first_changed"])
                self.bars.save(config.DUKA_DIR / "bars", config.KEEP_FLAGS)
            if summary["stopped"] or not summary["remaining"] or summary["through"] is None:
                break
            # Carry on after this chunk; a day with a failed hour is retried on
            # the next run, not again and again in this one.
            start_day = summary["through"] + timedelta(days=1)
        gaps = f"; {job['failed_hours']} hours did not answer and are retried next time" if job["failed_hours"] else ""
        job.update(running=False, done=job["total"], finished_ms=int(time.time() * 1000),
                   message=("Stopped; it resumes next time" if self._job_stop.is_set() else
                            f"Up to date: {job['days']} days fetched, {job['ticks']:,} new ticks{gaps}"))

    def history_info(self) -> dict:
        if not self.ready:
            return {}
        days = self.source.days()
        last = self.source.load_day(days[-1])
        manifest = self.source.store.manifest()["days"]
        return {
            "first_day": days[0].isoformat(),
            "last_day": days[-1].isoformat(),
            "trading_days": len(days),
            "last_tick_ms": int(last.ts[-1]) // 1_000_000 if len(last) else None,
            "downloaded_days": sum(1 for e in manifest.values() if e.get("source") == "dukascopy"),
        }

    # -- live feeds --------------------------------------------------------------

    def feeds(self) -> dict[str, Feed]:
        out = {}
        if self.ready:
            out[REPLAY_FEED] = Feed(REPLAY_FEED, "Dukascopy history", self.source, self.bars, live=False)
        for feed_dir in sorted(config.MT5_DIR.glob(f"*/{config.SYMBOL}")):
            key = f"mt5:{feed_dir.parent.name}"
            if key not in self._mt5_feeds:
                if not ((feed_dir / "manifest.json").exists() or any((feed_dir / "live").glob("*.csv"))):
                    continue
                src = LiveFeedSource(feed_dir)
                manifest = src.store.manifest()
                label = f"MT5 {manifest.get('server', feed_dir.parent.name)}"
                self._mt5_feeds[key] = Feed(key, label, src, FeedBars(src), live=True)
            out[key] = self._mt5_feeds[key]
        return out

    def feed(self, key: str | None) -> Feed:
        feeds = self.feeds()
        key = key or REPLAY_FEED
        if key not in feeds:
            raise SessionError(f"No data for feed {key!r} yet")
        if not feeds[key].source.days():
            raise SessionError(f"{feeds[key].label} has no ticks recorded yet")
        return feeds[key]

    def live_settings(self) -> dict:
        try:
            return json.loads(config.LIVE_SETTINGS.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def save_live_settings(self, settings: dict) -> None:
        config.LIVE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        config.LIVE_SETTINGS.write_text(json.dumps(settings, indent=1), encoding="utf-8")

    def start_recorder(self, terminal: str) -> None:
        if self.recorder is not None:
            self.recorder.stop()
        self.recorder = mt5rec.MT5Recorder(terminal)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub.start()
    settings = hub.live_settings()
    if settings.get("autostart") and settings.get("terminal"):
        # Waits harmlessly until the trader opens that terminal.
        hub.recorder = mt5rec.MT5Recorder(settings["terminal"])
        hub.recorder.start()
    yield
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
            self.task = asyncio.create_task(self.clock_loop())

    def stop_clock(self) -> None:
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
    feed = hub.feed(row.get("feed") or REPLAY_FEED)
    is_live = row.get("mode") == "live"
    session = ReplaySession.from_state(feed.source, feed.bars, row["state"], sid,
                                       live=is_live, now=now_ns() if is_live else None)
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


def _session_info(live: Live) -> dict:
    m = live.meta
    return {
        "id": live.id,
        "name": m.get("name", ""),
        "start_ms": m["start_ns"] // 1_000_000,
        "hide_dates": bool(m.get("hide_dates")),
        "mode": m.get("mode") or "replay",
        "feed": m.get("feed") or REPLAY_FEED,
        "feed_label": hub.feeds().get(m.get("feed") or REPLAY_FEED, Feed("", m.get("feed") or "", None, None, False)).label,
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
        feed = hub.feed(msg.get("feed") or REPLAY_FEED)
        if mode == "live" and not feed.live:
            raise SessionError("Live sessions need a live feed: start the MT5 recorder in Data")
        if mode == "live":
            start_ns = max(now_ns(), feed.source.latest_ns() or 0)
        elif msg.get("random") or not msg.get("start"):
            start_ns = random_start_ns(feed.source.days())
        else:
            start_ns = _parse_time(msg["start"])
        tf = msg.get("timeframe") or config.DEFAULT_TIMEFRAME
        tf_seconds(tf)
        broker = Broker(
            balance=_num(msg, "balance", config.DEFAULT_BALANCE),
            leverage=_num(msg, "leverage", config.DEFAULT_LEVERAGE),
            commission_per_lot=_num(msg, "commission", 0.0),
            slippage_pips=_num(msg, "slippage", 0.0),
        )
        sid = hub.journal.create_session(
            start_ns=start_ns, timeframe=tf, broker_state=broker.to_state(),
            hide_dates=bool(msg.get("hide_dates")) and mode == "replay", name=str(msg.get("name") or "")[:120],
            mode=mode, feed=feed.key,
        )
        session = ReplaySession(feed.source, feed.bars, broker, start_ns, tf, sid, live=mode == "live")
        row = hub.journal.get_session(sid)
        new_live = Live(session, {k: v for k, v in row.items() if k != "state"})
        hub.live[sid] = new_live
        new_live.save()
        if live is not None and live is not new_live and live.ws is ws:
            live.ws = None
            live.stop_clock()
            live.save()
        await _attach(new_live, ws)
        return new_live

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
    days = hub.source.days()
    return {
        "ready": True,
        "symbol": config.SYMBOL,
        "first_day": days[0].isoformat(),
        "last_day": days[-1].isoformat(),
        "trading_days": len(days),
        "timeframes": [{"key": k, "label": v[1]} for k, v in TIMEFRAMES.items()],
        "speeds": list(SPEEDS),
        "defaults": {
            "balance": config.DEFAULT_BALANCE,
            "leverage": config.DEFAULT_LEVERAGE,
            "timeframe": config.DEFAULT_TIMEFRAME,
        },
        "keep_flags": list(hub.source.keep_flags),
        "feeds": _feed_list(),
    }


def _feed_list() -> list[dict]:
    out = []
    for key, f in hub.feeds().items():
        days = f.source.days()
        if not days:
            continue
        last = f.source.latest_ns() if f.live else None
        out.append({
            "key": key, "label": f.label, "live": f.live,
            "first_day": days[0].isoformat(), "last_day": days[-1].isoformat(), "days": len(days),
            "last_tick_ms": last // 1_000_000 if last else None,
        })
    return out


@app.get("/api/data")
def data_status(terminals: int = 0) -> dict:
    """Everything the Data panel shows: preparation and update progress,
    the replay history's range, and the live recorder."""
    live = {
        "settings": hub.live_settings(),
        "recorder": dict(hub.recorder.status) if hub.recorder else None,
        "feeds": [f for f in _feed_list() if f["live"]] if hub.ready else [],
    }
    if terminals:
        live["terminals"] = installed_terminals()
    return {
        "ready": hub.ready,
        "problem": hub.problem,
        "job": dict(hub.job),
        "auto_update": config.AUTO_UPDATE,
        "history": hub.history_info(),
        "live": live,
    }


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
    hub.journal.delete_session(sid)
    return {"ok": True}


def _trade_json(t: dict) -> dict:
    t = dict(t)
    t["entry_ms"] = t.pop("entry_ns") // 1_000_000
    t["exit_ms"] = t.pop("exit_ns") // 1_000_000
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
    feeds = hub.feeds()
    feed_key = (row or {}).get("feed") or REPLAY_FEED
    if feed_key not in feeds:
        raise HTTPException(404, "The data this trade was made on is not available")
    store = feeds[feed_key].bars[tf]
    a = store.index_of_time(trade["entry_ns"] // NS_PER_SECOND) - 200
    b = store.index_of_time(trade["exit_ns"] // NS_PER_SECOND) + 80
    if row is not None and row["status"] == "active":
        clock = hub.live[sid].session.sim_ns if sid in hub.live else row["sim_ns"]
        b = min(b, store.completed_count(clock))
    return {
        "trade": _trade_json(trade),
        "timeframe": tf,
        "seconds": seconds,
        "bars": store.slice(a, b),
        "session_active": bool(row and row["status"] == "active"),
    }
