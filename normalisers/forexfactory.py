"""ForexFactory weekly feed → CanonicalEvent — planning.md §4.2 Route B.

Two verified constraints from §4.2/§5.1 shape everything here:

1. **The weekly feed carries forecast and previous, but not the actual.**  So
   this normaliser never populates an actual, even if a future feed revision
   starts including one — that would need its own provenance decision.
2. The feed is the **forward point-in-time capture channel** (§4.3, §7.3).
   Whether its forecast is point-in-time depends on *when we fetched it*
   relative to the release, so the caller supplies `captured_at` and this
   module routes the value to the right column.  It is never assumed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from normalisers.base import CanonicalEvent, parse_numeric, unknown_period

PARSER_VERSION = "ff-weekly-1"

IMPACT_MAP = {
    "high": 3,
    "medium": 2,
    "low": 1,
    "holiday": 0,
    "non-economic": 0,
}

#: The eight economies of locked decision 6.  Everything else is fetched (the
#: feed is one payload) but not turned into rows.
IN_SCOPE = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"}


def parse(payload: bytes, *, captured_at: datetime) -> list[CanonicalEvent]:
    try:
        entries: Any = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"ForexFactory feed is not valid JSON: {exc}") from exc

    if not isinstance(entries, list):
        raise ValueError(
            f"ForexFactory feed shape changed: expected a list, got {type(entries).__name__}"
        )

    rows: list[CanonicalEvent] = []
    for entry in entries:
        row = _parse_entry(entry, captured_at=captured_at)
        if row is not None:
            rows.append(row)
    return rows


def _parse_entry(entry: dict, *, captured_at: datetime) -> CanonicalEvent | None:
    currency = (entry.get("country") or "").strip().upper()
    title = (entry.get("title") or "").strip()
    if currency not in IN_SCOPE or not title:
        return None

    when = _parse_timestamp(entry.get("date"))
    if when is None:
        return None

    # §4.3: point-in-time only if we actually held it before the release.
    is_point_in_time = captured_at < when
    impact = IMPACT_MAP.get((entry.get("impact") or "").strip().lower(), 0)

    return CanonicalEvent(
        source_key="forexfactory_weekly",
        currency=currency,
        # The feed carries no stable event id, so the title is the key.  It is
        # the reason the canonical mapping (§9) needs a human screen: a title
        # change at the source silently forks into a new alias.
        source_event_key=f"{currency}|{title}",
        source_event_name=title,
        reference_period=unknown_period(when),
        reference_period_start=when.date(),
        release_time_utc=None,          # scheduled, not observed
        scheduled_time_utc=when,
        timestamp_confidence="minute",  # §4.1: verify against a known release
        forecast=parse_numeric(entry.get("forecast")),
        previous=parse_numeric(entry.get("previous")),
        forecast_target=(
            "forecast_point_in_time" if is_point_in_time else "forecast_stored"
        ),
        importance_source=impact,
        parser_version=PARSER_VERSION,
        raw=dict(entry),
    )


def _parse_timestamp(value: Any) -> datetime | None:
    """The feed stamps with an explicit UTC offset, so conversion is exact.

    Anything naive is rejected rather than assumed — §4.1: there is no such
    thing as *the* forex market timestamp, and guessing one is how half a
    history ends up an hour out.
    """
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
