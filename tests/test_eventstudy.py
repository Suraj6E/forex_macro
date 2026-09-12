"""Event study measurement.

These pin the arithmetic against constructed series where the right answer is
known by hand, so a regression shows up as a wrong number rather than a
plausible one.
"""

import math
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from analytics import eventstudy
from analytics.horizons import Horizon

T0 = datetime(2024, 9, 6, 12, 30, tzinfo=timezone.utc)
START = datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc)


def flat_bars(hours: int = 3000, price: float = 1.1) -> pd.DataFrame:
    """A perfectly still market: every bar identical."""
    stamps = pd.date_range(START, periods=hours, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "ts_utc": stamps,
            "open": price, "high": price, "low": price, "close": price,
            "volume": 1.0,
        }
    )


def stepped_bars(step_at: datetime, size: float, hours: int = 3000,
                 price: float = 1.1) -> pd.DataFrame:
    """Still, then a one-off jump of `size` at `step_at` that persists."""
    frame = flat_bars(hours, price)
    after = frame["ts_utc"] >= step_at
    for column in ("open", "high", "low", "close"):
        frame.loc[after, column] = price + size
    return frame


class HorizonSupportTests(unittest.TestCase):
    def test_hourly_bars_cannot_resolve_minute_rungs(self):
        # Returning a number for "+5m" from hourly data would look like a
        # measurement and be an artefact.
        labels = [h.label for h in eventstudy.supported_horizons(3600)]
        self.assertNotIn("+5m", labels)
        self.assertNotIn("+15m", labels)
        self.assertNotIn("-5m", labels)
        self.assertIn("+1h", labels)
        self.assertIn("+1d", labels)
        self.assertIn("-1d", labels)

    def test_minute_bars_resolve_almost_everything(self):
        labels = [h.label for h in eventstudy.supported_horizons(60)]
        self.assertIn("+1m", labels)
        self.assertIn("+5m", labels)
        self.assertIn("-5m", labels)


class PriceLookupTests(unittest.TestCase):
    def setUp(self):
        self.bars = flat_bars()

    def test_price_before_uses_the_open_of_the_containing_bar(self):
        # The release is at 12:30; the 12:00 bar's open is the last price
        # known before it. Using that bar's close would include the reaction.
        frame = self.bars.copy()
        frame.loc[frame["ts_utc"] == pd.Timestamp(T0).floor("h"), "open"] = 1.05
        frame.loc[frame["ts_utc"] == pd.Timestamp(T0).floor("h"), "close"] = 1.20
        self.assertAlmostEqual(eventstudy.price_before(frame, T0), 1.05, places=6)

    def test_price_at_uses_the_close(self):
        frame = self.bars.copy()
        frame.loc[frame["ts_utc"] == pd.Timestamp(T0).floor("h"), "close"] = 1.23
        self.assertAlmostEqual(eventstudy.price_at(frame, T0), 1.23, places=6)

    def test_a_moment_before_all_data_has_no_price(self):
        self.assertIsNone(eventstudy.price_before(self.bars, START - timedelta(days=5)))


class ReturnTests(unittest.TestCase):
    def test_a_still_market_returns_zero(self):
        bars = flat_bars()
        for label in ("+1h", "+1d", "-1d"):
            horizon = next(h for h in eventstudy.supported_horizons(3600) if h.label == label)
            self.assertAlmostEqual(eventstudy.raw_return(bars, T0, horizon), 0.0, places=9)

    def test_a_jump_after_the_release_is_measured_forward(self):
        bars = stepped_bars(T0, 0.011)          # +1.1% step, roughly 110 pips
        horizon = Horizon("+4h", 4 * 3600)
        value = eventstudy.raw_return(bars, T0, horizon)
        self.assertAlmostEqual(value, math.log(1.111 / 1.1), places=6)

    def test_a_pre_release_horizon_measures_the_move_into_the_event(self):
        # A step one day BEFORE t0 must show up in "-1d" and not in "+1d".
        bars = stepped_bars(T0 - timedelta(hours=12), 0.011)
        before = eventstudy.raw_return(bars, T0, Horizon("-1d", -86400))
        after = eventstudy.raw_return(bars, T0, Horizon("+1d", 86400))
        self.assertAlmostEqual(before, math.log(1.111 / 1.1), places=6)
        self.assertAlmostEqual(after, 0.0, places=9)

    def test_pre_release_sign_points_the_same_way_as_the_move(self):
        # A rise into the release must read positive, not negative.
        bars = stepped_bars(T0 - timedelta(hours=6), 0.011)
        self.assertGreater(eventstudy.raw_return(bars, T0, Horizon("-1d", -86400)), 0)


