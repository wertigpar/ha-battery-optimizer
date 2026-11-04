"""Solar forecast scale tests — auto-tune EWMA, clamps, record filters.

Pure-module tests (solar_scale.py): no HA imports, deterministic history.
"""

from __future__ import annotations

import unittest

from . import load_solar_scale

solar_scale, const = load_solar_scale()
SOLAR_SCALE_MIN = const.SOLAR_SCALE_MIN
SOLAR_SCALE_MAX = const.SOLAR_SCALE_MAX
ALPHA = const.SOLAR_SCALE_EWMA_ALPHA


def rec(elapsed_slots=12, planned=10.0, err=-2.0, scale_used=1.0, reset=False):
    """Default record: ratio 0.8 on the raw basis."""
    d = {
        "elapsed_slots": elapsed_slots,
        "planned_solar_kwh": planned,
        "solar_error_kwh": err,
        "solar_scale_used": scale_used,
    }
    if reset:
        d["solar_reset_crossed"] = True
    return d


class EffectiveScaleTests(unittest.TestCase):
    def test_manual_value_passes_through(self):
        self.assertEqual(solar_scale.effective_scale(0.8), 0.8)

    def test_zero_is_auto_sentinel(self):
        self.assertIsNone(solar_scale.effective_scale(0.0))

    def test_manual_below_floor_raised(self):
        self.assertEqual(solar_scale.effective_scale(0.1), SOLAR_SCALE_MIN)

    def test_manual_above_ceiling_capped(self):
        self.assertEqual(solar_scale.effective_scale(2.0), SOLAR_SCALE_MAX)

    def test_manual_disables_autotune(self):
        self.assertEqual(solar_scale.resolve_solar_scale(0.8, []), 0.8)


class AutoTuneTests(unittest.TestCase):
    def test_warmup_returns_one(self):
        self.assertEqual(solar_scale.resolve_solar_scale(0.0, [rec()] * 4), 1.0)

    def test_empty_history_returns_one(self):
        self.assertEqual(solar_scale.resolve_solar_scale(0.0, []), 1.0)

    def test_ewma_math(self):
        # 2 neutral records (ratio 1.0, EWMA unchanged) + 3 below-unity
        hist = [
            rec(planned=10.0, err=0.0),   # raw ratio 1.0
            rec(planned=10.0, err=0.0),   # raw ratio 1.0
            rec(planned=10.0, err=-2.0),  # raw ratio 0.8
            rec(planned=10.0, err=-4.0),  # raw ratio 0.6
            rec(planned=10.0, err=-3.0),  # raw ratio 0.7
        ]
        e = 1.0
        for r in (1.0, 1.0, 0.8, 0.6, 0.7):
            e += ALPHA * (r - e)
        self.assertAlmostEqual(
            solar_scale.resolve_solar_scale(0.0, hist), e, places=5
        )

    def test_raw_basis_ratio_normalized(self):
        # True bias 0.5, plan ran with scale 0.5 -> stored planned (SCALED)
        # equals actual -> naive scaled-basis ratio = 1.0 (the feedback trap:
        # the system sees a perfect forecast and never corrects). Raw-basis
        # ratio must equal the true bias 0.5.
        r = rec(planned=10.0, err=0.0, scale_used=0.5)
        self.assertAlmostEqual(solar_scale.raw_basis_ratio(r), 0.5, places=5)
        self.assertNotAlmostEqual(solar_scale.raw_basis_ratio(r), 1.0, places=5)

    def test_fixed_point_converges_to_bias(self):
        # True bias 0.5, plan ran with scale 0.5, actual == stored planned.
        # Raw-basis EWMA must pull toward 0.5 (e_n = 0.5 + 0.9^n * 0.5), not
        # sit at 1.0 as the naive scaled-basis feedback would.
        hist = [rec(planned=10.0, err=0.0, scale_used=0.5)] * 8
        expected = 0.5 + (0.9 ** 8) * 0.5  # 0.715233605
        self.assertAlmostEqual(
            solar_scale.resolve_solar_scale(0.0, hist), expected, places=5
        )

    def test_output_stays_in_clamp_range(self):
        low = solar_scale.resolve_solar_scale(0.0, [rec(planned=10.0, err=-9.0)] * 6)
        high = solar_scale.resolve_solar_scale(0.0, [rec(planned=10.0, err=2.0)] * 6)
        self.assertGreaterEqual(low, SOLAR_SCALE_MIN)
        self.assertLessEqual(low, SOLAR_SCALE_MAX)
        self.assertGreaterEqual(high, SOLAR_SCALE_MIN)
        self.assertLessEqual(high, SOLAR_SCALE_MAX)


class FilterTests(unittest.TestCase):
    def test_reset_crossed_excluded(self):
        # 4 valid + 1 reset -> 4 valid -> warmup not reached -> 1.0
        hist = [rec()] * 4 + [rec(reset=True)]
        self.assertEqual(solar_scale.resolve_solar_scale(0.0, hist), 1.0)

    def test_short_window_excluded(self):
        hist = [rec(elapsed_slots=4)] * 6
        self.assertEqual(solar_scale.resolve_solar_scale(0.0, hist), 1.0)

    def test_non_numeric_fields_excluded(self):
        bad = {"elapsed_slots": "x", "planned_solar_kwh": 10.0, "solar_error_kwh": -2.0}
        hist = [rec()] * 4 + [bad]
        self.assertEqual(solar_scale.resolve_solar_scale(0.0, hist), 1.0)

    def test_small_planned_excluded(self):
        hist = [rec()] * 4 + [rec(planned=0.05, err=-0.01)]
        self.assertEqual(solar_scale.resolve_solar_scale(0.0, hist), 1.0)

    def test_legacy_record_defaults_scale_one(self):
        legacy = {
            "elapsed_slots": 12,
            "planned_solar_kwh": 10.0,
            "solar_error_kwh": -3.0,  # raw ratio 0.7
        }
        self.assertAlmostEqual(solar_scale.raw_basis_ratio(legacy), 0.7, places=5)
