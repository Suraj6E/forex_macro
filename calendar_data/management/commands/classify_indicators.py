"""Assign `Indicator.concept` from the indicator's name — §6.4's pooling axis.

§6.4 wants event *types* ranked by measured impact and the result compared
against the traffic-light rating the calendar sites publish.  "US CPI" is not a
type; *inflation* is, and it spans eight economies.  That grouping is what turns
670 individual series into an answerable question.

The rules are ordered and the first match wins, which is the whole design.  The
order encodes what a name means when two readings compete:

* **Speeches and political events go first.**  "Fed Chair Powell Speaks" and
  "BOJ Policy Rate" both contain a central bank, and only the second is a
  number.  Reading the rate first would file every central banker's diary under
  monetary policy.
* **Policy rate before inflation**, or "Prelim UoM Inflation Expectations"-style
  names attached to rate decisions drift into the wrong family.
* **Labour before growth**, because "Unit Labor Costs" is a labour series that
  also contains "costs".

Everything the rules miss is left `unclassified` rather than guessed at, and
the command prints what it could not place so the list can be read.  A wrong
concept is worse than a missing one: it puts a series into a pooled average
where it does not belong and nothing downstream can tell.

    manage.py classify_indicators            # what it would do
    manage.py classify_indicators --apply    # do it
"""

from __future__ import annotations

import re
from collections import defaultdict

from django.core.management.base import BaseCommand

from calendar_data.models import Concept, Indicator

#: Ordered. First match wins — see the module docstring for why the order is
#: not alphabetical and must not be made so.
RULES: list[tuple[str, str]] = [
    (
        Concept.SPEECH,
        r"speaks|testifies|testimony|press conference|remarks|panel|symposium|q&a"
        r"|holds|address|speech|hearing",
    ),
    (
        Concept.POLITICAL,
        r"election|referendum|\bvote\b|summit|court ruling|stress test|treaty"
        r"|bailout|coalition|confidence motion|spending review|parliament"
        r"|ruling"
        r"|nomination|constitutional",
    ),
    (
        Concept.POLICY_RATE,
        r"rate statement|bank rate|cash rate|funds rate|policy rate|overnight rate"
        r"|overnight call|refinancing rate|refinancing operation|rate decision"
        r"|monetary policy|asset purchase|libor|fed announcement|\bminutes\b"
        r"|\bfomc\b"
        r"|official.*rate|financial stability|economic projections|outlook report"
        r"|monthly bulletin|statement of intent|credit conditions",
    ),
    (
        Concept.LABOUR,
        r"employ|unemploy|payroll|jobless|claimant|jolts|earnings|labor cost"
        r"|labour cost|\bwage|\bjob",
    ),
    (
        Concept.INFLATION,
        r"\bcpi\b|\bppi\b|\brpi\b|inflation|price index|gdp price|retail price"
        r"|import price|export price|wholesale price|producer price|\bhpi\b",
    ),
    (
        Concept.CONSUMPTION,
        r"retail sales|consumer spending|personal spending|durable goods"
        r"|household spending|vehicle sales|consumer credit|high street lending"
        r"|consumption indicator",
    ),
    (
        Concept.HOUSING,
        r"housing|home sales|home loans|building permits|building approvals"
        r"|building consents|construction|mortgage|house price|nahb|reinz",
    ),
    (
        Concept.SURVEY,
        r"\bpmi\b|\bism\b|tankan|\bzew\b|\bifo\b|sentix|\bgfk\b|business climate"
        r"|business confidence|business outlook|business survey|business investment"
        r"|consumer confidence|consumer sentiment|consumer climate|\bnbb\b"
        r"|economic sentiment|empire state|philly fed|richmond|manufacturing index",
    ),
    (
        Concept.GROWTH,
        r"\bgdp\b|industrial production|manufacturing production|manufacturing sales"
        r"|capacity util|productivity|capital expenditure|machinery orders"
        r"|factory orders|new orders|\boutput\b",
    ),
    (Concept.TRADE, r"trade balance|current account|goods trade|\bexports\b|\bimports\b|terms of trade"),
    (Concept.ENERGY, r"crude oil|natural gas|oil inventor|gasoline"),
    (
        Concept.FISCAL,
        r"budget|public sector|\bdebt\b|deficit|treasury|auction|borrowing"
        r"|fiscal outlook|forecast statement|stimulus package",
    ),
]


def classify(name: str) -> str:
    low = name.lower()
    for concept, pattern in RULES:
        if re.search(pattern, low):
            return concept
    return Concept.UNCLASSIFIED


class Command(BaseCommand):
    help = "Derive Indicator.concept from indicator names (§6.4)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--min-importance",
            type=int,
            default=0,
            help="Only classify indicators at or above this tier. Default 0 (all).",
        )
        parser.add_argument(
            "--apply", action="store_true", help="Write. Without it, only report."
        )

    def handle(self, *args, **options):
        indicators = Indicator.objects.filter(
            importance__gte=options["min_importance"]
        ).order_by("currency", "name")

        buckets = defaultdict(list)
        changes = []
        for indicator in indicators:
            concept = classify(indicator.name)
            buckets[concept].append(indicator)
            if indicator.concept != concept:
                changes.append((indicator, concept))

        total = sum(len(v) for v in buckets.values())
        labels = dict(Concept.choices)
        self.stdout.write(f"{total} indicator(s) considered.")
        self.stdout.write("")
        for concept, _pattern in RULES:
            members = buckets.get(concept, [])
            if members:
                self.stdout.write(f"  {labels[concept]:<58} {len(members):>4}")

        missed = buckets.get(Concept.UNCLASSIFIED, [])
        self.stdout.write(
            f"  {'unclassified — left alone rather than guessed at':<58} {len(missed):>4}"
        )
        if missed:
            self.stdout.write("")
            for indicator in missed[:40]:
                self.stdout.write(f"    [{indicator.importance}] {indicator.currency} {indicator.name}")
            if len(missed) > 40:
                self.stdout.write(f"    … and {len(missed) - 40} more")

        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write(f"{len(changes)} would change. Nothing written; pass --apply.")
            return

        for indicator, concept in changes:
            indicator.concept = concept
        Indicator.objects.bulk_update([i for i, _ in changes], ["concept"], batch_size=500)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"{len(changes)} indicator(s) updated."))
