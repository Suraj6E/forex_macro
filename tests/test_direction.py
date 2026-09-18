"""Tests for analytics/direction.py — the signed-response mode.

The arithmetic here is the part that produces a number a person will read as
"inflation rising moves the euro up", so the tests check the things that would
make that sentence wrong: a leaked sigma, a sign flip, a fit reported off three
points, and a beta claimed where the driver never varied.
"""

import math
import unittest

import numpy as np

from analytics import direction


class StandardiseChangesTests(unittest.TestCase):
    def test_sigma_is_trailing_not_full_sample(self):
        """A late outlier must not change the scaling of an earlier release.

        If it does, every early `change_z` has been computed with knowledge of
        the future — §4.3's look-ahead problem, applied to the denominator.
        """
        base = [1.0, -1.0] * 10
        quiet = direction.standardise_changes(base + [1.0])
        shocked = direction.standardise_changes(base + [1000.0])
        self.assertEqual(quiet[:-1], shocked[:-1])

    def test_leading_releases_have_no_sigma_yet(self):
        values = [1.0] * (direction.SIGMA_MIN_PERIODS + 2)
        out = direction.standardise_changes(values)
        self.assertTrue(all(v is None for v in out[: direction.SIGMA_MIN_PERIODS]))

    def test_constant_series_yields_no_z(self):
        """Zero trailing sigma is a division by zero, not a large z."""
        out = direction.standardise_changes([2.0] * 30)
        self.assertTrue(all(v is None for v in out))

    def test_nones_pass_through_without_entering_history(self):
        values = [1.0, None, -1.0, None] * 8
        out = direction.standardise_changes(values)
        self.assertEqual(len(out), len(values))
        for value, z in zip(values, out):
            if value is None:
                self.assertIsNone(z)

    def test_z_scales_with_the_change(self):
        history = [1.0, -1.0] * 10
        out = direction.standardise_changes(history + [3.0])
        self.assertIsNotNone(out[-1])
        # sigma of an alternating +-1 series is ~1, so a change of 3 is ~3 sigma.
        self.assertAlmostEqual(out[-1], 3.0, delta=0.3)


class FitTests(unittest.TestCase):
    def test_recovers_a_known_slope(self):
        xs = [float(i) for i in range(-25, 26)]
        ys = [0.5 * x for x in xs]
        result = direction.fit("+1h", 3600, xs, ys)
        self.assertAlmostEqual(result.beta, 0.5, places=6)
        self.assertAlmostEqual(result.r_squared, 1.0, places=6)
        self.assertEqual(result.n, len(xs))

    def test_sign_is_preserved(self):
        """A negative relationship must not come back positive — this is the
        whole claim of the mode."""
        xs = [float(i) for i in range(-25, 26)]
        ys = [-2.0 * x for x in xs]
        result = direction.fit("+1h", 3600, xs, ys)
        self.assertLess(result.beta, 0)

    def test_no_variation_in_the_driver_yields_no_beta(self):
        result = direction.fit("+1h", 3600, [1.0] * 30, [0.1] * 30)
        self.assertIsNone(result.beta)

    def test_too_few_points_returns_an_empty_fit(self):
        result = direction.fit("+1h", 3600, [1.0, 2.0], [0.1, 0.2])
        self.assertIsNone(result.beta)
        self.assertEqual(result.n, 2)

    def test_pairs_with_a_missing_side_are_dropped(self):
        xs = [1.0, None, 3.0, 4.0, None, 6.0]
        ys = [1.0, 2.0, None, 4.0, 5.0, 6.0]
        result = direction.fit("+1h", 3600, xs, ys)
        self.assertEqual(result.n, 3)

    def test_minimum_n_gate(self):
        xs = [float(i) for i in range(10)]
        result = direction.fit("+1h", 3600, xs, [x * 2 for x in xs])
        self.assertTrue(result.gated)

    def test_noise_is_rarely_significant(self):
        """An unrelated driver must come back significant at about the 5% rate,
        not more. Deterministic trig series look like noise and are not: the
        first draft of this test used sin/cos and the fit found a real
        relationship between them."""
        rng = np.random.default_rng(20260918)
        hits = 0
        trials = 200
        for _ in range(trials):
            xs = rng.normal(size=120).tolist()
            ys = rng.normal(size=120).tolist()
            if direction.fit("+1h", 3600, xs, ys).significant:
                hits += 1
        self.assertLess(hits / trials, 0.12)

    def test_detectability_floor_is_reported_when_beta_is_not_significant(self):
        rng = np.random.default_rng(1)
        xs = rng.normal(size=200).tolist()
        ys = rng.normal(size=200).tolist()
        result = direction.fit("+1h", 3600, xs, ys)
        self.assertIsNotNone(result.detectability_floor)
        self.assertGreater(result.detectability_floor, 0)


class AsymmetryTests(unittest.TestCase):
    def test_splits_rises_from_falls(self):
        xs = [float(i) for i in range(-25, 26) if i != 0]
        # Twice as responsive to falls as to rises.
        ys = [x if x > 0 else 2.0 * x for x in xs]
        result = direction.fit("+1h", 3600, xs, ys)
        self.assertAlmostEqual(result.beta_up, 1.0, places=6)
        self.assertAlmostEqual(result.beta_down, 2.0, places=6)
        self.assertEqual(result.n_up, 25)
        self.assertEqual(result.n_down, 25)

    def test_asymmetric_flag_only_fires_on_a_sign_disagreement(self):
        xs = [float(i) for i in range(-25, 26) if i != 0]
        same = direction.fit("+1h", 3600, xs, [2.0 * x for x in xs])
        self.assertFalse(same.asymmetric)

        opposed = direction.fit("+1h", 3600, xs, [abs(x) for x in xs])
        self.assertTrue(opposed.asymmetric)


if __name__ == "__main__":
    unittest.main()
