"""Economic calendar events on the replay chart, from the fxmacro database.

The Django app in this repository collects the calendar (ForexFactory, MT5,
agencies) into `db.sqlite3`. The simulator only reads it, opened read-only,
and keeps the events of the four currencies it trades in memory: USD, EUR,
GBP and JPY. It never writes there.

The replay's clock decides what is known, as with prices:
  * the schedule, the forecast and the previous value are known before the
    release, as they are to a trader on the day;
  * the actual and the revised previous exist only once the release time
    has passed on the simulation clock;
  * the actual shown is the first print. A later revision of it is the
    future, and is never shown;
  * an indicator's history lists only releases before the clock.

The database is kept current by fxmacro's `refresh_calendar`, which the
simulator runs in the background (calendar_update.py). The file is checked
for changes every 30 seconds, the write-ahead log included: the database runs
in WAL mode, so a write lands in `db.sqlite3-wal` and reaches the main file
only at a checkpoint.

Impact is the calendar's own rating (3 high, 2 medium, 1 low). Events with
no rating (bank holidays) and events with no time of day are left out: a
dot needs a time.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from pathlib import Path

from simulator import config

CURRENCIES = ("USD", "EUR", "GBP", "JPY")
IMPACTS = {3: "high", 2: "medium", 1: "low"}
#: How often the database file is checked for changes.
RECHECK_SECONDS = 30.0
HISTORY = 8

_QUERY = f"""
SELECT r.id, COALESCE(r.release_time_utc, r.scheduled_time_utc) AS t, i.currency, i.importance, i.name,
       i.id AS indicator_id, i.concept, i.frequency, i.unit, i.explainer_md, i.transmission_channel,
       r.reference_period, r.actual_first_print, r.actual_current, r.forecast_point_in_time,
       r.forecast_stored, r.previous, r.revised_previous, r.timestamp_confidence, r.forecast_provenance,
       r.confounded, r.source_map_json, g.name AS group_name
FROM calendar_data_eventrelease r
JOIN calendar_data_indicator i ON i.id = r.indicator_id
LEFT JOIN calendar_data_releasegroup g ON g.id = r.release_group_id
WHERE i.currency IN ({",".join("?" for _ in CURRENCIES)})
  AND COALESCE(r.release_time_utc, r.scheduled_time_utc) IS NOT NULL
  AND r.timestamp_confidence != 'date_only'
  AND i.importance IN (1, 2, 3)
  AND i.mapping_status != 'ignored'
