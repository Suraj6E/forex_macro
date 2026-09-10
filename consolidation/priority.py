"""Field-level source priority — planning.md §4.4.

"Priority order (configurable, not hardcoded)" is a requirement, so the table
below is a *default* that `data/priority.json` overrides.  The merged
`event_release` row is a composite: each field records which source supplied
it, and re-running the merge with a different order is possible without
re-crawling because every source's raw claim survives in
`source_observation` (§9).

No Django imports.
"""

from __future__ import annotations

import json
from pathlib import Path

#: The table in §4.4, by source key.  Left to right is best to worst.
DEFAULT_PRIORITY: dict[str, list[str]] = {
    "release_time_utc": ["agency_schedule", "mt5_calendar", "forexfactory_pages", "forexfactory_weekly"],
    "scheduled_time_utc": ["agency_schedule", "forexfactory_weekly", "mt5_calendar", "forexfactory_pages"],
    "actual_first_print": ["alfred", "agency_schedule", "mt5_calendar", "forexfactory_pages"],
    "actual_current": ["dbnomics", "agency_schedule", "mt5_calendar"],
    # Only one source can ever supply this: a forecast is point-in-time
    # because we held it before the release, not because a site says so (§4.3).
    "forecast_point_in_time": ["forexfactory_weekly"],
    "forecast_stored": ["mt5_calendar", "forexfactory_pages", "forexfactory_weekly", "scraped_dataset"],
    "previous": ["mt5_calendar", "agency_schedule", "forexfactory_weekly", "forexfactory_pages"],
    "revised_previous": ["mt5_calendar", "agency_schedule", "forexfactory_pages"],
}

#: Fallback when a field has no explicit ordering.
DEFAULT_ORDER: list[str] = [
    "alfred",
    "agency_schedule",
    "dbnomics",
    "mt5_calendar",
    "forexfactory_weekly",
    "forexfactory_pages",
    "scraped_dataset",
]

_UNRANKED = 10_000


def load_priority(config_path: Path | None = None) -> dict[str, list[str]]:
    table = {field: list(order) for field, order in DEFAULT_PRIORITY.items()}
    if config_path and config_path.exists():
        override = json.loads(config_path.read_text(encoding="utf-8"))
        for field, order in override.items():
            table[field] = list(order)
    return table


def rank(field: str, source_key: str, table: dict[str, list[str]] | None = None) -> int:
    """Lower is better.  An unlisted source ranks below every listed one
    rather than being silently discarded — it can still fill an empty field."""
    table = table if table is not None else DEFAULT_PRIORITY
    order = table.get(field) or DEFAULT_ORDER
    try:
        return order.index(source_key)
    except ValueError:
        return _UNRANKED


def wins(
    field: str,
    incoming_source: str,
    current_source: str | None,
    table: dict[str, list[str]] | None = None,
) -> bool:
    """Should `incoming_source` overwrite the value currently held?

    An empty field is always filled.  Equal rank means the incumbent stays —
    re-fetching the same source does not churn the merged row (revisions from
    the same source are handled separately, in merge.py).
    """
    if current_source is None:
        return True
    if current_source == incoming_source:
        return True
    return rank(field, incoming_source, table) < rank(field, current_source, table)
