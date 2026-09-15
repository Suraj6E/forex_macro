"""Timestamp validation against price.

Constructed series where the answer is known by hand: a market that is still
except for one hour, whose location we choose. If the check cannot find a spike
we put there on purpose, it cannot be trusted to find one we did not.
"""

import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

from analytics import volcheck

START = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
#: A Friday 13:30 release, the shape of most of the US calendar.
T0 = datetime(2024, 9, 6, 13, 30, tzinfo=timezone.utc)

NORMAL_RANGE = 0.0010  # ten pips of ordinary hourly range
PRICE = 1.1000


def quiet_bars(hours: int = 7000) -> pd.DataFrame:
    """Every hour identical: the same small range, no drift.

    `high`/`low` carry the volatility here because the check measures the
    Parkinson range — a release that whipsaws and comes back is a violent
    reaction with a close-to-open return of zero.
    """
    stamps = pd.date_range(START, periods=hours, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "ts_utc": stamps,
            "open": PRICE,
            "high": PRICE + NORMAL_RANGE / 2,
            "low": PRICE - NORMAL_RANGE / 2,
            "close": PRICE,
            "volume": 1.0,
        }
    )


def with_spike(frame: pd.DataFrame, when: datetime, size: float = 8.0) -> pd.DataFrame:
    """Widen exactly one bar to `size` times the normal range."""
    frame = frame.copy()
    bar = frame["ts_utc"] == pd.Timestamp(when).floor("1h")
    assert bar.any(), "test asked for a spike outside the constructed series"
    frame.loc[bar, "high"] = PRICE + NORMAL_RANGE * size / 2
    frame.loc[bar, "low"] = PRICE - NORMAL_RANGE * size / 2
    return frame


class BarIndexTests(unittest.TestCase):
    def test_bar_size_is_read_from_the_stamps_not_assumed(self):
        # The Parquet store writes microsecond stamps. Assuming nanoseconds
        # rescales every timestamp and yields a "bar size" of four seconds,
        # after which every lookup misses and every release reads uncheckable.
        frame = quiet_bars(48)
        frame["ts_utc"] = frame["ts_utc"].astype("datetime64[us, UTC]")
        self.assertEqual(volcheck.BarIndex(frame).bar_seconds, 3600)

    def test_an_instant_resolves_to_the_bar_containing_it(self):
        index = volcheck.BarIndex(with_spike(quiet_bars(), T0))
        on_the_half_hour = index.range_at(T0)
        on_the_hour = index.range_at(T0.replace(minute=0))
        self.assertIsNotNone(on_the_half_hour)
        self.assertAlmostEqual(on_the_half_hour, on_the_hour)

    def test_finer_bars_are_folded_up_to_the_hour(self):
        # The neighbourhood is counted in bars, so an M1 backbone scanned as-is
        # would be a ±3 *minute* window — blind to exactly the hour-scale bug
        # this check exists to find. Minute bars become hourly high/low first.
        minutes = pd.date_range(START, periods=600, freq="1min", tz="UTC")
        frame = pd.DataFrame(
            {
                "ts_utc": minutes,
                "open": PRICE,
                "high": PRICE + NORMAL_RANGE / 2,
                "low": PRICE - NORMAL_RANGE / 2,
                "close": PRICE,
            }
        )
        index = volcheck.BarIndex(frame)
        self.assertEqual(index.bar_seconds, 3600)
        self.assertEqual(len(index), 10)

    def test_a_missing_bar_reads_as_missing_not_as_zero(self):
        frame = quiet_bars()
        frame = frame[frame["ts_utc"] != pd.Timestamp(T0).floor("1h")]
        self.assertIsNone(volcheck.BarIndex(frame).range_at(T0))


