"""New ticks from the Dukascopy datafeed, after the Layer 0 file ends.

The same source as the 2023-2024 file (trading-research downloaded it from
here), so new days match old ones: Bid and Ask, millisecond timestamps, UTC.
One LZMA file per instrument-hour, published a few minutes after the hour
closes. Measured on 13 Nov 2026: at 08:16 UTC the 07:00-08:00 file was
there, ticks to 07:59:59.

`update()` fetches every missing hour up to the last published one, cleans
each day with Layer 0's rules (`quality.flag_day`), and writes it into the
feed. It saves after every day, so it can be stopped and rerun; an hour that
failed or was not yet published is fetched again next time, so a day is
never left with a silent hole.

The feed rate-limits: under load it answers HTTP 429, resets connections or
stops answering for a while. Requests are paced (about 3 a second across all
workers), and when the feed stops answering every worker pauses together,
2 s doubling to 2 minutes, until it answers again.
"""

from __future__ import annotations

import lzma
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import requests

from simulator import config
from simulator.data.quality import flag_day
from simulator.data.ticks import NS_PER_DAY, NS_PER_SECOND, FeedStore, day_entry, day_start_ns, now_ns

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
USER_AGENT = "forex_macro-simulator/1.0"

#: 20 bytes per tick, big-endian: ms into the hour, Ask and Bid in points,
#: Ask and Bid volume.
RECORD = np.dtype([("ms", ">u4"), ("ask", ">u4"), ("bid", ">u4"), ("ask_vol", ">f4"), ("bid_vol", ">f4")])

HOUR_NS = 3600 * NS_PER_SECOND
#: An hour is requested once it closed this long ago.
PUBLISH_DELAY_NS = 10 * 60 * NS_PER_SECOND
#: A 404 for an hour older than this means "no data", not "not yet".
SETTLED_NS = 3 * HOUR_NS

WORKERS = 4
RETRIES = 8
TIMEOUT = (10, 25)
MAX_PAUSE_SECONDS = 120.0
#: Steady pace across all workers. The feed answers HTTP 429 when pushed;
#: about 3 requests a second is gentler than the dukascopy-node downloader
#: trading-research used (batches of 10, a second apart), and still fetches
#: a year of hours in under an hour.
MIN_INTERVAL_SECONDS = 0.33


def point_scale(symbol: str) -> float:
    return 1_000.0 if symbol.upper().endswith("JPY") else 100_000.0


def hour_url(symbol: str, hour_ns: int) -> str:
    t = datetime.fromtimestamp(hour_ns / 1e9, tz=timezone.utc)
    # Months are zero-based in the path; getting this wrong returns a valid
    # file for the wrong month.
    return f"{BASE_URL}/{symbol.upper()}/{t.year:04d}/{t.month - 1:02d}/{t.day:02d}/{t.hour:02d}h_ticks.bi5"


