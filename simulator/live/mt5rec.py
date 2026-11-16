"""Record ticks from a MetaTrader 5 terminal: live, and its history.

What it does, in order:
  1. Waits until the chosen terminal is running. It never starts one:
     starting a terminal restores its charts and can switch on an Expert
     Advisor that trades the account. It only attaches to a terminal the
     trader opened.
  2. Attaches, reads the account's trade server and finds the broker's name
     for each instrument (EURUSD, XAGUSD, USOIL.cash, ...). It only reads:
     it never calls an order function.
  3. Works out the server's clock. The MetaTrader5 package returns tick
     times in the trade server's time zone (commonly UTC+2/+3), and the
     offset changes with daylight saving, so it is read for every week of
     history from the broker's hourly candles (see `week_offsets`), which
     works with the market shut too, and checked against each new quote
     while the market moves.
  4. Fills the gap since the last recorded tick from the terminal (from the
     start of the UTC day `backfill_days` ago at most), then streams: every
     25 ms it reads each instrument's latest quote, and when one changed,
     copies the new ticks, converts them to UTC, appends them to
     `live/YYYY-MM-DD.csv` (flushed at once) and tells `on_ticks`, which
     wakes live sessions and the quote board the same moment.
  5. In a second thread, fetches older days from the broker's tick history,
     newest first, back to each instrument's start day, until the broker
     has nothing older. Calls to the terminal take turns (one connection),
     but the live poll never waits on a history day being worked out. A terminal keeps only a small buffer of recent ticks
     on disk; older ones are downloaded from the broker's server when asked
     for, which is what this does. A day is kept once its ticks cover every
     hour the broker has a candle for.
  6. When a UTC day closes, cleans it with Layer 0's rules and stores it as
     Parquet, like the replay feed.

The FX week opens at 17:00 New York on Sunday and closes at 17:00 on Friday
whatever the broker's clock, so where the hourly candles start after a
weekend and end before one gives that week's offset. Most MT5 brokers keep
New York + 7 hours (the week opens at 00:00 server time all year); some
follow European summer time or keep one offset. When every week read fits
one of those rules, the rule fills weeks the candles do not cover;
otherwise each week uses its own reading.

One feed per trade server and instrument: prices and spreads differ by
broker, so feeds are never mixed. A file lock stops two recorders writing
the same feed.
"""

from __future__ import annotations

import json
import re
import threading
from bisect import bisect_left
from collections import deque
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
#: How often each instrument's latest quote is read. Reading a quote is
#: cheap; ticks are copied only when it changed.
POLL_SECONDS = 0.025
STATUS_EVERY_SECONDS = 2.0
BACKFILL_CHUNK_MS = 6 * 3600 * 1000
#: The broker has no older history once this many weekdays in a row are empty.
HISTORY_EMPTY_RUN = 10
#: A read is accepted once two reads a moment apart return as many rows; the
#: terminal may still be downloading from the broker on the first.
HISTORY_SETTLE_TRIES = 8
HISTORY_SETTLE_WAIT = 0.3
#: A day that comes back empty, or with hours missing that have a candle, is
#: read once more after this long before anything is concluded.
HISTORY_RECHECK_WAIT = 2.0
#: A day still missing hours is left for a later pass this many times, then
#: kept as it is (the broker lacks those ticks).
HISTORY_DAY_TRIES = 3
#: How often each instrument's list of missing days is worked out again.
HISTORY_TODO_SECONDS = 60.0
#: Seconds of history work on one instrument before the next one's turn.
HISTORY_BUDGET_SECONDS = 0.5
#: The history thread's pause when nothing is left to fetch.
HISTORY_IDLE_SECONDS = 1.0
#: A day's open file is closed and compacted this long after midnight UTC,
#: so ticks stamped just before midnight that arrive just after still land
#: in their day.
ROLL_GRACE_MS = 10_000
#: The live delay shown is measured over this many seconds.
DELAY_WINDOW_SECONDS = 60.0
#: How often the clock is read again from the hourly candles (new weeks).
CLOCK_REFRESH_SECONDS = 3600.0
#: How often, while recording, the recorder checks the terminal still runs.
TERMINAL_CHECK_SECONDS = 5.0
#: The market counts as moving when its last quote is this recent.
ACTIVE_WITHIN_MS = 10 * 60 * 1000

TRADE_MODES = {0: "demo", 1: "contest", 2: "real"}


