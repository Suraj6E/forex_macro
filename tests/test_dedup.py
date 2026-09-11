"""Deduplication and quality checks.

The merges here delete rows, so the tests are about what *survives*: every
source's raw claim, the revision history, and the canonical code.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.test import TestCase

from calendar_data.models import (
    EventRelease,
    Indicator,
    IndicatorAlias,
    SourceObservation,
    ValueRevision,
)
from consolidation import dedup
from quality import checks
from quality.enums import MappingStatus
from sources.models import Source, SourceKind

T0 = datetime(2024, 9, 6, 12, 30, tzinfo=timezone.utc)


class NameNormalisationTests(TestCase):
    def test_case_and_punctuation_are_ignored(self):
        self.assertEqual(
            dedup.normalise_name("Non-Farm Employment Change"),
            dedup.normalise_name("non farm employment change"),
        )

    def test_period_markers_are_preserved(self):
        # "CPI m/m" and "CPI y/y" are different indicators. Collapsing them
        # would be a far worse error than leaving a duplicate in place.
        self.assertNotEqual(dedup.normalise_name("CPI m/m"), dedup.normalise_name("CPI y/y"))

    def test_related_but_distinct_series_stay_below_the_threshold(self):
        # Different transformations of one series must never be suggested as
        # duplicates — merging them would fuse two economic series silently.
        self.assertLess(dedup.similarity("CPI m/m", "CPI y/y"), dedup.SIMILARITY_FLOOR)

    def test_the_same_series_named_differently_is_suggested(self):
        self.assertGreaterEqual(
            dedup.similarity("US Unemployment Rate", "Unemployment Rate"),
            dedup.SIMILARITY_FLOOR,
        )


class SimilarCandidateProvenanceTests(TestCase):
    """Two indicators from the same source are two things that source chose to
    keep apart. Suggesting a merge there is how "ADP Non-Farm Employment
    Change" gets fused into "Non-Farm Employment Change"."""

    def setUp(self):
        self.a = Source.objects.create(key="forexfactory_pages", name="FFP", kind=SourceKind.CALENDAR)
        self.b = Source.objects.create(key="mt5_calendar", name="MT5", kind=SourceKind.CALENDAR)

    def _indicator(self, name, source, key):
        indicator = Indicator.objects.create(currency="USD", name=name)
        IndicatorAlias.objects.create(source=source, source_key=key, indicator=indicator)
        return indicator

    def test_same_source_pairs_are_not_suggested(self):
        self._indicator("Non-Farm Employment Change", self.a, "ff:66")
        self._indicator("ADP Non-Farm Employment Change", self.a, "ff:75")
        self.assertEqual(dedup.similar_candidate_groups(), [])

    def test_cross_source_pairs_are_suggested(self):
        self._indicator("US Unemployment Rate", self.a, "ff:1")
        self._indicator("Unemployment Rate", self.b, "mt5:1")
        groups = dedup.similar_candidate_groups()
        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0].exact)


