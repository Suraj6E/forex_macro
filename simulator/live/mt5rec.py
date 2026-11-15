"""Record ticks from a MetaTrader 5 terminal: live, and its history.

What it does, in order:
  1. Waits until the chosen terminal is running. It never starts one:
     starting a terminal restores its charts and can switch on an Expert
     Advisor that trades the account. It only attaches to a terminal the
     trader opened.
  2. Attaches, reads the account's trade server and finds the broker's name
     for each instrument (EURUSD, XAGUSD, USOIL.cash, ...). It only reads:
     it never calls an order function.
  3. Works out the server's clock offset. The MetaTrader5 package returns
     tick times in the trade server's time zone (commonly UTC+2/+3), so
     offset = round(latest tick time - now, to 30 min) while the market is
     moving, and is re-checked every 10 minutes (daylight saving).
  4. Fills the gap since the last recorded tick from the terminal (from the
     start of the UTC day `backfill_days` ago at most), then polls new ticks
     twice a second and appends them, converted to UTC, to
     `live/YYYY-MM-DD.csv`, flushing every poll.
  5. Between polls, fetches older days from the broker's tick history,
     newest first, back to each instrument's start day, until the broker
     has nothing older. A terminal keeps only a small buffer of recent ticks
     on disk; older ones are downloaded from the broker's server when asked
     for, which is what this does.
  6. When a UTC day closes, cleans it with Layer 0's rules and stores it as
     Parquet, like the replay feed.

History needs the server's offset on past days, not today's: a broker on
UTC+2 in winter is on UTC+3 in summer. Most MT5 brokers keep their clock at
New York + 7 hours, so the week opens at 00:00 server time all year; when
the offset measured now matches that rule, history uses it, otherwise the
measured offset as a fixed one.

One feed per trade server and instrument: prices and spreads differ by
broker, so feeds are never mixed. A file lock stops two recorders writing
the same feed.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import numpy as np

from simulator import config
from simulator.data.quality import flag_day
from simulator.data.ticks import FeedStore, day_entry
from simulator.instruments import INSTRUMENTS, Instrument
from simulator.live.feed import read_csv_bytes
from simulator.live.winproc import FileLock, running_terminals, same_path

MS_PER_DAY = 86_400_000
MS_PER_HOUR = 3_600_000
POLL_SECONDS = 0.5
OFFSET_CHECK_SECONDS = 600
STATUS_EVERY_SECONDS = 2.0
BACKFILL_CHUNK_MS = 6 * 3600 * 1000
#: The broker has no older history once this many weekdays in a row are empty.
HISTORY_EMPTY_RUN = 10
#: A day of history is accepted once two reads a moment apart agree; the
#: terminal may still be downloading it from the broker on the first.
HISTORY_SETTLE_TRIES = 8
HISTORY_SETTLE_WAIT = 0.3
#: Seconds of history work between two polls of live ticks.
HISTORY_BUDGET_SECONDS = 0.5

TRADE_MODES = {0: "demo", 1: "contest", 2: "real"}

#: After a pause this long (weekend, holiday) the server clock is read again
#: before the next tick is converted: brokers change their offset for
#: daylight saving while the market is closed.
REMEASURE_AFTER_MS = 30 * 60 * 1000


class TerminalGone(RuntimeError):
    """The terminal was closed; wait for it to come back."""


def slug(server: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", server.lower()).strip("-") or "unknown"


def feed_dir_for(server: str, symbol: str = config.SYMBOL, root: Path = config.MT5_DIR) -> Path:
    return Path(root) / slug(server) / symbol


def measure_offset_ms(tick_time_msc: int, now_ms: int) -> int | None:
    """Server-time offset from a tick known to be fresh, to the nearest
    30 minutes. None when the numbers do not fit a fresh tick.

    Freshness has to be established by the caller (`_measure_offset` waits
    for the latest tick to change): a tick exactly some whole hours old
    looks like a fresh tick under a different offset, which is how an hour
    after Friday's close would turn into an offset an hour wrong."""
    diff = tick_time_msc - now_ms
    off = round(diff / 1_800_000) * 1_800_000
    age = off - diff  # how long ago the tick was stamped, allowing clock skew
    if -120_000 <= age <= 120_000 and abs(off) <= 14 * 3_600_000:
        return int(off)
    return None


