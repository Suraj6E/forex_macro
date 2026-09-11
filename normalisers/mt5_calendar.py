"""MT5 calendar CSV → CanonicalEvent — planning.md §4.2 Route A.

The MQL5 exporter in `mql5/CalendarExport.mq5` writes raw terminal values and
lets this module do the arithmetic, deliberately: the two traps below are the
kind that produce plausible-looking wrong output, so they belong where unit
tests can reach them.

**Trap 1 — values are stored multiplied by 1,000,000.**  Miss it and every
number is six orders of magnitude out.

**Trap 2 — unset fields hold `LONG_MIN`, not null.**  Miss it and a single
−9.2×10¹⁸ outlier quietly destroys every standard deviation you compute.

**Trap 3 — the clock.**  `MqlCalendarValue.time` is trade-server time, the
server's DST rules have changed over the years, and a 2007 start crosses many
of those changes.  The exporter records the offset in force *at export time*;
applying it to a 2009 event is an assumption, not a conversion.  So every row
from this source is marked `timestamp_confidence = inferred` until the
volatility cross-check (§4.1) promotes it.  Confidently wrong is worse than
flagged.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from normalisers.base import CanonicalEvent

PARSER_VERSION = "mt5-calendar-csv-1"

#: MQL5 writes this for "no value". It is not a number you ever want in a sample.
LONG_MIN = -9223372036854775808
VALUE_SCALE = Decimal(1_000_000)

EXPECTED_COLUMNS = {
    "event_id",
    "event_name",
    "currency",
    "importance",
    "server_time",
    "server_gmt_offset",
    "period",
    "revision",
    "actual_value",
    "forecast_value",
    "prev_value",
    "revised_prev_value",
}

IMPORTANCE_MAP = {"NONE": 0, "LOW": 1, "MODERATE": 2, "HIGH": 3}

IN_SCOPE = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"}


def decode_value(raw: Any) -> Decimal | None:
    """Undo the ×1,000,000 scaling and treat LONG_MIN as null."""
    if raw is None or str(raw).strip() in {"", "NULL", "None"}:
        return None
    try:
        scaled = int(str(raw).strip())
    except ValueError:
        return None
    if scaled == LONG_MIN:
        return None
    return Decimal(scaled) / VALUE_SCALE


def decode_period(raw: Any) -> tuple[str, date | None]:
    """MQL5 writes the reporting period as a datetime; we want a label.

    `period` is what makes §4.4's identity key stable across sources — it is
    the one field that does not move when two providers disagree about a
    release time.
    """
    text = (str(raw) or "").strip()
    if not text or text in {"0", "1970.01.01 00:00:00", "1970-01-01 00:00:00"}:
        return "", None
    parsed = _parse_mql_datetime(text)
    if parsed is None:
        return text, None
    return f"{parsed:%Y-%m}", parsed.date().replace(day=1)


def _parse_mql_datetime(text: str) -> datetime | None:
    text = text.strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:  # a bare epoch, which the exporter also accepts
        return datetime.fromtimestamp(int(text), tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, OSError, OverflowError):
        return None


def parse(payload: bytes) -> list[CanonicalEvent]:
    text = payload.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")

    if reader.fieldnames is None:
        raise ValueError("MT5 calendar export is empty.")
    header = {name.strip() for name in reader.fieldnames}
    missing = EXPECTED_COLUMNS - header
    if missing:
        raise ValueError(
            "MT5 calendar export is missing column(s): "
            + ", ".join(sorted(missing))
            + f". Found: {', '.join(sorted(header))}. Re-run CalendarExport.mq5."
        )

    rows: list[CanonicalEvent] = []
    for record in reader:
        row = _parse_record(record)
        if row is not None:
            rows.append(row)
    return rows


def _parse_record(record: dict) -> CanonicalEvent | None:
    currency = (record.get("currency") or "").strip().upper()
    name = (record.get("event_name") or "").strip()
    if currency not in IN_SCOPE or not name:
        return None

    server_time = _parse_mql_datetime(record.get("server_time") or "")
    if server_time is None:
        return None

    try:
        offset_seconds = int((record.get("server_gmt_offset") or "0").strip() or 0)
    except ValueError:
        offset_seconds = 0

    # The offset in force when the export ran. For a recent event this is
    # right; for a 2009 event it is an assumption about a server whose DST
    # rules have since changed — hence `inferred` below, never `exact`.
    release_utc = (server_time - timedelta(seconds=offset_seconds)).replace(
        tzinfo=timezone.utc
    )

    period_label, period_start = decode_period(record.get("period"))
    if not period_label:
        period_label = f"release:{release_utc:%Y-%m-%dT%H:%M}"

    try:
        revision = int((record.get("revision") or "0").strip() or 0)
    except ValueError:
        revision = 0

    importance_raw = (record.get("importance") or "").strip().upper()
    importance = IMPORTANCE_MAP.get(
        importance_raw, int(importance_raw) if importance_raw.isdigit() else 0
    )

    return CanonicalEvent(
        source_key="mt5_calendar",
        currency=currency,
        source_event_key=(record.get("event_id") or "").strip() or f"{currency}|{name}",
        source_event_name=name,
        reference_period=period_label,
        reference_period_start=period_start,
        revision_no=revision,
        release_time_utc=release_utc,
        scheduled_time_utc=None,
        timestamp_confidence="inferred",
        actual=decode_value(record.get("actual_value")),
        forecast=decode_value(record.get("forecast_value")),
        previous=decode_value(record.get("prev_value")),
        revised_previous=decode_value(record.get("revised_prev_value")),
        # Whether MT5's stored forecast is the consensus *as of release* or a
        # later snapshot is an open question (§6.10 UNKNOWN), so it goes to the
        # vendor-stored column and never to the point-in-time one.
        forecast_target="forecast_stored",
        # Whether the actual is a first print or a revised figure is likewise
        # undocumented; ALFRED is the vintage source.
        actual_target="actual_current",
        importance_source=importance,
        unit=(record.get("unit") or "").strip(),
        parser_version=PARSER_VERSION,
        raw={k: v for k, v in record.items() if v not in (None, "")},
    )