class SingleReleaseTests(unittest.TestCase):
    def test_a_spike_in_the_stored_hour_confirms_the_timestamp(self):
        index = volcheck.BarIndex(with_spike(quiet_bars(), T0))
        result = volcheck.check_release(index, T0)
        self.assertEqual(result.status, volcheck.CONFIRMED_HOUR)
        self.assertEqual(result.offset_minutes, 0)
        self.assertAlmostEqual(result.ratio, 8.0, places=1)

    def test_a_spike_an_hour_later_is_reported_as_an_offset(self):
        # The signature of a daylight-saving or source-clock bug.
        index = volcheck.BarIndex(with_spike(quiet_bars(), T0 + timedelta(hours=1)))
        result = volcheck.check_release(index, T0)
        self.assertEqual(result.status, volcheck.OFFSET)
        self.assertEqual(result.offset_minutes, 60)

    def test_a_spike_an_hour_earlier_is_signed_the_other_way(self):
        index = volcheck.BarIndex(with_spike(quiet_bars(), T0 - timedelta(hours=1)))
        result = volcheck.check_release(index, T0)
        self.assertEqual(result.status, volcheck.OFFSET)
        self.assertEqual(result.offset_minutes, -60)

    def test_a_still_market_is_no_spike_rather_than_an_offset(self):
        # Nothing happened anywhere nearby. For a low-impact release this is
        # the expected answer, and calling it an offset would invent a bug.
        index = volcheck.BarIndex(quiet_bars())
        result = volcheck.check_release(index, T0)
        self.assertEqual(result.status, volcheck.NO_SPIKE)
        self.assertIsNone(result.offset_minutes)

    def test_a_neighbour_barely_ahead_does_not_overturn_the_stored_hour(self):
        # Two adjacent hours of one news event are routinely within a few
        # percent of each other; that is not evidence the timestamp is wrong.
        frame = with_spike(quiet_bars(), T0, size=8.0)
        frame = with_spike(frame, T0 + timedelta(hours=1), size=8.4)
        result = volcheck.check_release(volcheck.BarIndex(frame), T0)
        self.assertEqual(result.status, volcheck.CONFIRMED_HOUR)

    def test_a_spike_owned_by_a_bigger_release_is_refused_not_blamed(self):
        # The 08:30 New York cluster two hours before a 10:00 release. Calling
        # that an offset invents a clock bug out of someone else's news.
        index = volcheck.BarIndex(with_spike(quiet_bars(), T0 - timedelta(hours=2)))
        result = volcheck.check_release(index, T0, blocked={-2 * 3600})
        self.assertEqual(result.status, volcheck.CONFOUNDED)
        self.assertEqual(result.offset_minutes, -120)

    def test_blocking_does_not_suppress_the_release_own_hour(self):
        # Only neighbouring bars can be blocked: a heavyweight in the stored
        # hour is a coincidence of our timestamp being where it is, not a
        # mechanism that manufactures confirmations.
        index = volcheck.BarIndex(with_spike(quiet_bars(), T0))
        result = volcheck.check_release(index, T0, blocked={-3600, 3600})
        self.assertEqual(result.status, volcheck.CONFIRMED_HOUR)

    def test_an_unblocked_offset_is_still_reported(self):
        index = volcheck.BarIndex(with_spike(quiet_bars(), T0 + timedelta(hours=1)))
        result = volcheck.check_release(index, T0, blocked={-2 * 3600})
        self.assertEqual(result.status, volcheck.OFFSET)

    def test_a_missing_release_bar_is_uncheckable_not_a_verdict(self):
        frame = quiet_bars()
        frame = frame[frame["ts_utc"] != pd.Timestamp(T0).floor("1h")]
        result = volcheck.check_release(volcheck.BarIndex(frame), T0)
        self.assertEqual(result.status, volcheck.UNCHECKABLE)
        self.assertFalse(result.graded)

    def test_no_history_behind_a_release_is_uncheckable(self):
        # The first weeks of the price series have nothing to be normal
        # against. Grading them would compare against two observations.
        index = volcheck.BarIndex(with_spike(quiet_bars(), START + timedelta(hours=10)))
        result = volcheck.check_release(index, START + timedelta(hours=10))
        self.assertEqual(result.status, volcheck.UNCHECKABLE)


class BaselineTests(unittest.TestCase):
    def test_the_normal_is_the_matching_weekday_and_hour(self):
        index = volcheck.BarIndex(quiet_bars())
        normal, n = volcheck.normal_range(index, T0)
        self.assertEqual(n, volcheck.BASELINE_WEEKS)
        self.assertAlmostEqual(normal, index.range_at(T0), places=9)

    def test_the_indicators_own_history_can_be_excluded(self):
        # A weekly release recurs at the same weekday and hour, so every
        # look-back lands on a previous instance of itself. Compared against
        # itself the ratio collapses to 1 and a good timestamp reads no_spike.
        # Twenty weeks of its own history, and clean matching slots behind
        # those for the baseline to walk back to.
        own = [T0 - timedelta(weeks=week) for week in range(0, 21)]
        frame = quiet_bars()
        for moment in own:
            frame = with_spike(frame, moment)
        index = volcheck.BarIndex(frame)

        self.assertEqual(
            volcheck.check_release(index, T0).status, volcheck.NO_SPIKE
        )
        self.assertEqual(
            volcheck.check_release(
                index, T0, exclude=volcheck.exclusion_epochs(own)
            ).status,
            volcheck.CONFIRMED_HOUR,
        )


