"""External actual-solar sensor tests — normalization, counter diff, source.

Pure-module tests (solar_actual.py): no HA imports, deterministic input.
"""

from __future__ import annotations

import unittest

from . import load_solar_actual

solar_actual, const = load_solar_actual()
SOLAR_ACTUAL_SENSOR = const.CONF_SOLAR_ACTUAL_SENSOR
DEFAULT_SOLAR_ACTUAL_SENSOR = const.DEFAULT_SOLAR_ACTUAL_SENSOR


class NormalizeToKwhTests(unittest.TestCase):
    def test_wh_divided_by_1000(self):
        self.assertAlmostEqual(solar_actual.normalize_to_kwh(2500.0, "Wh"), 2.5)

    def test_kwh_identity(self):
        self.assertAlmostEqual(solar_actual.normalize_to_kwh(2.5, "kWh"), 2.5)

    def test_unit_case_insensitive(self):
        self.assertAlmostEqual(solar_actual.normalize_to_kwh(1000.0, "wh"), 1.0)

    def test_missing_unit_returns_none(self):
        self.assertIsNone(solar_actual.normalize_to_kwh(5.0, None))

    def test_unknown_unit_returns_none(self):
        self.assertIsNone(solar_actual.normalize_to_kwh(5.0, "W"))

    def test_non_numeric_value_returns_none(self):
        self.assertIsNone(solar_actual.normalize_to_kwh("5.0", "Wh"))

    def test_bool_value_returns_none(self):
        self.assertIsNone(solar_actual.normalize_to_kwh(True, "Wh"))


class CounterDeltaKwhTests(unittest.TestCase):
    def test_normal_delta(self):
        self.assertEqual(solar_actual.counter_delta_kwh(1.0, 3.5), (2.5, False))

    def test_zero_delta(self):
        self.assertEqual(solar_actual.counter_delta_kwh(1.0, 1.0), (0.0, False))

    def test_small_negative_float_noise_clamped_to_zero(self):
        self.assertEqual(solar_actual.counter_delta_kwh(1.0, 0.95), (0.0, False))

    def test_reset_crossing_flagged(self):
        delta, reset = solar_actual.counter_delta_kwh(10.0, 0.3)
        self.assertEqual(delta, 0.3)
        self.assertTrue(reset)

    def test_reset_crossing_negative_post_value(self):
        delta, reset = solar_actual.counter_delta_kwh(10.0, -1.0)
        self.assertEqual(delta, 0.0)
        self.assertTrue(reset)


class ResolveSolarSourceTests(unittest.TestCase):
    def test_not_configured_uses_emaldo(self):
        self.assertEqual(
            solar_actual.resolve_solar_source(False, False, False), "emaldo"
        )

    def test_configured_available_uses_external(self):
        self.assertEqual(
            solar_actual.resolve_solar_source(True, True, True), "external"
        )

    def test_configured_missing_snapshot_skips(self):
        self.assertEqual(
            solar_actual.resolve_solar_source(True, False, True), "skip"
        )

    def test_configured_current_unavailable_skips(self):
        self.assertEqual(
            solar_actual.resolve_solar_source(True, True, False), "skip"
        )


if __name__ == "__main__":
    unittest.main()
