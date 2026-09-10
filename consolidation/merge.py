"""Merging one source's claims into the canonical dataset — planning.md §4.4.

The rules this module enforces, all of them from the plan rather than from
convenience:

* Identity is `(currency, canonical_code, reference_period, revision_no)`.
  The timestamp is an *attribute*, never part of the key, so two sources that
  disagree by a minute produce one row with a disagreement flag rather than
  two rows.
* **We never silently pick.**  Outside tolerance the row is flagged
  `cross_source = disagree`, both values are retained (the loser survives in
  `source_observation`), and it surfaces on the Data Quality screen.
* A re-fetch that changes a value writes a `value_revision` row rather than
  overwriting — that is how you see a site quietly changing a forecast after
  the fact (§7.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from analytics.timeutils import trading_day
from calendar_data.models import EventRelease, Indicator, IndicatorAlias, SourceObservation, ValueRevision
from consolidation.priority import load_priority, wins
from normalisers.base import CanonicalEvent
from quality.enums import CrossSource, ForecastProvenance, MappingStatus

#: §4.4 — timestamps compare within ±2 minutes.
TIMESTAMP_TOLERANCE = timedelta(minutes=2)

#: Per-indicator numeric tolerance lands with the indicator registry (P3).
#: Until then, exact comparison, which over-reports disagreement rather than
#: under-reporting it.  That is the right direction to be wrong in.
NUMERIC_TOLERANCE = Decimal("0")


@dataclass
class MergeOutcome:
    created: bool = False
    changed_fields: list[str] = None
    disagreed_fields: list[str] = None
    newly_unmapped: bool = False

    def __post_init__(self):
        self.changed_fields = self.changed_fields or []
        self.disagreed_fields = self.disagreed_fields or []


def _priority_table():
    return load_priority(settings.DATA_DIR / "priority.json")


def resolve_indicator(source, row: CanonicalEvent) -> tuple[Indicator, bool]:
    """Map a source's own event name onto an Indicator via the alias table.

    A source we have never seen this event from creates an alias *and* an
    auto Indicator, so the pipeline keeps flowing.  The Indicator carries
    `mapping_status = auto` and no `canonical_code` until a human assigns one
    — which is exactly the "unmapped events" report of §9.
    """
    alias, alias_created = IndicatorAlias.objects.get_or_create(
        source=source,
        source_key=row.source_event_key,
        defaults={
            "source_name": row.source_event_name,
            "currency": row.currency,
        },
    )
    newly_unmapped = False
    if alias.indicator_id is None:
        alias.indicator = Indicator.objects.create(
            currency=row.currency,
            name=row.source_event_name,
            importance=row.importance_source,
            unit=row.unit,
            mapping_status=MappingStatus.AUTO,
        )
        newly_unmapped = True

    alias.times_seen = alias.times_seen + 1
    alias.save(update_fields=["indicator", "times_seen"])
    return alias.indicator, newly_unmapped


def _values_differ(field: str, current, incoming) -> bool:
    if current is None or incoming is None:
        return current is not incoming
    if isinstance(current, datetime):
        return abs(current - incoming) > TIMESTAMP_TOLERANCE
    if isinstance(current, Decimal):
        return abs(current - incoming) > NUMERIC_TOLERANCE
    return current != incoming


def _incoming_fields(row: CanonicalEvent) -> dict[str, object]:
    """Map a CanonicalEvent onto EventRelease columns.

    `forecast_target` and `actual_target` are set by the normaliser, which is
    the only layer that knows *when* the value was captured relative to the
    release — the whole point of §4.3.
    """
    fields: dict[str, object] = {}
    if row.release_time_utc is not None:
        fields["release_time_utc"] = row.release_time_utc
    if row.scheduled_time_utc is not None:
        fields["scheduled_time_utc"] = row.scheduled_time_utc
    if row.forecast is not None:
        fields[row.forecast_target] = row.forecast
    if row.actual is not None:
        fields[row.actual_target] = row.actual
    if row.previous is not None:
        fields["previous"] = row.previous
    if row.revised_previous is not None:
        fields["revised_previous"] = row.revised_previous
    return fields


@transaction.atomic
def apply_observation(source, fetch_run, row: CanonicalEvent) -> MergeOutcome:
    """Fold one source's claim into the canonical row.  Idempotent (§7.2)."""
    table = _priority_table()
    indicator, newly_unmapped = resolve_indicator(source, row)

    release, created = EventRelease.objects.get_or_create(
        indicator=indicator,
        reference_period=row.reference_period,
        revision_no=row.revision_no,
        defaults={
            "release_group": indicator.release_group,
            "reference_period_start": row.reference_period_start,
            "timestamp_confidence": row.timestamp_confidence,
        },
    )
    outcome = MergeOutcome(created=created, newly_unmapped=newly_unmapped)

    source_map = dict(release.source_map_json or {})
    to_update: list[str] = []
    confirmed_by_second_source = False

    for field, incoming in _incoming_fields(row).items():
        current = getattr(release, field)
        owner = source_map.get(field)

        if current is not None and owner not in (None, source.key) and not _values_differ(
            field, current, incoming
        ):
            confirmed_by_second_source = True

        if current is not None and _values_differ(field, current, incoming):
            if owner == source.key:
                # Same source, different number: the source changed its mind.
                ValueRevision.objects.create(
                    event_release=release,
                    field=field,
                    old_value=str(current),
                    new_value=str(incoming),
                    source=source,
                    fetch_run=fetch_run,
                )
                outcome.changed_fields.append(field)
            else:
                # Different sources, outside tolerance.  Both values survive:
                # the incumbent here, the challenger in source_observation.
                outcome.disagreed_fields.append(field)

        if not wins(field, source.key, owner, table):
            continue
        if current is not None and not _values_differ(field, current, incoming):
            source_map.setdefault(field, source.key)
            continue

        setattr(release, field, incoming)
        source_map[field] = source.key
        to_update.append(field)

    # AGREE means a second source *confirmed a value*, not merely that two
    # sources each filled a different column.
    if outcome.disagreed_fields:
        release.cross_source = CrossSource.DISAGREE
    elif confirmed_by_second_source and release.cross_source != CrossSource.DISAGREE:
        release.cross_source = CrossSource.AGREE
    elif release.cross_source == CrossSource.SINGLE:
        release.cross_source = CrossSource.SINGLE

    if "forecast_point_in_time" in source_map:
        release.forecast_provenance = ForecastProvenance.POINT_IN_TIME
    elif "forecast_stored" in source_map:
        release.forecast_provenance = ForecastProvenance.VENDOR_STORED

    if row.reference_period_start and release.reference_period_start is None:
        release.reference_period_start = row.reference_period_start
        to_update.append("reference_period_start")

    # §4.1: derived, anchored to 17:00 America/New_York — never inferred from
    # the UTC calendar date.
    anchor = release.release_time_utc or release.scheduled_time_utc
    release.trading_day = trading_day(anchor) if anchor else None

    release.source_map_json = source_map
    release.timestamp_confidence = row.timestamp_confidence
    release.save()

    SourceObservation.objects.create(
        event_release=release,
        source=source,
        fetch_run=fetch_run,
        raw_json={
            "parser_version": row.parser_version,
            "source_event_key": row.source_event_key,
            "reference_period": row.reference_period,
            "payload": row.raw,
        },
    )
    return outcome