# -- the broker's clock on past days ------------------------------------------


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_sunday(year: int, month: int) -> date:
    d = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def us_dst(utc_ms: int) -> bool:
    """Whether New York is on summer time at this instant (rules since 2007:
    second Sunday of March to first Sunday of November; before, first
    Sunday of April to last Sunday of October; both at 02:00 local)."""
    t = datetime.fromtimestamp(utc_ms / 1000, tz=timezone.utc)
    y = t.year
    if y >= 2007:
        start, end = _nth_sunday(y, 3, 2), _nth_sunday(y, 11, 1)
    else:
        start, end = _nth_sunday(y, 4, 1), _last_sunday(y, 10)
    begin = datetime(start.year, start.month, start.day, 7, tzinfo=timezone.utc)  # 02:00 EST
    finish = datetime(end.year, end.month, end.day, 6, tzinfo=timezone.utc)  # 02:00 EDT
    return begin <= t < finish


def ny7_offset_ms(utc_ms: int) -> int:
    """Server offset of a broker whose clock is New York + 7 hours."""
    return (3 if us_dst(utc_ms) else 2) * MS_PER_HOUR


class ServerClock:
    """The broker's offset from UTC at any instant, past or present."""

    def __init__(self, measured_ms: int, now_ms: int) -> None:
        self.rule = "ny+7" if measured_ms == ny7_offset_ms(now_ms) else "fixed"
        self.fixed_ms = measured_ms

    def offset(self, utc_ms: int) -> int:
        return ny7_offset_ms(utc_ms) if self.rule == "ny+7" else self.fixed_ms

    def to_server(self, utc_ms: int) -> int:
        return utc_ms + self.offset(utc_ms)

    def to_utc(self, server_ms: np.ndarray) -> np.ndarray:
        """Server stamps to UTC. Offsets change on Sunday mornings, when the
        market is shut, so the offset two hours before a stamp is its own."""
        server_ms = np.asarray(server_ms, np.int64)
        if self.rule == "fixed" or not len(server_ms):
            return server_ms - self.fixed_ms
        first = self.offset(int(server_ms[0]) - 2 * MS_PER_HOUR)
        last = self.offset(int(server_ms[-1]) - 2 * MS_PER_HOUR)
        if first == last:
            return server_ms - first
        offs = np.array([self.offset(int(s) - 2 * MS_PER_HOUR) for s in server_ms], np.int64)
        return server_ms - offs

    def describe(self) -> str:
        if self.rule == "ny+7":
            return "UTC+2 in winter, UTC+3 in US summer time (New York + 7 h)"
        return f"UTC{self.fixed_ms / MS_PER_HOUR:+g} h all year"


# -- one instrument ---------------------------------------------------------------


class Track:
    """One instrument being recorded: its broker symbol, its feed folder and
    how far recording and history have got."""

    def __init__(self, inst: Instrument, broker_symbol: str, digits: int, feed: Path) -> None:
        self.inst = inst
        self.broker_symbol = broker_symbol
        self.digits = digits
        self.feed = feed
        self.store = FeedStore(feed, scale=max(inst.scale, 10**digits))
        self.lock: FileLock | None = None
        self.last_ms: int | None = None  # last recorded tick, UTC ms
        self.at_last = 0  # how many recorded ticks share that millisecond
        self.files: dict[date, object] = {}
        self.ticks_today = 0
        self.day: date | None = None
        self.history = {"state": "off", "first_day": None, "pending": None, "floor": None, "days": 0}
        self.todo: list[date] | None = None  # missing history days, newest first
        self.todo_at = 0.0
        self.empty_run = 0
        self.empty_from: date | None = None

    def status(self) -> dict:
        return {"symbol": self.inst.symbol, "label": self.inst.label, "broker_symbol": self.broker_symbol,
                "last_tick_ms": self.last_ms, "ticks_today": self.ticks_today, "history": dict(self.history)}


