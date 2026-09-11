"""ForexFactory calendar pages → CanonicalEvent — planning.md §4.2 Route B.

This is the historical calendar. The page embeds its data as a JavaScript
object rather than rendering only a table, which makes it far less brittle
than the "HTML crawl" §4.2 anticipated — but it is still an unofficial
interface that can change without notice, which is why every payload is
snapshotted.

**Verified against known history** before this parser was written: NFP on
5 Jan 2007 reads 167K, 6 Jun 2008 reads −49K, 9 Jan 2015 reads 252K and
5 Jan 2024 reads 216K, all correct. The timestamps are Unix seconds and track
US daylight saving properly — 08:30 New York is 13:30 UTC in January and 12:30
UTC in June, and the feed reports exactly that. That removes the per-source
clock ambiguity of §4.1 for this source.

Three things it still cannot do, kept explicit so nothing downstream assumes
otherwise:

* **Its forecast is not point-in-time.** What the page shows today is the
  currently displayed consensus, and calendar sites revise those. §4.3: a
  historical scrape can capture numbers the market never saw. So the forecast
  goes to `forecast_stored`, never to `forecast_point_in_time`, and the weekly
  forward capture remains the only channel that earns that label.
* **Whether its `actual` is the first print is undocumented.** The site models
  revisions by carrying a separate `revision` for the *previous* value, which
  suggests the actual is as-published — but suggests is not knows, so it fills
  `actual_current` and ALFRED remains the vintage authority.
* **It carries no reporting period**, so rows are keyed provisionally by
  release instant and cannot be joined to an agency figure by period until a
  source that does carry one supplies it.

No Django imports.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from normalisers.base import CanonicalEvent, parse_numeric, unknown_period

PARSER_VERSION = "ff-pages-1"

IMPACT_MAP = {"high": 3, "medium": 2, "low": 1, "holiday": 0, "non-economic": 0}

IN_SCOPE = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"}

_ANCHOR = "days:"


def extract_days(body: str) -> list[dict]:
    """Pull the `days: [...]` array out of the page's embedded state.

    A bracket scanner rather than a regular expression: a lazy `.*?` would stop
    at the first `],` that happens to fall inside the data and silently return
    a truncated calendar, which is the failure mode §5.4 warns about — a
    plausible-looking wrong answer instead of an error.
    """
    start = body.find(_ANCHOR)
    while start != -1:
        bracket = body.find("[", start)
        if bracket == -1:
            break
        blob = _scan_array(body, bracket)
        if blob is not None:
            try:
                return json.loads(blob)
            except json.JSONDecodeError:
                pass
        start = body.find(_ANCHOR, start + len(_ANCHOR))

    raise ValueError(
        "No calendar data found on the page. The embedded state has changed "
        "shape, or the request was served a block page instead of a calendar."
    )


def _scan_array(text: str, start: int) -> str | None:
    """Return the balanced `[...]` beginning at `start`, honouring strings."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse(payload: bytes) -> list[CanonicalEvent]:
    body = payload.decode("utf-8", errors="replace")
    days = extract_days(body)

    rows: list[CanonicalEvent] = []
    for day in days:
        for event in day.get("events") or []:
            row = _parse_event(event)
            if row is not None:
                rows.append(row)
    return rows


def _parse_event(event: dict) -> CanonicalEvent | None:
    currency = (event.get("currency") or "").strip().upper()
    name = (event.get("name") or "").strip()
    if currency not in IN_SCOPE or not name:
        return None

    dateline = event.get("dateline")
    if not dateline:
        return None
    try:
        when = datetime.fromtimestamp(int(dateline), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None

    actual = parse_numeric(event.get("actual"))
    # A masked time means the site itself does not know when this landed —
    # usually a "tentative" or all-day release. Carrying it as a minute-precise
    # instant would be a fabricated t0.
    masked = bool(event.get("timeMasked"))

    # `ebaseId` identifies the recurring series, not the occurrence. It is
    # stable across nineteen years, which makes it a far better alias key than
    # the display title the weekly feed forces us to use.
    series_id = event.get("ebaseId")
    source_event_key = f"ff:{series_id}" if series_id else f"{currency}|{name}"

    return CanonicalEvent(
        source_key="forexfactory_pages",
        currency=currency,
        source_event_key=source_event_key,
        source_event_name=name,
        reference_period=unknown_period(when),
        reference_period_start=when.date(),
        # An event with an actual has happened; one without is still scheduled.
        release_time_utc=None if actual is None else when,
        scheduled_time_utc=when if actual is None else None,
        timestamp_confidence="date_only" if masked else "minute",
        actual=actual,
        forecast=parse_numeric(event.get("forecast")),
        previous=parse_numeric(event.get("previous")),
        # The site's `revision` is the restated *previous* value: on the
        # September 2024 payrolls row, previous reads 114K and revision 89K.
        revised_previous=parse_numeric(event.get("revision")),
        forecast_target="forecast_stored",
        actual_target="actual_current",
        importance_source=IMPACT_MAP.get(
            (event.get("impactName") or "").strip().lower(), 0
        ),
        parser_version=PARSER_VERSION,
        raw={
            k: event.get(k)
            for k in (
                "id", "ebaseId", "name", "currency", "dateline", "timeLabel",
                "impactName", "actual", "forecast", "previous", "revision",
                "timeMasked", "leaked", "soloUrl",
            )
            if event.get(k) not in (None, "")
        },
    )


def summarise(rows: list[CanonicalEvent]) -> dict[str, Any]:
    with_actual = sum(1 for r in rows if r.actual is not None)
    with_forecast = sum(1 for r in rows if r.forecast is not None)
    with_revision = sum(1 for r in rows if r.revised_previous is not None)
    masked = sum(1 for r in rows if r.timestamp_confidence == "date_only")
    return {
        "rows": len(rows),
        "with_actual": with_actual,
        "with_forecast": with_forecast,
        "with_revision": with_revision,
        "time_masked": masked,
    }
