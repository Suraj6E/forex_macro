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


#: Sources that print the same vendor's event titles. The weekly feed keys an
#: event by its title, having no series id; the pages key it by ForexFactory's
#: series id. Without this, a title seen first by one source made a second
#: indicator beside the other's, and every week fetched from both was stored
#: twice (found 18 Nov 2026: 20–26 Sep and 15–20 Nov, ~170 events).
SAME_TITLES: dict[str, tuple[str, ...]] = {
    "forexfactory_weekly": ("forexfactory_pages",),
    "forexfactory_pages": ("forexfactory_weekly",),
}


def title_twin(source_key: str, currency: str, name: str) -> Indicator | None:
    """The indicator another source of the same vendor already holds under
    this exact title and currency, if there is one."""
    others = SAME_TITLES.get(source_key)
    if not others or not name:
        return None
    aliases = IndicatorAlias.objects.filter(source__key__in=others, indicator__isnull=False)
    ids = set(aliases.filter(currency=currency, source_name__iexact=name).values_list("indicator_id", flat=True))
    ids |= set(Indicator.objects.filter(currency=currency, name__iexact=name,
                                        aliases__source__key__in=others).values_list("id", flat=True))
    if not ids:
        return None
    if len(ids) > 1:
        # A title held under several series ids: the one released last.
        latest = (EventRelease.objects.filter(indicator_id__in=ids).exclude(release_time_utc=None)
                  .order_by("-release_time_utc").values_list("indicator_id", flat=True).first())
        return Indicator.objects.get(pk=latest or min(ids))
    return Indicator.objects.get(pk=ids.pop())


def merge_title_twins(log=lambda msg: None) -> dict:
    """Fold the indicators the weekly feed made beside a same-titled pages
    series into that series, release by release. Idempotent; a release with
    study results on both sides is left as it is and counted."""
    table = _priority_table()
    weekly_only = (Indicator.objects.filter(aliases__source__key="forexfactory_weekly")
                   .exclude(aliases__source__key="forexfactory_pages").distinct())
    indicators = moved = merged = kept = 0
    for dup in list(weekly_only):
        twin = title_twin("forexfactory_weekly", dup.currency, dup.name)
        if twin is None or twin.pk == dup.pk:
            continue
        with transaction.atomic():
            for rel in list(dup.releases.all()):
                target = EventRelease.objects.filter(indicator=twin, reference_period=rel.reference_period,
                                                     revision_no=rel.revision_no).first()
                if target is None:
                    rel.indicator = twin
                    rel.save(update_fields=["indicator"])
                    moved += 1
                elif _fold_release(rel, target, table):
                    merged += 1
                else:
                    kept += 1
            IndicatorAlias.objects.filter(indicator=dup).update(indicator=twin)
            if not dup.releases.exists() and not dup.decay_curves.exists():
                dup.delete()
            indicators += 1
    summary = (f"{indicators} duplicate indicator(s) folded into their pages series: "
               f"{merged} release(s) merged, {moved} moved" + (f", {kept} left (study results on both)" if kept else ""))
    if indicators:
        log(summary)
    return {"indicators": indicators, "merged": merged, "moved": moved, "kept": kept, "summary": summary}


def _fold_release(src: EventRelease, dst: EventRelease, table) -> bool:
    """Move `src`'s values and history onto `dst` (the same release) and
    delete `src`. Each field goes to whichever source ranks higher, as a merge
    would have done had they matched in the first place."""
    if src.impacts.exists() and dst.impacts.exists():
        return False
    owners = dict(dst.source_map_json or {})
    for field, owner in (src.source_map_json or {}).items():
        incoming = getattr(src, field, None)
        if incoming is None:
            continue
        if getattr(dst, field) is None or wins(field, owner, owners.get(field), table):
            setattr(dst, field, incoming)
            owners[field] = owner
    dst.source_map_json = owners
    if "forecast_point_in_time" in owners:
        dst.forecast_provenance = ForecastProvenance.POINT_IN_TIME
    elif "forecast_stored" in owners:
        dst.forecast_provenance = ForecastProvenance.VENDOR_STORED
    anchor = dst.release_time_utc or dst.scheduled_time_utc
    dst.trading_day = trading_day(anchor) if anchor else None
    dst.save()
    SourceObservation.objects.filter(event_release=src).update(event_release=dst)
    ValueRevision.objects.filter(event_release=src).update(event_release=dst)
    src.impacts.update(event_release=dst)
    src.delete()
    return True


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
        alias.indicator = title_twin(source.key, row.currency, row.source_event_name)
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