class IndicatorMergeTests(TestCase):
    def setUp(self):
        self.ff = Source.objects.create(
            key="forexfactory_weekly", name="FF", kind=SourceKind.CALENDAR
        )
        self.mt5 = Source.objects.create(
            key="mt5_calendar", name="MT5", kind=SourceKind.CALENDAR
        )

    def _indicator(self, name, code=None):
        return Indicator.objects.create(
            currency="USD",
            name=name,
            canonical_code=code,
            mapping_status=MappingStatus.MAPPED if code else MappingStatus.AUTO,
        )

    def test_exact_groups_find_same_name_different_spelling(self):
        self._indicator("Non-Farm Employment Change")
        self._indicator("non farm employment change")
        self._indicator("CPI m/m")

        groups = dedup.exact_duplicate_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].members), 2)

    def test_mapped_indicator_is_kept_as_the_survivor(self):
        plain = self._indicator("Nonfarm Payrolls")
        mapped = self._indicator("nonfarm payrolls", code="US_NFP")
        EventRelease.objects.create(indicator=plain, reference_period="2024-08")

        group = dedup.exact_duplicate_groups()[0]
        self.assertEqual(group.primary.pk, mapped.pk, "the canonical code must survive")

    def test_merge_moves_releases_and_aliases(self):
        keep = self._indicator("Nonfarm Payrolls", code="US_NFP")
        drop = self._indicator("Non-Farm Employment Change")
        IndicatorAlias.objects.create(source=self.ff, source_key="USD|NFP", indicator=drop)
        EventRelease.objects.create(indicator=drop, reference_period="2024-08")

        result = dedup.merge_indicators(keep, [drop])

        self.assertEqual(result["moved_releases"], 1)
        self.assertEqual(result["moved_aliases"], 1)
        self.assertFalse(Indicator.objects.filter(pk=drop.pk).exists())
        self.assertEqual(keep.releases.count(), 1)
        self.assertEqual(IndicatorAlias.objects.get().indicator_id, keep.pk)

    def test_colliding_releases_are_folded_not_duplicated(self):
        keep = self._indicator("Nonfarm Payrolls", code="US_NFP")
        drop = self._indicator("non farm payrolls")

        kept_release = EventRelease.objects.create(
            indicator=keep,
            reference_period="2024-08",
            previous=Decimal("89000"),
            source_map_json={"previous": "mt5_calendar"},
        )
        dropped_release = EventRelease.objects.create(
            indicator=drop,
            reference_period="2024-08",
            actual_current=Decimal("142000"),
            source_map_json={"actual_current": "forexfactory_weekly"},
        )
        SourceObservation.objects.create(
            event_release=dropped_release, source=self.ff, raw_json={"x": 1}
        )

        dedup.merge_indicators(keep, [drop])

        self.assertEqual(EventRelease.objects.count(), 1)
        kept_release.refresh_from_db()
        # Both sources' contributions survive on the single surviving row.
        self.assertEqual(kept_release.previous, Decimal("89000"))
        self.assertEqual(kept_release.actual_current, Decimal("142000"))
        self.assertEqual(SourceObservation.objects.get().event_release_id, kept_release.pk)

    def test_merging_adopts_a_canonical_code_from_the_absorbed_row(self):
        keep = self._indicator("Nonfarm Payrolls")
        drop = self._indicator("nonfarm payrolls", code="US_NFP")
        dedup.merge_indicators(keep, [drop])
        keep.refresh_from_db()
        self.assertEqual(keep.canonical_code, "US_NFP")
        self.assertEqual(keep.mapping_status, MappingStatus.MAPPED)


class ProvisionalDuplicateTests(TestCase):
    """The duplicate that minute-level provisional keys deliberately allow."""

    def setUp(self):
        self.source = Source.objects.create(
            key="forexfactory_weekly", name="FF", kind=SourceKind.CALENDAR
        )
        self.indicator = Indicator.objects.create(currency="USD", name="ADP Weekly")

    def _release(self, minute_offset, previous):
        when = T0 + timedelta(minutes=minute_offset)
        release = EventRelease.objects.create(
            indicator=self.indicator,
            reference_period=f"release:{when:%Y-%m-%dT%H:%M}",
            scheduled_time_utc=when,
            previous=Decimal(previous),
            source_map_json={"previous": "forexfactory_weekly"},
        )
        SourceObservation.objects.create(
            event_release=release, source=self.source, raw_json={}
        )
        return release

    def test_rows_one_minute_apart_are_one_event(self):
        self._release(0, "11800")
        self._release(1, "10000")
        groups = dedup.provisional_duplicate_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_rows_far_apart_are_two_events(self):
        # Two ECB speeches on one day must not be collapsed.
        self._release(0, "1")
        self._release(300, "2")
        self.assertEqual(dedup.provisional_duplicate_groups(), [])

    def test_different_indicators_are_never_grouped(self):
        self._release(0, "1")
        other = Indicator.objects.create(currency="USD", name="Something Else")
        EventRelease.objects.create(
            indicator=other,
            reference_period=f"release:{T0:%Y-%m-%dT%H:%M}",
            scheduled_time_utc=T0,
        )
        self.assertEqual(dedup.provisional_duplicate_groups(), [])

    def test_periodkeyed_rows_are_never_grouped(self):
        EventRelease.objects.create(
            indicator=self.indicator, reference_period="2024-08", scheduled_time_utc=T0
        )
        EventRelease.objects.create(
            indicator=self.indicator,
            reference_period="2024-09",
            scheduled_time_utc=T0 + timedelta(minutes=1),
        )
        self.assertEqual(dedup.provisional_duplicate_groups(), [])

    def test_collapsing_records_the_revision_the_fork_hid(self):
        first = self._release(0, "11800")
        self._release(1, "10000")

        result = dedup.merge_provisional_duplicates()

        self.assertEqual(result["absorbed"], 1)
        self.assertEqual(result["revisions"], 1)
        self.assertEqual(EventRelease.objects.count(), 1)

        survivor = EventRelease.objects.get()
        self.assertEqual(survivor.pk, first.pk)
        # The later claim wins, and the change is on the record — exactly what
        # would have happened had the key not forked.
        self.assertEqual(survivor.previous, Decimal("10000"))
        revision = ValueRevision.objects.get()
        self.assertEqual(revision.field, "previous")
        self.assertIn("11800", revision.old_value)
        self.assertIn("10000", revision.new_value)
        self.assertEqual(SourceObservation.objects.count(), 2, "both claims survive")


