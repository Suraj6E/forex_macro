"""DBnomics series → CanonicalEvent — planning.md §4.2 Route D.

DBnomics aggregates national and international statistical institutions into
one API, deliberately non-opinionated: original codes preserved, numerical
values never modified.  That last property is exactly what a
provenance-tracked pipeline wants.

What it is and is not, stated once so nothing downstream assumes otherwise:

* It carries **no survey forecasts**.
* It is indexed by **reference period, not release event** — there is no
  release timestamp here at all, so these rows fill and validate `actual` and
  can never anchor an event study on their own.
* Its values are **current**, not first prints.  ALFRED is where vintages come
  from; conflating the two is the look-ahead problem of §4.3.

No Django imports.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

from normalisers.base import CanonicalEvent

PARSER_VERSION = "dbnomics-series-1"

_QUARTER_RE = re.compile(r"^(\d{4})-?Q([1-4])$", re.I)
_SEMESTER_RE = re.compile(r"^(\d{4})-?S([1-2])$", re.I)
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")


def period_start(period: str) -> date | None:
    """First day of the period a DBnomics observation describes."""
    text = (period or "").strip()
    if match := _DAY_RE.match(text):
        return date(int(match[1]), int(match[2]), int(match[3]))
    if match := _MONTH_RE.match(text):
        return date(int(match[1]), int(match[2]), 1)
    if match := _QUARTER_RE.match(text):
        return date(int(match[1]), (int(match[2]) - 1) * 3 + 1, 1)
    if match := _SEMESTER_RE.match(text):
        return date(int(match[1]), (int(match[2]) - 1) * 6 + 1, 1)
    if match := _YEAR_RE.match(text):
        return date(int(match[1]), 1, 1)
    return None


def parse(
    payload: bytes,
    *,
    series_config: dict[str, dict],
    since: date | None = None,
    until: date | None = None,
) -> tuple[list[CanonicalEvent], list[str], list[str]]:
    """Returns (rows, series_ids_found, series_ids_missing)."""
    try:
        document: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"DBnomics response is not valid JSON: {exc}") from exc

    docs = (document.get("series") or {}).get("docs")
    if docs is None:
        raise ValueError(
            "DBnomics response shape changed: expected series.docs, got "
            f"{sorted(document.keys())}"
        )

    rows: list[CanonicalEvent] = []
    found: list[str] = []

    for doc in docs:
        series_id = "/".join(
            [doc.get("provider_code", ""), doc.get("dataset_code", ""), doc.get("series_code", "")]
        )
        found.append(series_id)
        config = series_config.get(series_id, {})
        currency = (config.get("currency") or "").upper()
        if not currency:
            continue

        name = config.get("name") or doc.get("series_name") or series_id
        unit = doc.get("unit") or ""

        periods = doc.get("period") or []
        values = doc.get("value") or []
        for period, value in zip(periods, values):
            if value is None or value == "NA":
                continue
            start = period_start(str(period))
            if since and (start is None or start < since):
                continue
            if until and start and start > until:
                continue
            try:
                actual = Decimal(str(value))
            except (ArithmeticError, ValueError):
                continue

            rows.append(
                CanonicalEvent(
                    source_key="dbnomics",
                    currency=currency,
                    source_event_key=series_id,
                    source_event_name=name,
                    reference_period=str(period),
                    reference_period_start=start,
                    # There is no release instant in this source. Leaving both
                    # timestamps null is the honest representation; inventing
                    # one would put a fabricated anchor into an event study.
                    release_time_utc=None,
                    scheduled_time_utc=None,
                    timestamp_confidence="date_only",
                    actual=actual,
                    actual_target="actual_current",
                    importance_source=int(config.get("importance", 0)),
                    unit=unit,
                    parser_version=PARSER_VERSION,
                    raw={"series_id": series_id, "period": str(period), "value": value},
                )
            )

    missing = [sid for sid in series_config if sid not in set(found)]
    return rows, found, missing