"""

_PERCENT = re.compile(r"\b[mqy]/[mqy]\b|\bRate\b", re.I)


def _ns(text: str) -> int:
    dt = datetime.fromisoformat(str(text).replace("Z", ""))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # Django stores UTC
    return int(dt.timestamp()) * 1_000_000_000


def fmt_value(value, name: str = "") -> str | None:
    """A calendar number as the calendar sites print it: 272K, -7.6B, 0.4%."""
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= size and float(v).is_integer():
            return f"{v / size:.3f}".rstrip("0").rstrip(".") + suffix
    text = f"{v:.4f}".rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    return text + ("%" if _PERCENT.search(name or "") else "")


def _num(value) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


class Calendar:
    def __init__(self, path: Path = config.CALENDAR_DB) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._version = None
        self._checked = 0.0
        self.error: str | None = None
        self.times: list[int] = []
        self.rows: list[dict] = []
        self.by_id: dict[int, dict] = {}
        self.by_indicator: dict[int, list[dict]] = {}

    # -- loading -------------------------------------------------------------

    def _file_version(self) -> tuple:
        """The database file and its write-ahead log, as they stand."""
        st = self.path.stat()
        try:
            wal = self.path.with_name(self.path.name + "-wal").stat()
            wal_v = (wal.st_mtime_ns, wal.st_size)
        except FileNotFoundError:
            wal_v = None
        return st.st_mtime_ns, st.st_size, wal_v

    def invalidate(self) -> None:
        """Read the database again at the next look (after a refresh wrote it)."""
        self._version = None
        self._checked = 0.0

    def refresh(self) -> None:
        now = time.monotonic()
        if now - self._checked < RECHECK_SECONDS and self._version is not None:
            return
        self._checked = now
        try:
            version = self._file_version()
        except FileNotFoundError:
            self.error = f"The fxmacro database was not found at {self.path}"
            return
        if version == self._version:
            return
        try:
            self._load()
            self._version = version
            self.error = None
        except sqlite3.Error as exc:
            self.error = f"Could not read the calendar: {exc}"

    def _load(self) -> None:
        db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            raw = db.execute(_QUERY, CURRENCIES).fetchall()
        finally:
            db.close()
        rows = []
        for r in raw:
            row = dict(r)
            row["ns"] = _ns(row.pop("t"))
            rows.append(row)
        rows.sort(key=lambda r: (r["ns"], -r["importance"], r["currency"], r["name"]))
        by_indicator: dict[int, list[dict]] = {}
        for row in rows:
            by_indicator.setdefault(row["indicator_id"], []).append(row)
        with self._lock:
            self.rows = rows
            self.times = [r["ns"] for r in rows]
            self.by_id = {r["id"]: r for r in rows}
            self.by_indicator = by_indicator

    # -- reading -------------------------------------------------------------

    def status(self) -> dict:
        self.refresh()
        return {
            "ok": bool(self.rows) and self.error is None,
            "path": str(self.path),
            "error": self.error,
            "events": len(self.rows),
            "first_ms": self.times[0] // 1_000_000 if self.times else None,
            "last_ms": self.times[-1] // 1_000_000 if self.times else None,
            "currencies": list(CURRENCIES),
        }

    def next_after(self, after_ns: int, min_impact: int = 1) -> int | None:
        """The time of the first event after `after_ns` rated at least
        `min_impact`, or None."""
        self.refresh()
        with self._lock:
            i = bisect_right(self.times, after_ns)
            for r in self.rows[i:]:
                if r["importance"] >= min_impact:
                    return r["ns"]
        return None

    def window(self, a_ns: int, b_ns: int, currencies, clock_ns: int | None) -> list[dict]:
        """Events in [a_ns, b_ns] for these currencies, as chart dots.
        `released` says whether the clock has passed them."""
        self.refresh()
        wanted = set(currencies)
        with self._lock:
            lo, hi = bisect_left(self.times, a_ns), bisect_right(self.times, b_ns)
            rows = self.rows[lo:hi]
        return [{
            "id": r["id"], "ms": r["ns"] // 1_000_000, "ccy": r["currency"], "impact": r["importance"],
            "name": r["name"], "released": clock_ns is None or r["ns"] <= clock_ns,
        } for r in rows if r["currency"] in wanted]

    def detail(self, event_id: int, clock_ns: int | None, hide_dates: bool = False) -> dict | None:
        """Everything known about one release at `clock_ns`.
        None for an unknown id. `clock_ns=None` means no clock (review of a
        finished session): everything up to now is known."""
        self.refresh()
        with self._lock:
            r = self.by_id.get(int(event_id))
            if r is None:
                return None
            siblings = list(self.by_indicator.get(r["indicator_id"], []))
            lo, hi = bisect_left(self.times, r["ns"]), bisect_right(self.times, r["ns"])
            same_time = [x for x in self.rows[lo:hi] if x["id"] != r["id"]]
        known = clock_ns is None or r["ns"] <= clock_ns
        name = r["name"]
        forecast = _num(r["forecast_point_in_time"]) if r["forecast_point_in_time"] is not None else _num(r["forecast_stored"])
        # The first print is what came out at the time; actual_current may
        # hold a revision published later, which the clock has not reached.
        actual = _num(r["actual_first_print"]) if r["actual_first_print"] is not None else _num(r["actual_current"])
        if not known:
            actual = None
        surprise = None
        if actual is not None and forecast is not None:
            surprise = "above forecast" if actual > forecast else "below forecast" if actual < forecast else "as forecast"
        period = r["reference_period"] or ""
        if period.startswith("release:") or hide_dates:
            period = None  # a synthetic key, or it would give the date away
        history = []
        for s in reversed(siblings):
            if s["ns"] >= r["ns"] or (clock_ns is not None and s["ns"] > clock_ns):
                continue
            s_actual = _num(s["actual_first_print"]) if s["actual_first_print"] is not None else _num(s["actual_current"])
            s_fc = _num(s["forecast_point_in_time"]) if s["forecast_point_in_time"] is not None else _num(s["forecast_stored"])
            history.append({"ms": s["ns"] // 1_000_000, "actual": fmt_value(s_actual, name),
                            "forecast": fmt_value(s_fc, name), "previous": fmt_value(s["previous"], name)})
            if len(history) >= HISTORY:
                break
        try:
            sources = json.loads(r["source_map_json"] or "{}")
        except ValueError:
            sources = {}
        return {
            "id": r["id"],
            "ms": r["ns"] // 1_000_000,
            "released": known,
            "currency": r["currency"],
            "impact": r["importance"],
            "impact_label": IMPACTS.get(r["importance"], "unknown"),
            "name": name,
            "concept": r["concept"] or None,
            "frequency": r["frequency"] or None,
            "group": r["group_name"],
            "period": period,
            "actual": fmt_value(actual, name),
            "forecast": fmt_value(forecast, name),
            "previous": fmt_value(r["previous"], name),
            "revised_previous": fmt_value(r["revised_previous"], name) if known else None,
            "surprise": surprise,
            "timestamp": r["timestamp_confidence"],
            "source": sources.get("actual_current") or sources.get("release_time_utc") or sources.get("scheduled_time_utc"),
            "confounded": bool(r["confounded"]),
            "explainer": r["explainer_md"] or None,
            "channel": r["transmission_channel"] or None,
            "same_time": [{"id": x["id"], "currency": x["currency"], "name": x["name"], "impact": x["importance"]}
                          for x in same_time],
            "history": history,
        }
