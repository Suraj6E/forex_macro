"""The simulator's web server (guide step 3).

FastAPI serves the pages and one WebSocket per browser tab. The engine runs
here and the browser only draws. Each replay session is driven by its own
clock task, which advances time every 50 ms and sends at most one screen
update per frame (20 per second); the broker still sees every tick.

A session belongs to one tab at a time. Opening it in a second tab moves it
there and tells the first tab, so there is never a second copy advancing the
same account.
"""

from __future__ import annotations

import asyncio
import base64
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from simulator import config
from simulator.data.bars import TIMEFRAMES, BarStore, tf_seconds
from simulator.data.ticks import NS_PER_SECOND, ParquetTickSource
from simulator.engine.broker import UNSET, Broker, OrderError
from simulator.engine.clock import FRAME_SECONDS, SPEEDS
from simulator.engine.session import ReplaySession, SessionError, random_start_ns
from simulator.journal import stats
from simulator.journal.store import Journal

SAVE_EVERY_SECONDS = 5.0
MAX_SNAPSHOT_BYTES = 6_000_000


class Hub:
    """Shared, read-only market data plus the live sessions."""

    def __init__(self) -> None:
        self.journal = Journal()
        self.source: ParquetTickSource | None = None
        self.bars: BarStore | None = None
        self.problem: str | None = None
        self.live: dict[str, Live] = {}

    def load(self) -> None:
        source = ParquetTickSource()
        if not source.cached or not BarStore.exists():
            self.problem = (
                "The tick and candle cache is not built. Stop the server and run "
                "`python -m simulator build-cache` (about 20 seconds), then start it again."
            )
            if not config.TICK_FILE.exists():
                self.problem = f"Tick file not found: {config.TICK_FILE}. Set SIM_TICK_FILE to its path."
            return
        self.source = source
        self.bars = BarStore.load()
        self.problem = None

    @property
    def ready(self) -> bool:
        return self.source is not None and self.bars is not None


hub = Hub()


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub.load()
    yield
    for live in list(hub.live.values()):
        live.save()


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
            if not s.clock.playing or self.ws is None:
                continue
            s.advance(s.sim_ns + s.clock.market_ns(dt))
            await self.send_frame()
            if s.finished:
                s.clock.playing = False
                await self.send({"type": "status", "status": s.status(), "message": "End of the tick data."})
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
    session = ReplaySession.from_state(hub.source, hub.bars, row["state"], sid)
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
    live.session.clock.playing = False
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
            await ws.send_json({"type": "fatal", "message": hub.problem})
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
        if msg.get("random") or not msg.get("start"):
            start_ns = random_start_ns(hub.source.days())
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
            hide_dates=bool(msg.get("hide_dates")), name=str(msg.get("name") or "")[:120],
        )
        session = ReplaySession(hub.source, hub.bars, broker, start_ns, tf, sid)
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

    if kind == "play":
        if s.finished:
            raise SessionError("The replay has reached the end of the tick data")
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
    }


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
    store = hub.bars[tf]
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