class VerdictTests(unittest.TestCase):
    @staticmethod
    def rows(confirmed=0, offsets=(), quiet=0, confounded=0):
        out = [(volcheck.CONFIRMED_HOUR, 0, 3.0)] * confirmed
        out += [(volcheck.OFFSET, minutes, 3.0) for minutes in offsets]
        out += [(volcheck.NO_SPIKE, None, 1.0)] * quiet
        out += [(volcheck.CONFOUNDED, -120, 4.0)] * confounded
        return out

    def test_confounded_releases_vote_on_nothing(self):
        # Twenty releases in the shadow of a bigger one must not drag an
        # otherwise clean verdict towards "quiet", nor count towards the gate.
        clean = volcheck.summarise(self.rows(confirmed=14, offsets=(60, -60), quiet=4))
        shadowed = volcheck.summarise(
            self.rows(confirmed=14, offsets=(60, -60), quiet=4, confounded=20)
        )
        self.assertEqual(clean.status, shadowed.status)
        self.assertEqual(clean.modal_share, shadowed.modal_share)
        self.assertEqual(shadowed.confounded, 20)
        self.assertEqual(shadowed.attributable, clean.attributable)

    def test_an_indicator_that_is_always_in_a_shadow_gets_no_verdict(self):
        verdict = volcheck.summarise(self.rows(confirmed=3, quiet=2, confounded=90))
        self.assertEqual(verdict.status, volcheck.INSUFFICIENT)
        self.assertIn("bigger release", verdict.message)

    def test_mostly_on_time_reads_as_aligned(self):
        verdict = volcheck.summarise(self.rows(confirmed=18, offsets=(60, -60), quiet=2))
        self.assertEqual(verdict.status, volcheck.ALIGNED)
        self.assertEqual(verdict.modal_offset_minutes, 0)

    def test_agreement_on_one_wrong_offset_reads_as_a_broken_clock(self):
        verdict = volcheck.summarise(self.rows(confirmed=2, offsets=(60,) * 16, quiet=2))
        self.assertEqual(verdict.status, volcheck.OFFSET_CLOCK)
        self.assertEqual(verdict.modal_offset_hours, 1)
        self.assertTrue(verdict.wrong)
        self.assertIn("1h later", verdict.message)

    def test_spikes_everywhere_read_as_scattered_not_as_a_clock_bug(self):
        verdict = volcheck.summarise(
            self.rows(confirmed=4, offsets=(60, 60, 120, -60, -120, 180, -180, 120))
        )
        self.assertEqual(verdict.status, volcheck.SCATTERED)
        self.assertFalse(verdict.wrong)

    def test_a_modest_share_still_counts_when_the_window_has_seven_bars(self):
        # Chance alone puts about 14% in each bar of the scan, so a third of
        # several hundred spikes landing on time is decisive evidence the clock
        # is right — a flat "needs a majority" rule would call this scattered.
        verdict = volcheck.summarise(
            self.rows(confirmed=80, offsets=(60,) * 40 + (-60,) * 40 + (120,) * 80)
        )
        self.assertEqual(verdict.modal_offset_minutes, 0)
        self.assertLess(verdict.modal_share, 0.5)
        self.assertEqual(verdict.status, volcheck.ALIGNED)

    def test_a_bin_barely_above_chance_decides_nothing(self):
        # The mirror image: with enough spikes, a bin 2 points above the null
        # is "significant" and substantively meaningless. Calling that a broken
        # clock would manufacture bugs out of large samples.
        verdict = volcheck.summarise(
            self.rows(confirmed=140, offsets=(60,) * 170 + (-60,) * 160 + (120,) * 160
                      + (-120,) * 160 + (180,) * 155 + (-180,) * 155)
        )
        self.assertEqual(verdict.status, volcheck.SCATTERED)

    def test_a_quiet_indicator_is_unverified_rather_than_wrong(self):
        verdict = volcheck.summarise(self.rows(confirmed=1, offsets=(60,), quiet=30))
        self.assertEqual(verdict.status, volcheck.SILENT)
        self.assertFalse(verdict.wrong)

    def test_too_few_releases_gets_no_verdict_at_all(self):
        verdict = volcheck.summarise(self.rows(confirmed=5))
        self.assertEqual(verdict.status, volcheck.INSUFFICIENT)

    def test_an_even_split_resolves_towards_the_stored_timestamp(self):
        # A tie is not evidence of a bug, and the verdict must not depend on
        # which grade happened to be read first.
        verdict = volcheck.summarise(self.rows(confirmed=10, offsets=(60,) * 10))
        self.assertEqual(verdict.modal_offset_minutes, 0)
        self.assertEqual(verdict.status, volcheck.ALIGNED)

    def test_counts_and_rows_give_the_same_verdict(self):
        # The screen pools rows; the Data Quality page pools a GROUP BY. If the
        # two ever disagree, one page is lying about the same database.
        rows = self.rows(confirmed=3, offsets=(60,) * 15, quiet=4)
        self.assertEqual(
            volcheck.summarise(rows).status,
            volcheck.summarise_counts(offsets={0: 3, 60: 15}, no_spike=4).status,
        )


