"""Regression tests for issue #10 — plan-accuracy sensor must not go unknown.

When ``CONF_SOLAR_ACTUAL_SENSOR`` is configured but the external solar counter
is momentarily unreadable, ``resolve_solar_source`` returns ``"skip"``.  The
old code then ``return None`` for the WHOLE accuracy record, blanking the
plan-accuracy sensor (the reported permanent ``unknown``).  The fix keeps
discharge/charge accuracy and only omits the solar portion, so the sensor
stays populated through solar-sensor outages.

Tests drive the real ``_compute_plan_accuracy`` with a fake ``self`` (no Home
Assistant runtime) via the ``load_coordinator`` harness.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace

from . import load_coordinator

coord, const = load_coordinator()
BatteryOptimizerCoordinator = coord.BatteryOptimizerCoordinator
SlotPlan = coord.SlotPlan


def _make_fake(
    *,
    solar_actual_sensor,
    external_now,
    snapshot,
    emaldo_reads,
):
    """Build a stand-in coordinator `self` for _compute_plan_accuracy."""
    slots = [
        SlotPlan(
            index=42, action="discharge", slot_value=0.0, buy_price=0.0,
            sell_price=0.0, solar_kw=0.0, soc_after=50.0,
        ),
        SlotPlan(
            index=44, action="discharge", slot_value=0.0, buy_price=0.0,
            sell_price=0.0, solar_kw=1.0, soc_after=48.0,
        ),
        SlotPlan(
            index=50, action="charge", slot_value=0.0, buy_price=0.0,
            sell_price=0.0, solar_kw=0.0, soc_after=70.0,
        ),
    ]
    last_result = SimpleNamespace(slots=slots)

    def build_battery_config():
        return SimpleNamespace(capacity_kwh=15.0)

    def read_external_solar_kwh():
        return external_now

    def read_emaldo_sensor_float(key):
        return emaldo_reads.get(key)

    return SimpleNamespace(
        _last_result=last_result,
        _last_run_slot=42,
        _last_run_initial_soc=100.0,
        _last_run_actual_snapshot=snapshot,
        _last_result_solar_scale=0.844,
        _last_run=datetime(2026, 8, 23, 17, 23, 31),
        config=(
            {const.CONF_SOLAR_ACTUAL_SENSOR: solar_actual_sensor}
            if solar_actual_sensor
            else {}
        ),
        _build_battery_config=build_battery_config,
        _read_external_solar_kwh=read_external_solar_kwh,
        _read_emaldo_sensor_float=read_emaldo_sensor_float,
    )


class PlanAccuracySkipTests(unittest.TestCase):
    def test_skip_keeps_discharge_charge_omits_solar(self):
        fake = _make_fake(
            solar_actual_sensor="sensor.solis",
            external_now=None,  # external counter momentarily unreadable
            snapshot={"discharge": 2.0, "charge": 1.0},  # no solar_ext
            emaldo_reads={
                "battery_discharged_today": 5.0,
                "battery_charged_today": 3.0,
            },
        )
        acc = BatteryOptimizerCoordinator._compute_plan_accuracy(fake, 50)
        self.assertIsNotNone(acc)
        self.assertEqual(acc["solar_source"], "skip")
        # discharge/charge accuracy still recorded
        self.assertIn("discharge_error_kwh", acc)
        self.assertIn("charge_error_kwh", acc)
        # solar portion omitted (no solar actuals mixed in)
        self.assertNotIn("actual_solar_kwh", acc)
        self.assertNotIn("solar_error_kwh", acc)

    def test_external_source_still_records_solar(self):
        fake = _make_fake(
            solar_actual_sensor="sensor.solis",
            external_now=12.5,
            snapshot={"discharge": 2.0, "charge": 1.0, "solar_ext": 10.0},
            emaldo_reads={
                "battery_discharged_today": 5.0,
                "battery_charged_today": 3.0,
            },
        )
        acc = BatteryOptimizerCoordinator._compute_plan_accuracy(fake, 50)
        self.assertIsNotNone(acc)
        self.assertEqual(acc["solar_source"], "external")
        self.assertIn("actual_solar_kwh", acc)
        self.assertIn("solar_error_kwh", acc)

    def test_guard_returns_none_without_last_result(self):
        fake = _make_fake(
            solar_actual_sensor="sensor.solis",
            external_now=None,
            snapshot={},
            emaldo_reads={},
        )
        fake._last_result = None
        acc = BatteryOptimizerCoordinator._compute_plan_accuracy(fake, 50)
        self.assertIsNone(acc)


if __name__ == "__main__":
    unittest.main()
