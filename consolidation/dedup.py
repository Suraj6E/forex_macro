"""Finding and merging duplicates — planning.md §4.4, §9.

Two kinds of duplicate exist in this dataset, and they arise for different
reasons:

**Duplicate indicators.**  Every source names a release its own way, so the
first ingest from each source creates its own `Indicator`.  MT5's "Nonfarm
Payrolls", ForexFactory's "Non-Farm Employment Change" and a BLS series are
three rows describing one thing until a human assigns them the same canonical
code.  §9 calls this "the buggiest artifact in the project", so merging is
deliberate and reviewable — never automatic on a guess.

**Duplicate releases.**  The identity key of §4.4 makes these structurally
impossible *when a source supplies a reporting period*.  Sources that do not —
the ForexFactory weekly feed — get a provisional `release:<UTC minute>` key
instead.  That was a considered trade-off: keying on the date alone silently
merges two same-day releases, and a visible duplicate is recoverable where a
silent merge is not.  This module is the recovery.

Nothing here guesses.  Exact normalised matches are offered for merging;
anything softer is a *suggestion* that needs a human to confirm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import Coalesce, TruncDate

from calendar_data.models import (
    EventRelease,
    Indicator,
    IndicatorAlias,
    SourceObservation,
    ValueRevision,
)
from consolidation.priority import load_priority, rank
from normalisers.base import PERIOD_UNKNOWN_PREFIX
from quality.enums import MappingStatus

#: How far apart two provisional-key releases of one indicator may sit and
#: still be the same event.  Matches the cross-source tolerance of §4.4.
RELEASE_TOLERANCE = timedelta(minutes=2)

#: Below this token overlap, two names are not proposed as the same indicator.
SIMILARITY_FLOOR = 0.6

_PUNCT = re.compile(r"[^a-z0-9/ ]+")
_SPACE = re.compile(r"\s+")

#: True stopwords only. An earlier version also dropped "rate", "index" and
#: "change" as filler — but macro names are short, so those words are most of
#: the signal: stripping them pushed "US Unemployment Rate" and "Unemployment
#: Rate" below the suggestion threshold, which is the exact pair this is for.
_NOISE = {"the", "of", "a", "an", "and", "for"}


def normalise_name(name: str) -> str:
    """Conservative normalisation for comparing two sources' names.

    Case and punctuation go; the `m/m`, `y/y`, `q/q` markers stay, because
    "CPI m/m" and "CPI y/y" are different indicators and collapsing them would
    be a far worse error than leaving a duplicate in place.
    """
    text = _PUNCT.sub(" ", (name or "").lower())
    return _SPACE.sub(" ", text).strip()


def _tokens(name: str) -> set[str]:
    return {t for t in normalise_name(name).split() if t not in _NOISE}


def similarity(left: str, right: str) -> float:
    return _token_similarity(_tokens(left), _tokens(right))


def _token_similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class DuplicateGroup:
    """Indicators that may describe one release."""

    currency: str
    label: str
    members: list = field(default_factory=list)
    exact: bool = True
    score: float = 1.0

    @property
    def primary(self):
        """The best survivor: a mapped indicator wins, then the one with the
        most releases, then the oldest. Merging into the mapped row keeps the
        canonical code that other sources will join on."""
        return sorted(
            self.members,
            key=lambda i: (
                i.canonical_code is None,
                -getattr(i, "release_count", 0),
                i.pk,
            ),
        )[0]

    @property
    def others(self):
        primary = self.primary
        return [i for i in self.members if i.pk != primary.pk]

    @property
    def total_releases(self) -> int:
        return sum(getattr(i, "release_count", 0) for i in self.members)


def _annotated_indicators():
    return Indicator.objects.annotate(release_count=Count("releases", distinct=True)).exclude(
        mapping_status=MappingStatus.IGNORED
    )


def exact_duplicate_groups() -> list[DuplicateGroup]:
    """Indicators whose names are identical once normalised.

    Safe to merge in bulk: the only way two rows collide here is that two
    sources spelled the same thing the same way.
    """
    buckets: dict[tuple[str, str], list] = {}
    for indicator in _annotated_indicators():
        key = (indicator.currency, normalise_name(indicator.name))
        buckets.setdefault(key, []).append(indicator)

    return [
        DuplicateGroup(currency=currency, label=label, members=members, exact=True, score=1.0)
        for (currency, label), members in sorted(buckets.items())
        if len(members) > 1
    ]


def similar_candidate_groups(floor: float = SIMILARITY_FLOOR) -> list[DuplicateGroup]:
    """Same currency, overlapping names, *not* identical, **and coming from
    different sources**.

    That last condition is the one that makes this list usable. A duplicate
    indicator exists because two sources named one release differently; two
    indicators carrying the *same* provenance are two things that source chose
    to keep apart, and by construction it is right. Without the filter this
    list was 2,054 pairs on a single-source dataset, of which the highest
    scoring were "ADP Non-Farm Employment Change" against "Non-Farm Employment
    Change", "Core CPI" against "CPI", and German industrial production against
    the euro-area aggregate — every one a distinct series, and merging any of
    them would silently fuse two economic series with nothing downstream able
    to detect it. 94% of the rest were pairs of speaker events.

    Suggestions only, still. Name similarity is evidence, not proof.
    """
    indicators = list(
        _annotated_indicators().prefetch_related("aliases")
    )
    provenance = {
        indicator.pk: frozenset(a.source_id for a in indicator.aliases.all())
        for indicator in indicators
    }

    # Normalise once per indicator, not twice per comparison: at 670
    # indicators this is ~220k pairs, and re-tokenising inside the loop was
    # most of a fifteen-second page load.
    normalised = {i.pk: normalise_name(i.name) for i in indicators}
    tokens = {i.pk: _tokens(i.name) for i in indicators}

    by_currency: dict[str, list] = {}
    for indicator in indicators:
        by_currency.setdefault(indicator.currency, []).append(indicator)

    groups: list[DuplicateGroup] = []
    for currency, members in by_currency.items():
        for i, left in enumerate(members):
            for right in members[i + 1 :]:
                if provenance[left.pk] == provenance[right.pk]:
                    continue  # one source keeping two things apart on purpose
                if normalised[left.pk] == normalised[right.pk]:
                    continue  # already an exact group
                score = _token_similarity(tokens[left.pk], tokens[right.pk])
                if score >= floor:
                    groups.append(
                        DuplicateGroup(
                            currency=currency,
                            label=f"{left.name}  ~  {right.name}",
                            members=[left, right],
                            exact=False,
                            score=score,
                        )
                    )
    return sorted(groups, key=lambda g: -g.score)


@transaction.atomic
def merge_indicators(primary: Indicator, others: list[Indicator]) -> dict:
    """Fold `others` into `primary`, then delete them.

    Releases are merged on the §4.4 identity key: where both indicators hold
    the same period and revision, the two rows become one and every source's
    raw claim survives on the survivor. Nothing is discarded — that is the
    point of keeping `source_observation` separate from the merged row.
    """
    moved_releases = merged_releases = moved_aliases = 0

    for other in others:
        if other.pk == primary.pk:
            continue

        moved_aliases += IndicatorAlias.objects.filter(indicator=other).update(
            indicator=primary
        )

        for release in list(other.releases.all()):
            twin = (
                EventRelease.objects.filter(
                    indicator=primary,
                    reference_period=release.reference_period,
                    revision_no=release.revision_no,
                )
                .exclude(pk=release.pk)
                .first()
            )
            if twin is None:
                release.indicator = primary
                release.release_group = primary.release_group or release.release_group
                release.save(update_fields=["indicator", "release_group"])
                moved_releases += 1
            else:
                merge_releases(twin, [release])
                merged_releases += 1

        if primary.canonical_code is None and other.canonical_code:
            primary.canonical_code = other.canonical_code
            primary.mapping_status = MappingStatus.MAPPED
        primary.importance = max(primary.importance, other.importance)
        if not primary.unit and other.unit:
            primary.unit = other.unit
        other.delete()

    primary.save()
    return {
        "primary_id": primary.pk,
        "merged_indicators": len(others),
        "moved_releases": moved_releases,
        "merged_releases": merged_releases,
        "moved_aliases": moved_aliases,
    }


@transaction.atomic
def merge_releases(primary: EventRelease, others: list[EventRelease]) -> dict:
    """Fold duplicate releases into one row, keeping every source's claim.

    Field-level conflicts are resolved by the same priority table the ingest
    uses (§4.4), so a merge cannot promote a worse source over a better one.
    """
    table = load_priority()
    source_map = dict(primary.source_map_json or {})
    absorbed = 0
    revisions = 0

    # Oldest first, so the newest claim ends up winning a same-source conflict.
    for other in sorted(others, key=lambda r: (r.first_seen_at, r.pk)):
        if other.pk == primary.pk:
            continue

        other_map = other.source_map_json or {}
        for field_name in EventRelease.MERGEABLE_FIELDS:
            incoming = getattr(other, field_name)
            if incoming is None:
                continue
            current = getattr(primary, field_name)
            owner = source_map.get(field_name)
            challenger = other_map.get(field_name)

            if current is None:
                setattr(primary, field_name, incoming)
                if challenger:
                    source_map[field_name] = challenger
            elif challenger and challenger == owner and current != incoming:
                # One source, two values: the fork hid a revision. Record it,
                # which is what would have happened had the key not split, and
                # take the later value.
                ValueRevision.objects.create(
                    event_release=primary,
                    field=field_name,
                    old_value=str(current),
                    new_value=str(incoming),
                    source_id=other.observations.values_list("source_id", flat=True).first(),
                )
                setattr(primary, field_name, incoming)
                revisions += 1
            elif challenger and rank(field_name, challenger, table) < rank(
                field_name, owner or "", table
            ):
                setattr(primary, field_name, incoming)
                source_map[field_name] = challenger

        SourceObservation.objects.filter(event_release=other).update(event_release=primary)
        ValueRevision.objects.filter(event_release=other).update(event_release=primary)

        if primary.reference_period_start is None:
            primary.reference_period_start = other.reference_period_start
        other.delete()
        absorbed += 1

    primary.source_map_json = source_map
    primary.save()
    return {"primary_id": primary.pk, "absorbed": absorbed, "revisions": revisions}


def _same_day_provisional():
    """(indicator, day) pairs holding more than one provisionally keyed row."""
    return (
        EventRelease.objects.filter(reference_period__startswith=PERIOD_UNKNOWN_PREFIX)
        .annotate(day=TruncDate(Coalesce("scheduled_time_utc", "release_time_utc")))
        .values("indicator_id", "day")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )


def provisional_duplicate_candidate_count() -> int:
    """Upper bound on duplicate releases, computed entirely in SQL.

    The exact answer needs the ±2 minute grouping, which means loading rows;
    at 86,000 releases that allocation is what made the quality overview slow
    and slower on every reload. The overview asks "is there anything here?",
    so it gets the cheap bound and the duplicates screen does the real work.
    """
    return sum(row["n"] - 1 for row in _same_day_provisional())


def provisional_duplicate_groups(tolerance: timedelta = RELEASE_TOLERANCE) -> list[list]:
    """Releases of one indicator keyed provisionally and sitting within
    `tolerance` of each other — almost certainly the same event, re-keyed
    because a source shifted its scheduled time by a minute."""
    # Narrow before scanning. A duplicate needs two provisional rows for one
    # indicator on one day, so anything else cannot qualify — and at 86,000
    # releases, walking them all in Python was the other half of a
    # fifteen-second page load.
    indicator_ids = {row["indicator_id"] for row in _same_day_provisional()}
    if not indicator_ids:
        return []

    candidates = (
        EventRelease.objects.filter(
            reference_period__startswith=PERIOD_UNKNOWN_PREFIX,
            indicator_id__in=indicator_ids,
        )
        .select_related("indicator")
        .order_by("indicator_id", "scheduled_time_utc", "release_time_utc")
    )

    groups: list[list] = []
    current: list = []
    for release in candidates:
        anchor = release.scheduled_time_utc or release.release_time_utc
        if anchor is None:
            continue
        if current:
            previous = current[-1]
            previous_anchor = previous.scheduled_time_utc or previous.release_time_utc
            same_indicator = previous.indicator_id == release.indicator_id
            close = abs(anchor - previous_anchor) <= tolerance
            if same_indicator and close:
                current.append(release)
                continue
            if len(current) > 1:
                groups.append(current)
        current = [release]
    if len(current) > 1:
        groups.append(current)
    return groups


def merge_provisional_duplicates() -> dict:
    """Collapse every provisional duplicate group. Safe: same indicator, same
    instant within tolerance, and every source claim is preserved."""
    groups = provisional_duplicate_groups()
    absorbed = revisions = 0
    for group in groups:
        survivor = min(group, key=lambda r: r.pk)
        result = merge_releases(survivor, [r for r in group if r.pk != survivor.pk])
        absorbed += result["absorbed"]
        revisions += result["revisions"]
    return {"groups": len(groups), "absorbed": absorbed, "revisions": revisions}


def purge_source(source) -> dict:
    """Remove everything a source contributed.

    For when a bad configuration loaded junk. Releases whose *only* observation
    came from this source go; releases another source also claims are kept,
    because deleting them would discard that other source's work.
    """
    observation_ids = set(
        SourceObservation.objects.filter(source=source).values_list(
            "event_release_id", flat=True
        )
    )
    SourceObservation.objects.filter(source=source).delete()
    ValueRevision.objects.filter(source=source).delete()

    orphaned = (
        EventRelease.objects.filter(pk__in=observation_ids)
        .annotate(remaining=Count("observations"))
        .filter(remaining=0)
    )
    deleted_releases = orphaned.count()
    orphaned.delete()

    IndicatorAlias.objects.filter(source=source).delete()
    empty = Indicator.objects.annotate(
        n=Count("releases"), a=Count("aliases")
    ).filter(Q(n=0) & Q(a=0))
    deleted_indicators = empty.count()
    empty.delete()

    return {
        "touched_releases": len(observation_ids),
        "deleted_releases": deleted_releases,
        "deleted_indicators": deleted_indicators,
    }
