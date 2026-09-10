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


def to_utc(naive_or_aware: datetime, source_tz) -> datetime:
    """Attach a source's clock and convert.  Nothing downstream ever sees a
    naive datetime (§4.1)."""
    if naive_or_aware.tzinfo is None:
        localised = naive_or_aware.replace(tzinfo=source_tz)
    else:
        localised = naive_or_aware
    return localised.astimezone(timezone.utc)
