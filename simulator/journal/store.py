"""The automatic journal, on SQLite (guide step 5.1).

Nothing here waits for the trader to press save. The server writes a row the
moment an order is placed, filled, changed or closed, and the session row
(clock, settings, open orders and positions) on every change and every few
seconds of play, so closing the browser or the server mid-session loses
nothing.

Each call opens its own short connection: the server calls in from the event
loop and from FastAPI's worker threads, and SQLite connections must not be
shared across threads.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from simulator import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    start_ns INTEGER NOT NULL,
    sim_ns INTEGER NOT NULL,
    timeframe TEXT NOT NULL,
    starting_balance REAL NOT NULL,
    leverage REAL NOT NULL,
    commission_per_lot REAL NOT NULL,
    slippage_pips REAL NOT NULL,
    hide_dates INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    mode TEXT NOT NULL DEFAULT 'replay',
    feed TEXT NOT NULL DEFAULT 'dukascopy',
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    id INTEGER NOT NULL,
    side TEXT NOT NULL,
    type TEXT NOT NULL,
    lots REAL NOT NULL,
    price REAL,
    sl REAL,
    tp REAL,
    risk_pct REAL,
    placed_ns INTEGER NOT NULL,
    status TEXT NOT NULL,
    filled_ns INTEGER,
    fill_price REAL,
    position_id INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, id)
);
CREATE TABLE IF NOT EXISTS trades (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    id INTEGER NOT NULL,
    position_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    side TEXT NOT NULL,
    lots REAL NOT NULL,
    entry_ns INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_ns INTEGER NOT NULL,
    exit_price REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    initial_sl REAL,
    sl REAL,
    tp REAL,
    pips REAL NOT NULL,
    risk_usd REAL,
    gross_usd REAL NOT NULL,
    commission_usd REAL NOT NULL,
    net_usd REAL NOT NULL,
    r_multiple REAL,
    tags TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, id)
);
CREATE TABLE IF NOT EXISTS notes (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_ns INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    position_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('entry', 'exit')),
    png BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, position_id, kind)
);
CREATE TABLE IF NOT EXISTS drawings (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    points TEXT NOT NULL,
    style TEXT NOT NULL DEFAULT '{}',
    created_sim_ns INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, id)
);
"""