class WindowMeasurementTests(unittest.TestCase):
    def test_window_reports_bar_counts(self):
        bars = eventstudy._price_frame(flat_bars())
        m = eventstudy.measure_window(bars, T0, Horizon("+4h", 4 * 3600), 3600)
        self.assertEqual(m.bars_expected, 4)
        self.assertGreaterEqual(m.n_bars, 4)
        self.assertTrue(m.usable)

    def test_a_window_with_too_few_bars_is_not_measured(self):
        # A gap-shortened window reads as a smaller effect, so it is refused.
        bars = eventstudy._price_frame(flat_bars(hours=200))
        late = START + timedelta(hours=199)
        m = eventstudy.measure_window(bars, late, Horizon("+1w", 604800), 3600)
        self.assertFalse(m.usable)
        self.assertIsNone(m.ret)

    def test_excursions_are_relative_to_the_pre_release_price(self):
        bars = eventstudy._price_frame(flat_bars())
        hour = pd.Timestamp(T0).floor("h") + pd.Timedelta(hours=1)
        bars.loc[bars["ts_utc"] == hour, "high"] = 1.15
        bars.loc[bars["ts_utc"] == hour, "low"] = 1.05

        m = eventstudy.measure_window(bars, T0, Horizon("+4h", 4 * 3600), 3600)
        self.assertAlmostEqual(m.mfe, math.log(1.15 / 1.1), places=6)
        self.assertAlmostEqual(m.mae, math.log(1.05 / 1.1), places=6)


class BaselineTests(unittest.TestCase):
    def test_baseline_draws_from_the_same_weekday_and_hour(self):
        bars = flat_bars()
        mean, sd, abs_mean, n = eventstudy.matched_baseline(
            bars, T0, Horizon("+1h", 3600), weeks=8
        )
        self.assertEqual(n, 8)
        self.assertAlmostEqual(mean, 0.0, places=9)
        self.assertAlmostEqual(sd, 0.0, places=9)
        self.assertAlmostEqual(abs_mean, 0.0, places=9)

    def test_baseline_reports_the_normal_absolute_move(self):
        # Mode A compares against how far price usually travels, so the
        # baseline has to carry that separately from the signed mean.
        stamps = pd.date_range(START, periods=3000, freq="1h", tz="UTC")
        rng = np.random.default_rng(3)
        closes = 1.1 * np.exp(np.cumsum(rng.normal(0, 0.001, len(stamps))))
        frame = pd.DataFrame({
            "ts_utc": stamps, "open": closes, "close": closes,
            "high": closes * 1.0005, "low": closes * 0.9995, "volume": 1.0,
        })
        _mean, _sd, abs_mean, n = eventstudy.matched_baseline(
            frame, T0, Horizon("+1h", 3600), weeks=12
        )
        self.assertEqual(n, 12)
        self.assertGreater(abs_mean, 0.0)

    def test_too_few_matched_samples_gives_no_baseline(self):
        bars = flat_bars(hours=40)     # not enough history to look back weeks
        mean, sd, abs_mean, n = eventstudy.matched_baseline(
            bars, START + timedelta(hours=30), Horizon("+1h", 3600)
        )
        self.assertIsNone(mean)
        self.assertIsNone(abs_mean)
        self.assertLess(n, 3)

    def test_abnormal_return_removes_the_normal_drift(self):
        # A market that always rises 0.1% in this hour, plus a genuine event
        # jump: the abnormal move must be the jump alone.
        stamps = pd.date_range(START, periods=3000, freq="1h", tz="UTC")
        frame = pd.DataFrame({"ts_utc": stamps, "open": 1.1, "high": 1.1,
                              "low": 1.1, "close": 1.1, "volume": 1.0})
        drift = math.log(1.001)
        target_hour = pd.Timestamp(T0).floor("h")
        for offset in range(0, 20):
            moment = target_hour - pd.Timedelta(weeks=offset)
            mask = frame["ts_utc"] > moment
            frame.loc[mask, ["open", "high", "low", "close"]] *= math.exp(drift)

        measurements = eventstudy.measure_event(frame, T0, [Horizon("+1h", 3600)])
        m = measurements[0]
        self.assertIsNotNone(m.baseline_mean)
        self.assertAlmostEqual(m.baseline_mean, drift, places=4)
        self.assertAlmostEqual(m.abnormal_ret, 0.0, places=4)


