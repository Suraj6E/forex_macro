"""Canonical event row — planning.md §4.4.

A normaliser turns one source's payload into `CanonicalEvent` rows.  It does
*not* merge, does not decide priority, and does not touch the database.  It is
versioned (`parser_version`) and stamped onto every row it produces, so a
parser bug is traceable to the rows it created (§5.4).

No Django imports (§8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

#: §4.4 identity needs a reporting period.  Some sources — the ForexFactory
#: weekly feed among them — simply do not carry one.
#:
#: Rather than invent one, those rows are keyed by scheduled instant behind
#: this prefix, and the prefix is what a later job greps for to re-key them
#: once a source that *does* carry `period` arrives.
#:
#: The trade-off, stated rather than discovered later: §4.4 keeps the
#: timestamp out of the identity key because sources disagree by a minute, and
#: a minute of disagreement would manufacture duplicate rows.  That reasoning
#: assumes we *have* a period to key on instead.  Here we do not, and keying on
#: the date alone silently merges two same-day releases of the same event —
#: two ECB speeches on a Tuesday become one row, and one of them is gone with
#: no trace.  A visible duplicate is recoverable; a silent merge is not, so
#: these rows carry the minute.
PERIOD_UNKNOWN_PREFIX = "release:"

_SUFFIX_MULTIPLIERS = {
    "K": Decimal("1e3"),
    "M": Decimal("1e6"),
    "B": Decimal("1e9"),
    "T": Decimal("1e12"),
}

_NUMERIC_RE = re.compile(r"^\s*([<>~]?)\s*(-?\d[\d,]*\.?\d*)\s*([KMBT]?)\s*%?\s*$", re.I)


def parse_numeric(raw: Any) -> Decimal | None:
    """Parse a calendar site's value string into a Decimal.

    Handles `227K`, `3.2%`, `-0.1%`, `$52.3B`, `1,234`, and the many ways a
    site says "nothing here".  Returns None rather than guessing.

    Percentages are returned as the number shown (3.2, not 0.032): the unit
    belongs to the indicator, and silently rescaling here would make two
    sources disagree for a reason that is ours, not theirs.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        return Decimal(str(raw))

    text = str(raw).strip()
    if text in {"", "-", "--", "N/A", "n/a", "null", "None"}:
        return None
    text = text.replace("$", "").replace("€", "").replace("£", "").replace("¥", "")

    match = _NUMERIC_RE.match(text)
    if not match:
        return None
    _prefix, number, suffix = match.groups()
    try:
        value = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return None
    if suffix:
        value *= _SUFFIX_MULTIPLIERS[suffix.upper()]
    return value


def unknown_period(scheduled_utc: datetime) -> str:
    """Provisional identity for a source that carries no reporting period."""
    return f"{PERIOD_UNKNOWN_PREFIX}{scheduled_utc:%Y-%m-%dT%H:%M}"


def is_unknown_period(period: str) -> bool:
    return period.startswith(PERIOD_UNKNOWN_PREFIX)


@dataclass
class CanonicalEvent:
    """One source's claim about one release.

    Deliberately *not* a merged row.  Fields the source does not supply stay
    None; the consolidation layer decides what wins (§4.4), and never here.
    """

    source_key: str
    currency: str
    source_event_key: str
    source_event_name: str

    reference_period: str
    reference_period_start: date | None = None
    revision_no: int = 0

    release_time_utc: datetime | None = None
    scheduled_time_utc: datetime | None = None
    timestamp_confidence: str = "inferred"

    actual: Decimal | None = None
    forecast: Decimal | None = None
    previous: Decimal | None = None
    revised_previous: Decimal | None = None

    #: Which EventRelease column this source's `forecast` belongs in.  The FF
    #: weekly feed captured before a release is point-in-time; the same value
    #: read from a historical page is not (§4.3).
    forecast_target: str = "forecast_stored"
    actual_target: str = "actual_current"

    importance_source: int = 0
    unit: str = ""
    release_group_key: str = ""

    parser_version: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> tuple[str, str, str, int]:
        """§4.4: `(currency, source event, reference period, revision)`.
        The timestamp is deliberately absent."""
        return (
            self.currency,
            self.source_event_key,
            self.reference_period,
            self.revision_no,
        )
