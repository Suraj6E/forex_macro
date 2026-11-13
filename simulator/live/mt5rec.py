"""Record live ticks from a MetaTrader 5 terminal into a feed.

What it does, in order:
  1. Waits until the chosen terminal is running. It never starts one:
     starting a terminal restores its charts and can switch on an Expert
     Advisor that trades the account. It only attaches to a terminal the
     trader opened.
  2. Attaches, reads the account's trade server and finds the broker's name
     for EUR/USD. It only reads: it never calls an order function.
  3. Works out the server's clock offset. The MetaTrader5 package returns
     tick times in the trade server's time zone (commonly UTC+2/+3), so
     offset = round(latest tick time - now, to 30 min) while the market is
     moving, and is re-checked every 10 minutes (daylight saving).
  4. Fills the gap since the last recorded tick from the terminal's own tick
     history (up to `backfill_days`), so stopping the recorder for a while
     does not leave a hole.
  5. Polls new ticks twice a second and appends them, converted to UTC, to
     `live/YYYY-MM-DD.csv`, flushing every poll.
  6. When a UTC day closes, cleans it with Layer 0's rules and stores it as
     Parquet, like the replay feed.

One feed per trade server: prices and spreads differ by broker, so feeds
are never mixed. A file lock stops two recorders writing the same feed.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from simulator import config
from simulator.data.quality import flag_day
from simulator.data.ticks import FeedStore, day_entry
from simulator.live.feed import read_csv_bytes
from simulator.live.winproc import FileLock, running_terminals, same_path

MS_PER_DAY = 86_400_000
POLL_SECONDS = 0.5
OFFSET_CHECK_SECONDS = 600
STATUS_EVERY_SECONDS = 2.0
BACKFILL_CHUNK_MS = 6 * 3600 * 1000

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


class MT5Recorder:
    def __init__(
        self,
        terminal_path: str,
        symbol: str = config.SYMBOL,
        root: Path = config.MT5_DIR,
        backfill_days: float = 3.0,
        api=None,
        clock=time.time,
        terminals=running_terminals,
        offset_wait: float = 20.0,
    ) -> None:
        self.terminal_path = terminal_path
        self.offset_wait = offset_wait
        self.symbol = symbol
        self.root = Path(root)
        self.backfill_days = backfill_days
        self._api = api
        self.clock = clock
        self.terminals = terminals
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.status = {"state": "stopped", "message": "", "terminal": terminal_path}
        self.feed: Path | None = None
        self.store: FeedStore | None = None
        self.offset_ms: int | None = None
        self.broker_symbol: str | None = None
        self.digits = 5
        self.last_ms: int | None = None  # last recorded tick, UTC ms
        self.at_last = 0  # how many recorded ticks share that millisecond
        self._files: dict[date, object] = {}
        self._lock: FileLock | None = None
        self._ticks_today = 0
        self._status_at = 0.0

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

    def _set(self, state: str, message: str = "", **extra) -> None:
        self.status.update({"state": state, "message": message, **extra})
        self._write_status(force=True)

    def _write_status(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._status_at < STATUS_EVERY_SECONDS:
            return
        self._status_at = now
        self.status.update({
            "terminal": self.terminal_path,
            "broker_symbol": self.broker_symbol,
            "offset_hours": None if self.offset_ms is None else self.offset_ms / 3_600_000,
            "last_tick_ms": self.last_ms,
            "ticks_today": self._ticks_today,
            "updated_ms": int(now * 1000),
            "feed": str(self.feed) if self.feed else None,
        })
        if self.feed is not None:
            path = self.feed / "status.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.status, indent=1), encoding="utf-8")
            tmp.replace(path)

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
                self._close_files()
                try:
                    self.api.shutdown()
                except Exception:
                    pass
                if self._lock:
                    self._lock.release()
                    self._lock = None
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
        self.broker_symbol = self._find_symbol()
        info = api.symbol_info(self.broker_symbol)
        self.digits = int(info.digits) if info is not None else 5
        api.symbol_select(self.broker_symbol, True)

        self.feed = feed_dir_for(account.server, self.symbol, self.root)
        self.store = FeedStore(self.feed)
        (self.feed / "live").mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(self.feed / "recorder.lock")
        if not self._lock.acquire():
            self._lock = None
            raise RuntimeError("Another recorder is already writing this feed")
        self.status.update({"server": account.server, "company": account.company, "account_type": mode})

        manifest = self.store.manifest()
        manifest.update({"symbol": self.symbol, "source": "mt5", "server": account.server,
                         "company": account.company, "broker_symbol": self.broker_symbol})
        self.store.save_manifest(manifest)
        measured = self._measure_offset()
        self.offset_ms = measured if measured is not None else manifest.get("offset_ms")
        self._load_last_recorded()
        self._compact_closed_days()

        while self.offset_ms is None and not self.stop_event.is_set():
            self._set("waiting", "Market quiet: waiting for a live tick to read the server clock")
            self.stop_event.wait(30)
            self.offset_ms = self._measure_offset()
        if self.stop_event.is_set():
            return
        self._save_offset()

        self._set("backfilling", "Filling the gap from the terminal's tick history")
        self._backfill()
        self._roll_day()
        self._set("recording", f"Recording {self.broker_symbol} from {account.server} ({mode})")

        checked = self.clock()
        while not self.stop_event.is_set():
            now = self.clock()
            quiet = self.last_ms is None or now * 1000 - self.last_ms > REMEASURE_AFTER_MS
            if quiet or now - checked > OFFSET_CHECK_SECONDS:
                checked = now
                fresh = self._measure_offset()
                if fresh is not None and fresh != self.offset_ms:
                    self.offset_ms = fresh
                    self._save_offset()
            ticks = api.copy_ticks_from(self.broker_symbol, self._server_seconds(self.last_ms), 100_000, api.COPY_TICKS_ALL)
            if ticks is None:
                if api.terminal_info() is None:
                    raise TerminalGone()
                ticks = []
            self._append(ticks)
            self._roll_day()
            self._write_status()
            self.stop_event.wait(POLL_SECONDS)

    # -- pieces --------------------------------------------------------------

    def _find_symbol(self) -> str:
        api = self.api
        exact = api.symbol_info(self.symbol)
        if exact is not None:
            return self.symbol
        candidates = api.symbols_get(f"*{self.symbol}*") or []
        base, quote = self.symbol[:3], self.symbol[3:]
        matches = [s for s in candidates if getattr(s, "currency_base", "") == base and getattr(s, "currency_profit", "") == quote]
        if not matches:
            raise RuntimeError(f"This account has no {self.symbol} symbol")
        return min(matches, key=lambda s: len(s.name)).name

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
        manifest = self.store.manifest()
        manifest["offset_ms"] = self.offset_ms
        self.store.save_manifest(manifest)

    def _server_seconds(self, utc_ms: int | None) -> int:
        utc_ms = utc_ms if utc_ms is not None else int(self.clock() * 1000)
        return int((utc_ms + self.offset_ms) // 1000)

    def _load_last_recorded(self) -> None:
        """Last tick already on disk: the newest open CSV, else the manifest."""
        self.last_ms, self.at_last = None, 0
        live = sorted((self.feed / "live").glob("*.csv"))
        for path in live:
            _trim_partial_line(path)
        for path in reversed(live):
            ts, _b, _a = read_csv_bytes(path.read_bytes())
            if len(ts):
                ms = ts // 1_000_000
                self.last_ms = int(ms[-1])
                self.at_last = int((ms == ms[-1]).sum())
                return
        days = self.store.manifest().get("days", {})
        lasts = [e["last_ns"] for e in days.values() if e.get("last_ns")]
        if lasts:
            self.last_ms = max(lasts) // 1_000_000
            self.at_last = 1

    def _backfill(self) -> None:
        now_ms = int(self.clock() * 1000)
        start = now_ms - int(self.backfill_days * MS_PER_DAY)
        if self.last_ms is not None:
            start = max(start, self.last_ms)
        api = self.api
        while start < now_ms and not self.stop_event.is_set():
            end = min(start + BACKFILL_CHUNK_MS, now_ms)
            ticks = api.copy_ticks_range(self.broker_symbol, self._server_seconds(start),
                                         self._server_seconds(end) + 1, api.COPY_TICKS_ALL)
            if ticks is not None:
                self._append(ticks)
            start = end

    def _append(self, ticks) -> None:
        """Write ticks after the last recorded one, in UTC, to their day's CSV."""
        if ticks is None or len(ticks) == 0:
            return
        msc = np.asarray(ticks["time_msc"], np.int64) - self.offset_ms
        bid = np.asarray(ticks["bid"], np.float64)
        ask = np.asarray(ticks["ask"], np.float64)
        keep = np.ones(len(msc), bool)
        if self.last_ms is not None:
            keep = msc > self.last_ms
            same = np.flatnonzero(msc == self.last_ms)
            keep[same[self.at_last:]] = True  # same millisecond, not yet written
        msc, bid, ask = msc[keep], bid[keep], ask[keep]
        if not len(msc):
            return
        order = np.argsort(msc, kind="stable")
        msc, bid, ask = msc[order], bid[order], ask[order]
        fmt = f"{{}},{{:.{self.digits}f}},{{:.{self.digits}f}}\n"
        day_idx = msc // MS_PER_DAY
        today = int(self.clock() * 1000) // MS_PER_DAY
        for d in np.unique(day_idx):
            m = day_idx == d
            day = date(1970, 1, 1) + timedelta(days=int(d))
            fh = self._file(day)
            fh.write("".join(fmt.format(int(t), float(b), float(a)) for t, b, a in zip(msc[m], bid[m], ask[m])))
            fh.flush()
            if d == today:
                self._ticks_today += int(m.sum())
        last = int(msc[-1])
        self.at_last = int((msc == last).sum()) + (self.at_last if last == self.last_ms else 0)
        self.last_ms = last

    def _file(self, day: date):
        fh = self._files.get(day)
        if fh is None:
            fh = open(self.feed / "live" / f"{day.isoformat()}.csv", "a", encoding="ascii", newline="\n")
            self._files[day] = fh
        return fh

    def _close_files(self) -> None:
        for fh in self._files.values():
            try:
                fh.close()
            except OSError:
                pass
        self._files = {}

    def _roll_day(self) -> None:
        """Close and compact every day before today (UTC)."""
        today = date(1970, 1, 1) + timedelta(days=int(self.clock() * 1000) // MS_PER_DAY)
        for day in [d for d in self._files if d < today]:
            self._files.pop(day).close()
        self._compact_closed_days(before=today)
        if self.status.get("day") != today.isoformat():
            self.status["day"] = today.isoformat()
            self._ticks_today = 0

    def _compact_closed_days(self, before: date | None = None) -> None:
        """Clean every finished day's CSV with Layer 0's rules into Parquet."""
        before = before or date(1970, 1, 1) + timedelta(days=int(self.clock() * 1000) // MS_PER_DAY)
        for path in sorted((self.feed / "live").glob("*.csv")):
            day = date.fromisoformat(path.stem)
            if day >= before or day in self._files:
                continue
            ts, bid, ask = read_csv_bytes(path.read_bytes())
            ts, bid, ask, flag = flag_day(ts, bid, ask)
            if len(ts):
                old_ts, old_bid, old_ask, _f = self.store.read_day(day)
                if len(old_ts):  # a day recorded in two sittings
                    ts, bid, ask, flag = flag_day(np.r_[old_ts, ts], np.r_[old_bid, bid], np.r_[old_ask, ask])
                self.store.write_day(day, ts, bid, ask, flag)
            manifest = self.store.manifest()
            manifest.setdefault("days", {})[day.isoformat()] = day_entry("mt5", ts, flag, complete=True)
            manifest["days"] = dict(sorted(manifest["days"].items()))
            self.store.save_manifest(manifest)
            path.unlink()


def _trim_partial_line(path: Path) -> None:
    """Cut a half-written last line (the recorder was killed mid-write), so
    the next append starts on a fresh line."""
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        with open(path, "r+b") as fh:
            fh.truncate(data.rfind(b"\n") + 1)


def read_status(root: Path = config.MT5_DIR) -> list[dict]:
    """Status of every recorded feed, newest first (for the UI and CLI)."""
    out = []
    for status_path in Path(root).glob("*/*/status.json"):
        try:
            s = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        s["feed_key"] = f"mt5:{status_path.parent.parent.name}"
        out.append(s)
    return sorted(out, key=lambda s: s.get("updated_ms") or 0, reverse=True)