class SweepWideTests(unittest.TestCase):
    """A sweep tests one hypothesis per indicator, so it needs FDR (§6.9)."""

    @staticmethod
    def spread(top: int, *, at: int = 60, spikes: int = 100, bins: int = 7):
        """`top` spikes in one bin, the rest shared evenly among the others."""
        others = [b * 60 for b in range(-(bins // 2), bins // 2 + 1) if b * 60 != at]
        offsets = {at: top}
        remaining = spikes - top
        for index, bin_minutes in enumerate(others):
            share = remaining // len(others) + (1 if index < remaining % len(others) else 0)
            offsets[bin_minutes] = share
        return volcheck.summarise_counts(offsets=offsets, no_spike=0)

    def test_one_marginal_offset_among_hundreds_does_not_survive(self):
        # Three hundred indicators tested at p < 0.01 hand back a few "broken
        # clocks" by luck alone — and a false alarm here sends you hunting a
        # timezone bug that was never there.
        marginal = self.spread(26, at=60)
        self.assertEqual(marginal.status, volcheck.OFFSET_CLOCK)

        sweep = [marginal] + [self.spread(20, at=0) for _ in range(299)]
        volcheck.apply_fdr(sweep)
        self.assertEqual(marginal.status, volcheck.SCATTERED)
        self.assertGreater(marginal.offset_p_fdr, marginal.offset_p)

    def test_a_blatant_offset_survives_the_whole_sweep(self):
        blatant = self.spread(80, at=60)
        volcheck.apply_fdr([blatant] + [self.spread(20, at=0) for _ in range(299)])
        self.assertEqual(blatant.status, volcheck.OFFSET_CLOCK)

    def test_alignment_is_never_downgraded_by_the_correction(self):
        # "The spike is where the timestamp says" is this test's null, not a
        # discovery against it.
        aligned = self.spread(40, at=0)
        self.assertEqual(aligned.status, volcheck.ALIGNED)
        volcheck.apply_fdr([aligned] + [self.spread(30, at=60) for _ in range(50)])
        self.assertEqual(aligned.status, volcheck.ALIGNED)

    def test_every_checked_indicator_is_part_of_the_family(self):
        # Adjusting only the verdicts that came out significant would pretend
        # the other tests never ran, which is the mistake FDR exists to stop.
        aligned = self.spread(20, at=0)
        volcheck.apply_fdr([aligned])
        self.assertIsNotNone(aligned.offset_p)
        self.assertIsNotNone(aligned.offset_p_fdr)


class DaylightSavingTests(unittest.TestCase):
    def test_summer_and_winter_are_told_apart(self):
        # 13:30 UTC is 08:30 in New York in summer; in winter that same local
        # release lands at 14:30 UTC. Pooling the two hides a DST bug.
        self.assertEqual(volcheck.dst_window(T0), "EDT")
        self.assertEqual(
            volcheck.dst_window(datetime(2024, 1, 5, 14, 30, tzinfo=timezone.utc)), "EST"
        )


if __name__ == "__main__":
    unittest.main()