ORDER_FIELDS = (
    "id", "side", "type", "lots", "price", "sl", "tp", "risk_pct", "placed_ns",
    "status", "filled_ns", "fill_price", "position_id", "reason",
)
TRADE_FIELDS = (
    "id", "position_id", "order_id", "side", "lots", "entry_ns", "entry_price",
    "exit_ns", "exit_price", "exit_reason", "initial_sl", "sl", "tp", "pips",
    "risk_usd", "gross_usd", "commission_usd", "net_usd", "r_multiple",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: Path = config.JOURNAL_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.executescript(SCHEMA)
            # Journals made before live sessions existed: add the columns.
            have = {r["name"] for r in db.execute("PRAGMA table_info(sessions)")}
            if "mode" not in have:
                db.execute("ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'replay'")
            if "feed" not in have:
                db.execute("ALTER TABLE sessions ADD COLUMN feed TEXT NOT NULL DEFAULT 'dukascopy'")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # -- sessions ----------------------------------------------------------

    def create_session(self, *, start_ns: int, timeframe: str, broker_state: dict, hide_dates: bool, name: str = "",
                       mode: str = "replay", feed: str = "dukascopy", symbol: str = config.SYMBOL) -> str:
        sid = uuid.uuid4().hex[:12]
        now = _now()
        state = {"sim_ns": start_ns, "timeframe": timeframe, "speed": 60.0, "broker": broker_state}
        with self._conn() as db:
            db.execute(
                """INSERT INTO sessions (id, name, symbol, created_at, updated_at, start_ns, sim_ns,
                   timeframe, starting_balance, leverage, commission_per_lot, slippage_pips,
                   hide_dates, status, mode, feed, state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (
                    sid, name, symbol, now, now, start_ns, start_ns, timeframe,
                    broker_state["starting_balance"], broker_state["leverage"],
                    broker_state["commission_per_lot"], broker_state["slippage_pips"],
                    int(hide_dates), mode, feed, json.dumps(state),
                ),
            )
        return sid

    def save_state(self, sid: str, state: dict) -> None:
        with self._conn() as db:
            db.execute(
                "UPDATE sessions SET state = ?, sim_ns = ?, timeframe = ?, updated_at = ? WHERE id = ?",
                (json.dumps(state), state["sim_ns"], state["timeframe"], _now(), sid),
            )

    def end_session(self, sid: str) -> None:
        with self._conn() as db:
            db.execute("UPDATE sessions SET status = 'ended', updated_at = ? WHERE id = ?", (_now(), sid))

    def rename_session(self, sid: str, name: str) -> None:
        with self._conn() as db:
            db.execute("UPDATE sessions SET name = ? WHERE id = ?", (name[:120], sid))

    def delete_session(self, sid: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM sessions WHERE id = ?", (sid,))

    def get_session(self, sid: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["state"] = json.loads(out["state"])
        return out

    def list_sessions(self) -> list[dict]:
        with self._conn() as db:
            rows = db.execute(
                """SELECT s.id, s.name, s.symbol, s.created_at, s.updated_at, s.start_ns, s.sim_ns,
                          s.timeframe, s.starting_balance, s.leverage, s.commission_per_lot,
                          s.slippage_pips, s.hide_dates, s.status, s.mode, s.feed,
                          COUNT(t.id) AS trades, COALESCE(SUM(t.net_usd), 0) AS net_usd
                   FROM sessions s LEFT JOIN trades t ON t.session_id = s.id
                   GROUP BY s.id ORDER BY s.updated_at DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    # -- orders and trades --------------------------------------------------

    def upsert_order(self, sid: str, order: dict) -> None:
        cols = ", ".join(ORDER_FIELDS)
        marks = ", ".join("?" for _ in ORDER_FIELDS)
        with self._conn() as db:
            db.execute(
                f"INSERT OR REPLACE INTO orders (session_id, {cols}) VALUES (?, {marks})",
                (sid, *[order.get(f) for f in ORDER_FIELDS]),
            )

    def insert_trade(self, sid: str, trade: dict) -> None:
        cols = ", ".join(TRADE_FIELDS)
        marks = ", ".join("?" for _ in TRADE_FIELDS)
        with self._conn() as db:
            db.execute(
                f"INSERT OR IGNORE INTO trades (session_id, {cols}) VALUES (?, {marks})",
                (sid, *[trade.get(f) for f in TRADE_FIELDS]),
            )

    def annotate_trade(self, sid: str, trade_id: int, *, tags: str | None = None, note: str | None = None) -> None:
        with self._conn() as db:
            if tags is not None:
                clean = ",".join(t.strip().lower() for t in tags.split(",") if t.strip())
                db.execute("UPDATE trades SET tags = ? WHERE session_id = ? AND id = ?", (clean[:500], sid, trade_id))
            if note is not None:
                db.execute("UPDATE trades SET note = ? WHERE session_id = ? AND id = ?", (note[:5000], sid, trade_id))

    def trades(self, sid: str | None = None) -> list[dict]:
        query = """SELECT t.*, s.name AS session_name, s.symbol AS symbol,
                          (SELECT COUNT(*) FROM snapshots p WHERE p.session_id = t.session_id
                             AND p.position_id = t.position_id AND p.kind = 'entry') AS has_entry_png,
                          (SELECT COUNT(*) FROM snapshots p WHERE p.session_id = t.session_id
                             AND p.position_id = t.position_id AND p.kind = 'exit') AS has_exit_png
                   FROM trades t JOIN sessions s ON s.id = t.session_id"""
        args: tuple = ()
        if sid:
            query += " WHERE t.session_id = ?"
            args = (sid,)
        query += " ORDER BY t.exit_ns, t.session_id, t.id"
        with self._conn() as db:
            return [dict(r) for r in db.execute(query, args).fetchall()]

    def trade(self, sid: str, trade_id: int) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM trades WHERE session_id = ? AND id = ?", (sid, trade_id)).fetchone()
        return dict(row) if row else None

    def orders(self, sid: str) -> list[dict]:
        with self._conn() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM orders WHERE session_id = ? ORDER BY id", (sid,)
            ).fetchall()]

    # -- snapshots ----------------------------------------------------------

    def save_snapshot(self, sid: str, position_id: int, kind: str, png: bytes) -> None:
        with self._conn() as db:
            db.execute(
                "INSERT OR REPLACE INTO snapshots (session_id, position_id, kind, png, created_at) VALUES (?, ?, ?, ?, ?)",
                (sid, position_id, kind, png, _now()),
            )

    def snapshot(self, sid: str, position_id: int, kind: str) -> bytes | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT png FROM snapshots WHERE session_id = ? AND position_id = ? AND kind = ?",
                (sid, position_id, kind),
            ).fetchone()
        return bytes(row["png"]) if row else None

    # -- notes --------------------------------------------------------------

    def add_note(self, sid: str, sim_ns: int, body: str) -> None:
        with self._conn() as db:
            db.execute(
                "INSERT INTO notes (session_id, sim_ns, created_at, body) VALUES (?, ?, ?, ?)",
                (sid, sim_ns, _now(), body[:5000]),
            )

    def notes(self, sid: str) -> list[dict]:
        with self._conn() as db:
            return [dict(r) for r in db.execute(
                "SELECT id, sim_ns, created_at, body FROM notes WHERE session_id = ? ORDER BY sim_ns, id", (sid,)
            ).fetchall()]

    # -- drawings -----------------------------------------------------------

    def save_drawing(self, sid: str, drawing: dict) -> None:
        with self._conn() as db:
            db.execute(
                """INSERT INTO drawings (session_id, id, kind, timeframe, points, style, created_sim_ns, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (session_id, id) DO UPDATE SET
                     points = excluded.points, style = excluded.style, updated_at = excluded.updated_at""",
                (
                    sid, str(drawing["id"])[:64], drawing["kind"], drawing.get("timeframe", ""),
                    json.dumps(drawing["points"]), json.dumps(drawing.get("style", {})),
                    int(drawing["created_sim_ns"]), _now(),
                ),
            )

    def delete_drawing(self, sid: str, drawing_id: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM drawings WHERE session_id = ? AND id = ?", (sid, drawing_id))

    def drawings(self, sid: str, visible_at_ns: int | None = None) -> list[dict]:
        """Drawings of a session. With `visible_at_ns`, only those created at
        or before that simulated time: a line drawn at 14:00 with knowledge
        of that day must not appear when the clock is back at 09:00."""
        query = "SELECT * FROM drawings WHERE session_id = ?"
        args: tuple = (sid,)
        if visible_at_ns is not None:
            query += " AND created_sim_ns <= ?"
            args = (sid, visible_at_ns)
        with self._conn() as db:
            rows = db.execute(query + " ORDER BY created_sim_ns, id", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["points"] = json.loads(d["points"])
            d["style"] = json.loads(d["style"])
            out.append(d)
        return out