class PurgeTests(TestCase):
    def setUp(self):
        self.a = Source.objects.create(key="dbnomics", name="A", kind=SourceKind.ACTUALS)
        self.b = Source.objects.create(key="mt5_calendar", name="B", kind=SourceKind.CALENDAR)
        self.indicator = Indicator.objects.create(currency="USD", name="CPI")

    def test_purge_removes_only_rows_no_one_else_claims(self):
        solo = EventRelease.objects.create(indicator=self.indicator, reference_period="2024-01")
        shared = EventRelease.objects.create(indicator=self.indicator, reference_period="2024-02")
        SourceObservation.objects.create(event_release=solo, source=self.a, raw_json={})
        SourceObservation.objects.create(event_release=shared, source=self.a, raw_json={})
        SourceObservation.objects.create(event_release=shared, source=self.b, raw_json={})

        result = dedup.purge_source(self.a)

        self.assertEqual(result["deleted_releases"], 1)
        self.assertFalse(EventRelease.objects.filter(pk=solo.pk).exists())
        self.assertTrue(EventRelease.objects.filter(pk=shared.pk).exists())


class CheckTests(TestCase):
    def test_a_real_trillion_is_not_flagged_as_absurd(self):
        # Japan's current account is genuinely of order ¥10¹². A threshold that
        # flags it reports correct data as broken.
        indicator = Indicator.objects.create(currency="JPY", name="Current Account")
        EventRelease.objects.create(
            indicator=indicator,
            reference_period="2024-07",
            forecast_stored=Decimal("2480000000000"),
        )
        finding = next(f for f in checks.run_checks() if f.code == "absurd_values")
        self.assertEqual(finding.count, 0)

    def test_long_min_is_detected_raw_and_scaled(self):
        indicator = Indicator.objects.create(currency="USD", name="Broken")
        EventRelease.objects.create(
            indicator=indicator, reference_period="2024-01", previous=checks.LONG_MIN_SCALED
        )
        finding = next(f for f in checks.run_checks() if f.code == "long_min_sentinel")
        self.assertEqual(finding.count, 1)
        self.assertEqual(finding.severity, checks.BLOCKING)

    def test_timestampless_rows_are_reported(self):
        indicator = Indicator.objects.create(currency="USD", name="Series")
        EventRelease.objects.create(indicator=indicator, reference_period="2024-01")
        EventRelease.objects.create(
            indicator=indicator, reference_period="2024-02", scheduled_time_utc=T0
        )
        finding = next(f for f in checks.run_checks() if f.code == "no_timestamp")
        self.assertEqual(finding.count, 1)
        self.assertEqual(finding.total, 2)

    def test_a_clean_dataset_reports_no_problems(self):
        findings = checks.run_checks()
        self.assertTrue(all(f.clean for f in findings if f.severity == checks.BLOCKING))