def decode(payload: bytes, hour_ns: int, scale: float = 100_000.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(ts ns, bid, ask) from one hour file. An empty file is a closed hour."""
    if not payload:
        e = np.empty(0)
        return e.astype(np.int64), e, e
    try:
        raw = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(payload)
    except lzma.LZMAError:
        raw = lzma.decompress(payload)
    rec = np.frombuffer(raw[: len(raw) - len(raw) % RECORD.itemsize], dtype=RECORD)
    digits = int(round(np.log10(scale)))
    ts = hour_ns + rec["ms"].astype(np.int64) * 1_000_000
    bid = np.round(rec["bid"].astype(np.float64) / scale, digits)
    ask = np.round(rec["ask"].astype(np.float64) / scale, digits)
    return ts, bid, ask


class Stopped(Exception):
    """The update was asked to stop while waiting."""


class Fetcher:
    """GET with one back-off shared by every worker. Returns bytes, or None
    for a 404.

    When the feed stops answering (timeouts, resets, 429, 5xx), all workers
    pause together, 2 s doubling to 2 minutes, instead of each retrying on
    its own and keeping the pressure on. One answer resets the pause.
    `on_wait(seconds)` is told whenever a pause starts.
    """

    def __init__(self, stop: threading.Event | None = None, on_wait=None) -> None:
        self._local = threading.local()
        self._lock = threading.Lock()
        self.cool_until = 0.0
        self._penalty = 0.0
        self._next_slot = 0.0
        self.stop = stop
        self.on_wait = on_wait

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            self._local.session = s
        return s

    def _pace(self) -> None:
        with self._lock:
            now = time.time()
            slot = max(now, self._next_slot)
            self._next_slot = slot + MIN_INTERVAL_SECONDS
        if slot > now:
            time.sleep(slot - now)

    def _wait(self) -> None:
        while True:
            with self._lock:
                left = self.cool_until - time.time()
            if left <= 0:
                return
            if self.stop is not None:
                if self.stop.wait(min(left, 1.0)):
                    raise Stopped()
            else:
                time.sleep(min(left, 1.0))

    def _penalise(self) -> None:
        with self._lock:
            self._penalty = min(max(self._penalty * 2, 2.0), MAX_PAUSE_SECONDS)
            start = time.time() + self._penalty
            if start > self.cool_until:
                self.cool_until = start
                if self.on_wait:
                    self.on_wait(self._penalty)

    def _answered(self) -> None:
        with self._lock:
            self._penalty = 0.0

    def __call__(self, url: str) -> bytes | None:
        for _attempt in range(RETRIES):
            self._wait()
            self._pace()
            try:
                r = self._session().get(url, timeout=TIMEOUT)
            except (requests.ConnectionError, requests.Timeout):
                self._penalise()
                continue
            if r.status_code == 404:
                self._answered()
                return None
            if r.status_code == 200:
                self._answered()
                return r.content
            if r.status_code in (429, 500, 502, 503, 504):
                self._penalise()
                continue
            raise RuntimeError(f"HTTP {r.status_code} for {url}")
        raise TimeoutError(f"Dukascopy did not answer after {RETRIES} tries: {url}")


def last_published_hour(now: int) -> int:
    """Start (ns) of the latest hour whose file should exist at `now`."""
    return ((now - HOUR_NS - PUBLISH_DELAY_NS) // HOUR_NS) * HOUR_NS


def pending_days(feed_dir=config.DUKA_DIR, *, start_day: date | None = None, now: int | None = None) -> list[date]:
    """Days `update` would fetch: new days after the end, days a previous
    run left partial, and days missing in the middle (the Layer 0 file lacks
    six weekdays). Saturdays are closed in every UTC hour and skipped."""
    now = now if now is not None else now_ns()
    entries = FeedStore(feed_dir).manifest()["days"]
    if start_day is None:
        if not entries:
            raise ValueError("The feed is empty: import the Layer 0 file first, or give a start day")
        start_day = min(date.fromisoformat(d) for d in entries)
    last_hour = last_published_hour(now)
    end_day = date(1970, 1, 1) + timedelta(days=int(last_hour // NS_PER_DAY))
    days = []
    d = start_day
    while d <= end_day:
        entry = entries.get(d.isoformat())
        if d.weekday() != 5 and not (entry and entry.get("complete")):
            days.append(d)
        d += timedelta(days=1)
    return days


def update(
    feed_dir=config.DUKA_DIR,
    symbol: str = config.SYMBOL,
    *,
    start_day: date | None = None,
    now: int | None = None,
    max_days: int | None = None,
    fetch=None,
    progress=None,
    stop: threading.Event | None = None,
) -> dict:
    """Fetch every missing hour up to the last published one.

    With `max_days`, only the first that many pending days, so a caller can
    rebuild candles between chunks of a long catch-up. Returns a summary;
    `first_changed` is the earliest day written, from which candles must be
    rebuilt, and `remaining` how many pending days are left.
    """
    store = FeedStore(feed_dir)
    fetch = fetch or Fetcher()
    now = now if now is not None else now_ns()
    scale = point_scale(symbol)
    progress = progress or (lambda **kw: None)
    last_hour = last_published_hour(now)

    with store.lock:
        manifest = store.manifest()
    entries = manifest["days"]
    all_days = pending_days(feed_dir, start_day=start_day, now=now)
    days = all_days[:max_days] if max_days else all_days

    summary = {"days": 0, "ticks": 0, "first_changed": None, "failed_hours": 0, "last_ns": None,
               "up_to_ns": last_hour + HOUR_NS, "stopped": False, "remaining": len(all_days) - len(days),
               "through": None}
    if not days:
        return summary

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, day in enumerate(days, 1):
            if stop is not None and stop.is_set():
                summary["stopped"] = True
                summary["remaining"] += len(days) - i + 1
                break
            progress(day=day, done=i - 1, total=len(days))
            summary["through"] = day
            day0 = day_start_ns(day)
            entry = entries.get(day.isoformat()) or {}
            done_hours = set(entry.get("hours_done", [])) if entry.get("source") == "dukascopy" else set()
            wanted = [h for h in range(24) if day0 + h * HOUR_NS <= last_hour and h not in done_hours]
            if not wanted:
                continue

            def get(h: int):
                hour_ns = day0 + h * HOUR_NS
                try:
                    payload = fetch(hour_url(symbol, hour_ns))
                except Exception:
                    return h, "failed", None
                if payload is None:
                    settled = now - (hour_ns + HOUR_NS) > SETTLED_NS
                    return h, ("empty" if settled else "pending"), None
                return h, "ok", decode(payload, hour_ns, scale)

            results = list(pool.map(get, wanted))
            new_parts = [r[2] for r in results if r[1] == "ok"]
            fetched = {h for h, status, _ in results if status in ("ok", "empty")}
            summary["failed_hours"] += sum(1 for _, status, _ in results if status == "failed")
            if not fetched:
                continue

            with store.lock:
                old_ts, old_bid, old_ask, _old_flag = store.read_day(day)
                parts = [(old_ts, old_bid, old_ask), *new_parts]
                ts = np.concatenate([p[0] for p in parts])
                bid = np.concatenate([p[1] for p in parts])
                ask = np.concatenate([p[2] for p in parts])
                added = len(ts) - len(old_ts)
                ts, bid, ask, flag = flag_day(ts, bid, ask)
                if len(ts):
                    store.write_day(day, ts, bid, ask, flag)
                hours_done = sorted(done_hours | fetched)
                manifest = store.manifest()
                manifest["days"][day.isoformat()] = day_entry(
                    "dukascopy", ts, flag, hours_done=hours_done, complete=len(hours_done) == 24,
                    fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
                manifest["days"] = dict(sorted(manifest["days"].items()))
                manifest.setdefault("symbol", symbol)
                store.save_manifest(manifest)
                entries = manifest["days"]
            summary["days"] += 1
            summary["ticks"] += added
            if len(ts):
                summary["last_ns"] = int(ts[-1])
            if summary["first_changed"] is None:
                summary["first_changed"] = day
    progress(day=days[-1], done=len(days), total=len(days))
    return summary