class PoolingTests(unittest.TestCase):
    def _event(self, label: str, abnormal: float, normal_abs: float = 0.004) -> list:
        m = eventstudy.WindowMeasurement(horizon=label, seconds=3600)
        m.ret = abnormal
        m.abs_ret = abs(abnormal)
        m.abnormal_ret = abnormal
        m.baseline_abs_mean = normal_abs
        return [m]

    def test_pooling_averages_and_reports_n(self):
        events = [self._event("+1h", v) for v in (0.01, 0.02, 0.03)]
        points = eventstudy.pool(events)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].n, 3)
        self.assertAlmostEqual(points[0].mean_abnormal, 0.02, places=9)

    def test_a_symmetric_event_moves_price_without_a_signed_mean(self):
        # The heart of Mode A: half the releases push each way, so the signed
        # mean is ~0 while the excess movement is large and obvious. Reading
        # the signed mean would call this event inert.
        rng = np.random.default_rng(23)
        magnitudes = np.abs(rng.normal(0.010, 0.002, 40))
        events = [
            self._event("+1h", float(m) * (1 if i % 2 else -1))
            for i, m in enumerate(magnitudes)
        ]
        point = eventstudy.pool(events)[0]

        self.assertFalse(point.signed_significant, "direction cancels out")
        self.assertTrue(point.significant, "excess movement must still register")
        # The signed mean is an order of magnitude smaller than the excess:
        # reading it would report a violently reactive event as inert.
        self.assertLess(abs(point.mean_abnormal), point.mean_abs_excess / 5)

    def test_a_consistent_signed_effect_shows_in_both(self):
        events = [self._event("+1h", 0.01 + i * 1e-5) for i in range(40)]
        point = eventstudy.pool(events)[0]
        self.assertLess(point.p_value, 0.001)
        self.assertTrue(point.signed_significant)
        self.assertTrue(point.significant)

    def test_an_ordinary_window_is_not_significant(self):
        # Moves the same size as the usual move for that slot: no excess.
        rng = np.random.default_rng(7)
        events = [
            self._event("+1h", float(v), normal_abs=0.008)
            for v in rng.normal(0, 0.01, 60)
        ]
        point = eventstudy.pool(events)[0]
        self.assertGreater(point.p_value, 0.05)
        self.assertFalse(point.signed_significant)

    def test_ratio_reports_how_many_times_the_normal_move(self):
        events = [self._event("+1h", 0.008, normal_abs=0.004) for _ in range(10)]
        self.assertAlmostEqual(eventstudy.pool(events)[0].abs_ratio, 2.0, places=6)

    def test_detectability_floor_is_reported_so_nulls_mean_something(self):
        rng = np.random.default_rng(11)
        events = [self._event("+1h", float(v)) for v in rng.normal(0, 0.01, 50)]
        point = eventstudy.pool(events)[0]
        self.assertIsNotNone(point.detectability_floor)
        self.assertGreater(point.detectability_floor, 0)

    def test_hit_rate_counts_direction_consistency(self):
        events = [self._event("+1h", v) for v in (0.01, 0.01, 0.01, -0.01)]
        self.assertAlmostEqual(eventstudy.pool(events)[0].hit_rate, 0.75, places=9)

    def test_points_come_back_in_horizon_order(self):
        events = []
        for label, seconds in (("+1d", 86400), ("-1h", -3600), ("+1h", 3600)):
            m = eventstudy.WindowMeasurement(horizon=label, seconds=seconds)
            m.ret = m.abnormal_ret = 0.01
            events.append([m])
        labels = [p.horizon for p in eventstudy.pool(events)]
        self.assertEqual(labels, ["-1h", "+1h", "+1d"])


class FdrTests(unittest.TestCase):
    def test_adjustment_never_lowers_a_p_value(self):
        raw = [0.001, 0.02, 0.04, 0.3, 0.8]
        adjusted = eventstudy.benjamini_hochberg(raw)
        for before, after in zip(raw, adjusted):
            self.assertGreaterEqual(after, before)

    def test_adjustment_is_monotone_in_rank(self):
        adjusted = eventstudy.benjamini_hochberg([0.001, 0.02, 0.04, 0.3, 0.8])
        self.assertEqual(adjusted, sorted(adjusted))

    def test_one_borderline_result_among_nulls_is_pulled_back(self):
        # The realistic shape of §6.6's problem: search enough horizons and
        # pairs and something lands just under 0.05 by chance. Beside a crowd
        # of nulls, FDR says it is not a discovery.
        raw = [0.049] + [0.4 + i * 0.02 for i in range(19)]
        adjusted = eventstudy.benjamini_hochberg(raw)
        self.assertGreater(adjusted[0], 0.05)

    def test_a_uniformly_strong_family_survives_adjustment(self):
        # Benjamini-Hochberg controls the false-discovery *rate*, so twenty
        # results all at 0.049 stay significant — unlike Bonferroni, which
        # would reject every one. Pinning this stops a future "fix" from
        # silently swapping in the harsher correction.
        adjusted = eventstudy.benjamini_hochberg([0.049] * 20)
        self.assertTrue(all(p <= 0.05 for p in adjusted))

    def test_missing_values_survive_as_none(self):
        self.assertEqual(
            eventstudy.benjamini_hochberg([None, None]), [None, None]
        )


class StandardisationTests(unittest.TestCase):
    def test_standardised_move_is_abnormal_over_normal_spread(self):
        m = eventstudy.WindowMeasurement(horizon="+1h", seconds=3600)
        m.ret = 0.02
        m.abnormal_ret = 0.015
        m.baseline_sd = 0.005
        self.assertAlmostEqual(m.standardised, 3.0, places=9)

    def test_without_a_spread_there_is_no_standardised_value(self):
        m = eventstudy.WindowMeasurement(horizon="+1h", seconds=3600)
        m.abnormal_ret = 0.015
        m.baseline_sd = 0.0
        self.assertIsNone(m.standardised)
