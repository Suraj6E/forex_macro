"""Clock handling — planning.md §4.1.

The correction that matters: there is no such thing as *the* forex market
timestamp.  FX is decentralised — no central exchange, no official clock, no
consolidated tape.  UTC is the right storage format precisely because it is
arbitrary and constant.

The trading day is a different thing from the UTC calendar date.  The FX week
runs Sunday ~17:00 New York to Friday 17:00 New York, and because New York
observes DST that boundary is 21:00 UTC in summer and 22:00 UTC in winter.
So the trading day is derived from `America/New_York`, never from the UTC date.

No Django imports.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")

#: HistData stamps in Eastern Standard Time with **no DST adjustment**, per
#: their own file spec.  Using `America/New_York` for it would apply DST and
#: shift half the history by an hour (§4.1 table).
HISTDATA_FIXED = timezone(timedelta(hours=-5), name="EST-fixed")

ROLLOVER_HOUR_NY = 17


def trading_day(ts_utc: datetime) -> date:
    """The FX trading day a UTC instant belongs to.

    17:00 New York starts the *next* trading day.  Weekend instants map to the
    following Monday, since the week opens Sunday 17:00 New York.
    """
    if ts_utc.tzinfo is None:
        raise ValueError("trading_day() requires a timezone-aware datetime (§4.1)")

    local = ts_utc.astimezone(NEW_YORK)
    day = local.date()
    if local.hour >= ROLLOVER_HOUR_NY:
        day += timedelta(days=1)

    # Saturday belongs to nothing; roll it forward to Monday.  Sunday before
    # 17:00 NY likewise.
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def fx_sessions(start: date, end: date) -> list[tuple[datetime, datetime]]:
    """UTC intervals during which the FX market is open, covering [start, end).

    The week runs Sunday 17:00 New York to Friday 17:00 New York.  Because that
    anchor is local, each interval's UTC boundary shifts by an hour twice a
    year — which is exactly why the expected-bar count for a month cannot be
    "minutes in the month minus weekends".
    """
    sessions: list[tuple[datetime, datetime]] = []
    # Step back to the Sunday on or before `start` so a session that opened
    # before the window still contributes its overlapping part.
    cursor = start - timedelta(days=(start.weekday() + 1) % 7 + 7)
    limit = end + timedelta(days=7)

    while cursor <= limit:
        if cursor.weekday() == 6:  # Sunday
            open_local = datetime(
                cursor.year, cursor.month, cursor.day, ROLLOVER_HOUR_NY, tzinfo=NEW_YORK
            )
            friday = cursor + timedelta(days=5)
            close_local = datetime(
                friday.year, friday.month, friday.day, ROLLOVER_HOUR_NY, tzinfo=NEW_YORK
            )
            sessions.append(
                (
                    open_local.astimezone(timezone.utc),
                    close_local.astimezone(timezone.utc),
                )
            )
        cursor += timedelta(days=1)
    return sessions


def expected_minutes(month_start: date) -> int:
    """How many M1 bars a complete month of FX data should contain."""
    if month_start.month == 12:
        month_end = date(month_start.year + 1, 1, 1)
    else:
        month_end = date(month_start.year, month_start.month + 1, 1)

    window_start = datetime(
        month_start.year, month_start.month, month_start.day, tzinfo=timezone.utc
    )
    window_end = datetime(month_end.year, month_end.month, month_end.day, tzinfo=timezone.utc)

    total = 0
    for open_utc, close_utc in fx_sessions(month_start, month_end):
        lo = max(open_utc, window_start)
        hi = min(close_utc, window_end)
        if hi > lo:
            total += int((hi - lo).total_seconds() // 60)
    return total


def to_utc(naive_or_aware: datetime, source_tz) -> datetime:
    """Attach a source's clock and convert.  Nothing downstream ever sees a
    naive datetime (§4.1)."""
    if naive_or_aware.tzinfo is None:
        localised = naive_or_aware.replace(tzinfo=source_tz)
    else:
        localised = naive_or_aware
    return localised.astimezone(timezone.utc)