class TerminalGone(RuntimeError):
    """The terminal was closed; wait for it to come back."""


class _OneAtATime:
    """The MetaTrader5 module with one call at a time. The live poll and the
    history fetch share the process's one connection to the terminal from
    two threads; the module is not documented as safe for that."""

    def __init__(self, api, lock: threading.RLock) -> None:
        self._api, self._lock = api, lock
        #: (when, ms, name) of calls that held the terminal 50 ms or more:
        #: the live poll waited that long behind them.
        self.slow: deque[tuple[float, float, str]] = deque(maxlen=1000)

    def __getattr__(self, name):
        attr = getattr(self._api, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            with self._lock:
                t0 = time.perf_counter()
                try:
                    return attr(*args, **kwargs)
                finally:
                    ms = (time.perf_counter() - t0) * 1000
                    if ms >= 50:
                        self.slow.append((time.time(), ms, name))
        return call


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


def eu_summer(utc_ms: int) -> bool:
    """Whether Europe is on summer time (last Sunday of March to last Sunday
    of October, at 01:00 UTC)."""
    t = datetime.fromtimestamp(utc_ms / 1000, tz=timezone.utc)
    start, end = _last_sunday(t.year, 3), _last_sunday(t.year, 10)
    begin = datetime(start.year, start.month, start.day, 1, tzinfo=timezone.utc)
    finish = datetime(end.year, end.month, end.day, 1, tzinfo=timezone.utc)
    return begin <= t < finish


def eet_offset_ms(utc_ms: int) -> int:
    """Server offset of a broker on Eastern European time (UTC+2, +3 in
    European summer time)."""
    return (3 if eu_summer(utc_ms) else 2) * MS_PER_HOUR


def _sunday_open_ms(sunday: date) -> int:
    """17:00 New York on a Sunday, in UTC ms."""
    t = int(datetime(sunday.year, sunday.month, sunday.day, 21, tzinfo=timezone.utc).timestamp() * 1000)
    return t if us_dst(t) else t + MS_PER_HOUR


def week_open_ms(utc_ms: int) -> int:
    """When the FX week holding this instant opened: the last Sunday 17:00
    New York at or before it, in UTC ms. A weekend belongs to the week
    before it. The week closes 5 days after it opens (Friday 17:00)."""
    d = datetime.fromtimestamp(utc_ms / 1000, tz=timezone.utc).date()
    sunday = d - timedelta(days=(d.weekday() + 1) % 7)
    opened = _sunday_open_ms(sunday)
    return opened if opened <= utc_ms else _sunday_open_ms(sunday - timedelta(days=7))


#: Offsets a trade server can have: UTC-12 to UTC+14.
_MIN_OFFSET_MS, _MAX_OFFSET_MS = -12 * MS_PER_HOUR, 14 * MS_PER_HOUR
#: A pause between two hourly candles at least this long is a weekend (they
#: are 49 hours, open to open); holidays inside a week are shorter or are
#: rejected below because they do not open on a Sunday.
WEEKEND_GAP_MS = 40 * MS_PER_HOUR


def _week_from_open(server_ms: int) -> tuple[int, int] | None:
    """(week, offset) if a candle opening at this server time can be the
    first of a week: 17:00 New York on a Sunday, in whole hours."""
    week = week_open_ms(server_ms - _MIN_OFFSET_MS)
    off = server_ms - week
    return (week, off) if off <= _MAX_OFFSET_MS and off % MS_PER_HOUR == 0 else None


def _week_from_close(server_ms: int) -> tuple[int, int] | None:
    """(week, offset) if a candle ending at this server time can be the last
    of a week: 17:00 New York on a Friday."""
    week = week_open_ms(server_ms - _MIN_OFFSET_MS - 5 * MS_PER_DAY)
    off = server_ms - (week + 5 * MS_PER_DAY)
    return (week, off) if off <= _MAX_OFFSET_MS and off % MS_PER_HOUR == 0 else None


def week_offsets(bar_times_ms) -> dict[int, int]:
    """The server's offset in each trading week, from the opening times of a
    run of hourly candles (server time, ms): {week open in UTC ms: offset}.

    The first candle after a weekend starts at the week's open (Sunday 17:00
    New York) in server time, and the last one before a weekend ends at its
    close (Friday 17:00). A week counts only when its open and its close
    give the same offset: one that opened late or closed early for a holiday
    is left out rather than trusted."""
    t = np.unique(np.asarray(bar_times_ms, np.int64))
    opens: dict[int, int] = {}
    closes: dict[int, int] = {}
    for i in np.flatnonzero(np.diff(t) >= WEEKEND_GAP_MS):
        closed = _week_from_close(int(t[i]) + MS_PER_HOUR)
        if closed:
            closes[closed[0]] = closed[1]
        opened = _week_from_open(int(t[i + 1]))
        if opened:
            opens[opened[0]] = opened[1]
    return {w: off for w, off in sorted(opens.items()) if closes.get(w) == off}


#: Clock rules a broker may follow, tried in this order.
CLOCK_RULES = (("ny+7", ny7_offset_ms), ("eu", eet_offset_ms))


class ServerClock:
    """The broker's offset from UTC at any instant, past or present.

    Built from offsets read for some weeks ({week open in UTC ms: offset}).
    When all of them fit New York + 7 h, or European summer time at UTC+2/+3,
    or one offset, that rule gives every week; otherwise each week uses its
    own reading, and a week not read uses the nearest one that was."""

    def __init__(self, weeks: dict[int, int]) -> None:
        if not weeks:
            raise ValueError("a clock needs at least one week's offset")
        self.weeks = dict(sorted(weeks.items()))
        self._keys = list(self.weeks)
        values = set(self.weeks.values())
        self.fixed_ms = next(iter(values)) if len(values) == 1 else None
        self.rule = "weeks"
        self._fn = None
        for name, fn in CLOCK_RULES:
            if all(fn(w) == off for w, off in self.weeks.items()):
                self.rule, self._fn = name, fn
                break
        else:
            if self.fixed_ms is not None:
                self.rule = "fixed"

    @classmethod
    def measured(cls, offset_ms: int, now_ms: int) -> ServerClock:
        """A clock from one offset read off a live quote."""
        return cls({quote_week(now_ms): offset_ms})

    def week_offset(self, week: int) -> int:
        if self.rule == "fixed":
            return self.fixed_ms
        if self._fn is not None:
            return self._fn(week)
        if week in self.weeks:
            return self.weeks[week]
        i = bisect_left(self._keys, week)
        near = self._keys[max(i - 1, 0):i + 1]
        return self.weeks[min(near, key=lambda k: abs(k - week))]

    def offset(self, utc_ms: int) -> int:
        return self.week_offset(week_open_ms(utc_ms))

    def to_server(self, utc_ms: int) -> int:
        return utc_ms + self.offset(utc_ms)

    def to_utc(self, server_ms) -> np.ndarray:
        """Server stamps to UTC. Each stamp belongs to the last week that
        opened before it in server time: offsets change over a weekend,
        which is far longer than any change, so no tick is ambiguous."""
        server_ms = np.asarray(server_ms, np.int64)
        if self.rule == "fixed" or not len(server_ms):
            return server_ms - (self.fixed_ms or 0)
        week = week_open_ms(int(server_ms.min()) - _MAX_OFFSET_MS)
        last = int(server_ms.max()) - _MIN_OFFSET_MS
        weeks = []
        while week <= last:
            weeks.append(week)
            week = week_open_ms(week + 7 * MS_PER_DAY + 2 * MS_PER_HOUR)
        offs = np.array([self.week_offset(w) for w in weeks], np.int64)
        starts = np.array(weeks, np.int64) + offs
        i = np.clip(np.searchsorted(starts, server_ms, side="right") - 1, 0, len(weeks) - 1)
        return server_ms - offs[i]

    def describe(self) -> str:
        if self.rule == "ny+7":
            text = "UTC+2 in winter, UTC+3 in US summer time (New York + 7 h)"
        elif self.rule == "eu":
            text = "UTC+2 in winter, UTC+3 in European summer time"
        elif self.rule == "fixed":
            text = f"UTC{self.fixed_ms / MS_PER_HOUR:+g} h all year"
        else:
            lo, hi = min(self.weeks.values()), max(self.weeks.values())
            text = f"read week by week, UTC{lo / MS_PER_HOUR:+g} to UTC{hi / MS_PER_HOUR:+g} h"
        n = len(self.weeks)
        return text + (f"; fits all {n} weeks read from the broker's candles" if n > 1 else "")


def quote_week(now_ms: int) -> int:
    """The week a live quote at this instant belongs to. Past Friday's close
    it is the coming week's: some brokers quote minutes before the open."""
    week = week_open_ms(now_ms)
    if now_ms - week >= 5 * MS_PER_DAY:
        week = week_open_ms(week + 7 * MS_PER_DAY + 2 * MS_PER_HOUR)
    return week


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
        self.tries: dict[date, int] = {}  # passes a day came back with hours missing
        self.seen: tuple | None = None  # the latest quote already copied: (time_msc, bid, ask)
        self.quote: dict | None = None  # the latest tick recorded, for the quote board
        self.rolled_at = 0.0

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
        on_ticks: Callable[[str], None] | None = None,
    ) -> None:
        self.terminal_path = terminal_path
        #: Called from the recorder thread with an instrument's symbol each
        #: time new live ticks of it are on disk.
        self.on_ticks = on_ticks
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
        self.offset_ms: int | None = None  # the server's offset now
        self.server_clock: ServerClock | None = None
        #: Offsets read from the hourly candles, and from live quotes (which
        #: win), by week.
        self.weeks: dict[int, int] = {}
        self.live_weeks: dict[int, int] = {}
        #: The instrument whose candles and quotes set the clock: EUR/USD
        #: when the broker has it, being the busiest; always a currency
        #: pair, which keeps the FX week.
        self.broker_symbol: str | None = None
        self._clock_track: Track | None = None
        self._seen_quote: tuple[int, float] | None = None  # (time_msc, when seen)
        self._status_at = 0.0
        self._history_turn = 0
        self._api_lock = threading.RLock()
        self._one_at_a_time: _OneAtATime | None = None
        #: Set when the current attachment ends, so the history thread stops.
        self._halt = threading.Event()
        self._history_thread: threading.Thread | None = None
        #: (when received, delay in ms) of each live tick, and (when, gap in
        #: ms) between live polls, for the delay shown in the page.
        self._delays: deque[tuple[float, float]] = deque(maxlen=50_000)
        self._gaps: deque[tuple[float, float]] = deque(maxlen=10_000)

    # -- control -------------------------------------------------------------

    @property
    def api(self):
        if self._one_at_a_time is None:
            if self._api is None:
                import MetaTrader5

                self._api = MetaTrader5
            self._one_at_a_time = _OneAtATime(self._api, self._api_lock)
        return self._one_at_a_time

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="mt5-recorder", daemon=True)
        self.thread.start()

    def stop(self, wait: float = 5.0) -> None:
        self.stop_event.set()
        self._halt.set()
        if self.thread:
            self.thread.join(wait)

    def _pause(self, seconds: float) -> bool:
        """Wait in the history thread; True when it should stop."""
        return self._halt.wait(seconds) or self.stop_event.is_set()

    def quotes(self) -> list[dict]:
        """The latest recorded tick of every instrument, for the quote board."""
        return [{"symbol": t.inst.symbol, "label": t.inst.label, "broker_symbol": t.broker_symbol,
                 "digits": t.digits, "pip": t.inst.pip, "ticks_today": t.ticks_today, **(t.quote or {})}
                for t in self.tracks]

    def live_stats(self) -> dict:
        """How long live ticks took from the broker's stamp to the file, and
        the longest pause between two polls, over the last minute."""
        since = self.clock() - DELAY_WINDOW_SECONDS
        delays = np.array([d for at, d in list(self._delays) if at >= since])
        gaps = [g for at, g in list(self._gaps) if at >= since]
        slow = [(ms, name) for at, ms, name in list(self._one_at_a_time.slow if self._one_at_a_time else []) if at >= since]
        longest = max(slow) if slow else None
        return {
            "slowest_call_ms": round(longest[0]) if longest else None,
            "slowest_call": longest[1] if longest else None,
            "ticks": int(len(delays)),
            "delay_ms_median": round(float(np.median(delays))) if len(delays) else None,
            "delay_ms_p90": round(float(np.percentile(delays, 90))) if len(delays) else None,
            "delay_ms_max": round(float(delays.max())) if len(delays) else None,
            "poll_gap_ms_max": round(max(gaps)) if gaps else None,
            "poll_ms": round(POLL_SECONDS * 1000),
        }

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
        if self.server_clock is not None:
            self.offset_ms = self.server_clock.offset(int(now * 1000))
        self.status.update({
            "live": self.live_stats(),
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
            with track.store.lock:
                manifest = track.store.manifest()
                manifest.update({"symbol": inst.symbol, "source": "mt5", "server": account.server,
                                 "company": account.company, "broker_symbol": name, "scale": track.store.scale})
                track.store.save_manifest(manifest)
            self.tracks.append(track)
        if not self.tracks:
            raise RuntimeError("This account has none of the simulator's instruments")
        self.status["missing"] = missing
        fx = [t for t in self.tracks if t.inst.kind == "fx"]
        clock_track = self.track(config.SYMBOL) or (fx[0] if fx else self.tracks[0])
        self._clock_track = clock_track
        self.broker_symbol = clock_track.broker_symbol
        self._halt = threading.Event()
        if self.stop_event.is_set():
            self._halt.set()
        for t in self.tracks:
            self._load_last_recorded(t)
            self._compact_closed_days(t)

        self._set("connecting", f"Reading {account.server}'s clock from its {clock_track.broker_symbol} hourly candles")
        self.weeks, self.live_weeks, self._seen_quote = self._read_weeks(), {}, None
        self.server_clock = ServerClock(self.weeks) if self.weeks else None
        if self._market_moving():
            live = self._measure_offset()
            if live is not None:
                self._confirm_offset(live, int(self.clock() * 1000))
        while self.server_clock is None and not self.stop_event.is_set():
            # Only without candles to read: wait for the market to move.
            for t in self.tracks:
                t.history["state"] = "clock"
            self._set("waiting", f"Waiting for a {clock_track.broker_symbol} quote to read the server clock: "
                                 "the terminal returned no hourly candles to read it from")
            self.stop_event.wait(10)
            self._check_terminal()
            self.weeks = self._read_weeks()
            if self.weeks:
                self.server_clock = ServerClock(self.weeks)
                break
            live = self._measure_offset()
            if live is not None:
                self._confirm_offset(live, int(self.clock() * 1000))
        if self.stop_event.is_set():
            return
        self._use_clock()

        self._set("backfilling", "Filling the gap from the terminal's tick history")
        for t in self.tracks:
            self._backfill(t)
            self._roll_day(t)
        names = ", ".join(t.broker_symbol for t in self.tracks)
        self._set("recording", f"Streaming {names} from {account.server} ({mode})")

        self._history_thread = threading.Thread(target=self._history_loop, name="mt5-history", daemon=True)
        self._history_thread.start()
        try:
            checked = last_poll = self.clock()
            while not self.stop_event.is_set():
                now = self.clock()
                self._gaps.append((now, (now - last_poll) * 1000))
                last_poll = now
                if now - checked > TERMINAL_CHECK_SECONDS:
                    checked = now
                    self._check_terminal()
                for t in self.tracks:
                    self._poll(t)
                self._write_status()
                self.stop_event.wait(POLL_SECONDS)
        finally:
            self._halt.set()
            self._history_thread.join()

    def _poll(self, t: Track) -> None:
        """Copy an instrument's new ticks if its latest quote changed."""
        api = self.api
        q = api.symbol_info_tick(t.broker_symbol)
        if q is None or not getattr(q, "time_msc", 0):
            if api.terminal_info() is None:
                raise TerminalGone()
            return
        seen = (int(q.time_msc), float(q.bid), float(q.ask))
        if seen == t.seen:
            self._roll_day(t)
            return
        if t is self._clock_track:
            self._watch_clock(seen[0])
        ticks = api.copy_ticks_from(t.broker_symbol, self._server_seconds(t.last_ms), 100_000, api.COPY_TICKS_ALL)
        if ticks is None:
            if api.terminal_info() is None:
                raise TerminalGone()
            return
        self._roll_day(t)
        self._append(t, ticks, live=True)
        # Only once the quote's own tick is on disk: the terminal may show a
        # quote a moment before its tick can be copied.
        if t.last_ms is not None and t.last_ms >= int(self.server_clock.to_utc([seen[0]])[0]):
            t.seen = seen

    def _history_loop(self) -> None:
        """The history thread: broker history day by day, and the clock read
        again from the candles every hour, until the attachment ends."""
        clock_read = self.clock()
        while not self._pause(0):
            try:
                if self.clock() - clock_read > CLOCK_REFRESH_SECONDS:
                    clock_read = self.clock()
                    weeks = self._read_weeks()
                    if weeks and weeks != self.weeks:
                        self.weeks = weeks
                        self._use_clock(rebuild=True)
                busy = self._history_step()
                self.status.pop("history_error", None)
            except Exception as exc:  # the live stream goes on regardless
                self.status["history_error"] = f"{type(exc).__name__}: {exc}"
                busy = False
            if self._pause(0.0 if busy else HISTORY_IDLE_SECONDS):
                return

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

    def _check_terminal(self) -> None:
        """Raise TerminalGone once the trader has closed the terminal."""
        if not any(same_path(p, self.terminal_path) for p in self.terminals()) or self.api.terminal_info() is None:
            raise TerminalGone()

    def _settled(self, read):
        """Read until two reads a moment apart return as many rows: the
        terminal may still be downloading from the broker on the first.
        None when the terminal did not answer."""
        prev = out = None
        for _ in range(HISTORY_SETTLE_TRIES):
            out = read()
            n = None if out is None else len(out)
            if n is not None and n == prev:
                return out
            prev = n
            if self._pause(HISTORY_SETTLE_WAIT if n else 2 * HISTORY_SETTLE_WAIT):
                return None
        return out

    def _rates(self, symbol: str, from_s: int, to_s: int):
        """Hourly candles opening from `from_s` to `to_s` (server seconds)."""
        api = self.api
        return self._settled(lambda: api.copy_rates_range(symbol, api.TIMEFRAME_H1, from_s, to_s))

    def _read_weeks(self) -> dict[int, int]:
        """The server's offset in every week from the earliest start day to
        now, from the clock instrument's hourly candles. Empty when the
        terminal has none, or the clock instrument is not a currency pair."""
        t = self._clock_track
        if t is None or t.inst.kind != "fx":
            return {}
        starts = [self.history_from(x.inst.symbol) for x in self.tracks] if self.history_from else []
        first = min([d for d in starts if d], default=self._today() - timedelta(days=int(self.backfill_days) + 21))
        from_s = int(datetime(first.year, first.month, first.day, tzinfo=timezone.utc).timestamp()) - 8 * 86_400
        rates = self._rates(t.broker_symbol, from_s, int(self.clock()) + 86_400)
        if rates is None or not len(rates):
            return {}
        return week_offsets(np.asarray(rates["time"], np.int64) * 1000)

    def _latest_quote_msc(self) -> int | None:
        t = self.api.symbol_info_tick(self.broker_symbol)
        return int(t.time_msc) if t is not None and getattr(t, "time_msc", 0) else None

    def _market_moving(self) -> bool:
        """Whether the clock instrument quoted in the last few minutes (by
        the clock read from the candles); always True without one."""
        if self.server_clock is None:
            return True
        msc = self._latest_quote_msc()
        if msc is None:
            return False
        return int(self.clock() * 1000) - int(self.server_clock.to_utc([msc])[0]) < ACTIVE_WITHIN_MS

    def _confirm_offset(self, offset_ms: int, now_ms: int) -> None:
        """Take an offset read off a live quote as this week's."""
        week = quote_week(now_ms)
        if self.live_weeks.get(week) == offset_ms and self.server_clock is not None:
            return
        self.live_weeks[week] = offset_ms
        if self.server_clock is None or self.server_clock.offset(now_ms) != offset_ms:
            self._use_clock(rebuild=True)

    def _use_clock(self, rebuild: bool = False) -> None:
        if rebuild:
            self.server_clock = ServerClock({**self.weeks, **self.live_weeks})
        self.offset_ms = self.server_clock.offset(int(self.clock() * 1000))
        self._save_offset()

    def _watch_clock(self, msc: int) -> None:
        """Check the clock against the clock instrument's latest quote,
        without waiting: a quote that changed since the last poll is
        moments old, so its stamp minus now is the offset. Runs before new
        ticks are converted, so the first quotes of a week whose offset the
        clock did not foresee are converted with the right one."""
        now = self.clock()
        seen = self._seen_quote
        if seen is not None and msc > seen[0] and now - seen[1] < 60:
            offset = measure_offset_ms(msc, int(now * 1000))
            if offset is not None:
                self._confirm_offset(offset, int(now * 1000))
        self._seen_quote = (msc, now)

    def _save_offset(self) -> None:
        for t in self.tracks:
            with t.store.lock:
                manifest = t.store.manifest()
                manifest["offset_ms"] = self.offset_ms
                manifest["history_clock"] = self.server_clock.rule if self.server_clock else None
                t.store.save_manifest(manifest)

    def _server_seconds(self, utc_ms: int | None) -> int:
        utc_ms = utc_ms if utc_ms is not None else int(self.clock() * 1000)
        return int(self.server_clock.to_server(utc_ms) // 1000)

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

    def _append(self, t: Track, ticks, live: bool = False) -> None:
        """Write ticks after the last recorded one, in UTC, to their day's CSV.
        Live ones are timed (broker stamp to file) and announced."""
        if ticks is None or len(ticks) == 0:
            return
        msc = self.server_clock.to_utc(np.asarray(ticks["time_msc"], np.int64))
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
        received = self.clock()
        t.quote = {"time_ms": last, "bid": float(bid[-1]), "ask": float(ask[-1]), "received_ms": int(received * 1000)}
        if live:
            self._delays.extend((received, received * 1000 - float(m)) for m in msc[-200:])
            if self.on_ticks is not None:
                try:
                    self.on_ticks(t.inst.symbol)
                except Exception:
                    pass  # a listener's trouble never stops the recording

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
        """Close and compact every day before today (UTC), once a few
        seconds into the new day. Checked at most once a second."""
        now = self.clock()
        today = self._today()
        if t.day == today and now - t.rolled_at < 1.0:
            return
        t.rolled_at = now
        settled = date(1970, 1, 1) + timedelta(days=(int(now * 1000) - ROLL_GRACE_MS) // MS_PER_DAY)
        for day in [d for d in t.files if d < settled]:
            t.files.pop(day).close()
        self._compact_closed_days(t, before=settled)
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
            with t.store.lock:
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
            if t.todo is None or now - t.todo_at > HISTORY_TODO_SECONDS:
                t.todo, t.todo_at = self._history_todo(t), now
            t.history["pending"] = len(t.todo)
            if not t.todo:
                t.history["state"] = "done" if self.history_from(t.inst.symbol) else "off"
                continue
            self._history_turn = (self._history_turn + k + 1) % len(self.tracks)
            t.history["state"] = "fetching"
            started = time.monotonic()
            while t.todo and time.monotonic() - started < HISTORY_BUDGET_SECONDS and not self._halt.is_set():
                self._history_day(t, t.todo.pop(0))
            t.history["pending"] = len(t.todo)
            return True
        return False

    def _read_day(self, t: Track, day: date):
        """The broker's ticks for one UTC day, and the server hours it has a
        candle for that have no tick. None when the terminal did not answer."""
        a = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
        frm = self.server_clock.to_server(a) // 1000
        to = self.server_clock.to_server(a + MS_PER_DAY) // 1000
        rates = self._rates(t.broker_symbol, frm, to - 1)
        ticks = self._settled(lambda: self.api.copy_ticks_range(t.broker_symbol, frm, to, self.api.COPY_TICKS_ALL))
        if rates is None or ticks is None:
            return None
        hours = np.asarray(rates["time"], np.int64) * 1000
        if "tick_volume" in (rates.dtype.names or ()):
            hours = hours[np.asarray(rates["tick_volume"]) > 0]
        ticked = np.unique(np.asarray(ticks["time_msc"], np.int64) // MS_PER_HOUR * MS_PER_HOUR)
        return ticks, np.setdiff1d(hours, ticked)

    def _history_day(self, t: Track, day: date) -> None:
        """Fetch and store one day. An empty day, or one with hours missing,
        is read again a moment later (the terminal may still be downloading
        it); one still missing hours is left for a later pass, a few times,
        then kept as it is."""
        read = self._read_day(t, day)
        if read is not None and (len(read[1]) or not len(read[0])):
            if self._pause(HISTORY_RECHECK_WAIT):
                return
            read = self._read_day(t, day)
        if read is None:
            return  # the terminal did not answer; the day comes round again
        ticks, missing = read
        if len(missing):
            t.tries[day] = t.tries.get(day, 0) + 1
            if t.tries[day] < HISTORY_DAY_TRIES:
                return
        t.tries.pop(day, None)
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
        if len(missing):
            entry["missing_hours"] = int(len(missing))
        with t.store.lock:
            self._record_history_day(t, day, entry)

    def _record_history_day(self, t: Track, day: date, entry: dict) -> None:
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