class MT5Recorder:
    def __init__(
        self,
        terminal_path: str,
        symbol: str | None = None,
        root: Path = config.MT5_DIR,
        backfill_days: float = 3.0,
        api=None,
        clock=time.time,
        terminals=running_terminals,
        offset_wait: float = 20.0,
        symbols: list[str] | None = None,
        history_from: Callable[[str], date | None] | None = None,
    ) -> None:
        self.terminal_path = terminal_path
        self.offset_wait = offset_wait
        self.symbols = [symbol] if symbol else list(symbols or INSTRUMENTS)
        self.root = Path(root)
        self.backfill_days = backfill_days
        self.history_from = history_from
        self._api = api
        self.clock = clock
        self.terminals = terminals
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.status = {"state": "stopped", "message": "", "terminal": terminal_path}
        self.server_dir: Path | None = None
        self.tracks: list[Track] = []
        self.offset_ms: int | None = None
        self.server_clock: ServerClock | None = None
        #: The instrument whose ticks set the clock: EUR/USD when the broker
        #: has it, being the busiest.
        self.broker_symbol: str | None = None
        self._status_at = 0.0
        self._history_turn = 0

    # -- control -------------------------------------------------------------

    @property
    def api(self):
        if self._api is None:
            import MetaTrader5

            self._api = MetaTrader5
        return self._api

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="mt5-recorder", daemon=True)
        self.thread.start()

    def stop(self, wait: float = 5.0) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(wait)

    def track(self, symbol: str) -> Track | None:
        return next((t for t in self.tracks if t.inst.symbol == symbol), None)

    def _set(self, state: str, message: str = "", **extra) -> None:
        self.status.update({"state": state, "message": message, **extra})
        self._write_status(force=True)

    def _write_status(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._status_at < STATUS_EVERY_SECONDS:
            return
        self._status_at = now
        lasts = [t.last_ms for t in self.tracks if t.last_ms]
        self.status.update({
            "terminal": self.terminal_path,
            "broker_symbol": self.broker_symbol,
            "offset_hours": None if self.offset_ms is None else self.offset_ms / 3_600_000,
            "history_clock": self.server_clock.describe() if self.server_clock else None,
            "last_tick_ms": max(lasts) if lasts else None,
            "ticks_today": sum(t.ticks_today for t in self.tracks),
            "updated_ms": int(now * 1000),
            "symbols": [t.status() for t in self.tracks],
            "feed": str(self.server_dir) if self.server_dir else None,
        })
        if self.server_dir is not None:
            path = self.server_dir / "status.json"
            tmp = path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(self.status, indent=1), encoding="utf-8")
                tmp.replace(path)
            except OSError:
                pass  # a reader had it open; the next write will do

    # -- main loop -----------------------------------------------------------

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if not any(same_path(p, self.terminal_path) for p in self.terminals()):
                    self._set("waiting", f"Open {Path(self.terminal_path).parent.name} to start recording. "
                                         "The recorder never starts a terminal itself.")
                    self.stop_event.wait(5)
                    continue
                self._session()
            except TerminalGone:
                self._set("waiting", "The terminal was closed. Recording resumes when it is open again.")
                self.stop_event.wait(5)
            except Exception as exc:  # keep recording through anything
                self._set("error", f"{type(exc).__name__}: {exc}")
                self.stop_event.wait(10)
            finally:
                for t in self.tracks:
                    self._close_files(t)
                    if t.lock:
                        t.lock.release()
                        t.lock = None
                try:
                    self.api.shutdown()
                except Exception:
                    pass
        self._set("stopped", "Recorder stopped.")

    def _session(self) -> None:
        api = self.api
        self._set("connecting", "Attaching to the terminal")
        if not api.initialize(path=self.terminal_path):
            raise RuntimeError(f"Could not attach to the terminal: {api.last_error()}")
        account = api.account_info()
        if account is None:
            raise RuntimeError("The terminal is not logged in to an account")
        mode = TRADE_MODES.get(int(account.trade_mode), str(account.trade_mode))
        self.server_dir = self.root / slug(account.server)
        self.status.update({"server": account.server, "company": account.company, "account_type": mode})

        self.tracks = []
        missing = []
        for symbol in self.symbols:
            inst = INSTRUMENTS[symbol]
            name = self._find_symbol(inst)
            if name is None:
                missing.append(inst.label)
                continue
            info = api.symbol_info(name)
            api.symbol_select(name, True)
            track = Track(inst, name, int(info.digits) if info is not None else inst.digits,
                          feed_dir_for(account.server, inst.symbol, self.root))
            (track.feed / "live").mkdir(parents=True, exist_ok=True)
            track.lock = FileLock(track.feed / "recorder.lock")
            if not track.lock.acquire():
                track.lock = None
                missing.append(f"{inst.label} (another recorder is writing it)")
                continue
            manifest = track.store.manifest()
            manifest.update({"symbol": inst.symbol, "source": "mt5", "server": account.server,
                             "company": account.company, "broker_symbol": name, "scale": track.store.scale})
            track.store.save_manifest(manifest)
            self.tracks.append(track)
        if not self.tracks:
            raise RuntimeError("This account has none of the simulator's instruments")
        self.status["missing"] = missing
        clock_track = self.track(config.SYMBOL) or self.tracks[0]
        self.broker_symbol = clock_track.broker_symbol

        measured = self._measure_offset()
        self.offset_ms = measured if measured is not None else clock_track.store.manifest().get("offset_ms")
        for t in self.tracks:
            self._load_last_recorded(t)
            self._compact_closed_days(t)

        while self.offset_ms is None and not self.stop_event.is_set():
            self._set("waiting", "Market quiet: waiting for a live tick to read the server clock")
            self.stop_event.wait(30)
            self.offset_ms = self._measure_offset()
        if self.stop_event.is_set():
            return
        self.server_clock = ServerClock(self.offset_ms, int(self.clock() * 1000))
        self._save_offset()

        self._set("backfilling", "Filling the gap from the terminal's tick history")
        for t in self.tracks:
            self._backfill(t)
            self._roll_day(t)
        names = ", ".join(t.broker_symbol for t in self.tracks)
        self._set("recording", f"Recording {names} from {account.server} ({mode})")

        checked = self.clock()
        while not self.stop_event.is_set():
            now = self.clock()
            last = clock_track.last_ms
            quiet = last is None or now * 1000 - last > REMEASURE_AFTER_MS
            if quiet or now - checked > OFFSET_CHECK_SECONDS:
                checked = now
                fresh = self._measure_offset()
                if fresh is not None and fresh != self.offset_ms:
                    self.offset_ms = fresh
                    self.server_clock = ServerClock(fresh, int(now * 1000))
                    self._save_offset()
            for t in self.tracks:
                ticks = api.copy_ticks_from(t.broker_symbol, self._server_seconds(t.last_ms), 100_000, api.COPY_TICKS_ALL)
                if ticks is None:
                    if api.terminal_info() is None:
                        raise TerminalGone()
                    ticks = []
                self._append(t, ticks)
                self._roll_day(t)
            busy = self._history_step()
            self._write_status()
            self.stop_event.wait(0.05 if busy else POLL_SECONDS)

    # -- pieces --------------------------------------------------------------

    def _find_symbol(self, inst: Instrument) -> str | None:
        """The broker's name for an instrument: its own name, a known alias,
        or the shortest symbol containing one (EURUSD.r, USOIL.cash)."""
        api = self.api
        for name in (inst.symbol, *inst.mt5_names):
            if api.symbol_info(name) is not None:
                return name
        if inst.kind == "fx":
            candidates = api.symbols_get(f"*{inst.symbol}*") or []
            matches = [s for s in candidates
                       if getattr(s, "currency_base", "") == inst.base and getattr(s, "currency_profit", "") == inst.quote]
        else:
            matches = []
            for alias in inst.mt5_names:
                matches += [s for s in api.symbols_get(f"*{alias}*") or [] if alias.upper() in s.name.upper()]
        if not matches:
            return None
        return min(matches, key=lambda s: (len(s.name), s.name)).name

    def _measure_offset(self) -> int | None:
        """Watch the latest tick until it changes; the new one is seconds
        old, so its stamp minus now is the server offset. None if nothing
        arrives within `offset_wait` (market closed or quiet)."""
        def latest():
            t = self.api.symbol_info_tick(self.broker_symbol)
            return int(t.time_msc) if t is not None and getattr(t, "time_msc", 0) else None

        first = latest()
        deadline = self.clock() + self.offset_wait
        while self.clock() < deadline and not self.stop_event.is_set():
            self.stop_event.wait(0.2)
            msc = latest()
            if msc is not None and first is not None and msc != first:
                return measure_offset_ms(msc, int(self.clock() * 1000))
            first = first if first is not None else msc
        return None

    def _save_offset(self) -> None:
        for t in self.tracks:
            manifest = t.store.manifest()
            manifest["offset_ms"] = self.offset_ms
            manifest["history_clock"] = self.server_clock.rule if self.server_clock else None
            t.store.save_manifest(manifest)

    def _server_seconds(self, utc_ms: int | None) -> int:
        utc_ms = utc_ms if utc_ms is not None else int(self.clock() * 1000)
        return int((utc_ms + self.offset_ms) // 1000)

    def _load_last_recorded(self, t: Track) -> None:
        """Last tick already on disk: the newest open CSV, else the manifest."""
        t.last_ms, t.at_last = None, 0
        live = sorted((t.feed / "live").glob("*.csv"))
        for path in live:
            _trim_partial_line(path)
        for path in reversed(live):
            ts, _b, _a = read_csv_bytes(path.read_bytes())
            if len(ts):
                ms = ts // 1_000_000
                t.last_ms = int(ms[-1])
                t.at_last = int((ms == ms[-1]).sum())
                return
        days = t.store.manifest().get("days", {})
        lasts = [e["last_ns"] for e in days.values() if e.get("last_ns")]
        if lasts:
            t.last_ms = max(lasts) // 1_000_000
            t.at_last = 1

    def _backfill(self, t: Track) -> None:
        """Everything since the last recorded tick, or since the start of the
        UTC day `backfill_days` ago, so the first day is a whole one."""
        now_ms = int(self.clock() * 1000)
        start = (now_ms - int(self.backfill_days * MS_PER_DAY)) // MS_PER_DAY * MS_PER_DAY
        if t.last_ms is not None:
            start = max(start, t.last_ms)
        api = self.api
        while start < now_ms and not self.stop_event.is_set():
            end = min(start + BACKFILL_CHUNK_MS, now_ms)
            ticks = api.copy_ticks_range(t.broker_symbol, self._server_seconds(start),
                                         self._server_seconds(end) + 1, api.COPY_TICKS_ALL)
            if ticks is not None:
                self._append(t, ticks)
            start = end

    def _append(self, t: Track, ticks) -> None:
        """Write ticks after the last recorded one, in UTC, to their day's CSV."""
        if ticks is None or len(ticks) == 0:
            return
        msc = np.asarray(ticks["time_msc"], np.int64) - self.offset_ms
        bid = np.asarray(ticks["bid"], np.float64)
        ask = np.asarray(ticks["ask"], np.float64)
        keep = np.ones(len(msc), bool)
        if t.last_ms is not None:
            keep = msc > t.last_ms
            same = np.flatnonzero(msc == t.last_ms)
            keep[same[t.at_last:]] = True  # same millisecond, not yet written
        msc, bid, ask = msc[keep], bid[keep], ask[keep]
        if not len(msc):
            return
        order = np.argsort(msc, kind="stable")
        msc, bid, ask = msc[order], bid[order], ask[order]
        fmt = f"{{}},{{:.{t.digits}f}},{{:.{t.digits}f}}\n"
        day_idx = msc // MS_PER_DAY
        today = int(self.clock() * 1000) // MS_PER_DAY
        for d in np.unique(day_idx):
            m = day_idx == d
            day = date(1970, 1, 1) + timedelta(days=int(d))
            fh = self._file(t, day)
            fh.write("".join(fmt.format(int(a), float(b), float(c)) for a, b, c in zip(msc[m], bid[m], ask[m])))
            fh.flush()
            if d == today:
                t.ticks_today += int(m.sum())
        last = int(msc[-1])
        t.at_last = int((msc == last).sum()) + (t.at_last if last == t.last_ms else 0)
        t.last_ms = last

    def _file(self, t: Track, day: date):
        fh = t.files.get(day)
        if fh is None:
            fh = open(t.feed / "live" / f"{day.isoformat()}.csv", "a", encoding="ascii", newline="\n")
            t.files[day] = fh
        return fh

    def _close_files(self, t: Track) -> None:
        for fh in t.files.values():
            try:
                fh.close()
            except OSError:
                pass
        t.files = {}

    def _today(self) -> date:
        return date(1970, 1, 1) + timedelta(days=int(self.clock() * 1000) // MS_PER_DAY)

    def _roll_day(self, t: Track) -> None:
        """Close and compact every day before today (UTC)."""
        today = self._today()
        for day in [d for d in t.files if d < today]:
            t.files.pop(day).close()
        self._compact_closed_days(t, before=today)
        if t.day != today:
            t.day = today
            t.ticks_today = 0

    def _compact_closed_days(self, t: Track, before: date | None = None) -> None:
        """Clean every finished day's CSV with Layer 0's rules into Parquet."""
        before = before or self._today()
        for path in sorted((t.feed / "live").glob("*.csv")):
            day = date.fromisoformat(path.stem)
            if day >= before or day in t.files:
                continue
            ts, bid, ask = read_csv_bytes(path.read_bytes())
            ts, bid, ask, flag = flag_day(ts, bid, ask)
            if len(ts):
                old_ts, old_bid, old_ask, _f = t.store.read_day(day)
                if len(old_ts):  # a day recorded in two sittings
                    ts, bid, ask, flag = flag_day(np.r_[old_ts, ts], np.r_[old_bid, bid], np.r_[old_ask, ask])
                t.store.write_day(day, ts, bid, ask, flag)
            manifest = t.store.manifest()
            manifest.setdefault("days", {})[day.isoformat()] = day_entry("mt5", ts, flag, complete=True)
            manifest["days"] = dict(sorted(manifest["days"].items()))
            t.store.save_manifest(manifest)
            path.unlink()

    # -- history ---------------------------------------------------------------

    def _history_todo(self, t: Track) -> list[date]:
        """Weekdays from the start day to yesterday that the feed lacks,
        newest first, stopping where the broker's history was found to end."""
        start = self.history_from(t.inst.symbol) if self.history_from else None
        if start is None:
            return []
        manifest = t.store.manifest()
        floor = manifest.get("history_floor")
        if floor and date.fromisoformat(floor) > start:
            start = date.fromisoformat(floor)
        have = set(manifest.get("days", {}))
        have |= {p.stem for p in (t.feed / "live").glob("*.csv")}
        out = []
        d = self._today() - timedelta(days=1)
        while d >= start:
            if d.weekday() != 5 and d.isoformat() not in have:
                out.append(d)
            d -= timedelta(days=1)
        return out

    def _history_step(self) -> bool:
        """Fetch one missing day for the next instrument in turn. True while
        there is history left to fetch. Each instrument's list of missing
        days is worked out again every minute, so a start day changed in the
        page applies without restarting."""
        if self.history_from is None or not self.tracks or self.server_clock is None:
            return False
        now = self.clock()
        for k in range(len(self.tracks)):
            t = self.tracks[(self._history_turn + k) % len(self.tracks)]
            if t.todo is None or now - t.todo_at > 60:
                t.todo, t.todo_at = self._history_todo(t), now
            t.history["pending"] = len(t.todo)
            if not t.todo:
                t.history["state"] = "done" if self.history_from(t.inst.symbol) else "off"
                continue
            self._history_turn = (self._history_turn + k + 1) % len(self.tracks)
            t.history["state"] = "fetching"
            started = time.monotonic()
            while t.todo and time.monotonic() - started < HISTORY_BUDGET_SECONDS and not self.stop_event.is_set():
                self._history_day(t, t.todo.pop(0))
            t.history["pending"] = len(t.todo)
            return True
        return False

    def _read_day(self, t: Track, day: date) -> np.ndarray | None:
        """The broker's ticks for one UTC day, read until two reads agree."""
        a = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
        b = a + MS_PER_DAY
        frm = self.server_clock.to_server(a) // 1000
        to = self.server_clock.to_server(b) // 1000
        prev = None
        for _ in range(HISTORY_SETTLE_TRIES):
            ticks = self.api.copy_ticks_range(t.broker_symbol, frm, to, self.api.COPY_TICKS_ALL)
            n = 0 if ticks is None else len(ticks)
            if prev is not None and n == prev:
                return ticks
            prev = n
            if self.stop_event.wait(HISTORY_SETTLE_WAIT if n else 2 * HISTORY_SETTLE_WAIT):
                return None
        return ticks

    def _history_day(self, t: Track, day: date) -> None:
        ticks = self._read_day(t, day)
        if ticks is None:
            return
        entry = {"source": "mt5-history", "ticks": 0, "complete": True}
        if len(ticks):
            utc = self.server_clock.to_utc(np.asarray(ticks["time_msc"], np.int64))
            a = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
            inside = (utc >= a) & (utc < a + MS_PER_DAY)
            ts = utc[inside] * 1_000_000
            bid = np.asarray(ticks["bid"], np.float64)[inside]
            ask = np.asarray(ticks["ask"], np.float64)[inside]
            order = np.argsort(ts, kind="stable")
            ts, bid, ask, flag = flag_day(ts[order], bid[order], ask[order])
            if len(ts):
                t.store.write_day(day, ts, bid, ask, flag)
                entry = day_entry("mt5-history", ts, flag, complete=True)
        manifest = t.store.manifest()
        manifest.setdefault("days", {})[day.isoformat()] = entry
        manifest["days"] = dict(sorted(manifest["days"].items()))
        if entry["ticks"]:
            t.empty_run, t.empty_from = 0, None
            t.history["days"] += 1
            if not t.history["first_day"] or day.isoformat() < t.history["first_day"]:
                t.history["first_day"] = day.isoformat()
        elif day.weekday() < 5:
            t.empty_run += 1
            t.empty_from = t.empty_from or day
            if t.empty_run >= HISTORY_EMPTY_RUN:
                # The broker keeps nothing older: remember where it ends, so
                # no later run asks again.
                manifest["history_floor"] = (t.empty_from + timedelta(days=1)).isoformat()
                t.history["floor"] = manifest["history_floor"]
                t.todo = []
        t.store.save_manifest(manifest)


def _trim_partial_line(path: Path) -> None:
    """Cut a half-written last line (the recorder was killed mid-write), so
    the next append starts on a fresh line."""
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        with open(path, "r+b") as fh:
            fh.truncate(data.rfind(b"\n") + 1)


def read_status(root: Path = config.MT5_DIR) -> list[dict]:
    """Status of every recorded server, newest first."""
    out = []
    for status_path in Path(root).glob("*/status.json"):
        try:
            s = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        s["feed_key"] = f"mt5:{status_path.parent.name}"
        out.append(s)
    return sorted(out, key=lambda s: s.get("updated_ms") or 0, reverse=True)
