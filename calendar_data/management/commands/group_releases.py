"""Assign `release_group` from the timestamps themselves — planning.md §3.3.

§3.3 Case B is the reason this field exists: the US unemployment rate is not a
standalone release.  It is published inside the Employment Situation report, in
the same instant as nonfarm payrolls and average hourly earnings.  Three
numbers, one timestamp, perfectly collinear regressors — no amount of data
separates them, so the move is attributed to *the report* and never to one
member.

Nothing about that needs a human to decide.  Two indicators belong to the same
report exactly when they keep arriving at the same instant, and the calendar
already records every instant either of them ever had.  So:

    overlap(A, B) = |shared instants| / min(|instants(A)|, |instants(B)|)

Two indicators are joined when that fraction clears `--threshold`, and a group
is a connected component of the resulting graph.  `min` rather than the union
is deliberate: a quarterly series published inside a monthly report shares
every one of its own instants and only a third of the other's, and it still
belongs to the report.

**The threshold is doing real work, and it separates the two cases §3.3 draws
apart.** Indicators that *always* co-occur are Case B, inseparable, and belong
in a group.  Indicators that *sometimes* co-occur — US and Canadian employment
landing the same morning — are Case A, which joint regression handles (§6.3)
and which grouping would wrongly fuse into one thing.  A high threshold is what
keeps Case A out.

Groups are matched on membership, so re-running after new data is idempotent: a
group whose members are unchanged keeps its key, its name and its description.

    manage.py group_releases              # what it would do
    manage.py group_releases --apply      # do it
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from calendar_data.models import EventRelease, Indicator, ReleaseGroup

#: Below this many releases an overlap fraction is not evidence of anything —
#: two indicators with three instants each can coincide twice by accident.
MIN_RELEASES = 10


class Command(BaseCommand):
    help = "Derive release groups from co-timed releases (§3.3 Case B)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--threshold",
            type=float,
            default=0.9,
            help="Share of the smaller series' instants that must coincide. "
            "Default 0.9 — high on purpose, so §3.3's Case A stays out.",
        )
        parser.add_argument(
            "--min-importance",
            type=int,
            default=2,
            help="Ignore indicators below this tier. Default 2.",
        )
        parser.add_argument(
            "--apply", action="store_true", help="Write. Without it, only report."
        )

    def handle(self, *args, **options):
        threshold = options["threshold"]
        instants = self._instants(options["min_importance"])
        if not instants:
            self.stdout.write("No timestamped releases to group.")
            return

        components = self._components(instants, threshold)
        names = dict(
            Indicator.objects.filter(pk__in=instants).values_list("pk", "name")
        )
        currencies = dict(
            Indicator.objects.filter(pk__in=instants).values_list("pk", "currency")
        )

        self.stdout.write(
            f"{len(components)} group(s) covering "
            f"{sum(len(c) for c in components)} of {len(instants)} indicators, "
            f"at overlap >= {threshold:.0%}."
        )
        self.stdout.write("")

        planned = []
        for members in sorted(
            components, key=lambda c: (currencies[min(c)], sorted(names[i] for i in c))
        ):
            currency = currencies[min(members)]
            labels = sorted(names[i] for i in members)
            planned.append((currency, members, labels))
            self.stdout.write(f"  {currency}: " + "  +  ".join(labels))

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write("Nothing written. Pass --apply to assign these.")
            return

        assigned = self._apply(planned)
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"{assigned} indicator(s) assigned to {len(planned)} group(s).")
        )

    # -- the graph ---------------------------------------------------------

    def _instants(self, min_importance) -> dict[int, set]:
        rows = EventRelease.objects.filter(
            release_time_utc__isnull=False,
            indicator__importance__gte=min_importance,
        ).values_list("indicator_id", "release_time_utc")

        instants: dict[int, set] = defaultdict(set)
        for indicator_id, stamp in rows.iterator():
            instants[indicator_id].add(stamp)
        return {k: v for k, v in instants.items() if len(v) >= MIN_RELEASES}

    def _components(self, instants, threshold) -> list[set[int]]:
        """Connected components of the co-timing graph, within a currency.

        Comparing only inside a currency is not just an optimisation: two
        indicators of different currencies that always coincide are a
        scheduling coincidence, not one report, and fusing them would attribute
        a USD move to a CAD release.
        """
        by_currency = defaultdict(list)
        for indicator_id, currency in Indicator.objects.filter(
            pk__in=instants
        ).values_list("pk", "currency"):
            by_currency[currency].append(indicator_id)

        adjacency = defaultdict(set)
        for members in by_currency.values():
            for left, right in itertools.combinations(members, 2):
                a, b = instants[left], instants[right]
                if len(a & b) / min(len(a), len(b)) >= threshold:
                    adjacency[left].add(right)
                    adjacency[right].add(left)

        seen: set[int] = set()
        components = []
        for node in adjacency:
            if node in seen:
                continue
            stack, component = [node], set()
            while stack:
                current = stack.pop()
                if current in component:
                    continue
                component.add(current)
                seen.add(current)
                stack.extend(adjacency[current] - component)
            components.append(component)
        return components

    # -- writing -----------------------------------------------------------

    @transaction.atomic
    def _apply(self, planned) -> int:
        """Idempotent on membership: a group whose members are unchanged keeps
        the row it already had, so a hand-written name or description survives
        a re-run."""
        existing = {
            group.pk: set(group.indicators.values_list("pk", flat=True))
            for group in ReleaseGroup.objects.prefetch_related("indicators")
        }

        taken = {
            group.key: members
            for group, members in (
                (g, existing[g.pk]) for g in ReleaseGroup.objects.all()
            )
        }

        assigned = 0
        for currency, members, labels in planned:
            match = next(
                (pk for pk, current in existing.items() if current == members), None
            )
            if match is not None:
                group = ReleaseGroup.objects.get(pk=match)
            else:
                # A generated key is a stem, and two different reports can share
                # one — USD Advance GDP and Final GDP both stem to "gdp release".
                # Letting get_or_create match on it fuses two components that the
                # timestamps deliberately kept apart, so the key is made unique
                # before it is used.
                key = _key(currency, labels)
                if taken.get(key, members) != members:
                    suffix = 2
                    while taken.get(f"{key}_{suffix}", members) != members:
                        suffix += 1
                    key = f"{key}_{suffix}"
                taken[key] = members
                group, _created = ReleaseGroup.objects.get_or_create(
                    key=key,
                    defaults=dict(
                        name=_name(labels),
                        currency=currency,
                        description=(
                            f"{len(labels)} indicators published at one instant: "
                            f"{', '.join(labels)}. Derived from the release timestamps, "
                            f"not assigned by hand. §3.3 Case B — the move is attributed "
                            f"to the group, never to one member."
                        ),
                    ),
                )
            assigned += Indicator.objects.filter(pk__in=members).update(
                release_group=group
            )

        empty = [
            group.key
            for group in ReleaseGroup.objects.filter(indicators__isnull=True).distinct()
        ]
        if empty:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"{len(empty)} group(s) hold no indicators and are left untouched: "
                    + ", ".join(sorted(empty))
                    + ". A seeded group is only adopted when its membership already "
                    "matches a derived one, so an empty one stays as it is rather than "
                    "being guessed at."
                )
            )
        return assigned


def _name(labels: list[str]) -> str:
    """The stem every member shares, so 'CPI m/m' + 'Core CPI y/y' reads as
    'CPI release', falling back to naming the members when there is no stem.

    Only the frequency tokens are dropped.  A qualifier like *Advance* or
    *Final* survives precisely when every member carries it — which is exactly
    when it distinguishes this report from another one sharing the same stem,
    and USD Advance GDP and Final GDP are different releases at different
    instants.
    """
    words = [set(re.findall(r"[A-Za-z]+", label.lower())) for label in labels]
    shared = set.intersection(*words) if words else set()
    shared -= {"m", "y", "q"}
    if shared:
        ordered, seen = [], set()
        for word in re.findall(r"[A-Za-z]+", labels[0]):
            if word.lower() in shared and word.lower() not in seen:
                ordered.append(word)
                seen.add(word.lower())
        if ordered:
            return " ".join(ordered) + " release"
    return " + ".join(labels)


def _key(currency: str, labels: list[str]) -> str:
    stem = re.sub(r"[^a-z0-9]+", "_", _name(labels).lower()).strip("_")
    return f"{currency.lower()}_{stem}"[:100]
