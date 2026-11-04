"""Unit tests for solar_balance.py — pure, no Home Assistant imports."""

import unittest
from datetime import date

from . import load_solar_balance

sb = load_solar_balance()


def rec(ts: str, actual: float) -> dict:
    return {"ts": ts, "actual_solar_kwh": actual}


class TestSolarBalance(unittest.TestCase):
    def test_avg_and_attrs(self):
        records = [
            rec(f"2026-08-{d:02d}T14:00:00+03:00", v)
            for d, v in zip(range(10, 18), (8, 9, 10, 11, 12, 13, 14, 15))
        ]
        r = sb.solar_balance_report(records, base_load_kw=1.1, usable_band_kwh=10.4)
        self.assertIsNotNone(r)
        # last 7 days (11..17) avg = (9+10+11+12+13+14+15)/7 = 84/7 = 12.0
        self.assertEqual(r["avg_daily_solar_kwh"], 12.0)
        self.assertEqual(r["days_sampled"], 7)
        self.assertEqual(r["window_start"], "2026-08-11")
        self.assertEqual(r["window_end"], "2026-08-17")
        self.assertEqual(r["daily_base_load_kwh"], 26.4)
        self.assertAlmostEqual(r["self_sufficiency"], round(12.0 / 26.4, 3))
        self.assertAlmostEqual(r["battery_days"], round(10.4 / 26.4, 2))
        self.assertEqual(r["usable_band_kwh"], 10.4)

    def test_less_than_min_days_returns_none(self):
        records = [rec("2026-08-14T14:00:00+03:00", 12.0)] * 3
        self.assertIsNone(sb.solar_balance_report(records, 1.1, 10.4))

    def test_window_capped_at_seven_days(self):
        records = [
            rec(f"2026-08-{d:02d}T14:00:00+03:00", 1.0) for d in range(1, 22)
        ]
        r = sb.solar_balance_report(records, 1.1, 10.4)
        self.assertEqual(r["days_sampled"], 7)
        self.assertEqual(r["window_start"], "2026-08-15")
        self.assertEqual(r["window_end"], "2026-08-21")

    def test_garbage_records_skipped(self):
        records = [
            rec("2026-08-14T14:00:00+03:00", 12.0),
            {"ts": "not-a-date", "actual_solar_kwh": 5.0},
            {"ts": "2026-08-15T14:00:00+03:00", "actual_solar_kwh": "n/a"},
            {"ts": "2026-08-15T14:00:00+03:00", "actual_solar_kwh": -2.0},
            None,
            {"actual_solar_kwh": 9.0},  # missing ts
        ]
        r = sb.solar_balance_report(records, 1.1, 10.4)
        self.assertIsNone(r)  # only 1 valid day < min_days

    def test_multiple_records_same_day_summed(self):
        records = [
            rec("2026-08-10T14:00:00+03:00", 4.0),
            rec("2026-08-10T19:00:00+03:00", 3.0),  # same day sums to 7.0
            rec("2026-08-11T14:00:00+03:00", 7.0),
            rec("2026-08-12T14:00:00+03:00", 7.0),
            rec("2026-08-13T14:00:00+03:00", 7.0),
            rec("2026-08-14T14:00:00+03:00", 7.0),
            rec("2026-08-15T14:00:00+03:00", 7.0),
        ]
        r = sb.solar_balance_report(records, 1.0, 10.0)
        self.assertEqual(r["days_sampled"], 6)
        self.assertEqual(r["avg_daily_solar_kwh"], 7.0)

    def test_zero_base_load_omits_derived(self):
        records = [
            rec(f"2026-08-{d:02d}T14:00:00+03:00", 10.0) for d in range(11, 18)
        ]
        r = sb.solar_balance_report(records, base_load_kw=0.0, usable_band_kwh=10.0)
        self.assertIsNotNone(r)
        self.assertIsNone(r["daily_base_load_kwh"])
        self.assertNotIn("self_sufficiency", r)
        self.assertNotIn("battery_days", r)

    def test_empty_records_none(self):
        self.assertIsNone(sb.solar_balance_report([], 1.1, 10.4))
        self.assertIsNone(sb.solar_balance_report(None, 1.1, 10.4))

    def test_utc_timestamps(self):
        records = [
            rec(f"2026-08-{d:02d}T12:00:00Z", 10.0) for d in range(11, 18)
        ]
        r = sb.solar_balance_report(records, 1.1, 10.4)
        self.assertEqual(r["days_sampled"], 7)

    def test_negative_self_sufficiency_clamped(self):
        # all-zero production -> avg 0 -> self_sufficiency 0, not negative
        records = [
            rec(f"2026-08-{d:02d}T14:00:00+03:00", 0.0) for d in range(11, 18)
        ]
        r = sb.solar_balance_report(records, 1.1, 10.4)
        self.assertEqual(r["avg_daily_solar_kwh"], 0.0)
        self.assertEqual(r["self_sufficiency"], 0.0)


if __name__ == "__main__":
    unittest.main()